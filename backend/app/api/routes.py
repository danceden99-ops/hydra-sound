"""FastAPI REST & internal hook routes for Hydra Sound."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.audio.camilladsp import get_effective_max_volume, get_hard_max_volume

from app.audio.alsa import (
    format_named_alsa_device,
    get_device_capabilities,
    get_usb_device_identity,
    invalidate_usb_cache,
    list_audio_devices,
    select_best_pcm_format,
)
from app.audio.eq import (
    auto_eq_from_measurement,
    compute_max_boost_db,
    evaluate_bands_db,
    import_rephase_impulse,
    parse_rew_camilladsp_yaml,
    parse_rew_filter_text,
    parse_rew_measurement,
    target_curve_db,
)
from app.audio.pipeline import has_alsa_cdsp
from app.config import (
    AudioOutputConfig,
    EQBand,
    EQPreset,
    HydraConfig,
    IRConfig,
    NetworkConfig,
    RenderersConfig,
    RoomEQConfig,
    SystemConfig,
    VolumeConfig,
)
from app.system.monitor import (
    check_all_module_updates,
    get_app_version_info,
    get_system_metrics,
    install_github_release,
    list_github_releases,
    run_module_update,
    run_script_update,
)

router = APIRouter(prefix="/api")


class VolumeSetRequest(BaseModel):
    volume: int


class RendererEventRequest(BaseModel):
    source: str
    event: str  # play | pause | stop | metadata | disconnect
    artist: str = ""
    title: str = ""
    album: str = ""
    artwork_url: str = ""
    incoming_format: str = ""
    incoming_rate: Optional[int] = None
    incoming_bit_depth: Optional[int] = None


class FlircLearnRequest(BaseModel):
    action: str  # vol_up | vol_down | mute
    mode: Optional[str] = None  # optional override: flirc | gpio


class IRClearRequest(BaseModel):
    mode: Optional[str] = None


class WiFiConnectRequest(BaseModel):
    ssid: str
    password: str = ""
    country: str = "US"


class SystemPowerRequest(BaseModel):
    action: str  # reboot | shutdown


class InstallReleaseRequest(BaseModel):
    tag: str


@router.get("/status")
async def get_status(request: Request) -> dict:
    state = request.app.state
    cfg: HydraConfig = state.config_store.get()
    state.orchestrator.refresh_outgoing_format(cfg)
    return {
        "stream": asdict(state.orchestrator.stream),
        "volume": cfg.volume.model_dump(),
        "effective_max_volume": get_effective_max_volume(cfg.volume),
        "hard_max_volume": get_hard_max_volume(),
        "audio": cfg.audio.model_dump(),
        "system": get_system_metrics(),
        "network": state.network_manager.get_status(cfg.network),
        "dsp_active": state.pipeline.requires_dsp(cfg.audio, cfg.volume, cfg.eq),
        "eq": cfg.eq.model_dump(),
        "usb_safety_muted": state.pipeline.requires_usb_safety_mute(cfg.audio, cfg.volume),
        "hardware_volume_error": state.volume_controller.hardware_error,
        "test_tone_active": _is_test_tone_running(state),
        "renderers": state.orchestrator.get_renderers_status(cfg),
    }


@router.get("/config")
async def get_config(request: Request) -> dict:
    cfg: HydraConfig = request.app.state.config_store.get()
    data = cfg.model_dump()
    # Mask passwords in GET response
    if data["renderers"]["spotify"]["password"]:
        data["renderers"]["spotify"]["password"] = "********"
    if data["renderers"]["qobuz"]["password"]:
        data["renderers"]["qobuz"]["password"] = "********"
    if data["network"]["wifi_password"]:
        data["network"]["wifi_password"] = "********"
    return data


@router.get("/audio/devices")
async def get_audio_devices() -> list[dict]:
    return [asdict(d) for d in list_audio_devices()]


def _is_test_tone_running(state) -> bool:
    proc = getattr(state, "test_tone_proc", None)
    return proc is not None and proc.poll() is None


def _stop_test_tone(state) -> None:
    import subprocess

    proc = getattr(state, "test_tone_proc", None)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
    state.test_tone_proc = None


@router.get("/audio/capabilities")
async def get_audio_capabilities(device_id: str) -> dict:
    return get_device_capabilities(device_id)


@router.post("/audio/test-tone")
async def toggle_test_tone(request: Request) -> dict:
    import subprocess

    state = request.app.state
    cfg: HydraConfig = state.config_store.get()

    if _is_test_tone_running(state):
        _stop_test_tone(state)
        return {"ok": True, "playing": False, "volume": cfg.volume.model_dump()}

    if state.pipeline.requires_usb_safety_mute(cfg.audio, cfg.volume):
        raise HTTPException(
            status_code=409,
            detail="Output is muted by USB speaker protection. Confirm 0 dB pass-through first.",
        )
    if cfg.volume.mode == "hardware" and state.volume_controller.hardware_error:
        raise HTTPException(status_code=409, detail=state.volume_controller.hardware_error)

    if cfg.volume.mode != "disabled":
        safe_vol = min(cfg.volume.current_volume, 50)
        await state.volume_controller.set_volume(
            safe_vol, cfg.audio, cfg.volume, unmute=True
        )
        state.config_store.save(cfg)

    target = state.pipeline.get_renderer_alsa_device(cfg.audio, cfg.volume, cfg.eq)
    is_hdmi = "hdmi" in cfg.audio.device_name.lower()
    if target not in ("camilladsp",) and "loopback" not in target.lower():
        target = format_named_alsa_device(target, is_hdmi=is_hdmi)

    fmt = select_best_pcm_format(cfg.audio.device_id)
    if fmt == "S24_4_RJ_LE":
        fmt = "S24_LE"
    elif fmt not in ("S16_LE", "S24_3_LE", "S32_LE"):
        fmt = "S32_LE"

    cmd = ["speaker-test", "-D", target, "-c", "2", "-t", "sine", "-f", "440", "-F", fmt]
    try:
        state.test_tone_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="speaker-test (alsa-utils) is not installed")
    return {"ok": True, "playing": True, "volume": cfg.volume.model_dump()}


@router.put("/config/audio")
async def update_audio_config(payload: AudioOutputConfig, request: Request) -> dict:
    state = request.app.state
    async with state.volume_controller._lock:
        cfg: HydraConfig = state.config_store.get()
        device_changed = (
            cfg.audio.device_id != payload.device_id
            or cfg.audio.device_name != payload.device_name
        )
        cfg.audio = payload
        if payload.device_id.startswith("hw:"):
            try:
                card_idx = payload.device_id.split(":", 1)[1].split(",", 1)[0]
                card_id, serial = get_usb_device_identity(card_idx)
                if card_id and not cfg.audio.alsa_card_id:
                    cfg.audio.alsa_card_id = card_id
                if serial and not cfg.audio.usb_serial:
                    cfg.audio.usb_serial = serial
            except Exception:
                pass
        if device_changed:
            invalidate_usb_cache()
            cfg.volume.usb_disabled_confirmed = False
            cfg.volume.current_volume = state.volume_controller.clamp_volume(
                cfg.volume.power_on_volume, cfg.volume
            )
        state.config_store.save(cfg)
        if cfg.volume.mode == "hardware":
            await state.volume_controller._apply_locked(cfg.audio, cfg.volume)
        state.orchestrator.start_enabled_renderers(cfg)
        if cfg.volume.mode != "hardware":
            await state.volume_controller._apply_locked(cfg.audio, cfg.volume)
    return {
        "ok": True,
        "audio": cfg.audio.model_dump(),
        "usb_safety_muted": state.pipeline.requires_usb_safety_mute(cfg.audio, cfg.volume),
    }


@router.put("/config/volume")
async def update_volume_config(payload: VolumeConfig, request: Request) -> dict:
    state = request.app.state
    async with state.volume_controller._lock:
        cfg: HydraConfig = state.config_store.get()
        prev_mode = cfg.volume.mode
        prev_vol = cfg.volume.current_volume
        prev_muted = cfg.volume.muted
        mode_changed = prev_mode != payload.mode
        max_changed = cfg.volume.max_volume != payload.max_volume
        curve_changed = cfg.volume.curve != payload.curve
        if "mode" in payload.model_fields_set and payload.mode != "disabled":
            cfg.volume.usb_disabled_confirmed = False
        for field_name in payload.model_fields_set:
            if field_name in payload.model_fields:
                setattr(cfg.volume, field_name, getattr(payload, field_name))
        if mode_changed and {prev_mode, cfg.volume.mode} == {"hardware", "software"}:
            safe_cap = 25 if cfg.volume.curve == "linear" else 70
            if cfg.volume.max_volume > safe_cap:
                cfg.volume.max_volume = safe_cap
                max_changed = True
        if curve_changed and cfg.volume.curve == "linear" and cfg.volume.max_volume > 25:
            cfg.volume.max_volume = 25
            max_changed = True
        cfg.volume.power_on_volume = state.volume_controller.clamp_volume(
            cfg.volume.power_on_volume, cfg.volume
        )
        cfg.volume.current_volume = state.volume_controller.clamp_volume(
            cfg.volume.current_volume, cfg.volume
        )
        state.config_store.save(cfg)

        fallback_error: Optional[str] = None
        if cfg.volume.mode == "hardware":
            await state.volume_controller._apply_locked(cfg.audio, cfg.volume)
            if state.volume_controller.hardware_error:
                fallback_error = state.volume_controller.hardware_error
                logging.getLogger(__name__).warning(
                    "Hardware mixer unavailable (%s); falling back to Software (CamillaDSP) volume control.",
                    fallback_error,
                )
                cfg.volume.mode = "software"
                state.config_store.save(cfg)
                mode_changed = True
        if (
            mode_changed
            or state.pipeline.requires_usb_safety_mute(cfg.audio, cfg.volume)
            or (cfg.volume.mode == "software" and has_alsa_cdsp() and (max_changed or curve_changed))
        ):
            state.orchestrator.start_enabled_renderers(cfg)
        elif cfg.volume.mode == "software" and (max_changed or curve_changed):
            state.orchestrator.pipeline.apply(
                cfg.audio,
                cfg.volume,
                capture_rate=state.orchestrator._active_capture_rate,
                eq_cfg=cfg.eq,
            )
        if cfg.volume.mode != "hardware":
            await state.volume_controller._apply_locked(cfg.audio, cfg.volume)
    return {
        "ok": True,
        "volume": cfg.volume.model_dump(),
        "usb_safety_muted": state.pipeline.requires_usb_safety_mute(cfg.audio, cfg.volume),
        "fallback_to_software": fallback_error is not None,
        "hardware_error": fallback_error,
    }


@router.post("/volume/confirm-usb-disabled")
async def confirm_usb_disabled(request: Request) -> dict:
    state = request.app.state
    async with state.volume_controller._lock:
        cfg: HydraConfig = state.config_store.get()
        cfg.volume.usb_disabled_confirmed = True
        state.config_store.save(cfg)
        logging.getLogger(__name__).info(
            "USB DAC speaker protection confirmed by user; enabling 0 dB bit-perfect pass-through on %s.",
            cfg.audio.device_id,
        )
        state.orchestrator.start_enabled_renderers(cfg)
        await state.volume_controller._apply_locked(cfg.audio, cfg.volume)
    return {
        "ok": True,
        "volume": cfg.volume.model_dump(),
        "usb_safety_muted": state.pipeline.requires_usb_safety_mute(cfg.audio, cfg.volume),
    }


@router.post("/volume/set")
async def set_volume(payload: VolumeSetRequest, request: Request) -> dict:
    state = request.app.state
    cfg: HydraConfig = state.config_store.get()
    await state.volume_controller.set_volume(payload.volume, cfg.audio, cfg.volume)
    state.config_store.save(cfg)
    return {"ok": True, "volume": cfg.volume.model_dump()}


@router.post("/volume/mute")
async def toggle_mute(request: Request) -> dict:
    state = request.app.state
    cfg: HydraConfig = state.config_store.get()
    await state.volume_controller.toggle_mute(cfg.audio, cfg.volume)
    state.config_store.save(cfg)
    return {"ok": True, "volume": cfg.volume.model_dump()}


@router.put("/config/renderers")
async def update_renderers_config(payload: RenderersConfig, request: Request) -> dict:
    state = request.app.state
    cfg: HydraConfig = state.config_store.get()
    # Preserve existing passwords if masked placeholder is sent back
    if payload.spotify.password == "********":
        payload.spotify.password = cfg.renderers.spotify.password
    if payload.qobuz.password == "********":
        payload.qobuz.password = cfg.renderers.qobuz.password
    # Propagate global advertised device name to all renderers
    name = payload.device_name.strip() or "Hydra Sound"
    payload.device_name = name
    payload.spotify.device_name = name
    payload.qobuz.device_name = name
    payload.airplay.device_name = name
    payload.dlna.device_name = name
    payload.googlecast.device_name = name
    cfg.renderers = payload
    state.config_store.save(cfg)
    state.orchestrator.start_enabled_renderers(cfg)
    await state.volume_controller.apply(cfg.audio, cfg.volume, notify_client=False)
    return {"ok": True}


@router.post("/stream/release")
async def release_stream_lock(request: Request) -> dict:
    state = request.app.state
    cfg: HydraConfig = state.config_store.get()
    state.orchestrator.force_release_lock(cfg)
    return {"ok": True}


@router.post("/internal/event")
async def internal_renderer_event(payload: RendererEventRequest, request: Request) -> dict:
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(status_code=403, detail="Internal events restricted to loopback")
    state = request.app.state
    cfg: HydraConfig = state.config_store.get()
    return await state.orchestrator.handle_renderer_event(
        source=payload.source,
        event=payload.event,
        cfg=cfg,
        artist=payload.artist,
        title=payload.title,
        album=payload.album,
        artwork_url=payload.artwork_url,
        incoming_format=payload.incoming_format,
        incoming_rate=payload.incoming_rate,
        incoming_bit_depth=payload.incoming_bit_depth,
    )


@router.get("/ir/status")
async def get_ir_status(request: Request) -> dict:
    state = request.app.state
    cfg: HydraConfig = state.config_store.get()
    return {
        "config": cfg.ir.model_dump(),
        "flirc": state.ir_manager.get_flirc_status(),
        "gpio": state.ir_manager.get_gpio_status(cfg.ir),
        "activity": state.ir_manager.get_activity_log(),
    }


@router.put("/config/ir")
async def update_ir_config(payload: IRConfig, request: Request) -> dict:
    state = request.app.state
    cfg: HydraConfig = state.config_store.get()
    if "gpio_keymap" not in payload.model_fields_set:
        payload.gpio_keymap = cfg.ir.gpio_keymap
    cfg.ir = payload
    state.config_store.save(cfg)
    gpio_res = state.ir_manager.apply_gpio_overlay(cfg.ir)
    return {"ok": True, "ir": cfg.ir.model_dump(), "gpio": gpio_res}


@router.post("/ir/learn")
async def learn_ir(payload: FlircLearnRequest, request: Request) -> dict:
    state = request.app.state
    cfg: HydraConfig = state.config_store.get()
    mode = payload.mode or cfg.ir.mode
    if mode == "gpio":
        res = await asyncio.to_thread(
            state.ir_manager.learn_gpio_command,
            payload.action,
            cfg.ir.gpio_keymap,
        )
        if res.get("ok") and "scancode" in res:
            cfg.ir.gpio_keymap[payload.action] = int(res["scancode"])
            state.config_store.save(cfg)
        return res
    if mode == "flirc":
        return await asyncio.to_thread(state.ir_manager.learn_flirc_command, payload.action)
    return {"ok": False, "error": "Enable FLIRC or GPIO IR mode and save before learning."}


@router.post("/ir/clear")
async def clear_ir(request: Request, payload: Optional[IRClearRequest] = None) -> dict:
    state = request.app.state
    cfg: HydraConfig = state.config_store.get()
    mode = (payload.mode if payload else None) or cfg.ir.mode
    if mode == "gpio":
        cfg.ir.gpio_keymap = {}
        state.config_store.save(cfg)
        return {"ok": True, "message": "GPIO learned IR keys cleared."}
    if mode == "flirc":
        return await asyncio.to_thread(state.ir_manager.clear_flirc)
    return {"ok": False, "error": "Enable FLIRC or GPIO IR mode first."}


@router.post("/ir/flirc/learn")
async def learn_flirc_ir(payload: FlircLearnRequest, request: Request) -> dict:
    return await asyncio.to_thread(request.app.state.ir_manager.learn_flirc_command, payload.action)


@router.post("/ir/flirc/clear")
async def clear_flirc_ir(request: Request) -> dict:
    return await asyncio.to_thread(request.app.state.ir_manager.clear_flirc)


@router.put("/config/network")
async def update_network_config(payload: NetworkConfig, request: Request) -> dict:
    state = request.app.state
    cfg: HydraConfig = state.config_store.get()
    if payload.wifi_password == "********":
        payload.wifi_password = cfg.network.wifi_password
    cfg.network = payload
    state.config_store.save(cfg)
    res = state.network_manager.apply(cfg.network)
    return {"ok": res["ok"], "details": res}


@router.get("/network/wifi/scan")
async def scan_wifi_networks(request: Request, country: str = "") -> list[dict]:
    cfg: HydraConfig = request.app.state.config_store.get()
    cc = (country or cfg.network.wifi_country or "US").strip().upper()
    return request.app.state.network_manager.scan_wifi(country=cc)


@router.post("/network/wifi/connect")
async def connect_wifi_network(payload: WiFiConnectRequest, request: Request) -> dict:
    state = request.app.state
    cfg: HydraConfig = state.config_store.get()
    password = (
        cfg.network.wifi_password
        if payload.password == "********"
        else payload.password
    )
    cfg.network.wifi_ssid = payload.ssid
    cfg.network.wifi_password = password
    cfg.network.wifi_country = (payload.country or "US").strip().upper()
    state.config_store.save(cfg)
    return state.network_manager.connect_wifi(
        payload.ssid, password, country=cfg.network.wifi_country
    )


@router.get("/system/version")
async def get_system_version(request: Request) -> dict:
    cfg: HydraConfig = request.app.state.config_store.get()
    return await get_app_version_info(
        request.app.version, configured_repo=cfg.system.github_repo
    )


def _read_camilladsp_log_lines(limit: int = 100) -> list[str]:
    from pathlib import Path

    cam_log = Path("/var/log/hydrasound-camilladsp.log")
    if not cam_log.is_file():
        return []
    try:
        raw_lines = [
            ln for ln in cam_log.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()
        ]
        return [f"[camilladsp] {ln}" for ln in raw_lines[-limit:]]
    except Exception:
        return []


@router.get("/system/logs")
async def get_system_logs(request: Request) -> dict:
    cfg: HydraConfig = request.app.state.config_store.get()
    handler = getattr(request.app.state, "log_handler", None)
    cam_lines = _read_camilladsp_log_lines()
    if not handler:
        return {
            "lines": cam_lines,
            "count": len(cam_lines),
            "path": "",
            "enabled": cfg.system.logging_enabled,
        }
    lines = handler.get_lines() + cam_lines
    return {
        "lines": lines,
        "count": len(lines),
        "max_entries": handler.max_entries,
        "path": str(handler.log_path),
        "enabled": handler.is_enabled(),
    }


@router.post("/system/logs/clear")
async def clear_system_logs(request: Request) -> dict:
    from pathlib import Path

    handler = getattr(request.app.state, "log_handler", None)
    if handler:
        handler.clear()
    cam_log = Path("/var/log/hydrasound-camilladsp.log")
    if cam_log.is_file():
        try:
            cam_log.write_text("", encoding="utf-8")
        except Exception:
            pass
    return {"ok": True}


@router.get("/system/logs/download")
async def download_system_logs(request: Request) -> PlainTextResponse:
    handler = getattr(request.app.state, "log_handler", None)
    lines = (handler.get_lines() if handler else []) + _read_camilladsp_log_lines(limit=250)
    content = "\n".join(lines) + ("\n" if lines else "")
    return PlainTextResponse(
        content=content,
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="hydrasound.log"'},
    )


@router.put("/config/system")
async def update_system_config(payload: SystemConfig, request: Request) -> dict:
    state = request.app.state
    cfg: HydraConfig = state.config_store.get()
    cfg.system = payload
    state.config_store.save(cfg)
    handler = getattr(state, "log_handler", None)
    if handler:
        handler.set_enabled(payload.logging_enabled)
    return {"ok": True, "system": cfg.system.model_dump()}


@router.get("/system/releases")
async def get_system_releases(request: Request) -> list[dict]:
    cfg: HydraConfig = request.app.state.config_store.get()
    if not cfg.system.github_repo.strip():
        return []
    return await list_github_releases(cfg.system.github_repo)


@router.post("/system/install-release")
async def install_system_release(payload: InstallReleaseRequest, request: Request) -> dict:
    import asyncio
    import subprocess

    cfg: HydraConfig = request.app.state.config_store.get()
    repo = cfg.system.github_repo.strip()
    if not repo:
        raise HTTPException(status_code=400, detail="GitHub repository is not configured.")
    res = install_github_release(repo, payload.tag)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Install failed"))

    async def _delayed_restart() -> None:
        await asyncio.sleep(0.5)
        try:
            subprocess.Popen(["systemctl", "restart", "hydrasound.service"])
        except Exception:
            pass

    asyncio.create_task(_delayed_restart())
    return res


@router.post("/system/power")
async def system_power_action(payload: SystemPowerRequest) -> dict:
    import asyncio
    import subprocess

    if payload.action not in ("reboot", "shutdown"):
        raise HTTPException(status_code=400, detail="Action must be 'reboot' or 'shutdown'")

    cmd = ["systemctl", "reboot"] if payload.action == "reboot" else ["systemctl", "poweroff"]

    async def _delayed_power() -> None:
        await asyncio.sleep(0.5)
        try:
            subprocess.Popen(cmd)
        except Exception:
            pass

    asyncio.create_task(_delayed_power())
    return {"ok": True, "action": payload.action}


@router.get("/modules/updates")
async def get_module_updates(request: Request) -> list[dict]:
    return await check_all_module_updates(request.app.state.orchestrator.renderers)


@router.post("/modules/{module_id}/update")
async def trigger_module_update(module_id: str, request: Request) -> dict:
    import asyncio

    state = request.app.state
    cfg: HydraConfig = state.config_store.get()
    if module_id == "camilladsp":
        state.pipeline.camilla.stop()
        res = await asyncio.to_thread(
            run_script_update, "scripts/modules/update_camilladsp.sh"
        )
        state.orchestrator.start_enabled_renderers(cfg)
        await state.volume_controller.apply(cfg.audio, cfg.volume)
        return res

    renderers = state.orchestrator.renderers
    if module_id not in renderers:
        raise HTTPException(status_code=404, detail="Unknown module")
    adapter = renderers[module_id]
    adapter.stop()
    res = await asyncio.to_thread(run_module_update, adapter)
    if adapter.is_enabled(cfg):
        adapter.start(cfg, state.orchestrator._current_alsa_device)
    return res


class EQFilterImportRequest(BaseModel):
    preset_id: Optional[str] = None
    preset_name: str = "REW EQ"
    channel: str = "both"  # both | left | right
    content: str
    replace_channel: bool = True


class EQMeasurementImportRequest(BaseModel):
    preset_id: Optional[str] = None
    preset_name: str = "REW Auto-EQ"
    channel: str = "both"
    target_curve: str = "harman"  # harman | flat
    content: str


class EQImpulseImportRequest(BaseModel):
    preset_id: Optional[str] = None
    preset_name: str = "rePhase FIR"
    side: str = "stereo"  # stereo | left | right
    filename: str
    content_b64: str
    sample_rate: int = 44100


class EQPreviewRequest(BaseModel):
    bands: list[EQBand] = []
    target_curve: str = "harman"
    impulse_peak_gain_db: float = 0.0


def _slugify_preset_id(name: str) -> str:
    import re
    import uuid

    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return f"{slug or 'preset'}-{uuid.uuid4().hex[:6]}"


async def _apply_eq_change_locked(state, cfg: HydraConfig, was_dsp: bool) -> None:
    state.config_store.save(cfg)
    now_dsp = state.pipeline.requires_dsp(cfg.audio, cfg.volume, cfg.eq)
    if was_dsp != now_dsp or (now_dsp and has_alsa_cdsp()):
        state.orchestrator.start_enabled_renderers(cfg)
    elif now_dsp:
        state.orchestrator.pipeline.apply(
            cfg.audio,
            cfg.volume,
            capture_rate=state.orchestrator._active_capture_rate,
            eq_cfg=cfg.eq,
        )
    if cfg.volume.mode != "hardware":
        await state.volume_controller._apply_locked(cfg.audio, cfg.volume)


@router.get("/eq")
async def get_eq_config(request: Request) -> dict:
    cfg: HydraConfig = request.app.state.config_store.get()
    return cfg.eq.model_dump()


@router.put("/eq")
async def update_eq_config(payload: RoomEQConfig, request: Request) -> dict:
    state = request.app.state
    async with state.volume_controller._lock:
        cfg: HydraConfig = state.config_store.get()
        was_dsp = state.pipeline.requires_dsp(cfg.audio, cfg.volume, cfg.eq)
        cfg.eq = payload
        await _apply_eq_change_locked(state, cfg, was_dsp)
    return {
        "ok": True,
        "eq": cfg.eq.model_dump(),
        "dsp_active": state.pipeline.requires_dsp(cfg.audio, cfg.volume, cfg.eq),
    }


@router.post("/eq/presets")
async def save_eq_preset(payload: EQPreset, request: Request) -> dict:
    state = request.app.state
    async with state.volume_controller._lock:
        cfg: HydraConfig = state.config_store.get()
        was_dsp = state.pipeline.requires_dsp(cfg.audio, cfg.volume, cfg.eq)
        if not payload.id:
            payload.id = _slugify_preset_id(payload.name)
        idx = next((i for i, p in enumerate(cfg.eq.presets) if p.id == payload.id), None)
        if idx is not None:
            cfg.eq.presets[idx] = payload
        else:
            cfg.eq.presets.append(payload)
        cfg.eq.active_preset_id = payload.id
        await _apply_eq_change_locked(state, cfg, was_dsp)
    return {"ok": True, "preset": payload.model_dump(), "eq": cfg.eq.model_dump()}


@router.delete("/eq/presets/{preset_id}")
async def delete_eq_preset(preset_id: str, request: Request) -> dict:
    from pathlib import Path

    state = request.app.state
    async with state.volume_controller._lock:
        cfg: HydraConfig = state.config_store.get()
        was_dsp = state.pipeline.requires_dsp(cfg.audio, cfg.volume, cfg.eq)
        target = next((p for p in cfg.eq.presets if p.id == preset_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="Preset not found")
        for imp in (target.left_impulse_path, target.right_impulse_path):
            if imp:
                try:
                    Path(imp).unlink(missing_ok=True)
                except Exception:
                    pass
        cfg.eq.presets = [p for p in cfg.eq.presets if p.id != preset_id]
        if cfg.eq.active_preset_id == preset_id:
            cfg.eq.active_preset_id = cfg.eq.presets[0].id if cfg.eq.presets else None
            if not cfg.eq.active_preset_id:
                cfg.eq.enabled = False
        await _apply_eq_change_locked(state, cfg, was_dsp)
    return {"ok": True, "eq": cfg.eq.model_dump()}


@router.post("/eq/import/filters")
async def import_eq_filters(payload: EQFilterImportRequest, request: Request) -> dict:
    channel = payload.channel if payload.channel in ("both", "left", "right") else "both"
    bands = parse_rew_filter_text(payload.content, channel=channel)
    if not bands:
        try:
            bands = parse_rew_camilladsp_yaml(payload.content, channel=channel)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid YAML filter file: {exc}") from exc
    if not bands:
        raise HTTPException(
            status_code=400,
            detail="No valid REW Filter text or CamillaDSP Biquad YAML filters found.",
        )

    state = request.app.state
    async with state.volume_controller._lock:
        cfg: HydraConfig = state.config_store.get()
        was_dsp = state.pipeline.requires_dsp(cfg.audio, cfg.volume, cfg.eq)
        preset = next((p for p in cfg.eq.presets if p.id == payload.preset_id), None) if payload.preset_id else None
        if preset is None:
            preset = EQPreset(
                id=_slugify_preset_id(payload.preset_name),
                name=payload.preset_name.strip() or "REW EQ",
                source_type="rew_filters",
                bands=bands[:24],
            )
            cfg.eq.presets.append(preset)
        else:
            preset.source_type = "rew_filters"
            if payload.replace_channel:
                kept = [b for b in preset.bands if b.channel != channel]
                preset.bands = (kept + bands)[:24]
            else:
                preset.bands = (preset.bands + bands)[:24]
        cfg.eq.active_preset_id = preset.id
        await _apply_eq_change_locked(state, cfg, was_dsp)
    return {"ok": True, "imported_count": len(bands), "preset": preset.model_dump(), "eq": cfg.eq.model_dump()}


@router.post("/eq/import/measurement")
async def import_eq_measurement(payload: EQMeasurementImportRequest, request: Request) -> dict:
    channel = payload.channel if payload.channel in ("both", "left", "right") else "both"
    target_curve = "flat" if payload.target_curve == "flat" else "harman"
    try:
        freqs, spl = parse_rew_measurement(payload.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    bands, prev_f, prev_s = auto_eq_from_measurement(
        freqs, spl, target_curve=target_curve, channel=channel, max_bands=10
    )
    state = request.app.state
    async with state.volume_controller._lock:
        cfg: HydraConfig = state.config_store.get()
        was_dsp = state.pipeline.requires_dsp(cfg.audio, cfg.volume, cfg.eq)
        preset = next((p for p in cfg.eq.presets if p.id == payload.preset_id), None) if payload.preset_id else None
        if preset is None:
            preset = EQPreset(
                id=_slugify_preset_id(payload.preset_name),
                name=payload.preset_name.strip() or "REW Auto-EQ",
                source_type="rew_measurement_auto",
                target_curve=target_curve,
                bands=bands,
                measurement_freqs=prev_f,
                measurement_spl=prev_s,
            )
            cfg.eq.presets.append(preset)
        else:
            preset.source_type = "rew_measurement_auto"
            preset.target_curve = target_curve
            kept = [b for b in preset.bands if b.channel != channel]
            preset.bands = (kept + bands)[:24]
            preset.measurement_freqs = prev_f
            preset.measurement_spl = prev_s
        cfg.eq.active_preset_id = preset.id
        await _apply_eq_change_locked(state, cfg, was_dsp)
    return {"ok": True, "imported_count": len(bands), "preset": preset.model_dump(), "eq": cfg.eq.model_dump()}


@router.post("/eq/import/impulse")
async def import_eq_impulse(payload: EQImpulseImportRequest, request: Request) -> dict:
    import base64

    try:
        raw = base64.b64decode(payload.content_b64)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid base64 impulse payload") from exc

    state = request.app.state
    async with state.volume_controller._lock:
        cfg: HydraConfig = state.config_store.get()
        was_dsp = state.pipeline.requires_dsp(cfg.audio, cfg.volume, cfg.eq)
        preset = next((p for p in cfg.eq.presets if p.id == payload.preset_id), None) if payload.preset_id else None
        if preset is None:
            preset = EQPreset(
                id=_slugify_preset_id(payload.preset_name),
                name=payload.preset_name.strip() or "rePhase FIR",
                source_type="rephase_fir",
            )
            cfg.eq.presets.append(preset)
        else:
            preset.source_type = "rephase_fir"

        try:
            if payload.side == "stereo":
                lp, sr, taps, peak_l = import_rephase_impulse(
                    raw, payload.filename, preset.id, "left", sample_rate=payload.sample_rate, channel_idx=0
                )
                rp, _, _, peak_r = import_rephase_impulse(
                    raw, payload.filename, preset.id, "right", sample_rate=payload.sample_rate, channel_idx=1
                )
                preset.left_impulse_path = str(lp)
                preset.right_impulse_path = str(rp)
                preset.impulse_sample_rate = sr
                preset.impulse_taps = taps
                preset.impulse_peak_gain_db = max(peak_l, peak_r)
            elif payload.side == "left":
                lp, sr, taps, peak_l = import_rephase_impulse(
                    raw, payload.filename, preset.id, "left", sample_rate=payload.sample_rate, channel_idx=0
                )
                preset.left_impulse_path = str(lp)
                preset.impulse_sample_rate = sr
                preset.impulse_taps = taps
                preset.impulse_peak_gain_db = max(preset.impulse_peak_gain_db, peak_l)
            else:
                rp, sr, taps, peak_r = import_rephase_impulse(
                    raw, payload.filename, preset.id, "right", sample_rate=payload.sample_rate, channel_idx=0
                )
                preset.right_impulse_path = str(rp)
                preset.impulse_sample_rate = sr
                preset.impulse_taps = taps
                preset.impulse_peak_gain_db = max(preset.impulse_peak_gain_db, peak_r)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        cfg.eq.active_preset_id = preset.id
        await _apply_eq_change_locked(state, cfg, was_dsp)
    return {"ok": True, "preset": preset.model_dump(), "eq": cfg.eq.model_dump()}


@router.post("/eq/preview")
async def preview_eq_response(payload: EQPreviewRequest) -> dict:
    grid = [20.0 * (1000.0 ** (i / 159.0)) for i in range(160)]
    left = evaluate_bands_db(payload.bands, "left", grid)
    right = evaluate_bands_db(payload.bands, "right", grid)
    tgt = target_curve_db(grid, payload.target_curve)
    headroom = max(
        compute_max_boost_db(payload.bands, "left", payload.impulse_peak_gain_db),
        compute_max_boost_db(payload.bands, "right", payload.impulse_peak_gain_db),
    )
    return {
        "freqs": [round(float(x), 2) for x in grid],
        "biquad_left": [round(float(x), 2) for x in left],
        "biquad_right": [round(float(x), 2) for x in right],
        "target": [round(float(x), 2) for x in tgt],
        "headroom_db": headroom,
    }
