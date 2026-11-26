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
    assert cfg["devices"]["resampler"]["profile"] == "Accurate"
    assert "master_volume" in cfg["filters"]


def test_volume_curves_and_limits() -> None:
    assert percent_to_db(100, "logarithmic") == 0.0
    assert percent_to_db(0, "logarithmic") == -80.0
    assert percent_to_db(50, "linear") < 0.0

    ctrl = VolumeController(CamillaDSPManager())
    vol_cfg = VolumeConfig(mode="software", max_volume=75, power_on_volume=90)
    assert ctrl.clamp_volume(90, vol_cfg) == 75
    assert ctrl.clamp_volume(-5, vol_cfg) == 0


@pytest.mark.asyncio
async def test_volume_max_limit_enforced_across_all_paths() -> None:
    ctrl = VolumeController(CamillaDSPManager())
    audio = AudioOutputConfig(device_id="hw:1,0")
    vol_cfg = VolumeConfig(mode="software", max_volume=60, power_on_volume=95, current_volume=90)

    await ctrl.apply_power_on(audio, vol_cfg)
    assert vol_cfg.power_on_volume == 60
    assert vol_cfg.current_volume == 60

    await ctrl.set_volume(100, audio, vol_cfg)
    assert vol_cfg.current_volume == 60

    await ctrl.step_volume(1, audio, vol_cfg)
    assert vol_cfg.current_volume == 60

    # Even if mutated directly, apply() clamps before dispatching
    vol_cfg.current_volume = 100
    await ctrl.apply(audio, vol_cfg)
    assert vol_cfg.current_volume == 60


@pytest.mark.asyncio
async def test_stream_orchestrator_lock_and_pause_timeout() -> None:
    cfg = HydraConfig()
    cfg.renderers.pause_timeout_seconds = 1
    cfg.renderers.lock_mode = "strict"
    pipeline = AudioPipelineManager(CamillaDSPManager())
    orch = StreamOrchestrator(pipeline)

    # Strict mode: Spotify acquires lock and blocks Qobuz even when paused
    res1 = await orch.handle_renderer_event("spotify", "play", cfg, title="Track A")
    assert res1["accepted"] is True
    assert orch.stream.active_source == "spotify"

    res2 = await orch.handle_renderer_event("qobuz", "play", cfg, title="Track B")
    assert res2["accepted"] is False
    assert res2["reason"] == "locked"

    await orch.handle_renderer_event("spotify", "pause", cfg)
    assert orch.stream.state == "paused"
    res2_paused = await orch.handle_renderer_event("qobuz", "play", cfg, title="Track B")
    assert res2_paused["accepted"] is False

    # After pause timeout, lock soft-releases
    await asyncio.sleep(1.3)
    assert orch.stream.active_source is None
    assert orch.stream.state == "idle"

    # Preempt-when-paused (default): blocks while playing, switches immediately when paused
    cfg.renderers.lock_mode = "preempt_paused"
    res3 = await orch.handle_renderer_event("qobuz", "play", cfg, title="Track B")
    assert res3["accepted"] is True
    assert (await orch.handle_renderer_event("spotify", "play", cfg))["accepted"] is False
    await orch.handle_renderer_event("qobuz", "pause", cfg)
    res4 = await orch.handle_renderer_event("spotify", "play", cfg, title="Track C")
    assert res4["accepted"] is True
    assert orch.stream.active_source == "spotify"

    # Last-wins: switches immediately even while playing
    cfg.renderers.lock_mode = "last_wins"
    res5 = await orch.handle_renderer_event("qobuz", "play", cfg, title="Track D")
    assert res5["accepted"] is True
    assert orch.stream.active_source == "qobuz"
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


def test_usb_identity_reconciliation(monkeypatch) -> None:
    from app.audio import alsa, hotplug

    fake_devices = [
        alsa.AudioDevice(
            id="hw:5,0",
            card=5,
            device=0,
            name="SMSL USB AUDIO",
            description="SMSL USB AUDIO — USB Audio (hw:5,0)",
            device_type="usb",
            mixers=["SMSL USB AUDIO "],
        )
    ]
    monkeypatch.setattr(alsa, "list_audio_devices", lambda: fake_devices)
    monkeypatch.setattr(alsa, "get_usb_device_identity", lambda c: ("AUDIO", "SMSL-SER-123"))
    monkeypatch.setattr(alsa, "resolve_card_id", lambda c: "AUDIO" if str(c) == "5" else str(c))
    monkeypatch.setattr(hotplug, "get_usb_device_identity", lambda c: ("AUDIO", "SMSL-SER-123"))

    cfg = HydraConfig()
    cfg.audio.device_id = "hw:4,0"
    cfg.audio.device_name = "SMSL USB AUDIO"
    cfg.audio.alsa_card_id = "AUDIO"
    cfg.audio.usb_serial = "SMSL-SER-123"

    changed = hotplug.reconcile_audio_device_identity(cfg)
    assert changed is True
    assert cfg.audio.device_id == "hw:5,0"


def test_spotify_go_librespot_and_fallback(tmp_path: Path, monkeypatch) -> None:
    from app.renderers import spotify as sp_mod

    monkeypatch.setattr(sp_mod, "GO_LIBRESPOT_CONFIG_DIR", tmp_path / "go-librespot")
    renderer = sp_mod.SpotifyRenderer()
    cfg = HydraConfig()

    # Fallback to librespot when go-librespot binary is absent
    monkeypatch.setattr(sp_mod.shutil, "which", lambda name: None)
    cmd_fallback = renderer.build_command(cfg, "hw:1,0")
    assert cmd_fallback[0] == "librespot"

    # Primary go-librespot mode when binary is present
    monkeypatch.setattr(
        sp_mod.shutil, "which", lambda name: "/usr/local/bin/go-librespot" if name == "go-librespot" else None
    )
    cmd_go = renderer.build_command(cfg, "hw:4,0")
    assert cmd_go[0] == "go-librespot"
    assert (tmp_path / "go-librespot" / "config.yml").is_file()

    # Bidirectional volume event from Spotify app
    received: list[int] = []
    renderer.set_volume_callback(lambda pct: received.append(pct))
    renderer._handle_ws_event({"type": "volume", "data": {"value": 42, "max": 100}})
    assert received == [42]


@pytest.mark.asyncio
async def test_orchestrator_stores_artwork_url() -> None:
    cfg = HydraConfig()
    orch = StreamOrchestrator(AudioPipelineManager(CamillaDSPManager()))
    res = await orch.handle_renderer_event(
        "spotify",
        "metadata",
        cfg,
        title="Track X",
        artist="Artist Y",
        artwork_url="https://i.scdn.co/image/test123",
    )
    assert res["accepted"] is True
    assert orch.stream.artwork_url == "https://i.scdn.co/image/test123"


def test_percent_to_db_monotonic_and_camilladsp_limit() -> None:
    for curve in ("logarithmic", "linear"):
        prev = -999.0
        for p in range(0, 101, 5):
            db = percent_to_db(p, curve)
            assert db >= prev
            assert db <= 0.0
            prev = db

    audio = AudioOutputConfig(device_id="hw:1,0")
    vol = VolumeConfig(mode="software", max_volume=60, curve="logarithmic")
    cfg = build_camilladsp_config(audio, vol)
    assert cfg["filters"]["master_volume"]["parameters"]["fader"] == "Aux1"
    assert cfg["filters"]["master_volume"]["parameters"]["limit"] == round(
        percent_to_db(60, "logarithmic"), 2
    )


