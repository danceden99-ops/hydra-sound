"""FastAPI REST & internal hook routes for Hydra Sound."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.audio.alsa import (
    format_named_alsa_device,
    get_device_capabilities,
    get_usb_device_identity,
    invalidate_usb_cache,
    list_audio_devices,
    select_best_pcm_format,
)
from app.config import (
    AudioOutputConfig,
    HydraConfig,
    IRConfig,
    NetworkConfig,
    RenderersConfig,
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
        "audio": cfg.audio.model_dump(),
        "system": get_system_metrics(),
        "network": state.network_manager.get_status(cfg.network),
        "dsp_active": state.pipeline.requires_dsp(cfg.audio, cfg.volume),
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

    if cfg.volume.mode != "disabled":
        cfg.volume.muted = False
        await state.volume_controller.set_volume(50, cfg.audio, cfg.volume)
        state.config_store.save(cfg)

    target = state.pipeline.get_renderer_alsa_device(cfg.audio, cfg.volume)
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
        await state.volume_controller.apply(cfg.audio, cfg.volume)
    state.orchestrator.start_enabled_renderers(cfg)
    if cfg.volume.mode != "hardware":
        await state.volume_controller.apply(cfg.audio, cfg.volume)
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
        mode_changed = cfg.volume.mode != payload.mode
        max_changed = cfg.volume.max_volume != payload.max_volume
        curve_changed = cfg.volume.curve != payload.curve
        if payload.mode != "disabled":
            payload.usb_disabled_confirmed = False
        cfg.volume = payload
        cfg.volume.power_on_volume = state.volume_controller.clamp_volume(
            cfg.volume.power_on_volume, cfg.volume
        )
        cfg.volume.current_volume = state.volume_controller.clamp_volume(
            cfg.volume.current_volume, cfg.volume
        )
        state.config_store.save(cfg)

    if cfg.volume.mode == "hardware":
        await state.volume_controller.apply(cfg.audio, cfg.volume)
    if mode_changed or state.pipeline.requires_usb_safety_mute(cfg.audio, cfg.volume):
        state.orchestrator.start_enabled_renderers(cfg)
    elif cfg.volume.mode == "software" and (max_changed or curve_changed):
        state.orchestrator.pipeline.apply(
            cfg.audio,
            cfg.volume,
            capture_rate=state.orchestrator._active_capture_rate,
        )
    if cfg.volume.mode != "hardware":
        await state.volume_controller.apply(cfg.audio, cfg.volume)
    return {
        "ok": True,
        "volume": cfg.volume.model_dump(),
        "usb_safety_muted": state.pipeline.requires_usb_safety_mute(cfg.audio, cfg.volume),
    }


@router.post("/volume/confirm-usb-disabled")
async def confirm_usb_disabled(request: Request) -> dict:
    state = request.app.state
    cfg: HydraConfig = state.config_store.get()
    cfg.volume.usb_disabled_confirmed = True
    state.config_store.save(cfg)
    logging.getLogger(__name__).info(
        "USB DAC speaker protection confirmed by user; enabling 0 dB bit-perfect pass-through on %s.",
        cfg.audio.device_id,
    )
    state.orchestrator.start_enabled_renderers(cfg)
    await state.volume_controller.apply(cfg.audio, cfg.volume)
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
    }


@router.put("/config/ir")
async def update_ir_config(payload: IRConfig, request: Request) -> dict:
    state = request.app.state
    cfg: HydraConfig = state.config_store.get()
    cfg.ir = payload
    state.config_store.save(cfg)
    gpio_res = state.ir_manager.apply_gpio_overlay(cfg.ir)
    return {"ok": True, "ir": cfg.ir.model_dump(), "gpio": gpio_res}


@router.post("/ir/flirc/learn")
async def learn_flirc_ir(payload: FlircLearnRequest, request: Request) -> dict:
    return request.app.state.ir_manager.learn_flirc_command(payload.action)


@router.post("/ir/flirc/clear")
async def clear_flirc_ir(request: Request) -> dict:
    return request.app.state.ir_manager.clear_flirc()


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
