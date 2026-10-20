"""Brand-neutral NVR client plumbing: HTTP session, event stream loop, reconnects."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from typing import Callable, Iterator, Protocol

import requests
import urllib3
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

from .config import NvrConfig
from .events import Event

log = logging.getLogger(__name__)


class NvrError(Exception):
    pass


class AuthError(NvrError):
    pass


class StreamIdle(NvrError):
    """No data (not even a heartbeat) arrived within the read timeout."""


class StreamParser(Protocol):
    def feed(self, chunk: bytes) -> Iterator[Event]: ...


class BaseClient:
    """Shared HTTP handling. Subclasses set ``stream_path`` and ``new_parser``."""

    stream_path: str = ""

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

    def new_parser(self) -> StreamParser:
        raise NotImplementedError

    def device_info(self) -> dict[str, str]:
        """Return at least deviceName / model / firmwareVersion where known."""
        raise NotImplementedError

    def list_channels(self) -> dict[int, str]:
        raise NotImplementedError

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

    def alert_stream(self, read_timeout: float = 90.0) -> Iterator[Event]:
        """Yield events from the NVR until the connection drops.

        The NVR sends heartbeats every few seconds, so a read timeout means the
        connection is dead and the caller should reconnect.
        """
        try:
            resp = self.session.get(
                self._url(self.stream_path),
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
            raise NvrError(f"Event stream returned HTTP {resp.status_code}")

        log.info("%s: connected, listening for events", self.cfg.name)
        self._active_response = resp
        parser = self.new_parser()
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
                for event in parser.feed(chunk):
                    yield replace(event, nvr=self.cfg.name)
        finally:
            self._active_response = None
            resp.close()

    def close(self) -> None:
        """Abort an in-progress alert stream (safe to call from another thread)."""
        resp = self._active_response
        if resp is not None:
            resp.close()
        self.session.close()


def make_client(cfg: NvrConfig, timeout: float = 10.0) -> BaseClient:
    """Return the client for the configured NVR brand."""
    # Imported here because both modules import this one.
    from .dahua import DahuaClient
    from .isapi import HikvisionClient

    cls = DahuaClient if cfg.brand.lower() == "dahua" else HikvisionClient
    return cls(cfg, timeout=timeout)


def listen_forever(
    client: BaseClient,
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
            log.info("%s: connecting to %s", client.cfg.name, client.cfg.base_url + client.stream_path)
            for event in client.alert_stream():
                on_event(event)
                if stop.is_set():
                    return
        except AuthError:
            raise
        except StreamIdle as exc:
            if stop.is_set():
                return
            log.info("%s: %s, reconnecting", client.cfg.name, exc)
            backoff = 1.0
            continue
        except NvrError as exc:
            if stop.is_set():
                return
            log.warning("%s: %s", client.cfg.name, exc)
        if time.monotonic() - connected_at > 60:
            backoff = 1.0
        log.info("%s: reconnecting in %.0fs", client.cfg.name, backoff)
        if stop.wait(backoff):
            return
        backoff = min(backoff * 2, max_backoff)


def listen_all(
    nvrs: list[NvrConfig],
    on_event: Callable[[Event], None],
    stop: threading.Event | None = None,
) -> None:
    """Listen to every NVR at once, one thread each, until stopped or Ctrl+C.

    ``on_event`` is called from the listener threads. An NVR that rejects the
    credentials is dropped (and logged) while the others keep running; if all
    of them are rejected, the AuthError is raised.
    """
    stop = stop or threading.Event()
    clients = [make_client(nvr) for nvr in nvrs]
    rejected: list[AuthError] = []

    def worker(client: BaseClient) -> None:
        try:
            listen_forever(client, on_event, stop)
        except AuthError as exc:
            if len(clients) > 1:
                exc = AuthError(f"{client.cfg.name}: {exc}")
                log.error("%s; not listening to this NVR", exc)
            rejected.append(exc)
        except Exception:
            log.exception("%s: listener crashed", client.cfg.name)

    threads = [
        threading.Thread(target=worker, args=(c,), name=f"nvr-{c.cfg.name}", daemon=True)
        for c in clients
    ]
    for t in threads:
        t.start()
    try:
        # A timed wait keeps the main thread responsive to Ctrl+C / SIGTERM.
        while not stop.is_set() and any(t.is_alive() for t in threads):
            stop.wait(0.5)
    finally:
        stop.set()
        for c in clients:
            c.close()
        for t in threads:
            t.join(5)
    if rejected and len(rejected) == len(clients):
        raise rejected[0]
