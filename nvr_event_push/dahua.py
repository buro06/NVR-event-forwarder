"""Dahua HTTP API client and event stream parser (also Amcrest and other rebrands).

Events come from ``eventManager.cgi?action=attach``: a never-ending multipart
response whose parts carry plain-text records such as::

    Code=VideoMotion;action=Start;index=0
    Code=CrossLineDetection;action=Pulse;index=1;data={ ...JSON... }

Dahua codes are mapped to the same event names used for Hikvision (VMD,
linedetection, ...) so rules work for either brand. Channels are reported
0-based and converted to the 1-based numbers shown in the NVR's UI.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from datetime import datetime
from typing import Any, Iterator

from .client import BaseClient, NvrError
from .events import Event, _find_key

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 5
EVENT_STREAM_PATH = (
    f"/cgi-bin/eventManager.cgi?action=attach&codes=[All]&heartbeat={HEARTBEAT_SECONDS}"
)
SYSTEM_INFO_PATH = "/cgi-bin/magicBox.cgi?action=getSystemInfo"
SOFTWARE_VERSION_PATH = "/cgi-bin/magicBox.cgi?action=getSoftwareVersion"
MACHINE_NAME_PATH = "/cgi-bin/magicBox.cgi?action=getMachineName"
CHANNEL_TITLE_PATH = "/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle"

# Dahua code -> event type used in rules. Codes not listed pass through as-is.
CODE_MAP: dict[str, str] = {
    "VideoMotion": "VMD",
    "SmartMotionHuman": "VMD",
    "SmartMotionVehicle": "VMD",
    "CrossLineDetection": "linedetection",
    "CrossRegionDetection": "fielddetection",
}
_CODE_TARGETS = {"SmartMotionHuman": "human", "SmartMotionVehicle": "vehicle"}
_REGION_DIRECTIONS = {"enter": "regionEntrance", "leave": "regionExiting"}
_TARGET_ALIASES = {
    "human": "human",
    "person": "human",
    "vehicle": "vehicle",
    "motorvehicle": "vehicle",
    "car": "vehicle",
}

_PART_HEAD = re.compile(rb"(?m)^--[^\r\n]*\r?\n((?:[^\r\n]+\r?\n)*?)\r?\n")
_CONTENT_LENGTH = re.compile(rb"(?im)^content-length:\s*(\d+)")
_NEXT_BOUNDARY = re.compile(rb"\r?\n--")
_RECORD_START = re.compile(r"(?m)^(?=Code=)")
_CHANNEL_TITLE = re.compile(r"(?m)^table\.ChannelTitle\[(\d+)\]\.Name=(.*?)\r?$")
# Longest boundary line that may be cut off at the end of a chunk.
_TAIL_KEEP = 256
# Give up on a part that never terminates (e.g. garbage) after this many bytes.
_MAX_PART = 256 * 1024


def _key_values(text: str) -> dict[str, str]:
    """Parse the ``key=value`` lines returned by magicBox.cgi."""
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _target(code: str, data: Any) -> str | None:
    if code in _CODE_TARGETS:
        return _CODE_TARGETS[code]
    found = _find_key(data, ("ObjectType",))
    if not found:
        return None
    name = str(found).strip().lower()
    return _TARGET_ALIASES.get(name, name)


def parse_record(record: str) -> tuple[str, Event] | None:
    """Parse one ``Code=...`` record into (action, event). None if invalid.

    The action is lowercased: start, stop or pulse.
    """
    head, sep, data_text = record.partition(";data=")
    fields = {}
    for item in head.strip().split(";"):
        key, _, value = item.partition("=")
        fields[key.strip().lower()] = value.strip()
    code = fields.get("code")
    if not code:
        return None
    data: Any = None
    if sep:
        try:
            data = json.loads(data_text)
        except json.JSONDecodeError:
            log.debug("Ignoring unparseable data for %s: %r", code, data_text[:200])

    index = fields.get("index")
    channel = int(index) + 1 if index is not None and index.isdigit() else None
    event_type = CODE_MAP.get(code, code)
    if code == "CrossRegionDetection" and isinstance(data, dict):
        direction = str(data.get("Direction") or "").lower()
        event_type = _REGION_DIRECTIONS.get(direction, event_type)
    timestamp = ""
    if isinstance(data, dict):
        timestamp = str(data.get("LocaleTime") or data.get("UTC") or "")
    if not timestamp:
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    action = fields.get("action", "").lower()
    return action, Event(
        channel=channel,
        event_type=event_type,
        state="inactive" if action == "stop" else "active",
        timestamp=timestamp,
        target=_target(code, data),
        description=code,
        raw=record.strip(),
    )


class DahuaStreamParser:
    """Incrementally extracts events from arbitrary chunks of eventManager bytes.

    Hikvision repeats "active" about once a second while motion continues, and
    the forwarder's cooldown and off timer rely on that. Dahua only sends Start
    and Stop, so events that started and haven't stopped are re-emitted on
    every heartbeat to give the same behaviour.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        # (Dahua code, channel) -> event to repeat until its Stop arrives.
        self._open: dict[tuple[str, int | None], Event] = {}

    def feed(self, chunk: bytes) -> Iterator[Event]:
        self._buf.extend(chunk)
        while True:
            head = _PART_HEAD.search(self._buf)
            if head is None:
                if len(self._buf) > _MAX_PART:
                    del self._buf[:-_TAIL_KEEP]
                return
            body_start = head.end()
            length = _CONTENT_LENGTH.search(head.group(1))
            if length is not None:
                body_end = body_start + int(length.group(1))
                if len(self._buf) < body_end:
                    if body_end - head.start() > _MAX_PART:
                        del self._buf[: head.end()]
                        continue
                    return
                next_start = body_end
            else:
                nxt = _NEXT_BOUNDARY.search(self._buf, body_start)
                if nxt is None:
                    if len(self._buf) - head.start() > _MAX_PART:
                        del self._buf[: head.end()]
                        continue
                    return
                body_end = next_start = nxt.start()
            body = bytes(self._buf[body_start:body_end])
            del self._buf[:next_start]
            yield from self._parse_body(body.decode("utf-8", "replace"))

    def _parse_body(self, text: str) -> Iterator[Event]:
        text = text.strip()
        if text.lower() == "heartbeat":
            yield Event(channel=None, event_type="heartbeat", state="inactive", raw=text)
            yield from self._open.values()
            return
        for record in _RECORD_START.split(text):
            if not record.strip():
                continue
            parsed = parse_record(record)
            if parsed is None:
                continue
            action, event = parsed
            key = (event.description, event.channel)
            if action == "start":
                self._open[key] = replace(event, raw="")
            elif action == "stop":
                self._open.pop(key, None)
            yield event


class DahuaClient(BaseClient):
    stream_path = EVENT_STREAM_PATH

    def new_parser(self) -> DahuaStreamParser:
        return DahuaStreamParser()

    def device_info(self) -> dict[str, str]:
        resp = self._get(SYSTEM_INFO_PATH)
        if resp.status_code != 200:
            raise NvrError(f"getSystemInfo returned HTTP {resp.status_code}")
        info = _key_values(resp.text)
        info["model"] = info.get("deviceType", "")
        for path, key, out in (
            (SOFTWARE_VERSION_PATH, "version", "firmwareVersion"),
            (MACHINE_NAME_PATH, "name", "deviceName"),
        ):
            resp = self._get(path)
            if resp.status_code == 200:
                info[out] = _key_values(resp.text).get(key, "")
        return info

    def list_channels(self) -> dict[int, str]:
        resp = self._get(CHANNEL_TITLE_PATH)
        if resp.status_code != 200:
            log.debug("ChannelTitle returned HTTP %s", resp.status_code)
            return {}
        channels = {}
        for m in _CHANNEL_TITLE.finditer(resp.text):
            cid = int(m.group(1)) + 1
            channels[cid] = m.group(2).strip() or f"Camera {cid}"
        return dict(sorted(channels.items()))
