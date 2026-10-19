"""Configuration model, YAML persistence and validation."""

from __future__ import annotations

import os
import string
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

DEFAULT_CONFIG_PATH = "config.yaml"
CONFIG_ENV = "NVR_EVENT_PUSH_CONFIG"
PASSWORD_ENV = "NVR_PASSWORD"

# Placeholders available in rule URL templates.
PLACEHOLDERS: dict[str, str] = {
    "camera_name": "Camera name as configured on the NVR",
    "channel": "NVR channel number",
    "event_type": "Raw event type, e.g. VMD, linedetection",
    "event_label": "Readable event type, e.g. Motion detection",
    "target": "human / vehicle (empty if the camera does not report it)",
    "timestamp": "Event time reported by the NVR",
    "nvr_host": "NVR host / IP",
    "rule_name": "Name of the rule that matched",
}


class ConfigError(ValueError):
    pass


@dataclass
class NvrConfig:
    host: str = ""
    port: int = 80
    https: bool = False
    verify_tls: bool = False
    username: str = "admin"
    password: str = ""
    auth: str = "digest"  # "digest" (standard) or "basic"

    @property
    def base_url(self) -> str:
        scheme = "https" if self.https else "http"
        default_port = 443 if self.https else 80
        port = "" if self.port == default_port else f":{self.port}"
        return f"{scheme}://{self.host}{port}"

    @property
    def effective_password(self) -> str:
        return os.environ.get(PASSWORD_ENV) or self.password


@dataclass
class Defaults:
    cooldown_seconds: float = 30.0
    timeout_seconds: float = 5.0
    retries: int = 2  # extra attempts after a connection error, timeout or HTTP 5xx/429


@dataclass
class Rule:
    name: str
    url: str
    channels: list[int] = field(default_factory=list)  # empty = all cameras
    event_types: list[str] = field(default_factory=lambda: ["VMD"])
    targets: list[str] = field(default_factory=list)  # empty = any target
    cooldown_seconds: float | None = None
    enabled: bool = True
    # Optional second GET, sent once no matching event has arrived for
    # off_delay_seconds (e.g. to turn the light back off).
    off_url: str = ""
    off_delay_seconds: float = 120.0


@dataclass
class Config:
    nvr: NvrConfig = field(default_factory=NvrConfig)
    defaults: Defaults = field(default_factory=Defaults)
    rules: list[Rule] = field(default_factory=list)
    # Last known channel -> name mapping, refreshed from the NVR when possible.
    cameras: dict[int, str] = field(default_factory=dict)

    def camera_name(self, channel: int | None) -> str:
        if channel is None:
            return ""
        return self.cameras.get(channel, f"Camera {channel}")

    def cooldown_for(self, rule: Rule) -> float:
        if rule.cooldown_seconds is not None:
            return rule.cooldown_seconds
        return self.defaults.cooldown_seconds


def resolve_path(path: str | os.PathLike | None) -> Path:
    return Path(path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG_PATH)


def validate_url_template(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigError("URL must start with http:// or https:// and include a host")
    try:
        fields = [f for _, f, _, _ in string.Formatter().parse(url) if f is not None]
    except ValueError as exc:
        raise ConfigError(f"Malformed placeholder in URL: {exc}") from None
    for name in fields:
        if name not in PLACEHOLDERS:
            raise ConfigError(
                f"Unknown placeholder {{{name}}}. Available: "
                + ", ".join(f"{{{p}}}" for p in PLACEHOLDERS)
            )


def validate(config: Config) -> None:
    if config.defaults.retries < 0:
        raise ConfigError("defaults.retries must be 0 or more")
    names = set()
    for rule in config.rules:
        if not rule.name:
            raise ConfigError("Every rule needs a name")
        if rule.name in names:
            raise ConfigError(f"Duplicate rule name: {rule.name}")
        names.add(rule.name)
        if not rule.event_types:
            raise ConfigError(f"Rule '{rule.name}' has no event types")
        try:
            validate_url_template(rule.url)
        except ConfigError as exc:
            raise ConfigError(f"Rule '{rule.name}': {exc}") from None
        if rule.off_url:
            try:
                validate_url_template(rule.off_url)
            except ConfigError as exc:
                raise ConfigError(f"Rule '{rule.name}' off URL: {exc}") from None
            if rule.off_delay_seconds <= 0:
                raise ConfigError(f"Rule '{rule.name}': off delay must be more than 0 seconds")


def _from_dict(data: dict) -> Config:
    try:
        nvr = NvrConfig(**(data.get("nvr") or {}))
        defaults = Defaults(**(data.get("defaults") or {}))
        defaults.retries = int(defaults.retries)
        rules = []
        for r in data.get("rules") or []:
            r = dict(r)
            r["channels"] = [int(c) for c in r.get("channels") or []]
            r["event_types"] = [str(e) for e in r.get("event_types") or []]
            r["targets"] = [str(t).lower() for t in r.get("targets") or []]
            r["off_url"] = str(r.get("off_url") or "")
            if "off_delay_seconds" in r:
                r["off_delay_seconds"] = float(r["off_delay_seconds"])
            rules.append(Rule(**r))
        cameras = {int(k): str(v) for k, v in (data.get("cameras") or {}).items()}
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid config: {exc}") from None
    return Config(nvr=nvr, defaults=defaults, rules=rules, cameras=cameras)


def load(path: str | os.PathLike | None = None) -> Config:
    p = resolve_path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    with p.open() as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{p} does not contain a YAML mapping")
    config = _from_dict(data)
    validate(config)
    return config


def save(config: Config, path: str | os.PathLike | None = None) -> Path:
    p = resolve_path(path)
    data = asdict(config)
    for rule in data["rules"]:
        if rule["cooldown_seconds"] is None:
            del rule["cooldown_seconds"]
        if not rule["off_url"]:
            del rule["off_url"], rule["off_delay_seconds"]
    tmp = p.with_name(p.name + ".tmp")
    # The file holds the NVR password, so keep it private to the owner.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, p)
    return p
