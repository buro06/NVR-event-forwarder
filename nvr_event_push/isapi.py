"""Minimal Hikvision ISAPI client (also works with Annke and other rebrands)."""

from __future__ import annotations

import logging
import threading
import time
import xml.etree.ElementTree as ET
from typing import Callable, Iterator

import requests
import urllib3
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

from .config import NvrConfig
from .events import AlertStreamSplitter, Event

log = logging.getLogger(__name__)

ALERT_STREAM_PATH = "/ISAPI/Event/notification/alertStream"
DEVICE_INFO_PATH = "/ISAPI/System/deviceInfo"
CHANNEL_PATHS = (
    # NVR IP-camera channels
    ("/ISAPI/ContentMgmt/InputProxy/channels", "InputProxyChannel"),
    # Analog channels on DVRs, or a camera's own video input
    ("/ISAPI/System/Video/inputs/channels", "VideoInputChannel"),
)


class NvrError(Exception):
    pass


class AuthError(NvrError):
    pass


class StreamIdle(NvrError):
    """No data (not even a heartbeat) arrived within the read timeout."""


def _parse_xml(content: bytes) -> ET.Element:
    root = ET.fromstring(content)
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


class NvrClient:
    def __init__(self, cfg: NvrConfig, timeout: float = 10.0) -> None:
        self.cfg = cfg
        self.timeout = timeout
        self.session = requests.Session()
        auth_cls = HTTPBasicAuth if cfg.auth.lower() == "basic" else HTTPDigestAuth
        self.session.auth = auth_cls(cfg.username, cfg.effective_password)
        self.session.verify = cfg.verify_tls
        if cfg.https and not cfg.verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._active_response: requests.Response | None = None

    def _url(self, path: str) -> str:
        return self.cfg.base_url + path

    def _get(self, path: str, **kwargs) -> requests.Response:
        try:
            resp = self.session.get(self._url(path), timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise NvrError(f"Cannot reach NVR at {self.cfg.base_url}: {exc}") from exc
        if resp.status_code == 401:
            raise AuthError("NVR rejected the username/password (HTTP 401)")
        return resp

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

    def alert_stream(self, read_timeout: float = 90.0) -> Iterator[Event]:
        """Yield events from the NVR until the connection drops.

        The NVR sends heartbeats every few seconds, so a read timeout means the
        connection is dead and the caller should reconnect.
        """
        try:
            resp = self.session.get(
                self._url(ALERT_STREAM_PATH),
                stream=True,
                timeout=(self.timeout, read_timeout),
            )
        except requests.RequestException as exc:
            raise NvrError(f"Cannot reach NVR at {self.cfg.base_url}: {exc}") from exc
        if resp.status_code == 401:
            resp.close()
            raise AuthError("NVR rejected the username/password (HTTP 401)")
        if resp.status_code != 200:
            resp.close()
            raise NvrError(f"alertStream returned HTTP {resp.status_code}")

        log.info("Connected, listening for events")
        self._active_response = resp
        splitter = AlertStreamSplitter()
        try:
            while True:
                try:
                    # read1 returns as soon as *any* data arrives, so events
                    # are not delayed waiting for a full buffer.
                    chunk = resp.raw.read1(8192)
                except (TimeoutError, urllib3.exceptions.ReadTimeoutError) as exc:
                    raise StreamIdle(f"No data from NVR for {read_timeout:.0f}s") from exc
                except Exception as exc:  # urllib3 wraps socket errors variously
                    raise NvrError(f"Event stream interrupted: {exc}") from exc
                if not chunk:
                    raise NvrError("NVR closed the event stream")
                yield from splitter.feed(chunk)
        finally:
            self._active_response = None
            resp.close()

    def close(self) -> None:
        """Abort an in-progress alert stream (safe to call from another thread)."""
        resp = self._active_response
        if resp is not None:
            resp.close()
        self.session.close()


def listen_forever(
    client: NvrClient,
    on_event: Callable[[Event], None],
    stop: threading.Event | None = None,
    max_backoff: float = 60.0,
) -> None:
    """Consume the alert stream, reconnecting with exponential backoff.

    Auth errors are not retried, since retrying could lock the NVR account.
    """
    stop = stop or threading.Event()
    backoff = 1.0
    while not stop.is_set():
        connected_at = time.monotonic()
        try:
            log.info("Connecting to %s", client.cfg.base_url + ALERT_STREAM_PATH)
            for event in client.alert_stream():
                on_event(event)
                if stop.is_set():
                    return
        except AuthError:
            raise
        except StreamIdle as exc:
            if stop.is_set():
                return
            log.info("%s, reconnecting", exc)
            backoff = 1.0
            continue
        except NvrError as exc:
            if stop.is_set():
                return
            log.warning("%s", exc)
        if time.monotonic() - connected_at > 60:
            backoff = 1.0
        log.info("Reconnecting in %.0fs", backoff)
        if stop.wait(backoff):
            return
        backoff = min(backoff * 2, max_backoff)
