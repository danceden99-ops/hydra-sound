"""Unit tests for Hydra Sound core pipeline, volume curves, orchestrator, and API."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.audio.camilladsp import CamillaDSPManager, build_camilladsp_config
from app.audio.pipeline import LOOPBACK_PLAYBACK_DEVICE, AudioPipelineManager
from app.config import AudioOutputConfig, HydraConfig, VolumeConfig
from app.main import app
from app.orchestrator.manager import StreamOrchestrator
from app.system.logger import Rolling500FileHandler
from app.volume.controller import VolumeController, percent_to_db


def test_dynamic_bypass_bit_perfect_vs_dsp(tmp_path: Path) -> None:
    camilla = CamillaDSPManager(config_path=tmp_path / "camilla.yml")
    pipeline = AudioPipelineManager(camilla)

    audio = AudioOutputConfig(device_id="hw:1,0", resample_enabled=False)
    vol_disabled = VolumeConfig(mode="disabled")
    assert pipeline.requires_dsp(audio, vol_disabled) is False
    assert pipeline.get_renderer_alsa_device(audio, vol_disabled) == "hw:1,0"

    vol_hw = VolumeConfig(mode="hardware")
    assert pipeline.requires_dsp(audio, vol_hw) is False

    vol_sw = VolumeConfig(mode="software")
    assert pipeline.requires_dsp(audio, vol_sw) is True
    assert pipeline.get_renderer_alsa_device(audio, vol_sw) == LOOPBACK_PLAYBACK_DEVICE

    audio_resample = AudioOutputConfig(device_id="hw:1,0", resample_enabled=True)
    assert pipeline.requires_dsp(audio_resample, vol_disabled) is True


def test_camilladsp_config_generation() -> None:
    audio = AudioOutputConfig(
        device_id="hw:2,0",
        resample_enabled=True,
        resample_rate=96000,
        resample_bit_depth=24,
        resample_algorithm="SincVeryHigh",
    )
    vol = VolumeConfig(mode="software")
    cfg = build_camilladsp_config(audio, vol)
    assert cfg["devices"]["samplerate"] == 96000
    assert cfg["devices"]["playback"]["device"] == "hw:2,0"
    assert cfg["devices"]["playback"]["format"] == "S24_3_LE"
    assert cfg["devices"]["resampler"]["profile"] == "VeryAccurate"
    assert "master_volume" in cfg["filters"]


def test_volume_curves_and_limits() -> None:
    assert percent_to_db(100, "logarithmic") == 0.0
    assert percent_to_db(0, "logarithmic") == -80.0
    assert percent_to_db(50, "linear") < 0.0

    ctrl = VolumeController(CamillaDSPManager())
    vol_cfg = VolumeConfig(mode="software", max_volume=75, power_on_volume=90)
    assert ctrl.clamp_volume(90, vol_cfg) == 75


@pytest.mark.asyncio
async def test_stream_orchestrator_lock_and_pause_timeout() -> None:
    cfg = HydraConfig()
    cfg.renderers.pause_timeout_seconds = 1
    pipeline = AudioPipelineManager(CamillaDSPManager())
    orch = StreamOrchestrator(pipeline)

    # First-come-first-serve: Spotify acquires lock
    res1 = await orch.handle_renderer_event("spotify", "play", cfg, title="Track A")
    assert res1["accepted"] is True
    assert orch.stream.active_source == "spotify"

    # Qobuz is rejected while Spotify is playing
    res2 = await orch.handle_renderer_event("qobuz", "play", cfg, title="Track B")
    assert res2["accepted"] is False
    assert res2["reason"] == "locked"

    # Pause starts timeout; after >1s lock releases
    await orch.handle_renderer_event("spotify", "pause", cfg)
    assert orch.stream.state == "paused"
    await asyncio.sleep(1.3)
    assert orch.stream.active_source is None
    assert orch.stream.state == "idle"

    # Now Qobuz can acquire lock, and Stop releases immediately
    res3 = await orch.handle_renderer_event("qobuz", "play", cfg, title="Track B")
    assert res3["accepted"] is True
    await orch.handle_renderer_event("qobuz", "stop", cfg)
    assert orch.stream.active_source is None


def test_api_defaults_and_googlecast_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HYDRA_CONFIG_PATH", str(tmp_path / "config.json"))
    with TestClient(app) as client:
        cfg_resp = client.get("/api/config")
        assert cfg_resp.status_code == 200
        data = cfg_resp.json()
        assert data["renderers"]["googlecast"]["enabled"] is False
        assert data["renderers"]["airplay"]["version"] == "1"
        assert data["audio"]["bit_perfect"] is True

        status_resp = client.get("/api/status")
        assert status_resp.status_code == 200
        status_data = status_resp.json()
        assert status_data["dsp_active"] is False
        assert "ip_address" in status_data["network"]
        assert "cidr" in status_data["network"]
        assert "mode" in status_data["network"]

        # Verify custom advertised device name propagates to all renderers
        renderers_payload = data["renderers"]
        renderers_payload["device_name"] = "Living Room HiFi"
        put_resp = client.put("/api/config/renderers", json=renderers_payload)
        assert put_resp.status_code == 200
        updated = client.get("/api/config").json()["renderers"]
        assert updated["device_name"] == "Living Room HiFi"
        assert updated["spotify"]["device_name"] == "Living Room HiFi"
        assert updated["qobuz"]["device_name"] == "Living Room HiFi"
        assert updated["airplay"]["device_name"] == "Living Room HiFi"
        assert updated["dlna"]["device_name"] == "Living Room HiFi"
        assert updated["googlecast"]["device_name"] == "Living Room HiFi"

        # Verify version endpoint & power action validation
        ver_resp = client.get("/api/system/version")
        assert ver_resp.status_code == 200
        assert ver_resp.json()["app_version"] == "1.0.0"

        bad_power = client.post("/api/system/power", json={"action": "sleep"})
        assert bad_power.status_code == 400

        # Verify GitHub repo config & release tag validation
        sys_put = client.put("/api/config/system", json={"github_repo": "example/hydra-sound"})
        assert sys_put.status_code == 200
        assert client.get("/api/config").json()["system"]["github_repo"] == "example/hydra-sound"

        bad_tag = client.post("/api/system/install-release", json={"tag": "v1.0.0; rm -rf /"})
        assert bad_tag.status_code == 400

        logs_resp = client.get("/api/system/logs")
        assert logs_resp.status_code == 200
        assert "lines" in logs_resp.json()


def test_rolling_500_log_handler(tmp_path: Path) -> None:
    import logging

    log_file = tmp_path / "hydrasound.log"
    handler = Rolling500FileHandler(log_path=log_file, max_entries=500)
    handler.setFormatter(logging.Formatter("%(message)s"))
    test_logger = logging.getLogger("hydra_test_500")
    test_logger.setLevel(logging.INFO)
    test_logger.addHandler(handler)
    try:
        for i in range(600):
            test_logger.info("entry-%d", i)
        lines = log_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 500
        assert lines[0] == "entry-100"
        assert lines[-1] == "entry-599"
    finally:
        test_logger.removeHandler(handler)
