"""CamillaDSP configuration generator and process/WebSocket controller."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import subprocess
from pathlib import Path
from typing import Optional

import websockets
import yaml

from app.audio.alsa import select_best_pcm_format
from app.audio.eq import compute_max_boost_db
from app.config import AudioOutputConfig, RoomEQConfig, VolumeConfig

logger = logging.getLogger(__name__)


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically write text via a sibling temp file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)

def _default_camilla_config_path() -> Path:
    cfg_env = os.environ.get("HYDRA_CONFIG_PATH")
    if cfg_env:
        return Path(cfg_env).parent / "camilladsp.yml"
    etc_dir = Path("/etc/hydrasound")
    if etc_dir.exists() and os.access(etc_dir, os.W_OK):
        return etc_dir / "camilladsp.yml"
    return Path(__file__).resolve().parents[2] / "data" / "camilladsp.yml"


CAMILLA_WS_PORT = 1234

ALSA_CDSP_PLUGIN_PATHS = (
    Path("/usr/lib/aarch64-linux-gnu/alsa-lib/libasound_module_pcm_cdsp.so"),
    Path("/usr/lib/alsa-lib/libasound_module_pcm_cdsp.so"),
)


def has_alsa_cdsp() -> bool:
    """Return True if the alsa_cdsp ALSA PCM plugin is installed and enabled."""
    if os.environ.get("HYDRA_DISABLE_ALSA_CDSP") == "1":
        return False
    return any(p.is_file() for p in ALSA_CDSP_PLUGIN_PATHS)

RESAMPLER_PROFILES = {
    "SincVeryHigh": {"type": "AsyncSinc", "profile": "Accurate"},
    "SincHigh": {"type": "AsyncSinc", "profile": "Balanced"},
    "SincMedium": {"type": "AsyncSinc", "profile": "Fast"},
    "SincFast": {"type": "AsyncSinc", "profile": "VeryFast"},
}


def bit_depth_to_alsa_format(bit_depth: int, device_id: str = "") -> str:
    if bit_depth == 24 and device_id:
        best = select_best_pcm_format(device_id)
        if best in ("S24_3_LE", "S24_4_RJ_LE"):
            return best
    return {
        16: "S16_LE",
        24: "S24_3_LE",
        32: "S32_LE",
    }.get(bit_depth, "S32_LE")


def percent_to_db(percent: int, curve: str = "logarithmic", min_db: float = -80.0) -> float:
    """Convert 0-100% volume to dB using logarithmic (perceptual) or linear scaling."""
    clamped = max(0, min(100, int(percent)))
    if clamped == 0:
        return min_db
    ratio = clamped / 100.0
    if curve == "logarithmic":
        # Perceptually smooth exponential amplitude -> dB
        # 100% -> 0 dB, 50% -> ~-20.8 dB, 1% -> ~-77.1 dB
        return round(min_db * (1.0 - math.log10(1.0 + 9.0 * ratio)), 2)
    db = 20.0 * math.log10(ratio)
    return round(max(min_db, db), 2)


def get_effective_max_volume(vol_cfg: VolumeConfig) -> int:
    """Return effective max_volume (0..100) respecting HYDRA_HARD_MAX_VOLUME."""
    max_allowed = max(0, min(100, int(vol_cfg.max_volume)))
    hard_cap_env = os.environ.get("HYDRA_HARD_MAX_VOLUME")
    if hard_cap_env is not None:
        try:
            max_allowed = min(max_allowed, max(0, min(100, int(hard_cap_env))))
        except ValueError:
            pass
    return max_allowed


def build_camilladsp_config(
    audio_cfg: AudioOutputConfig,
    volume_cfg: VolumeConfig,
    capture_device: str = "hw:Loopback,1,0",
    capture_rate: int = 44100,
    use_alsa_cdsp: bool = False,
    eq_cfg: Optional[RoomEQConfig] = None,
) -> dict:
    """Build a 64-bit float internal precision CamillaDSP YAML dictionary."""
    preset = eq_cfg.get_active_preset() if (eq_cfg and eq_cfg.has_active_processing()) else None
    has_fir = bool(preset and (preset.left_impulse_path or preset.right_impulse_path))
    fir_rate = preset.impulse_sample_rate if (has_fir and preset and preset.impulse_sample_rate) else None

    target_format = (
        bit_depth_to_alsa_format(audio_cfg.resample_bit_depth, audio_cfg.device_id)
        if audio_cfg.resample_enabled
        else select_best_pcm_format(audio_cfg.device_id)
    )
    if fir_rate is not None:
        target_rate: int | str = fir_rate
    elif audio_cfg.resample_enabled:
        target_rate = audio_cfg.resample_rate
    else:
        target_rate = capture_rate

    playback_device = audio_cfg.device_id
    if (
        "hdmi" in audio_cfg.device_name.lower()
        and playback_device.startswith("hw:")
        and not playback_device.startswith("hw:Loopback")
    ):
        card_dev = playback_device.split(":", 1)[1].split(",")
        if len(card_dev) == 2:
            playback_device = f"hdmi:CARD={card_dev[0]},DEV={card_dev[1]}"

    if use_alsa_cdsp:
        if fir_rate is not None:
            samplerate_val: int | str = fir_rate
        elif audio_cfg.resample_enabled:
            samplerate_val = audio_cfg.resample_rate
        else:
            samplerate_val = "$samplerate$"
        chunksize_val: int | str = (
            (1024 if samplerate_val <= 48000 else (4096 if samplerate_val > 96000 else 2048))
            if isinstance(samplerate_val, int)
            else "$chunksize$"
        )
        devices: dict = {
            "samplerate": samplerate_val,
            "chunksize": chunksize_val,
            "enable_rate_adjust": False,
            "capture": {
                "type": "Stdin",
                "channels": "$channels$",
                "format": "$format$",
            },
            "playback": {
                "type": "Alsa",
                "channels": 2,
                "device": playback_device,
                "format": target_format,
            },
        }
        if audio_cfg.resample_enabled or fir_rate is not None:
            devices["resampler"] = {"type": "Synchronous"}
            devices["capture_samplerate"] = "$samplerate$"
    else:
        chunksize = (
            1024 if target_rate <= 48000 else (2048 if target_rate <= 96000 else 4096)
        )
        devices = {
            "samplerate": target_rate,
            "chunksize": chunksize,
            "enable_rate_adjust": True,
            "capture": {
                "type": "Alsa",
                "channels": 2,
                "device": capture_device,
                "format": "S32_LE",
            },
            "playback": {
                "type": "Alsa",
                "channels": 2,
                "device": playback_device,
                "format": target_format,
            },
        }
        if (audio_cfg.resample_enabled or fir_rate is not None) and capture_rate != target_rate:
            profile = RESAMPLER_PROFILES.get(
                audio_cfg.resample_algorithm, RESAMPLER_PROFILES["SincVeryHigh"]
            )
            devices["resampler"] = {
                "type": "AsyncSinc",
                "profile": profile["profile"],
            }
            devices["capture_samplerate"] = capture_rate

    filters = {}
    pipeline = []

    if volume_cfg.mode == "disabled" and not volume_cfg.usb_disabled_confirmed:
        filters["safety_mute"] = {
            "type": "Gain",
            "parameters": {
                "gain": -80.0,
                "mute": True,
            },
        }
        pipeline.append({"type": "Filter", "channels": [0, 1], "names": ["safety_mute"]})

    if preset is not None:
        max_boost = max(
            compute_max_boost_db(preset.bands, "left", preset.impulse_peak_gain_db),
            compute_max_boost_db(preset.bands, "right", preset.impulse_peak_gain_db),
        )
        if max_boost > 0.0:
            filters["eq_headroom"] = {
                "type": "Gain",
                "parameters": {
                    "gain": round(-(max_boost + 0.1), 2),
                    "mute": False,
                },
            }
            pipeline.append({"type": "Filter", "channels": [0, 1], "names": ["eq_headroom"]})

        for idx, band in enumerate(preset.bands):
            if not band.enabled:
                continue
            if band.type not in ("Lowpass", "Highpass") and abs(band.gain) < 1e-4:
                continue
            fname = f"eq_band_{idx}"
            bparams: dict = {"type": band.type, "freq": float(band.freq), "q": float(band.q)}
            if band.type not in ("Lowpass", "Highpass"):
                bparams["gain"] = float(band.gain)
            filters[fname] = {"type": "Biquad", "parameters": bparams}
            chans = [0, 1] if band.channel == "both" else ([0] if band.channel == "left" else [1])
            pipeline.append({"type": "Filter", "channels": chans, "names": [fname]})

        if preset.left_delay_ms > 0.0:
            filters["eq_delay_l"] = {
                "type": "Delay",
                "parameters": {"delay": float(preset.left_delay_ms), "unit": "ms", "subsample": True},
            }
            pipeline.append({"type": "Filter", "channels": [0], "names": ["eq_delay_l"]})
        if preset.right_delay_ms > 0.0:
            filters["eq_delay_r"] = {
                "type": "Delay",
                "parameters": {"delay": float(preset.right_delay_ms), "unit": "ms", "subsample": True},
            }
            pipeline.append({"type": "Filter", "channels": [1], "names": ["eq_delay_r"]})

        if preset.left_impulse_path:
            filters["eq_fir_l"] = {
                "type": "Conv",
                "parameters": {"type": "Wav", "filename": preset.left_impulse_path, "channel": 0},
            }
            pipeline.append({"type": "Filter", "channels": [0], "names": ["eq_fir_l"]})
        if preset.right_impulse_path:
            filters["eq_fir_r"] = {
                "type": "Conv",
                "parameters": {"type": "Wav", "filename": preset.right_impulse_path, "channel": 0},
            }
            pipeline.append({"type": "Filter", "channels": [1], "names": ["eq_fir_r"]})
        if (
            bool(preset.left_impulse_path) != bool(preset.right_impulse_path)
            and preset.impulse_taps > 0
            and preset.impulse_sample_rate > 0
        ):
            fir_delay_ms = round(
                (preset.impulse_taps / (2.0 * preset.impulse_sample_rate)) * 1000.0, 4
            )
            if fir_delay_ms > 0.0:
                opp_ch = [1] if preset.left_impulse_path else [0]
                filters["eq_fir_skew_comp"] = {
                    "type": "Delay",
                    "parameters": {"delay": fir_delay_ms, "unit": "ms", "subsample": True},
                }
                pipeline.append({"type": "Filter", "channels": opp_ch, "names": ["eq_fir_skew_comp"]})

    if volume_cfg.mode == "software":
        eff_max = get_effective_max_volume(volume_cfg)
        max_db = round(percent_to_db(eff_max, volume_cfg.curve), 2)
        filters["master_volume"] = {
            "type": "Volume",
            "parameters": {
                "ramp_time": 120,
                "fader": "Aux1",
                "limit": max_db,
            },
        }
        pipeline.append({"type": "Filter", "channels": [0, 1], "names": ["master_volume"]})

    if audio_cfg.resample_enabled or preset is not None:
        filters["dsp_limiter"] = {
            "type": "Limiter",
            "parameters": {
                "clip_limit": -0.1,
                "soft_clip": False,
            },
        }
        pipeline.append({"type": "Filter", "channels": [0, 1], "names": ["dsp_limiter"]})

    if (audio_cfg.resample_enabled or volume_cfg.mode == "software" or preset is not None) and (
        target_format in ("S16_LE", "S24_3_LE", "S24_4_RJ_LE")
        or audio_cfg.dither_24bit_spdif
    ):
        dither_bits = 16 if target_format == "S16_LE" else 24
        filters["dsp_dither"] = {
            "type": "Dither",
            "parameters": {
                "type": "Flat",
                "bits": dither_bits,
                "amplitude": 2.0,
            },
        }
        pipeline.append({"type": "Filter", "channels": [0, 1], "names": ["dsp_dither"]})

    return {
        "devices": devices,
        "filters": filters,
        "pipeline": pipeline,
    }


class CamillaDSPManager:
    """Manages the CamillaDSP daemon and real-time 32-bit+ volume updates."""

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self.config_path = config_path or _default_camilla_config_path()
        self.template_path = self.config_path.parent / "camilladsp_in.yml"
        self.statefile_path = self.config_path.parent / "camilladsp_state.yml"
        self._proc: Optional[subprocess.Popen] = None
        self.running = False
        self._cdsp_mode = False

    def write_statefile(self, db: float, muted: bool) -> None:
        """Persist Aux1 volume & mute into CamillaDSP statefile for alsa_cdsp spawns."""
        state_data = {
            "config_path": str(self.config_path),
            "mute": [False, bool(muted), False, False, False],
            "volume": [0.0, round(float(db), 2), 0.0, 0.0, 0.0],
        }
        _atomic_write_text(
            self.statefile_path, yaml.safe_dump(state_data, sort_keys=False)
        )

    def render_cdsp_template(
        self,
        audio_cfg: AudioOutputConfig,
        volume_cfg: VolumeConfig,
        eq_cfg: Optional[RoomEQConfig] = None,
    ) -> str:
        """Render CamillaDSP YAML template with unquoted alsa_cdsp substitution tokens."""
        data = build_camilladsp_config(
            audio_cfg, volume_cfg, use_alsa_cdsp=True, eq_cfg=eq_cfg
        )
        dumped = yaml.safe_dump(data, sort_keys=False)
        for tok in ("$samplerate$", "$channels$", "$format$", "$chunksize$"):
            dumped = dumped.replace(f"'{tok}'", tok).replace(f'"{tok}"', tok)
        return dumped

    def configure_alsa_cdsp(
        self,
        audio_cfg: AudioOutputConfig,
        volume_cfg: VolumeConfig,
        asound_conf_path: Path = Path("/etc/asound.conf"),
        eq_cfg: Optional[RoomEQConfig] = None,
    ) -> None:
        """Configure alsa_cdsp template, config_cmd script, and /etc/asound.conf."""
        self.stop()
        self._cdsp_mode = True
        _atomic_write_text(
            self.template_path,
            self.render_cdsp_template(audio_cfg, volume_cfg, eq_cfg=eq_cfg),
        )
        if volume_cfg.mode == "disabled" and not volume_cfg.usb_disabled_confirmed:
            self.write_statefile(-80.0, True)
        else:
            eff_max = get_effective_max_volume(volume_cfg)
            safe_pct = max(0, min(eff_max, int(volume_cfg.current_volume)))
            self.write_statefile(
                percent_to_db(safe_pct, volume_cfg.curve), bool(volume_cfg.muted)
            )

        cfg_cmd = Path(__file__).resolve().parents[3] / "scripts" / "camilla_cdsp_config.sh"
        try:
            if cfg_cmd.exists():
                cfg_cmd.chmod(0o755)
        except Exception:
            pass

        asound_body = (
            'defaults.pcm.rate_converter "speexrate_best"\n\n'
            "pcm.camilladsp {\n"
            "    type cdsp\n"
            '    cpath "/usr/local/bin/camilladsp"\n'
            f'    config_cmd "{cfg_cmd}"\n'
            f'    config_out "{self.config_path}"\n'
            "    channels 2\n"
            "    rates = [ 44100 48000 88200 96000 176400 192000 352800 384000 ]\n"
            f'    cargs [ -p "{CAMILLA_WS_PORT}" -a "127.0.0.1" '
            f'-s "{self.statefile_path}" -o "/var/log/hydrasound-camilladsp.log" ]\n'
            "}\n"
        )
        try:
            if asound_conf_path.parent.exists() and os.access(
                asound_conf_path.parent, os.W_OK
            ):
                if (
                    not asound_conf_path.exists()
                    or asound_conf_path.read_text(encoding="utf-8") != asound_body
                ):
                    asound_conf_path.write_text(asound_body, encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to update %s for alsa_cdsp: %s", asound_conf_path, exc)

    def write_config(
        self,
        audio_cfg: AudioOutputConfig,
        volume_cfg: VolumeConfig,
        capture_rate: int = 44100,
        eq_cfg: Optional[RoomEQConfig] = None,
    ) -> Path:
        data = build_camilladsp_config(
            audio_cfg, volume_cfg, capture_rate=capture_rate, eq_cfg=eq_cfg
        )
        _atomic_write_text(self.config_path, yaml.safe_dump(data, sort_keys=False))
        return self.config_path

    def start(
        self,
        audio_cfg: AudioOutputConfig,
        volume_cfg: VolumeConfig,
        capture_rate: int = 44100,
        eq_cfg: Optional[RoomEQConfig] = None,
    ) -> None:
        self._cdsp_mode = False
        new_yaml = yaml.safe_dump(
            build_camilladsp_config(
                audio_cfg, volume_cfg, capture_rate=capture_rate, eq_cfg=eq_cfg
            ),
            sort_keys=False,
        )
        if (
            self.running
            and self._proc
            and self._proc.poll() is None
            and self.config_path.exists()
            and self.config_path.read_text(encoding="utf-8") == new_yaml
        ):
            return

        self.stop()
        _atomic_write_text(self.config_path, new_yaml)
        cmd = [
            "camilladsp",
            "-p",
            str(CAMILLA_WS_PORT),
            "-o",
            "/var/log/hydrasound-camilladsp.log",
        ]
        if volume_cfg.mode == "software" or (
            volume_cfg.mode == "disabled" and not volume_cfg.usb_disabled_confirmed
        ):
            cmd.extend(["--gain=-80.0", "--mute", "--gain1=-80.0", "--mute1"])
        cmd.append(str(self.config_path))
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.running = True
            logger.info("Started CamillaDSP on %s", audio_cfg.device_id)
        except FileNotFoundError:
            logger.warning("camilladsp binary not found; DSP mode simulated.")
            self.running = False

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self.running = False

    async def set_volume_db(self, db: float, muted: bool = False) -> None:
        """Send live volume/mute update to CamillaDSP via WebSocket and statefile."""
        if self._cdsp_mode:
            self.write_statefile(db, muted)
            uri = f"ws://127.0.0.1:{CAMILLA_WS_PORT}"
            for attempt in range(3):
                try:
                    async with websockets.connect(uri, open_timeout=0.35) as ws:
                        await ws.send(json.dumps({"SetFaderVolume": [1, db]}))
                        await ws.recv()
                        await ws.send(json.dumps({"SetFaderMute": [1, muted]}))
                        await ws.recv()
                    return
                except Exception:
                    if attempt < 2:
                        await asyncio.sleep(0.12)
            # Idle between streams (alsa_cdsp spawns camilladsp on demand)
            return

        if not self.running:
            return
        if self._proc and self._proc.poll() is not None:
            self.running = False
            tail = ""
            if self._proc.stdout:
                try:
                    out = self._proc.stdout.read().strip()
                    if out:
                        tail = " | ".join(out.splitlines()[-5:])
                except Exception:
                    pass
            if not tail:
                log_path = Path("/var/log/hydrasound-camilladsp.log")
                if log_path.is_file():
                    try:
                        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                        err_lines = [ln for ln in lines[-10:] if "ERROR" in ln or "Playback" in ln]
                        tail = " | ".join(err_lines or lines[-3:])
                    except Exception:
                        pass
            logger.warning(
                "CamillaDSP process exited; cannot apply volume %.2f dB%s",
                db,
                f" ({tail})" if tail else "",
            )
            return
        uri = f"ws://127.0.0.1:{CAMILLA_WS_PORT}"
        last_exc: Optional[Exception] = None
        for attempt in range(12):
            if not self.running or (self._proc and self._proc.poll() is not None):
                return
            try:
                async with websockets.connect(uri, open_timeout=0.5) as ws:
                    await ws.send(json.dumps({"SetFaderVolume": [1, db]}))
                    await ws.recv()
                    await ws.send(json.dumps({"SetFaderMute": [1, muted]}))
                    await ws.recv()
                return
            except Exception as exc:
                last_exc = exc
                if attempt < 11:
                    await asyncio.sleep(0.1)
        logger.warning("CamillaDSP WebSocket volume update failed after retries: %s", last_exc)
