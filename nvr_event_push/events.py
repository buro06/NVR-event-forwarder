"""Parsing of the Hikvision ISAPI alertStream.

The NVR sends a never-ending multipart/mixed response. Rather than relying on
the multipart boundary (which varies between firmwares and OEM rebrands), the
splitter scans the raw bytes for complete ``<EventNotificationAlert>`` XML
documents, plus JSON alert objects sent by some newer firmwares.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Iterator

# Event types offered in the menus. Anything else the NVR sends can still be
# matched by typing its name into the config file.
EVENT_TYPES: dict[str, str] = {
    "VMD": "Motion detection",
    "linedetection": "Line crossing",
    "fielddetection": "Intrusion",
    "regionEntrance": "Region entrance",
    "regionExiting": "Region exiting",
}

TARGET_TYPES: dict[str, str] = {
    "human": "Human",
    "vehicle": "Vehicle",
}

_XML_START = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?EventNotificationAlert[\s>/]")
_XML_END = re.compile(rb"</(?:[A-Za-z_][\w.-]*:)?EventNotificationAlert\s*>")
_JSON_START = re.compile(rb'\{\s*"')
_BOUNDARY = b"\r\n--"
# Longest prefix of a start marker that may be cut off at the end of a chunk.
_TAIL_KEEP = 64
# Give up on a part that never terminates (e.g. garbage) after this many bytes.
_MAX_PART = 256 * 1024

_TARGET_TAGS = ("targetType", "detectionTarget", "targetTypeName")


@dataclass(frozen=True)
class Event:
    channel: int | None
    event_type: str
    state: str
    timestamp: str = ""
    target: str | None = None
    description: str = ""
    nvr: str = ""  # name of the NVR it came from, set by the client
    raw: str = field(default="", repr=False, compare=False)

    @property
    def active(self) -> bool:
        return self.state.lower() == "active"

    @property
    def is_heartbeat(self) -> bool:
        # Idle Hikvision NVRs emit "videoloss / inactive" every few seconds as a
        # keep-alive; the Dahua parser reports its heartbeats as "heartbeat".
        return self.event_type.lower() in ("videoloss", "heartbeat") and not self.active

    @property
    def label(self) -> str:
        return EVENT_TYPES.get(self.event_type, self.event_type)


def _strip_namespaces(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def _to_int(value: Any) -> int | None:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _resolve_channel(dyn_channel: Any, channel: Any) -> int | None:
    # On NVRs dynChannelID matches the IP channel number (D1, D2, ...) used by
    # /ISAPI/ContentMgmt/InputProxy/channels; standalone cameras and DVRs only
    # send channelID.
    return _to_int(dyn_channel) or _to_int(channel)


def parse_alert(data: bytes | str) -> Event | None:
    """Parse one XML ``EventNotificationAlert`` document. Returns None if invalid."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    _strip_namespaces(root)
    if root.tag != "EventNotificationAlert":
        return None

    def text(tag: str) -> str:
        el = root.find(tag)
        return (el.text or "").strip() if el is not None else ""

    target = None
    for tag in _TARGET_TAGS:
        el = next(root.iter(tag), None)
        if el is not None and (el.text or "").strip():
            target = el.text.strip().lower()
            break

    event_type = text("eventType")
    if not event_type:
        return None
    raw = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    return Event(
        channel=_resolve_channel(text("dynChannelID"), text("channelID")),
        event_type=event_type,
        state=text("eventState") or "active",
        timestamp=text("dateTime"),
        target=target,
        description=text("eventDescription"),
        raw=raw,
    )


def _find_key(obj: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] not in (None, ""):
                return obj[k]
        for v in obj.values():
            found = _find_key(v, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_key(v, keys)
            if found is not None:
                return found
    return None


def parse_json_alert(obj: Any, raw: str = "") -> Event | None:
    """Best-effort parse of a JSON alert as sent by some newer firmwares."""
    if not isinstance(obj, dict):
        return None
    event_type = obj.get("eventType")
    if not event_type:
        return None
    target = _find_key(obj, _TARGET_TAGS)
    return Event(
        channel=_resolve_channel(obj.get("dynChannelID"), obj.get("channelID")),
        event_type=str(event_type),
        state=str(obj.get("eventState") or "active"),
        timestamp=str(obj.get("dateTime") or ""),
        target=str(target).lower() if target else None,
        description=str(obj.get("eventDescription") or ""),
        raw=raw,
    )


class AlertStreamSplitter:
    """Incrementally extracts events from arbitrary chunks of alertStream bytes."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._decoder = json.JSONDecoder()

    def feed(self, chunk: bytes) -> Iterator[Event]:
        self._buf.extend(chunk)
        while True:
            xml_m = _XML_START.search(self._buf)
            json_m = _JSON_START.search(self._buf)
            starts = [m for m in (xml_m, json_m) if m is not None]
            if not starts:
                # Nothing that looks like an alert: keep only a small tail in
                # case a start marker was split across chunks.
                del self._buf[:-_TAIL_KEEP]
                return
            m = min(starts, key=lambda m: m.start())
            del self._buf[: m.start()]

            if m is xml_m:
                end = _XML_END.search(self._buf)
                if end is None:
                    if len(self._buf) > _MAX_PART:
                        del self._buf[:1]
                        continue
                    return
                part = bytes(self._buf[: end.end()])
                del self._buf[: end.end()]
                event = parse_alert(part)
            else:
                text = self._buf.decode("utf-8", "replace")
                try:
                    obj, length = self._decoder.raw_decode(text)
                except json.JSONDecodeError:
                    # Either incomplete, or not JSON at all (e.g. bytes inside
                    # an attached JPEG). If the multipart part already ended,
                    # it was never valid, so skip it.
                    if _BOUNDARY in self._buf or len(self._buf) > _MAX_PART:
                        del self._buf[:1]
                        continue
                    return
                consumed = len(text[:length].encode("utf-8", "replace"))
                del self._buf[:consumed]
                event = parse_json_alert(obj, text[:length])

            if event is not None:
                yield event
