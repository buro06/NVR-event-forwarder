"""Long-running actions shared by the headless CLI and the interactive menu."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

import requests

from .config import Config, Rule
from .events import Event
from .forwarder import Forwarder
from .isapi import NvrClient, NvrError, listen_forever

log = logging.getLogger(__name__)


def refresh_cameras(config: Config) -> bool:
    """Update the cached channel names from the NVR. Returns True on success."""
    try:
        cameras = NvrClient(config.nvr).list_channels()
    except NvrError as exc:
        log.warning("Could not fetch camera names (%s); using cached names", exc)
        return False
    if cameras:
        config.cameras = cameras
    return bool(cameras)


def run_forwarder(config: Config) -> None:
    """Listen and forward until interrupted (KeyboardInterrupt) or an auth error."""
    refresh_cameras(config)
    enabled = [r for r in config.rules if r.enabled]
    if not enabled:
        log.warning("No enabled rules: events will be received but nothing is forwarded")
    for rule in enabled:
        cams = ", ".join(config.camera_name(c) for c in rule.channels) or "all cameras"
        log.info("Rule '%s': %s on %s", rule.name, "/".join(rule.event_types), cams)

    client = NvrClient(config.nvr)
    forwarder = Forwarder(config)
    try:
        listen_forever(client, forwarder.handle)
    finally:
        client.close()
        forwarder.shutdown(wait=True)


def format_event(config: Config, event: Event) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ch = "-" if event.channel is None else str(event.channel)
    name = config.camera_name(event.channel) if event.channel is not None else ""
    parts = [now, f"ch {ch:>2}", f"{name:<18.18}", f"{event.label:<18}", f"{event.state:<8}"]
    if event.target:
        parts.append(event.target)
    if event.description and event.description.lower() not in event.label.lower():
        parts.append(f"({event.description})")
    return "  ".join(parts)


def watch(
    config: Config,
    show_all: bool = False,
    raw: bool = False,
    out: Callable[[str], None] | None = None,
) -> None:
    """Print events as they arrive, without forwarding. Runs until interrupted."""
    # Flush every line so output piped to a file or `tee` appears immediately.
    out = out or (lambda line: print(line, flush=True))
    refresh_cameras(config)
    client = NvrClient(config.nvr)

    def on_event(event: Event) -> None:
        if event.is_heartbeat and not show_all:
            return
        out(format_event(config, event))
        if raw:
            out(event.raw.strip() + "\n")

    try:
        listen_forever(client, on_event)
    finally:
        client.close()


def sample_event(config: Config, rule: Rule) -> Event:
    channel = rule.channels[0] if rule.channels else next(iter(config.cameras), 1)
    return Event(
        channel=channel,
        event_type=rule.event_types[0] if rule.event_types else "VMD",
        state="active",
        timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
        target=rule.targets[0] if rule.targets else None,
        description="Test event from nvr-event-push",
    )


def send_test(config: Config, rule: Rule) -> requests.Response | None:
    """Fire the rule's request once with a synthetic event (no cooldown)."""
    if not config.cameras and config.nvr.host:
        refresh_cameras(config)
    forwarder = Forwarder(config, max_workers=1)
    try:
        return forwarder.fire(rule, sample_event(config, rule)).result()
    finally:
        forwarder.shutdown()
