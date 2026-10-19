from unittest import mock

import pytest
import requests

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


class FakeTimer:
    def __init__(self, delay, fn):
        self.delay, self.fn, self.cancelled = delay, fn, False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


def off_forwarder(config, now):
    timers = []

    def factory(delay, fn):
        timers.append(FakeTimer(delay, fn))
        return timers[-1]

    session = mock.Mock()
    session.get.return_value = mock.Mock(ok=True, status_code=200)
    fwd = Forwarder(config, session=session, max_workers=1, clock=lambda: now[0],
                    timer_factory=factory)
    return fwd, session, timers


def urls(session):
    return [c.args[0] for c in session.get.call_args_list]


def test_off_after_delay_and_reset_by_new_events():
    config = make_config(cooldown_seconds=60, off_url="http://h/off?cam={camera_name}",
                         off_delay_seconds=30)
    now = [0.0]
    fwd, session, timers = off_forwarder(config, now)

    fwd.handle(ev())
    now[0] += 10
    assert fwd.handle(ev()) == []  # in cooldown, but still restarts the off countdown
    assert len(timers) == 2 and timers[0].cancelled and timers[1].delay == 30
    timers[0].fn()  # a cancelled timer that fires anyway does nothing
    timers[1].fn()
    fwd._pool.shutdown(wait=True)
    assert urls(session)[-1] == "http://h/off?cam=Front%20Door"
    assert session.get.call_count == 2
    fwd.shutdown()
    assert session.get.call_count == 2  # nothing left pending


def test_off_clears_cooldown_and_shutdown_sends_pending():
    config = make_config(cooldown_seconds=60, off_url="http://h/off", off_delay_seconds=30)
    now = [0.0]
    fwd, session, timers = off_forwarder(config, now)

    for f in fwd.handle(ev()):
        f.result()
    timers[-1].fn()
    now[0] += 35  # well inside the 60 s cooldown
    assert len(fwd.handle(ev())) == 1
    fwd.shutdown()  # sends the pending off instead of leaving the light on
    assert [u.split("?")[0] for u in urls(session)] == [
        "http://h/m", "http://h/off", "http://h/m", "http://h/off"
    ]


def test_off_timer_shared_unless_url_is_per_camera():
    shared = make_config(off_url="http://h/off?ts={timestamp}")
    fwd, _, timers = off_forwarder(shared, [0.0])
    fwd.handle(ev(1))
    fwd.handle(ev(2))
    assert timers[0].cancelled and not timers[1].cancelled
    fwd.shutdown()

    per_cam = make_config(off_url="http://h/off?ch={channel}")
    fwd, _, timers = off_forwarder(per_cam, [0.0])
    fwd.handle(ev(1))
    fwd.handle(ev(2))
    assert not timers[0].cancelled and not timers[1].cancelled
    fwd.shutdown()


def test_off_url_validation_and_roundtrip(tmp_path):
    config = make_config(off_url="http://h/{nope}")
    path = save(config, tmp_path / "c.yaml")
    with pytest.raises(ConfigError, match="off URL"):
        load(path)
    config = make_config(off_url="http://h/off", off_delay_seconds=45)
    assert load(save(config, tmp_path / "c.yaml")) == config
    plain = make_config()
    path = save(plain, tmp_path / "plain.yaml")
    assert "off_url" not in path.read_text()


def retry_forwarder(responses, retries=2):
    config = make_config()
    config.defaults.retries = retries
    session = mock.Mock()
    session.get.side_effect = responses
    sleeps = []
    fwd = Forwarder(config, session=session, sleep=sleeps.append)
    return fwd, session, sleeps


def test_retries_transient_errors_then_succeeds():
    ok = mock.Mock(ok=True, status_code=200)
    fwd, session, sleeps = retry_forwarder(
        [requests.ConnectionError("refused"), mock.Mock(ok=False, status_code=503), ok]
    )
    assert fwd.fire(config_rule(fwd), ev()).result() is ok
    fwd.shutdown()
    assert session.get.call_count == 3
    assert sleeps == [1.0, 2.0]


def test_gives_up_after_retries():
    fwd, session, sleeps = retry_forwarder([requests.Timeout("slow")] * 3)
    assert fwd.fire(config_rule(fwd), ev()).result() is None
    fwd.shutdown()
    assert session.get.call_count == 3

    last = mock.Mock(ok=False, status_code=500)
    fwd, session, _ = retry_forwarder([mock.Mock(ok=False, status_code=500)] * 2 + [last])
    assert fwd.fire(config_rule(fwd), ev()).result() is last
    fwd.shutdown()


@pytest.mark.parametrize("status", [200, 404])
def test_no_retry_on_success_or_client_error(status):
    resp = mock.Mock(ok=status < 400, status_code=status)
    fwd, session, sleeps = retry_forwarder([resp])
    assert fwd.fire(config_rule(fwd), ev()).result() is resp
    fwd.shutdown()
    assert session.get.call_count == 1 and sleeps == []


def test_retries_disabled():
    fwd, session, sleeps = retry_forwarder([requests.ConnectionError("refused")], retries=0)
    assert fwd.fire(config_rule(fwd), ev()).result() is None
    fwd.shutdown()
    assert session.get.call_count == 1 and sleeps == []


def config_rule(fwd):
    return fwd.config.rules[0]


def test_cooldown_repeats_logged_once(caplog):
    config = make_config(cooldown_seconds=30)
    now = [0.0]
    fwd = Forwarder(config, session=mock.Mock(), clock=lambda: now[0])
    with caplog.at_level("DEBUG", logger="nvr_event_push.forwarder"):
        fwd.handle(ev())
        for _ in range(10):
            now[0] += 1
            fwd.handle(ev())
        now[0] += 30
        fwd.handle(ev())
    fwd.shutdown()
    messages = [r.getMessage() for r in caplog.records]
    assert sum("ignoring repeats" in m for m in messages) == 1
    assert any("10 repeat(s) ignored" in m for m in messages)
