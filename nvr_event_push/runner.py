"""Long-running actions shared by the headless CLI and the interactive menu."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Callable

import requests

from .client import NvrError, listen_all, make_client
from .config import Config, NvrConfig, Rule
from .events import EVENT_TYPES, Event
from .forwarder import Forwarder

log = logging.getLogger(__name__)


def refresh_cameras(config: Config, nvrs: list[NvrConfig] | None = None) -> bool:
    """Update cached channel names from the NVRs. Returns True if all succeeded."""
    all_ok = True
    for nvr in config.nvrs if nvrs is None else nvrs:
        try:
            cameras = make_client(nvr).list_channels()
        except NvrError as exc:
            log.warning("%s: could not fetch camera names (%s); using cached names", nvr.name, exc)
            all_ok = False
            continue
        if cameras:
            nvr.cameras = cameras
        all_ok = all_ok and bool(cameras)
    return all_ok


def describe_cameras(config: Config, rule: Rule) -> str:
    """Human-readable camera scope of a rule, e.g. "Driveway, Backyard (Garage)"."""
    if rule.channels:
        cams = ", ".join(config.camera_name(rule.nvr, c) for c in rule.channels)
    else:
        cams = "all cameras"
    if len(config.nvrs) > 1:
        cams += f" ({rule.nvr})" if rule.nvr else " (all NVRs)"
    return cams


def run_forwarder(config: Config) -> None:
    """Listen and forward until interrupted (KeyboardInterrupt) or an auth error."""
    refresh_cameras(config)
    enabled = [r for r in config.rules if r.enabled]
    if not enabled:
        log.warning("No enabled rules: events will be received but nothing is forwarded")
    for rule in enabled:
        log.info(
            "Rule '%s': %s on %s", rule.name, "/".join(rule.event_types), describe_cameras(config, rule)
        )

    forwarder = Forwarder(config)
    try:
        listen_all(config.nvrs, forwarder.handle)
    finally:
        forwarder.shutdown(wait=True)


def format_event(config: Config, event: Event) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ch = "-" if event.channel is None else str(event.channel)
    name = config.camera_name(event.nvr, event.channel)
    parts = [now]
    if len(config.nvrs) > 1:
        parts.append(f"{event.nvr:<12.12}")
    parts += [f"ch {ch:>2}", f"{name:<18.18}", f"{event.label:<18}", f"{event.state:<8}"]
    if event.target:
        parts.append(event.target)
    if event.description and event.description.lower() not in event.label.lower():
        parts.append(f"({event.description})")
    return "  ".join(parts)


def describe_watch(
    config: Config, nvrs: list[str] | None = None, event_types: list[str] | None = None
) -> str:
    """What a watch with these filters shows, e.g. "Motion detection events from Garage"."""
    if event_types:
        text = ", ".join(EVENT_TYPES.get(e, e) for e in event_types) + " events"
    else:
        text = "all events"
    if nvrs:
        text += " from " + ", ".join(nvrs)
    elif len(config.nvrs) > 1:
        text += " from all NVRs"
    return text


def watch(
    config: Config,
    show_all: bool = False,
    raw: bool = False,
    out: Callable[[str], None] | None = None,
    nvrs: list[str] | None = None,
    event_types: list[str] | None = None,
) -> None:
    """Print events as they arrive, without forwarding. Runs until interrupted.

    ``nvrs`` limits which NVRs are watched (by name) and ``event_types`` which
    event types are printed; None or empty means no filter.
    """
    # Flush every line so output piped to a file or `tee` appears immediately.
    out = out or (lambda line: print(line, flush=True))
    watched = [n for n in config.nvrs if not nvrs or n.name in nvrs]
    wanted = {e.lower() for e in event_types or ()}
    refresh_cameras(config, watched)
    # Events arrive on one thread per NVR; keep each event's lines together.
    lock = threading.Lock()

    def on_event(event: Event) -> None:
        if event.is_heartbeat:
            if not show_all:
                return
        elif wanted and event.event_type.lower() not in wanted:
            return
        with lock:
            out(format_event(config, event))
            # Dahua's heartbeat repeats of ongoing events carry no raw text.
            if raw and event.raw:
                out(event.raw.strip() + "\n")

    listen_all(watched, on_event)


def sample_event(config: Config, rule: Rule) -> Event:
    nvr = config.get_nvr(rule.nvr) or (config.nvrs[0] if config.nvrs else None)
    cameras = nvr.cameras if nvr is not None else {}
    channel = rule.channels[0] if rule.channels else next(iter(cameras), 1)
    return Event(
        channel=channel,
        event_type=rule.event_types[0] if rule.event_types else "VMD",
        state="active",
        timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
        target=rule.targets[0] if rule.targets else None,
        description="Test event from nvr-event-push",
        nvr=nvr.name if nvr is not None else "",
    )


def send_test(config: Config, rule: Rule, off: bool = False) -> requests.Response | None:
    """Fire the rule's request (or its off request) once with a synthetic event."""
    event = sample_event(config, rule)
    nvr = config.get_nvr(event.nvr)
    if nvr is not None and not nvr.cameras:
        refresh_cameras(config, [nvr])
        event = sample_event(config, rule)
    forwarder = Forwarder(config, max_workers=1)
    try:
        return forwarder.fire(rule, event, off=off).result()
    finally:
        forwarder.shutdown()
