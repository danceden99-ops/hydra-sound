"""Hydra Sound FastAPI Application Entrypoint."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.audio.camilladsp import CamillaDSPManager
from app.audio.pipeline import AudioPipelineManager
from app.config import ConfigStore
from app.ir.controller import IRManager
from app.network.manager import NetworkManager
from app.orchestrator.manager import StreamOrchestrator
from app.system.logger import Rolling500FileHandler
from app.system.monitor import get_system_metrics
from app.volume.controller import VolumeController

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config_store = ConfigStore()
    cfg = config_store.get()

    log_handler = Rolling500FileHandler(
        max_entries=500, enabled=cfg.system.logging_enabled
    )
    log_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root_logger = logging.getLogger()
    root_logger.addHandler(log_handler)

    camilla = CamillaDSPManager()
    pipeline = AudioPipelineManager(camilla)
    orchestrator = StreamOrchestrator(pipeline)
    volume_controller = VolumeController(
        camilla=camilla,
        on_volume_changed=orchestrator.push_volume_to_active_client,
    )
    ir_manager = IRManager()
    network_manager = NetworkManager()

    app.state.config_store = config_store
    app.state.log_handler = log_handler
    app.state.camilla = camilla
    app.state.pipeline = pipeline
    app.state.orchestrator = orchestrator
    app.state.volume_controller = volume_controller
    app.state.ir_manager = ir_manager
    app.state.network_manager = network_manager

    orchestrator.start_enabled_renderers(cfg)
    await volume_controller.apply_power_on(cfg.audio, cfg.volume)
    config_store.save(cfg)

    yield

    orchestrator.stop_all()
    root_logger.removeHandler(log_handler)


app = FastAPI(title="Hydra Sound", version="1.0.0", lifespan=lifespan)
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
                    "dsp_active": state.pipeline.requires_dsp(cfg.audio, cfg.volume),
                    "renderers": state.orchestrator.get_renderers_status(cfg),
                }
            )
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass


FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
