"""ALSA device discovery, hardware mixer probing, and stream format inspection."""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class AudioDevice:
    id: str
    card: int
    device: int
    name: str
    description: str
    device_type: str  # "usb", "hdmi", "analog", "other"
    mixers: List[str]


@dataclass
class ActiveAlsaFormat:
    format: Optional[str] = None
    rate: Optional[int] = None
    channels: Optional[int] = None
    bit_depth: Optional[int] = None


_APLAY_LINE_RE = re.compile(
    r"^card\s+(\d+):\s+([^\[]+)\[([^\]]+)\],\s+device\s+(\d+):\s+([^\[]+)\[([^\]]+)\]"
)


def _classify_device(card_id: str, card_name: str, dev_name: str) -> str:
    combined = f"{card_id} {card_name} {dev_name}".lower()
    if "hdmi" in combined or "vc4" in combined:
        return "hdmi"
    if "usb" in combined or "dac" in combined or "audio" in card_id.lower():
        if "headphones" not in combined and "bcm2835" not in combined:
            return "usb"
    if "headphones" in combined or "bcm2835" in combined or "analog" in combined:
        return "analog"
    return "other"


def resolve_card_id(card: str) -> str:
    """Resolve numeric ALSA card index (e.g. '1') to card ID (e.g. 'vc4hdmi0')."""
    proc_id = Path(f"/proc/asound/card{card}/id")
    try:
        if proc_id.exists():
            val = proc_id.read_text(encoding="utf-8").strip()
            if val:
                return val
    except Exception:
        pass
    return card


def format_named_alsa_device(alsa_device: str, is_hdmi: bool = False) -> str:
    """Convert `hw:X,Y` to `plughw:CARD=<id>,DEV=Y` (HDMI) or `hw:CARD=<id>,DEV=Y` for `aplay -L` enumeration."""
    if "loopback" in alsa_device.lower() or not alsa_device.startswith("hw:"):
        return alsa_device
    parts = alsa_device.split(":", 1)[1].split(",")
    if len(parts) != 2:
        return alsa_device
    card_id = resolve_card_id(parts[0].strip())
    dev = parts[1].strip()
    prefix = "plughw" if is_hdmi else "hw"
    return f"{prefix}:CARD={card_id},DEV={dev}"


def list_mixers(card: int) -> List[str]:
    """Return available simple ALSA mixer controls for a card."""
    try:
        res = subprocess.run(
            ["amixer", "-c", str(card), "scontrols"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode != 0:
            return []
        mixers: List[str] = []
        for line in res.stdout.splitlines():
            m = re.search(r"Simple mixer control '([^']+)'", line)
            if m:
                mixers.append(m.group(1))
        return mixers
    except Exception:
        return []


def list_audio_devices() -> List[AudioDevice]:
    """Enumerate ALSA playback devices via `aplay -l`, excluding Loopback."""
    try:
        res = subprocess.run(
            ["aplay", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode != 0:
            return _fallback_devices()
    except Exception:
        return _fallback_devices()

    devices: List[AudioDevice] = []
    for line in res.stdout.splitlines():
        m = _APLAY_LINE_RE.match(line.strip())
        if not m:
            continue
        card = int(m.group(1))
        card_id = m.group(2).strip()
        card_name = m.group(3).strip()
        dev = int(m.group(4))
        dev_name = m.group(6).strip()

        if "loopback" in card_id.lower() or "loopback" in card_name.lower():
            continue

        devices.append(
            AudioDevice(
                id=f"hw:{card},{dev}",
                card=card,
                device=dev,
                name=card_name,
                description=f"{card_name} — {dev_name} (hw:{card},{dev})",
                device_type=_classify_device(card_id, card_name, dev_name),
                mixers=list_mixers(card),
            )
        )
    return devices or _fallback_devices()


_USB_CACHE: Dict[str, Tuple[float, bool]] = {}


def invalidate_usb_cache() -> None:
    """Clear the cached USB device classification map (e.g. after hotplug)."""
    _USB_CACHE.clear()


def get_usb_device_identity(card: str | int) -> Tuple[str, str]:
    """Return (alsa_card_id, usb_serial) for a given ALSA card index."""
    card_str = str(card).strip()
    alsa_card_id = resolve_card_id(card_str)
    if alsa_card_id == card_str:
        alsa_card_id = ""
    usb_serial = ""
    sys_card = Path(f"/sys/class/sound/card{card_str}/device")
    try:
        resolved = sys_card.resolve()
        for parent in [resolved, *resolved.parents]:
            serial_path = parent / "serial"
            if serial_path.is_file():
                usb_serial = serial_path.read_text(encoding="utf-8").strip()
                break
            if parent.name == "usb" or parent == Path("/sys"):
                break
    except Exception:
        pass
    return alsa_card_id, usb_serial


def find_device_by_identity(
    alsa_card_id: str = "",
    usb_serial: str = "",
    device_name: str = "",
    preferred_dev: int = 0,
) -> Optional[str]:
    """Locate current `hw:X,Y` for a DAC using stable USB serial, card ID, or name."""
    devices = list_audio_devices()
    if usb_serial:
        for dev in devices:
            _, ser = get_usb_device_identity(dev.card)
            if ser and ser == usb_serial and dev.device == preferred_dev:
                return dev.id
        for dev in devices:
            _, ser = get_usb_device_identity(dev.card)
            if ser and ser == usb_serial:
                return dev.id
    if alsa_card_id:
        for dev in devices:
            if resolve_card_id(str(dev.card)) == alsa_card_id and dev.device == preferred_dev:
                return dev.id
        for dev in devices:
            if resolve_card_id(str(dev.card)) == alsa_card_id:
                return dev.id
    if device_name:
        for dev in devices:
            if dev.name == device_name and dev.device == preferred_dev:
                return dev.id
    return None


def is_usb_audio_device(device_id: str, device_name: str = "") -> bool:
    """Return True if the selected ALSA output is a USB audio device (cached 10s)."""
    now = time.monotonic()
    cached = _USB_CACHE.get(device_id)
    if cached and now - cached[0] < 10.0:
        return cached[1]
    result = "usb" in device_name.lower()
    for dev in list_audio_devices():
        if dev.id == device_id:
            result = dev.device_type == "usb"
            break
    _USB_CACHE[device_id] = (now, result)
    return result


def _fallback_devices() -> List[AudioDevice]:
    """Fallback list when running outside a Linux ALSA host (e.g. dev/test)."""
    return [
        AudioDevice(
            id="hw:0,0",
            card=0,
            device=0,
            name="Default ALSA Device",
            description="Default ALSA Device (hw:0,0)",
            device_type="other",
            mixers=["PCM"],
        )
    ]


def probe_active_hw_params(device_id: str) -> ActiveAlsaFormat:
    """Read `/proc/asound/cardX/pcmYp/sub0/hw_params` for live outgoing format."""
    m = re.match(r"^(?:hw|plughw):(\d+),(\d+)$", device_id)
    if not m:
        return ActiveAlsaFormat()
    card, dev = m.group(1), m.group(2)
    hw_params = Path(f"/proc/asound/card{card}/pcm{dev}p/sub0/hw_params")
    if not hw_params.exists():
        return ActiveAlsaFormat()
    try:
        content = hw_params.read_text(encoding="utf-8")
        if "closed" in content:
            return ActiveAlsaFormat()
        fmt = None
        rate = None
        channels = None
        for line in content.splitlines():
            if line.startswith("format:"):
                fmt = line.split(":", 1)[1].strip()
            elif line.startswith("rate:"):
                rate_str = line.split(":", 1)[1].strip().split()[0]
                rate = int(rate_str)
            elif line.startswith("channels:"):
                channels = int(line.split(":", 1)[1].strip())
        bit_depth = _format_to_bits(fmt)
        return ActiveAlsaFormat(
            format=fmt, rate=rate, channels=channels, bit_depth=bit_depth
        )
    except Exception:
        return ActiveAlsaFormat()


def _format_to_bits(fmt: Optional[str]) -> Optional[int]:
    if not fmt:
        return None
    for bits in (32, 24, 16):
        if str(bits) in fmt:
            return bits
    if "FLOAT_LE" in fmt:
        return 32
    if "DSD" in fmt:
        return 1
    return None


def get_device_capabilities(device_id: str) -> dict:
    """Inspect ALSA hardware capabilities (rates, formats, channels) for USB DAC / HDMI."""
    m = re.match(r"^(?:hw|plughw):(\d+),(\d+)$", device_id.strip())
    if not m:
        return {"device_id": device_id, "error": "Invalid ALSA device ID"}

    card, _dev = m.group(1), m.group(2)
    usb_stream = ""
    stream0 = Path(f"/proc/asound/card{card}/stream0")
    if stream0.exists():
        try:
            usb_stream = stream0.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    hw_dump = ""
    try:
        res = subprocess.run(
            ["aplay", "-D", device_id.strip(), "--dump-hw-params", "-d", "1", "/dev/zero"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        raw = (res.stderr or "") + "\n" + (res.stdout or "")
        lines = [
            ln for ln in raw.splitlines()
            if any(k in ln for k in ("ACCESS:", "FORMAT:", "SUBFORMAT:", "CHANNELS:", "RATE:", "PERIOD_"))
        ]
        hw_dump = "\n".join(lines).strip()
    except Exception as exc:
        hw_dump = f"Unable to probe ALSA hw_params: {exc}"

    return {
        "device_id": device_id,
        "hw_params": hw_dump or "No hardware parameters reported (device may be busy or unavailable).",
        "usb_stream": usb_stream,
    }
