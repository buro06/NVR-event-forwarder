"""Match NVR events against rules and fire the configured HTTP GET requests."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote

import requests

from .config import Config, Rule
from .events import Event

log = logging.getLogger(__name__)

# Seconds before the first retry; doubles on each further attempt.
RETRY_BACKOFF = 1.0


# Cooldowns are tracked per rule index, NVR name and channel.
CooldownKey = tuple[int, str, int | None]


def template_context(config: Config, rule: Rule, event: Event) -> dict[str, str]:
    nvr = config.get_nvr(event.nvr)
    return {
        "camera_name": config.camera_name(event.nvr, event.channel),
        "channel": "" if event.channel is None else str(event.channel),
        "event_type": event.event_type,
        "event_label": event.label,
        "target": event.target or "",
        "timestamp": event.timestamp,
        "nvr_name": nvr.name if nvr is not None else event.nvr,
        "nvr_host": nvr.host if nvr is not None else "",
        "rule_name": rule.name,
    }


def build_url(template: str, context: dict[str, str]) -> str:
    # Values are percent-encoded so names like "Front Door" or ISO timestamps
    # are safe in both the path and the query string.
    return template.format_map({k: quote(v, safe="") for k, v in context.items()})


def rule_matches(rule: Rule, event: Event) -> bool:
    if not rule.enabled:
        return False
    if rule.nvr and event.nvr != rule.nvr:
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


def _daemon_timer(delay: float, fn: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    return timer


@dataclass
class _PendingOff:
    timer: Any
    rule: Rule
    url: str
    # Cooldown entries to clear when the off request goes out, so the next
    # event turns the light back on instead of being swallowed by the cooldown.
    cooldown_keys: set[CooldownKey] = field(default_factory=set)


class Forwarder:
    def __init__(
        self,
        config: Config,
        session: requests.Session | None = None,
        max_workers: int = 4,
        clock: Callable[[], float] = time.monotonic,
        timer_factory: Callable[[float, Callable[[], None]], Any] = _daemon_timer,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.clock = clock
        self.timer_factory = timer_factory
        self.sleep = sleep
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="push")
        self._last_fired: dict[CooldownKey, float] = {}
        # Events swallowed by the cooldown since the last request, so the
        # once-a-second repeats the NVR sends during motion are logged once.
        self._suppressed: dict[CooldownKey, int] = {}
        # Keyed by rule index + off URL rendered without the timestamp: cameras
        # whose off URL comes out the same (one light) share a timer, while a
        # URL containing {camera_name} gets a timer per camera.
        self._pending_off: dict[tuple[int, str], _PendingOff] = {}
        self._lock = threading.Lock()

    def handle(self, event: Event) -> list[Future]:
        """Process one event. Returns futures for the requests that were sent."""
        if not event.active:
            return []
        futures = []
        for idx, rule in enumerate(self.config.rules):
            if not rule_matches(rule, event):
                continue
            key = (idx, event.nvr, event.channel)
            if rule.off_url:
                # Every matching event, even one in cooldown, pushes the off back.
                self._schedule_off(idx, rule, event, key)
            now = self.clock()
            with self._lock:
                last = self._last_fired.get(key)
                cooldown = self.config.cooldown_for(rule)
                if last is not None and now - last < cooldown:
                    self._suppressed[key] = self._suppressed.get(key, 0) + 1
                    if self._suppressed[key] == 1:
                        log.debug(
                            "Rule '%s' on channel %s in cooldown for %.0fs more; ignoring repeats",
                            rule.name, event.channel, cooldown - (now - last),
                        )
                    continue
                self._last_fired[key] = now
                repeats = self._suppressed.pop(key, 0)
            if repeats:
                log.debug("Rule '%s' on channel %s: %d repeat(s) ignored during cooldown",
                          rule.name, event.channel, repeats)
            log.info(
                "%s on %s (%sch %s%s) -> rule '%s'",
                event.label,
                self.config.camera_name(event.nvr, event.channel),
                f"{event.nvr} " if len(self.config.nvrs) > 1 else "",
                event.channel,
                f", {event.target}" if event.target else "",
                rule.name,
            )
            futures.append(self.fire(rule, event))
        return futures

    def fire(self, rule: Rule, event: Event, off: bool = False) -> Future:
        template = rule.off_url if off else rule.url
        url = build_url(template, template_context(self.config, rule, event))
        return self._pool.submit(self._send, rule, url)

    def _schedule_off(self, idx: int, rule: Rule, event: Event, key: CooldownKey) -> None:
        context = template_context(self.config, rule, event)
        timer_key = (idx, build_url(rule.off_url, {**context, "timestamp": ""}))
        with self._lock:
            pending = self._pending_off.get(timer_key)
            keys = {key}
            if pending is not None:
                pending.timer.cancel()
                keys |= pending.cooldown_keys
            new = _PendingOff(None, rule, build_url(rule.off_url, context), keys)
            new.timer = self.timer_factory(
                rule.off_delay_seconds, lambda: self._off_due(timer_key, new)
            )
            self._pending_off[timer_key] = new
            new.timer.start()

    def _off_due(self, timer_key: tuple[int, str], pending: _PendingOff) -> None:
        with self._lock:
            # A newer event may have replaced this timer just as it fired.
            if self._pending_off.get(timer_key) is not pending:
                return
            del self._pending_off[timer_key]
            for key in pending.cooldown_keys:
                self._last_fired.pop(key, None)
        log.info(
            "No events for %gs -> rule '%s' off", pending.rule.off_delay_seconds, pending.rule.name
        )
        self._pool.submit(self._send, pending.rule, pending.url)

    def _send(self, rule: Rule, url: str) -> requests.Response | None:
        """GET the URL, retrying transient failures. Returns the last response, if any."""
        attempts = self.config.defaults.retries + 1
        resp = None
        for attempt in range(1, attempts + 1):
            start = time.monotonic()
            try:
                resp = self.session.get(url, timeout=self.config.defaults.timeout_seconds)
            except requests.RequestException as exc:
                resp, problem = None, str(exc)
            else:
                ms = (time.monotonic() - start) * 1000
                # 4xx (bad URL, auth) would fail the same way again, so only
                # server errors and rate limiting are worth another try.
                if resp.status_code < 500 and resp.status_code != 429:
                    level = logging.INFO if resp.ok else logging.WARNING
                    log.log(
                        level, "Rule '%s': GET %s -> %s (%.0f ms)", rule.name, url, resp.status_code, ms
                    )
                    return resp
                problem = f"HTTP {resp.status_code}"
            if attempt < attempts:
                delay = RETRY_BACKOFF * 2 ** (attempt - 1)
                log.warning(
                    "Rule '%s': GET %s failed (%s), retrying in %gs (%d/%d)",
                    rule.name, url, problem, delay, attempt, attempts - 1,
                )
                self.sleep(delay)
        log.error(
            "Rule '%s': GET %s failed after %d attempt%s: %s",
            rule.name, url, attempts, "" if attempts == 1 else "s", problem,
        )
        return resp

    def shutdown(self, wait: bool = True) -> None:
        # Send outstanding off requests now rather than leaving lights on.
        with self._lock:
            pending = list(self._pending_off.values())
            self._pending_off.clear()
        for p in pending:
            p.timer.cancel()
            log.info("Shutting down -> rule '%s' off", p.rule.name)
            self._pool.submit(self._send, p.rule, p.url)
        self._pool.shutdown(wait=wait)
