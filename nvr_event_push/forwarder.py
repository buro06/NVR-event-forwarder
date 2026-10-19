"""Match NVR events against rules and fire the configured HTTP GET requests."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable
from urllib.parse import quote

import requests

from .config import Config, Rule
from .events import Event

log = logging.getLogger(__name__)


def template_context(config: Config, rule: Rule, event: Event) -> dict[str, str]:
    return {
        "camera_name": config.camera_name(event.channel),
        "channel": "" if event.channel is None else str(event.channel),
        "event_type": event.event_type,
        "event_label": event.label,
        "target": event.target or "",
        "timestamp": event.timestamp,
        "nvr_host": config.nvr.host,
        "rule_name": rule.name,
    }


def build_url(template: str, context: dict[str, str]) -> str:
    # Values are percent-encoded so names like "Front Door" or ISO timestamps
    # are safe in both the path and the query string.
    return template.format_map({k: quote(v, safe="") for k, v in context.items()})


def rule_matches(rule: Rule, event: Event) -> bool:
    if not rule.enabled:
        return False
    if rule.channels and event.channel not in rule.channels:
        return False
    wanted = {e.lower() for e in rule.event_types}
    if event.event_type.lower() not in wanted:
        return False
    if rule.targets:
        # Cameras without AcuSense don't report a target. A rule that asks for
        # specific targets only fires when the camera confirms one of them.
        if event.target is None or event.target.lower() not in rule.targets:
            return False
    return True


class Forwarder:
    def __init__(
        self,
        config: Config,
        session: requests.Session | None = None,
        max_workers: int = 4,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.clock = clock
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="push")
        self._last_fired: dict[tuple[int, int | None], float] = {}
        self._lock = threading.Lock()

    def handle(self, event: Event) -> list[Future]:
        """Process one event. Returns futures for the requests that were sent."""
        if not event.active:
            return []
        futures = []
        for idx, rule in enumerate(self.config.rules):
            if not rule_matches(rule, event):
                continue
            key = (idx, event.channel)
            now = self.clock()
            with self._lock:
                last = self._last_fired.get(key)
                if last is not None and now - last < self.config.cooldown_for(rule):
                    log.debug("Rule '%s' on channel %s in cooldown", rule.name, event.channel)
                    continue
                self._last_fired[key] = now
            log.info(
                "%s on %s (ch %s%s) -> rule '%s'",
                event.label,
                self.config.camera_name(event.channel),
                event.channel,
                f", {event.target}" if event.target else "",
                rule.name,
            )
            futures.append(self.fire(rule, event))
        return futures

    def fire(self, rule: Rule, event: Event) -> Future:
        url = build_url(rule.url, template_context(self.config, rule, event))
        return self._pool.submit(self._send, rule, url)

    def _send(self, rule: Rule, url: str) -> requests.Response | None:
        start = time.monotonic()
        try:
            resp = self.session.get(url, timeout=self.config.defaults.timeout_seconds)
        except requests.RequestException as exc:
            log.error("Rule '%s': GET %s failed: %s", rule.name, url, exc)
            return None
        ms = (time.monotonic() - start) * 1000
        level = logging.INFO if resp.ok else logging.WARNING
        log.log(level, "Rule '%s': GET %s -> %s (%.0f ms)", rule.name, url, resp.status_code, ms)
        return resp

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait)
