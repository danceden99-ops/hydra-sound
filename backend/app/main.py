"""Hydra Sound FastAPI Application Entrypoint."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.audio.alsa import get_usb_device_identity
from app.audio.camilladsp import CamillaDSPManager
from app.audio.hotplug import USBHotplugMonitor, reconcile_audio_device_identity
from app.audio.pipeline import AudioPipelineManager
from app.config import ConfigStore
from app.ir.controller import IRManager
from app.network.manager import NetworkManager
from app.orchestrator.manager import StreamOrchestrator
from app.system.logger import Rolling500FileHandler
from app.system.monitor import (
    get_app_version_info,
    get_installed_module_versions,
    get_system_metrics,
)
from app.volume.controller import VolumeController

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config_store = ConfigStore()
    cfg = config_store.get()

    log_handler = Rolling500FileHandler(
        max_entries=2000, enabled=cfg.system.logging_enabled
    )
    log_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root_logger = logging.getLogger()
    root_logger.addHandler(log_handler)
    ver_info = await get_app_version_info(configured_repo=cfg.system.github_repo)
    logger.info(
        "Hydra Sound starting (version=%s, tag=%s, commit=%s)",
        ver_info.get("app_version", "unknown"),
        ver_info.get("git_tag", "unknown"),
        ver_info.get("git_commit", "unknown"),
    )

    camilla = CamillaDSPManager()
    pipeline = AudioPipelineManager(camilla)
    orchestrator = StreamOrchestrator(pipeline)
    mod_versions = get_installed_module_versions(orchestrator.renderers)
    logger.info(
        "Installed modules: %s",
        ", ".join(f"{k}={v}" for k, v in mod_versions.items()),
    )
    volume_controller = VolumeController(
        camilla=camilla,
        on_volume_changed=orchestrator.push_volume_to_active_client,
    )
    orchestrator.set_volume_reapply_callback(
        lambda cur_cfg: volume_controller.apply(
            cur_cfg.audio, cur_cfg.volume, notify_client=False
        )
    )
    ir_manager = IRManager()
    network_manager = NetworkManager()
    hotplug_monitor = USBHotplugMonitor()

    reconcile_audio_device_identity(cfg)

    app.state.config_store = config_store
    app.state.log_handler = log_handler
    app.state.camilla = camilla
    app.state.pipeline = pipeline
    app.state.orchestrator = orchestrator
    app.state.volume_controller = volume_controller
    app.state.ir_manager = ir_manager
    app.state.network_manager = network_manager
    app.state.hotplug_monitor = hotplug_monitor

    if cfg.volume.mode == "hardware":
        await volume_controller.apply_power_on(cfg.audio, cfg.volume)
        if volume_controller.hardware_error:
            logger.warning(
                "Hardware mixer unavailable (%s); falling back to Software (CamillaDSP) volume control.",
                volume_controller.hardware_error,
            )
            cfg.volume.mode = "software"
    orchestrator.start_enabled_renderers(cfg)
    await volume_controller.apply_power_on(cfg.audio, cfg.volume)
    config_store.save(cfg)

    async def _handle_ir_action(action: str) -> None:
        cur_cfg = config_store.get()
        if cur_cfg.volume.mode == "disabled":
            logger.info("Ignoring IR %s because Volume Mode is Disabled.", action)
            return
        if action == "vol_up":
            await volume_controller.step_volume(1, cur_cfg.audio, cur_cfg.volume)
        elif action == "vol_down":
            await volume_controller.step_volume(-1, cur_cfg.audio, cur_cfg.volume)
        elif action == "mute":
            await volume_controller.toggle_mute(cur_cfg.audio, cur_cfg.volume)
        config_store.save(cur_cfg)

    ir_manager.start_listener(
        asyncio.get_running_loop(), config_store.get, _handle_ir_action
    )

    async def _handle_usb_disconnect(cur_cfg) -> None:
        orchestrator.stop_all()

    async def _handle_usb_reconnect(cur_cfg, new_device_id: str) -> None:
        async with volume_controller._lock:
            cur_cfg.audio.device_id = new_device_id
            try:
                card_idx = new_device_id.split(":", 1)[1].split(",", 1)[0]
                card_id, serial = get_usb_device_identity(card_idx)
                if card_id:
                    cur_cfg.audio.alsa_card_id = card_id
                if serial:
                    cur_cfg.audio.usb_serial = serial
            except Exception:
                pass
            cur_cfg.volume.usb_disabled_confirmed = False
            cur_cfg.volume.current_volume = volume_controller.clamp_volume(
                cur_cfg.volume.power_on_volume, cur_cfg.volume
            )
            config_store.save(cur_cfg)
            if cur_cfg.volume.mode == "hardware":
                await volume_controller._apply_locked(cur_cfg.audio, cur_cfg.volume)
                if volume_controller.hardware_error:
                    logger.warning(
                        "Hardware mixer unavailable on reconnect (%s); falling back to Software DSP.",
                        volume_controller.hardware_error,
                    )
                    cur_cfg.volume.mode = "software"
                    config_store.save(cur_cfg)
            orchestrator.start_enabled_renderers(cur_cfg)
            if cur_cfg.volume.mode != "hardware":
                await volume_controller._apply_locked(cur_cfg.audio, cur_cfg.volume)

    hotplug_monitor.start(
        asyncio.get_running_loop(),
        config_store.get,
        _handle_usb_disconnect,
        _handle_usb_reconnect,
    )

    loop = asyncio.get_running_loop()
    pending_client_vol: dict[str, Optional[int]] = {"value": None}
    client_vol_task_active = False

    async def _drain_client_volume() -> None:
        nonlocal client_vol_task_active
        try:
            while pending_client_vol["value"] is not None:
                pct = pending_client_vol["value"]
                pending_client_vol["value"] = None
                async with volume_controller._lock:
                    cur_cfg = config_store.get()
                    if cur_cfg.volume.mode == "disabled":
                        continue
                    clamped = volume_controller.clamp_volume(pct, cur_cfg.volume)
                    cur_cfg.volume.current_volume = clamped
                    await volume_controller._apply_locked(
                        cur_cfg.audio,
                        cur_cfg.volume,
                        notify_client=(clamped != pct),
                    )
                    config_store.save(cur_cfg)
        finally:
            client_vol_task_active = False

    def _on_client_volume(pct: int) -> None:
        cur_cfg = config_store.get()
        if cur_cfg.volume.mode == "disabled":
            return

        def _enqueue() -> None:
            nonlocal client_vol_task_active
            pending_client_vol["value"] = pct
            if not client_vol_task_active:
                client_vol_task_active = True
                asyncio.create_task(_drain_client_volume())

        loop.call_soon_threadsafe(_enqueue)

    for renderer_name in ("spotify", "qobuz"):
        renderer = orchestrator.renderers.get(renderer_name)
        if hasattr(renderer, "set_volume_callback"):
            renderer.set_volume_callback(_on_client_volume)

    yield

    hotplug_monitor.stop()
    ir_manager.stop_listener()
    proc = getattr(app.state, "test_tone_proc", None)
    if proc is not None and proc.poll() is None:
        proc.terminate()
    orchestrator.stop_all()
    root_logger.removeHandler(log_handler)
    log_handler.close()


app = FastAPI(title="Hydra Sound", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def _no_cache_frontend(request, call_next):
    response = await call_next(request)
    if request.url.path in ("/", "/index.html", "/app.js", "/styles.css"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


app.include_router(router)


@app.websocket("/ws/status")
async def ws_status(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            state = app.state
            cfg = state.config_store.get()
            state.orchestrator.refresh_outgoing_format(cfg)
            await websocket.send_json(
                {
                    "stream": asdict(state.orchestrator.stream),
                    "volume": cfg.volume.model_dump(),
                    "audio": cfg.audio.model_dump(),
                    "system": get_system_metrics(),
                    "dsp_active": state.pipeline.requires_dsp(cfg.audio, cfg.volume, cfg.eq),
                    "eq": cfg.eq.model_dump(),
                    "usb_safety_muted": state.pipeline.requires_usb_safety_mute(cfg.audio, cfg.volume),
                    "hardware_volume_error": state.volume_controller.hardware_error,
                    "test_tone_active": (
                        getattr(state, "test_tone_proc", None) is not None
                        and state.test_tone_proc.poll() is None
                    ),
                    "renderers": state.orchestrator.get_renderers_status(cfg),
                }
            )
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass


FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
