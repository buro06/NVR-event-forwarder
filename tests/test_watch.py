import pytest

from nvr_event_push import cli, runner
from nvr_event_push.config import Config, NvrConfig, save
from nvr_event_push.events import Event

EVENTS = [
    Event(channel=1, event_type="VMD", state="active", nvr="House"),
    Event(channel=1, event_type="VMD", state="active", nvr="Garage"),
    Event(channel=2, event_type="linedetection", state="active", nvr="Garage"),
    Event(channel=None, event_type="videoloss", state="inactive", nvr="House"),
]


def two_nvrs():
    return Config(
        nvrs=[
            NvrConfig(name="House", host="10.0.0.2", cameras={1: "Driveway"}),
            NvrConfig(name="Garage", host="10.0.0.3", cameras={1: "Gate", 2: "Side"}),
        ]
    )


@pytest.fixture
def fake_listen(monkeypatch):
    """Replace the network: feed EVENTS from whichever NVRs are watched."""
    listened = []

    def listen_all(nvrs, on_event, stop=None):
        names = [n.name for n in nvrs]
        listened.append(names)
        for e in EVENTS:
            if e.nvr in names:
                on_event(e)

    monkeypatch.setattr(runner, "listen_all", listen_all)
    monkeypatch.setattr(runner, "refresh_cameras", lambda config, nvrs=None: True)
    return listened


def watched(config, **kw):
    lines = []
    runner.watch(config, out=lines.append, **kw)
    return lines


def test_no_filter_shows_everything_but_heartbeats(fake_listen):
    lines = watched(two_nvrs())
    assert fake_listen == [["House", "Garage"]]
    assert len(lines) == 3


def test_nvr_filter_only_connects_to_chosen_nvrs(fake_listen):
    lines = watched(two_nvrs(), nvrs=["Garage"])
    assert fake_listen == [["Garage"]]
    assert all("Garage" in line for line in lines) and len(lines) == 2


def test_event_filter_is_case_insensitive(fake_listen):
    lines = watched(two_nvrs(), event_types=["LineDetection"])
    assert len(lines) == 1 and "Line crossing" in lines[0]


def test_filters_combine_and_heartbeats_follow_show_all(fake_listen):
    lines = watched(two_nvrs(), nvrs=["House"], event_types=["VMD"], show_all=True)
    assert len(lines) == 2  # House motion + House heartbeat
    assert any("videoloss" in line for line in lines)


def test_describe_watch():
    config = two_nvrs()
    assert runner.describe_watch(config) == "all events from all NVRs"
    assert (
        runner.describe_watch(config, ["Garage"], ["VMD", "custom"])
        == "Motion detection, custom events from Garage"
    )
    config.nvrs.pop()
    assert runner.describe_watch(config) == "all events"


def test_cli_watch_passes_filters_and_rejects_unknown_nvr(tmp_path, monkeypatch, capsys):
    path = save(two_nvrs(), tmp_path / "c.yaml")
    calls = []
    monkeypatch.setattr(runner, "watch", lambda config, **kw: calls.append(kw))

    assert cli.main(["-c", str(path), "watch", "--nvr", "Garage", "--event", "VMD,linedetection"]) == 0
    assert calls[0]["nvrs"] == ["Garage"]
    assert calls[0]["event_types"] == ["VMD", "linedetection"]
    assert "Motion detection, Line crossing events from Garage" in capsys.readouterr().out

    assert cli.main(["-c", str(path), "watch", "--nvr", "Shed"]) == 2
    assert "Unknown NVR 'Shed'" in capsys.readouterr().err
    assert len(calls) == 1
