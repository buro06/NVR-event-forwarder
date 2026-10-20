"""Minimal Hikvision ISAPI client (also works with Annke and other rebrands)."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from .client import BaseClient, NvrError
from .events import AlertStreamSplitter

log = logging.getLogger(__name__)

ALERT_STREAM_PATH = "/ISAPI/Event/notification/alertStream"
DEVICE_INFO_PATH = "/ISAPI/System/deviceInfo"
CHANNEL_PATHS = (
    # NVR IP-camera channels
    ("/ISAPI/ContentMgmt/InputProxy/channels", "InputProxyChannel"),
    # Analog channels on DVRs, or a camera's own video input
    ("/ISAPI/System/Video/inputs/channels", "VideoInputChannel"),
)


def _parse_xml(content: bytes) -> ET.Element:
    root = ET.fromstring(content)
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


class HikvisionClient(BaseClient):
    stream_path = ALERT_STREAM_PATH

    def new_parser(self) -> AlertStreamSplitter:
        return AlertStreamSplitter()

    def device_info(self) -> dict[str, str]:
        resp = self._get(DEVICE_INFO_PATH)
        if resp.status_code != 200:
            raise NvrError(f"deviceInfo returned HTTP {resp.status_code}")
        try:
            root = _parse_xml(resp.content)
        except ET.ParseError as exc:
            raise NvrError(f"Unexpected deviceInfo response: {exc}") from exc
        return {child.tag: (child.text or "").strip() for child in root}

    def list_channels(self) -> dict[int, str]:
        for path, tag in CHANNEL_PATHS:
            resp = self._get(path)
            if resp.status_code != 200:
                log.debug("%s returned HTTP %s", path, resp.status_code)
                continue
            try:
                root = _parse_xml(resp.content)
            except ET.ParseError:
                continue
            channels: dict[int, str] = {}
            for ch in root.iter(tag):
                id_el, name_el = ch.find("id"), ch.find("name")
                if id_el is None or not (id_el.text or "").strip().isdigit():
                    continue
                cid = int(id_el.text.strip())
                name = (name_el.text or "").strip() if name_el is not None else ""
                channels[cid] = name or f"Camera {cid}"
            if channels:
                return dict(sorted(channels.items()))
        return {}
