"""FastAPI REST & internal hook routes for Hydra Sound."""

from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.audio.alsa import list_audio_devices
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


@router.put("/config/audio")
async def update_audio_config(payload: AudioOutputConfig, request: Request) -> dict:
    state = request.app.state
    cfg: HydraConfig = state.config_store.get()
    cfg.audio = payload
    state.config_store.save(cfg)
    state.orchestrator.start_enabled_renderers(cfg)
    return {"ok": True, "audio": cfg.audio.model_dump()}


@router.put("/config/volume")
async def update_volume_config(payload: VolumeConfig, request: Request) -> dict:
    state = request.app.state
    cfg: HydraConfig = state.config_store.get()
    mode_changed = cfg.volume.mode != payload.mode
    cfg.volume = payload
    cfg.volume.current_volume = state.volume_controller.clamp_volume(
        cfg.volume.current_volume, cfg.volume
    )
    state.config_store.save(cfg)
    if mode_changed:
        state.orchestrator.start_enabled_renderers(cfg)
    await state.volume_controller.apply(cfg.audio, cfg.volume)
    return {"ok": True, "volume": cfg.volume.model_dump()}


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
    state = request.app.state
    cfg: HydraConfig = state.config_store.get()
    return await state.orchestrator.handle_renderer_event(
        source=payload.source,
        event=payload.event,
        cfg=cfg,
        artist=payload.artist,
        title=payload.title,
        album=payload.album,
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
async def scan_wifi_networks(request: Request) -> list[dict]:
    return request.app.state.network_manager.scan_wifi()


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


@router.put("/config/system")
async def update_system_config(payload: SystemConfig, request: Request) -> dict:
    state = request.app.state
    cfg: HydraConfig = state.config_store.get()
    cfg.system = payload
    state.config_store.save(cfg)
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
    state = request.app.state
    renderers = state.orchestrator.renderers
    if module_id not in renderers:
        raise HTTPException(status_code=404, detail="Unknown module")
    adapter = renderers[module_id]
    adapter.stop()
    res = run_module_update(adapter)
    cfg: HydraConfig = state.config_store.get()
    if adapter.is_enabled(cfg):
        adapter.start(cfg, state.orchestrator._current_alsa_device)
    return res
