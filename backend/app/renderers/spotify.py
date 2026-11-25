"""Spotify Connect adapter powered by librespot."""

from __future__ import annotations

from app.config import HydraConfig
from app.renderers.base import RendererAdapter, RendererMetadata


class SpotifyRenderer(RendererAdapter):
    meta = RendererMetadata(
        id="spotify",
        display_name="Spotify Connect",
        binary_name="librespot",
        github_repo="librespot-org/librespot",
        update_script="scripts/modules/update_librespot.sh",
        quality_note=(
            "Requires Spotify Premium. Capped at 320 kbps Ogg Vorbis due to "
            "proprietary PlayPlay DRM on Spotify Lossless."
        ),
    )

    def is_enabled(self, cfg: HydraConfig) -> bool:
        return cfg.renderers.spotify.enabled

    def build_command(self, cfg: HydraConfig, alsa_device: str) -> list[str]:
        sc = cfg.renderers.spotify
        # Raspberry Pi vc4-hdmi requires IEC958 framing and channel-status bits;
        # use ALSA's `hdmi:CARD=X,DEV=Y` device for HDMI outputs.
        target_device = alsa_device
        if (
            "hdmi" in cfg.audio.device_name.lower()
            and alsa_device.startswith("hw:")
            and not alsa_device.startswith("hw:Loopback")
        ):
            card_dev = alsa_device.split(":", 1)[1].split(",")
            if len(card_dev) == 2:
                target_device = f"hdmi:CARD={card_dev[0]},DEV={card_dev[1]}"

        cmd = [
            "librespot",
            "--name",
            sc.device_name,
            "--backend",
            "alsa",
            "--device",
            target_device,
            "--bitrate",
            str(sc.bitrate),
            "--onevent",
            "/opt/hydrasound/scripts/hooks/librespot_event.sh",
        ]
        if cfg.volume.mode == "disabled":
            cmd.extend(["--volume-ctrl", "fixed", "--initial-volume", "100"])
        else:
            cmd.extend(["--initial-volume", str(cfg.volume.current_volume)])

        if sc.auth_mode == "credentials" and sc.username and sc.password:
            cmd.extend(["--username", sc.username, "--password", sc.password])
        return cmd
