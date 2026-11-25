"""Persistent configuration management for Hydra Sound."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field


class AudioOutputConfig(BaseModel):
    device_id: str = "hw:0,0"
    device_name: str = "Default Audio Device"
    bit_perfect: bool = True
    resample_enabled: bool = False
    resample_rate: int = 96000
    resample_bit_depth: Literal[16, 24, 32] = 24
    resample_algorithm: Literal[
        "SincVeryHigh",
        "SincHigh",
        "SincMedium",
        "SincFast",
    ] = "SincVeryHigh"


class VolumeConfig(BaseModel):
    mode: Literal["disabled", "hardware", "software"] = "disabled"
    curve: Literal["logarithmic", "linear"] = "logarithmic"
    power_on_volume: int = Field(default=35, ge=0, le=100)
    max_volume: int = Field(default=100, ge=1, le=100)
    step_size: int = Field(default=2, ge=1, le=20)
    current_volume: int = Field(default=35, ge=0, le=100)
    muted: bool = False
    hardware_mixer_name: str = "PCM"


class IRConfig(BaseModel):
    mode: Literal["disabled", "flirc", "gpio"] = "disabled"
    gpio_pin: int = Field(default=17, ge=2, le=27)
    flirc_key_vol_up: str = "KEY_VOLUMEUP"
    flirc_key_vol_down: str = "KEY_VOLUMEDOWN"
    flirc_key_mute: str = "KEY_MUTE"


class NetworkConfig(BaseModel):
    hostname: str = "hydrasound"
    interface: str = "eth0"
    mode: Literal["dhcp", "static"] = "dhcp"
    static_ip: str = ""
    subnet_mask: str = "24"
    gateway: str = ""
    dns: str = "1.1.1.1,8.8.8.8"
    wifi_ssid: str = ""
    wifi_password: str = ""


class SpotifyConfig(BaseModel):
    enabled: bool = True
    device_name: str = "Hydra Sound"
    auth_mode: Literal["zeroconf", "credentials"] = "zeroconf"
    username: str = ""
    password: str = ""
    bitrate: int = 320


class QobuzConfig(BaseModel):
    enabled: bool = True
    device_name: str = "Hydra Sound"
    email: str = ""
    password: str = ""


class AirPlayConfig(BaseModel):
    enabled: bool = True
    device_name: str = "Hydra Sound"
    version: Literal["1", "2"] = "1"


class DLNAConfig(BaseModel):
    enabled: bool = True
    device_name: str = "Hydra Sound"


class GoogleCastConfig(BaseModel):
    enabled: bool = False  # Disabled by default due to proprietary DRM/cert limits
    device_name: str = "Hydra Sound"


class RenderersConfig(BaseModel):
    device_name: str = "Hydra Sound"
    pause_timeout_seconds: int = Field(default=60, ge=5, le=3600)
    spotify: SpotifyConfig = Field(default_factory=SpotifyConfig)
    qobuz: QobuzConfig = Field(default_factory=QobuzConfig)
    airplay: AirPlayConfig = Field(default_factory=AirPlayConfig)
    dlna: DLNAConfig = Field(default_factory=DLNAConfig)
    googlecast: GoogleCastConfig = Field(default_factory=GoogleCastConfig)


class SystemConfig(BaseModel):
    github_repo: str = "danceden99-ops/hydra-sound"


class HydraConfig(BaseModel):
    audio: AudioOutputConfig = Field(default_factory=AudioOutputConfig)
    volume: VolumeConfig = Field(default_factory=VolumeConfig)
    ir: IRConfig = Field(default_factory=IRConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    renderers: RenderersConfig = Field(default_factory=RenderersConfig)
    system: SystemConfig = Field(default_factory=SystemConfig)


class ConfigStore:
    """Thread-safe JSON-backed configuration store."""

    def __init__(self, path: Optional[Path] = None) -> None:
        default_path = Path(
            os.environ.get("HYDRA_CONFIG_PATH", "/etc/hydrasound/config.json")
        )
        if not default_path.parent.exists() and not os.environ.get("HYDRA_CONFIG_PATH"):
            default_path = Path(__file__).resolve().parents[2] / "data" / "config.json"
        self.path = path or default_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._config = self._load()

    def _load(self) -> HydraConfig:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                return HydraConfig.model_validate(raw)
            except Exception:
                pass
        cfg = HydraConfig()
        self.save(cfg)
        return cfg

    def get(self) -> HydraConfig:
        return self._config

    def save(self, config: HydraConfig) -> HydraConfig:
        self._config = config
        self.path.write_text(
            config.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return self._config
