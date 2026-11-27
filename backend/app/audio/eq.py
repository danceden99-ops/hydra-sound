"""REW & rePhase Parsers, Psychoacoustic Auto-EQ Solver, and FIR Impulse Utilities."""

from __future__ import annotations

import cmath
import math
import os
import re
import struct
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, List, Optional, Tuple

import yaml

if TYPE_CHECKING:
    import numpy as np

from app.config import EQBand

_REW_LINE_RE = re.compile(
    r"Filter\s+\d+\s*:\s*(ON|OFF)\s+(PK|LS|HS|LP|HP|LPQ|HPQ)\s+Fc\s+([\d.,]+)\s*Hz"
    r"(?:\s+Gain\s+([+-]?[\d.,]+)\s*dB)?(?:\s+Q\s+([\d.,]+))?",
    re.IGNORECASE,
)

_REW_TYPE_MAP = {
    "PK": "Peaking",
    "LS": "LowShelf",
    "HS": "HighShelf",
    "LP": "Lowpass",
    "LPQ": "Lowpass",
    "HP": "Highpass",
    "HPQ": "Highpass",
}


def default_impulses_dir() -> Path:
    cfg_env = os.environ.get("HYDRA_CONFIG_PATH")
    if cfg_env:
        return Path(cfg_env).parent / "impulses"
    etc_dir = Path("/etc/hydrasound")
    if etc_dir.exists() and os.access(etc_dir, os.W_OK):
        return etc_dir / "impulses"
    return Path(__file__).resolve().parents[2] / "data" / "impulses"


def _to_float(val: str) -> float:
    return float(val.replace(",", ".").strip())


def parse_rew_filter_text(text: str, channel: str = "both") -> List[EQBand]:
    """Parse REW 'Export filter settings as text' lines into validated EQBand objects."""
    bands: List[EQBand] = []
    for line in text.splitlines():
        m = _REW_LINE_RE.search(line.strip())
        if not m:
            continue
        state, code, freq_s, gain_s, q_s = m.groups()
        btype = _REW_TYPE_MAP.get(code.upper())
        if not btype:
            continue
        freq = max(10.0, min(24000.0, _to_float(freq_s)))
        gain = max(-24.0, min(12.0, _to_float(gain_s))) if gain_s is not None else 0.0
        q = max(0.1, min(25.0, _to_float(q_s))) if q_s is not None else 0.707
        if btype in ("Lowpass", "Highpass"):
            gain = 0.0
        bands.append(
            EQBand(
                channel=channel,  # type: ignore[arg-type]
                type=btype,  # type: ignore[arg-type]
                freq=round(freq, 2),
                gain=round(gain, 2),
                q=round(q, 3),
                enabled=(state.upper() == "ON"),
            )
        )
    return bands


def parse_rew_camilladsp_yaml(text: str, channel: str = "both") -> List[EQBand]:
    """Parse REW CamillaDSP YAML export into validated EQBand objects."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        return []
    filters = data.get("filters")
    if not isinstance(filters, dict):
        return []
    bands: List[EQBand] = []
    for _, fdef in filters.items():
        if not isinstance(fdef, dict) or fdef.get("type") != "Biquad":
            continue
        params = fdef.get("parameters")
        if not isinstance(params, dict):
            continue
        btype = str(params.get("type", ""))
        if btype in ("LowpassFO", "Lowpass"):
            btype = "Lowpass"
        elif btype in ("HighpassFO", "Highpass"):
            btype = "Highpass"
        elif btype not in ("Peaking", "LowShelf", "HighShelf"):
            continue
        freq = float(params.get("freq", 1000.0))
        gain = float(params.get("gain", 0.0)) if btype not in ("Lowpass", "Highpass") else 0.0
        q_raw = params.get("q")
        if q_raw is None:
            q_raw = 0.707
        bands.append(
            EQBand(
                channel=channel,  # type: ignore[arg-type]
                type=btype,  # type: ignore[arg-type]
                freq=round(max(10.0, min(24000.0, freq)), 2),
                gain=round(max(-24.0, min(12.0, gain)), 2),
                q=round(max(0.1, min(25.0, float(q_raw))), 3),
                enabled=True,
            )
        )
    return bands


def parse_rew_measurement(text: str) -> Tuple[List[float], List[float]]:
    """Parse REW measurement .txt/.frd (Freq SPL [Phase]) skipping * / # comments."""
    pairs: dict[float, float] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("*") or line.startswith("#"):
            continue
        parts = re.split(r"[\s,;]+", line)
        if len(parts) < 2:
            continue
        try:
            f = _to_float(parts[0])
            s = _to_float(parts[1])
        except ValueError:
            continue
        if 10.0 <= f <= 24000.0 and math.isfinite(s) and f not in pairs:
            pairs[f] = s
    if len(pairs) < 8:
        raise ValueError("Measurement file must contain at least 8 valid Frequency/SPL rows.")
    sorted_freqs = sorted(pairs.keys())
    return sorted_freqs, [pairs[f] for f in sorted_freqs]


def downsample_curve_for_preview(
    freqs: Iterable[float], spl: Iterable[float], n_points: int = 160
) -> Tuple[List[float], List[float]]:
    """Interpolate a measurement curve onto compact log-spaced points for JSON storage."""
    import numpy as np

    f_arr = np.asarray(list(freqs), dtype=np.float64)
    s_arr = np.asarray(list(spl), dtype=np.float64)
    f_min = max(20.0, float(f_arr[0]))
    f_max = min(20000.0, float(f_arr[-1]))
    if f_max <= f_min:
        f_min, f_max = 20.0, 20000.0
    grid = np.geomspace(f_min, f_max, n_points)
    interp = np.interp(np.log10(grid), np.log10(f_arr), s_arr)
    return [round(float(x), 2) for x in grid], [round(float(y), 2) for y in interp]


def target_curve_db(freqs_hz: Iterable[float], target: str = "harman") -> List[float]:
    """Return relative target response (dB) at freqs_hz."""
    freqs_list = list(freqs_hz)
    if target == "flat":
        return [0.0 for _ in freqs_list]
    out: List[float] = []
    denom = math.log10(20000.0) - math.log10(1000.0)
    for f in freqs_list:
        fc = max(20.0, min(20000.0, float(f)))
        log_f = math.log10(fc)
        bass = 4.5 / (1.0 + math.exp((log_f - math.log10(110.0)) / 0.14))
        t_pos = max(0.0, min(1.0, (log_f - math.log10(1000.0)) / denom))
        out.append(bass - 1.5 * t_pos)
    return out


def _variable_octave_smooth(freqs: "np.ndarray", spl: "np.ndarray") -> "np.ndarray":
    """Apply 1/12-oct (<250 Hz), 1/6-oct (250-2k Hz), and 1/3-oct (>2k Hz) smoothing."""
    import numpy as np

    log2_f = np.log2(freqs)
    out = np.empty_like(spl)
    for i, f in enumerate(freqs):
        if f < 250.0:
            half_bw = 1.0 / 24.0
        elif f <= 2000.0:
            half_bw = 1.0 / 12.0
        else:
            half_bw = 1.0 / 6.0
        weights = np.exp(-0.5 * ((log2_f - log2_f[i]) / half_bw) ** 2)
        out[i] = float(np.sum(weights * spl) / np.sum(weights))
    return out


def biquad_magnitude_db(
    btype: str, freq: float, gain_db: float, q: float, freqs_hz: Iterable[float], fs: float = 48000.0
) -> List[float]:
    """Compute Audio-EQ-Cookbook biquad magnitude response in dB."""
    freqs_list = list(freqs_hz)
    f0 = min(freq, fs * 0.49)
    w0 = 2.0 * math.pi * f0 / fs
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha = sin_w0 / (2.0 * max(0.1, q))
    A = 10.0 ** (gain_db / 40.0)

    if btype == "Peaking":
        b0 = 1.0 + alpha * A
        b1 = -2.0 * cos_w0
        b2 = 1.0 - alpha * A
        a0 = 1.0 + alpha / A
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha / A
    elif btype == "LowShelf":
        two_sqrt_a_alpha = 2.0 * math.sqrt(A) * alpha
        b0 = A * ((A + 1.0) - (A - 1.0) * cos_w0 + two_sqrt_a_alpha)
        b1 = 2.0 * A * ((A - 1.0) - (A + 1.0) * cos_w0)
        b2 = A * ((A + 1.0) - (A - 1.0) * cos_w0 - two_sqrt_a_alpha)
        a0 = (A + 1.0) + (A - 1.0) * cos_w0 + two_sqrt_a_alpha
        a1 = -2.0 * ((A - 1.0) + (A + 1.0) * cos_w0)
        a2 = (A + 1.0) + (A - 1.0) * cos_w0 - two_sqrt_a_alpha
    elif btype == "HighShelf":
        two_sqrt_a_alpha = 2.0 * math.sqrt(A) * alpha
        b0 = A * ((A + 1.0) + (A - 1.0) * cos_w0 + two_sqrt_a_alpha)
        b1 = -2.0 * A * ((A - 1.0) + (A + 1.0) * cos_w0)
        b2 = A * ((A + 1.0) + (A - 1.0) * cos_w0 - two_sqrt_a_alpha)
        a0 = (A + 1.0) - (A - 1.0) * cos_w0 + two_sqrt_a_alpha
        a1 = 2.0 * ((A - 1.0) - (A + 1.0) * cos_w0)
        a2 = (A + 1.0) - (A - 1.0) * cos_w0 - two_sqrt_a_alpha
    elif btype == "Lowpass":
        b0 = (1.0 - cos_w0) / 2.0
        b1 = 1.0 - cos_w0
        b2 = (1.0 - cos_w0) / 2.0
        a0 = 1.0 + alpha
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha
    elif btype == "Highpass":
        b0 = (1.0 + cos_w0) / 2.0
        b1 = -(1.0 + cos_w0)
        b2 = (1.0 + cos_w0) / 2.0
        a0 = 1.0 + alpha
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha
    else:
        return [0.0 for _ in freqs_list]

    out: List[float] = []
    f_cap = fs * 0.495
    for f in freqs_list:
        w = 2.0 * math.pi * max(1.0, min(f_cap, float(f))) / fs
        z_inv = cmath.rect(1.0, -w)
        z_inv2 = z_inv * z_inv
        num = b0 + b1 * z_inv + b2 * z_inv2
        den = a0 + a1 * z_inv + a2 * z_inv2
        mag = max(abs(num / den), 1e-9)
        out.append(20.0 * math.log10(mag))
    return out


def evaluate_bands_db(
    bands: Iterable[EQBand], channel: str, freqs_hz: Iterable[float], fs: float = 48000.0
) -> List[float]:
    """Sum enabled biquad magnitude responses (dB) for a given channel."""
    freqs_list = list(freqs_hz)
    total = [0.0] * len(freqs_list)
    for b in bands:
        if not b.enabled:
            continue
        if b.channel not in ("both", channel):
            continue
        resp = biquad_magnitude_db(b.type, b.freq, b.gain, b.q, freqs_list, fs=fs)
        for i, val in enumerate(resp):
            total[i] += val
    return total


def compute_max_boost_db(
    bands: Iterable[EQBand], channel: str, impulse_peak_db: float = 0.0
) -> float:
    """Return worst-case positive boost (dB) across 20 Hz - 20 kHz for headroom pre-gain."""
    band_list = list(bands)
    grid = [20.0 * (1000.0 ** (i / 399.0)) for i in range(400)]
    for b in band_list:
        if b.enabled and b.channel in ("both", channel) and b.freq > 0.0:
            grid.append(float(b.freq))
    total_db = evaluate_bands_db(band_list, channel, grid)
    biquad_peak = max([0.0] + total_db)
    return round(biquad_peak + max(0.0, float(impulse_peak_db)), 2)


def auto_eq_from_measurement(
    freqs: Iterable[float],
    spl: Iterable[float],
    target_curve: str = "harman",
    channel: str = "both",
    max_bands: int = 10,
) -> Tuple[List[EQBand], List[float], List[float]]:
    """Generate cut-biased, null-safe Peaking Biquad filters from a REW measurement."""
    try:
        import numpy as np
    except ImportError as exc:
        raise ValueError(
            "numpy is required for Auto-EQ calculation. Run: /opt/hydrasound/venv/bin/pip install numpy"
        ) from exc

    f_arr = np.asarray(list(freqs), dtype=np.float64)
    s_arr = np.asarray(list(spl), dtype=np.float64)
    grid = np.geomspace(20.0, 20000.0, 220)
    raw_interp = np.interp(np.log10(grid), np.log10(f_arr), s_arr)
    smoothed = _variable_octave_smooth(grid, raw_interp)
    target = np.asarray(target_curve_db(grid, target_curve), dtype=np.float64)

    # Align target to midrange (300 Hz - 2 kHz)
    mid_mask = (grid >= 300.0) & (grid <= 2000.0)
    if np.any(mid_mask):
        offset = float(np.median(smoothed[mid_mask] - target[mid_mask]))
    else:
        offset = float(np.median(smoothed - target))
    aligned_target = target + offset
    residual = smoothed - aligned_target  # positive = peak to cut, negative = dip

    # Mask out narrow deep cancellations (dips deeper than -6 dB)
    active_mask = (grid >= 25.0) & (grid <= 16000.0)
    null_mask = residual < -6.0
    weights = np.where(active_mask & ~null_mask, 1.0, 0.0)
    # Give slightly higher priority to bass/low-mid room modes (25 - 500 Hz)
    weights = np.where(grid <= 500.0, weights * 1.4, weights)

    bands: List[EQBand] = []
    q_candidates = (1.2, 2.0, 3.5, 6.0, 9.0)
    current_err = residual.copy()

    for _ in range(max_bands):
        weighted_err = current_err * weights
        idx = int(np.argmax(np.abs(weighted_err)))
        peak_err = float(current_err[idx])
        if abs(peak_err) < 1.0:
            break
        fc = float(grid[idx])
        if peak_err > 0:
            # Cut resonant peak
            target_gain = max(-12.0, -peak_err)
            q_choices = q_candidates
        else:
            # Cap positive boosts at <= +3.0 dB and Q <= 2.0
            target_gain = min(3.0, -peak_err)
            q_choices = (1.0, 1.5, 2.0)

        best_score = float(np.sum((current_err ** 2) * weights))
        best_band: Optional[EQBand] = None
        best_resp: Optional[np.ndarray] = None

        for q in q_choices:
            resp = np.asarray(biquad_magnitude_db("Peaking", fc, target_gain, q, grid), dtype=np.float64)
            cand_err = current_err + resp
            score = float(np.sum((cand_err ** 2) * weights))
            if score < best_score:
                best_score = score
                best_band = EQBand(
                    channel=channel,  # type: ignore[arg-type]
                    type="Peaking",
                    freq=round(fc, 1),
                    gain=round(target_gain, 2),
                    q=round(q, 2),
                    enabled=True,
                )
                best_resp = resp

        if best_band is None or best_resp is None:
            break
        bands.append(best_band)
        current_err = current_err + best_resp

    bands.sort(key=lambda b: b.freq)
    preview_f, preview_s = downsample_curve_for_preview(grid, smoothed - offset, n_points=160)
    return bands, preview_f, preview_s


def _parse_wav_bytes(raw: bytes, channel_idx: int = 0) -> Tuple["np.ndarray", int]:
    """Decode PCM (16/24/32-bit) or IEEE float (32/64-bit) WAV bytes into float64 taps."""
    import numpy as np
    if len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("Invalid WAV header.")

    pos = 12
    audio_fmt = 1
    channels = 1
    sample_rate = 44100
    bits_per_sample = 32
    data_bytes = b""

    while pos + 8 <= len(raw):
        chunk_id = raw[pos : pos + 4]
        chunk_size = struct.unpack_from("<I", raw, pos + 4)[0]
        pos += 8
        chunk_data = raw[pos : pos + chunk_size]
        pos += chunk_size + (chunk_size & 1)
        if chunk_id == b"fmt " and len(chunk_data) >= 16:
            audio_fmt, channels, sample_rate, _, _, bits_per_sample = struct.unpack_from(
                "<HHIIHH", chunk_data, 0
            )
            if audio_fmt == 0xFFFE and len(chunk_data) >= 26:
                audio_fmt = struct.unpack_from("<H", chunk_data, 24)[0]
        elif chunk_id == b"data":
            data_bytes = chunk_data
            break

    if not data_bytes:
        raise ValueError("WAV file contains no audio data chunk.")

    if audio_fmt == 3:
        if bits_per_sample == 32:
            arr = np.frombuffer(data_bytes, dtype="<f4").astype(np.float64)
        elif bits_per_sample == 64:
            arr = np.frombuffer(data_bytes, dtype="<f8").astype(np.float64)
        else:
            raise ValueError(f"Unsupported float WAV bit depth: {bits_per_sample}")
    elif audio_fmt == 1:
        if bits_per_sample == 16:
            arr = np.frombuffer(data_bytes, dtype="<i2").astype(np.float64) / 32768.0
        elif bits_per_sample == 32:
            arr = np.frombuffer(data_bytes, dtype="<i4").astype(np.float64) / 2147483648.0
        elif bits_per_sample == 24:
            n_samples = len(data_bytes) // 3
            raw_u8 = np.frombuffer(data_bytes[: n_samples * 3], dtype=np.uint8).reshape(-1, 3)
            vals = (
                raw_u8[:, 0].astype(np.int32)
                | (raw_u8[:, 1].astype(np.int32) << 8)
                | (raw_u8[:, 2].astype(np.int32) << 16)
            )
            vals = np.where(vals & 0x800000, vals - 0x1000000, vals)
            arr = vals.astype(np.float64) / 8388608.0
        else:
            raise ValueError(f"Unsupported PCM WAV bit depth: {bits_per_sample}")
    else:
        raise ValueError(f"Unsupported WAV format tag: {audio_fmt}")

    if channels > 1:
        use_ch = min(max(0, channel_idx), channels - 1)
        arr = arr[use_ch::channels]
    return arr, int(sample_rate)


def _write_float32_wav(path: Path, taps: "np.ndarray", sample_rate: int) -> None:
    """Write mono 32-bit IEEE float WAV impulse file for CamillaDSP Conv."""
    import numpy as np
    path.parent.mkdir(parents=True, exist_ok=True)
    f32 = np.asarray(taps, dtype="<f4")
    pcm_bytes = f32.tobytes()
    data_size = len(pcm_bytes)
    byte_rate = sample_rate * 4
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        3,  # IEEE float
        1,  # mono
        sample_rate,
        byte_rate,
        4,  # block align
        32,
        b"data",
        data_size,
    )
    path.write_bytes(header + pcm_bytes)


def import_rephase_impulse(
    raw: bytes,
    filename: str,
    preset_id: str,
    side: str,
    sample_rate: int = 44100,
    channel_idx: int = 0,
    impulses_dir: Optional[Path] = None,
) -> Tuple[Path, int, int, float]:
    """Validate and save a rePhase FIR impulse (.wav/.txt/.bin) as mono 32-bit float WAV.

    Returns (saved_path, sample_rate, tap_count, peak_gain_db).
    """
    try:
        import numpy as np
    except ImportError as exc:
        raise ValueError(
            "numpy is required for FIR impulse import. Run: /opt/hydrasound/venv/bin/pip install numpy"
        ) from exc

    ext = Path(filename).suffix.lower()
    if ext == ".wav":
        taps, detected_rate = _parse_wav_bytes(raw, channel_idx=channel_idx)
        sr = detected_rate
    elif ext == ".txt":
        text = raw.decode("utf-8", errors="replace")
        vals: List[float] = []
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("*") or s.startswith("#"):
                continue
            for tok in re.split(r"[\s,;]+", s):
                if tok:
                    vals.append(float(tok))
        taps = np.asarray(vals, dtype=np.float64)
        sr = sample_rate
    elif ext == ".bin":
        if len(raw) % 4 != 0:
            raise ValueError("Raw .bin impulse byte length must be a multiple of 4.")
        taps = np.frombuffer(raw, dtype="<f4").astype(np.float64)
        sr = sample_rate
    else:
        raise ValueError("Unsupported impulse file type. Use .wav, .txt, or .bin.")

    if len(taps) == 0 or len(taps) > 65536:
        raise ValueError(f"Impulse tap count must be between 1 and 65,536 (got {len(taps)}).")
    if not np.all(np.isfinite(taps)):
        raise ValueError("Impulse contains NaN or infinite values.")

    # Compute worst-case frequency-domain boost for clipping protection
    n_fft = max(4096, 1 << (len(taps) - 1).bit_length())
    mag = np.abs(np.fft.rfft(taps, n=n_fft))
    peak_mag = float(np.max(mag)) if len(mag) else 1.0
    peak_gain_db = round(max(0.0, 20.0 * math.log10(max(peak_mag, 1e-9))), 2)

    out_dir = impulses_dir or default_impulses_dir()
    safe_side = "left" if side == "left" else "right"
    out_path = out_dir / f"{preset_id}_{safe_side}.wav"
    _write_float32_wav(out_path, taps, sr)
    return out_path, sr, int(len(taps)), peak_gain_db
