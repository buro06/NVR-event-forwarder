from unittest import mock

import pytest

from nvr_event_push.config import Config, ConfigError, NvrConfig, Rule, load, save
from nvr_event_push.config import validate_url_template
from nvr_event_push.events import Event
from nvr_event_push.forwarder import Forwarder, build_url, rule_matches


def ev(channel=1, event_type="VMD", state="active", target=None):
    return Event(channel=channel, event_type=event_type, state=state, target=target,
                 timestamp="2026-10-18T21:30:01+01:00")


def make_config(**rule_kw):
    rule = Rule(name="Lights", url="http://h/m?cam={camera_name}&t={timestamp}", **rule_kw)
    return Config(nvr=NvrConfig(host="nvr.local"), rules=[rule], cameras={1: "Front Door"})


def test_build_url_encodes_values():
    url = build_url(
        "http://h/{camera_name}?t={timestamp}",
        {"camera_name": "Front Door/1", "timestamp": "2026-10-18T21:30:01+01:00"},
    )
    assert url == "http://h/Front%20Door%2F1?t=2026-10-18T21%3A30%3A01%2B01%3A00"


@pytest.mark.parametrize(
    "url",
    ["ftp://h/x", "not a url", "http://h/{nope}", "http://h/{camera_name"],
)
def test_validate_url_template_rejects(url):
    with pytest.raises(ConfigError):
        validate_url_template(url)


def test_validate_url_template_accepts_all_placeholders():
    validate_url_template(
        "https://h:8123/api/{camera_name}/{channel}?e={event_type}&l={event_label}"
        "&t={target}&ts={timestamp}&n={nvr_host}&r={rule_name}"
    )


def test_rule_matching():
    r = Rule(name="r", url="http://h", channels=[1, 3], event_types=["VMD", "linedetection"])
    assert rule_matches(r, ev(1))
    assert rule_matches(r, ev(3, "LineDetection"))
    assert not rule_matches(r, ev(2))
    assert not rule_matches(r, ev(1, "fielddetection"))
    assert rule_matches(Rule(name="all", url="http://h"), ev(7))
    r.enabled = False
    assert not rule_matches(r, ev(1))


def test_target_filter():
    r = Rule(name="r", url="http://h", event_types=["VMD"], targets=["human"])
    assert rule_matches(r, ev(target="human"))
    assert not rule_matches(r, ev(target="vehicle"))
    assert not rule_matches(r, ev(target=None))


def test_forwarder_cooldown_and_inactive():
    config = make_config(cooldown_seconds=10)
    session = mock.Mock()
    session.get.return_value = mock.Mock(ok=True, status_code=200)
    now = [100.0]
    fwd = Forwarder(config, session=session, clock=lambda: now[0])

    assert len(fwd.handle(ev())) == 1
    assert fwd.handle(ev(state="inactive")) == []
    now[0] += 5
    assert fwd.handle(ev()) == []  # still cooling down
    now[0] += 6
    assert len(fwd.handle(ev())) == 1
    fwd.shutdown()

    assert session.get.call_count == 2
    url = session.get.call_args.args[0]
    assert url == "http://h/m?cam=Front%20Door&t=2026-10-18T21%3A30%3A01%2B01%3A00"


def test_cooldown_is_per_channel():
    config = make_config()
    session = mock.Mock()
    fwd = Forwarder(config, session=session, clock=lambda: 0.0)
    assert len(fwd.handle(ev(1))) == 1
    assert len(fwd.handle(ev(2))) == 1
    assert fwd.handle(ev(1)) == []
    fwd.shutdown()


def test_config_roundtrip(tmp_path):
    config = make_config(channels=[1], targets=["human"])
    path = save(config, tmp_path / "c.yaml")
    assert (path.stat().st_mode & 0o777) == 0o600
    loaded = load(path)
    assert loaded == config


def test_password_env_override(monkeypatch):
    nvr = NvrConfig(password="file")
    assert nvr.effective_password == "file"
    monkeypatch.setenv("NVR_PASSWORD", "env")
    assert nvr.effective_password == "env"
