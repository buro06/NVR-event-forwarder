"""End-to-end: fake NVR -> client -> Forwarder -> local HTTP target, for each brand."""

import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import fake_nvr
from nvr_event_push.client import AuthError, listen_all, make_client
from nvr_event_push.config import Config, NvrConfig, Rule
from nvr_event_push.forwarder import Forwarder

MODELS = {"hikvision": "DS-FAKE", "dahua": "DH-FAKE"}


@pytest.fixture(params=["hikvision", "dahua"])
def nvr(request):
    server = fake_nvr.serve(interval=0.02, burst=3, brand=request.param)
    server.brand = request.param
    yield server
    server.shutdown()


@pytest.fixture
def target():
    hits = []
    got = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            got.set()
            self.send_response(204)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server, hits, got
    server.shutdown()


def nvr_config(server, password="secret", name="NVR"):
    return NvrConfig(
        name=name,
        host="127.0.0.1",
        port=server.server_port,
        password=password,
        brand=server.brand,
        cameras=dict(fake_nvr.CAMERAS),
    )


def test_device_info_and_channels(nvr):
    client = make_client(nvr_config(nvr))
    assert client.device_info()["model"] == MODELS[nvr.brand]
    assert client.list_channels() == fake_nvr.CAMERAS


def test_bad_password(nvr):
    with pytest.raises(AuthError):
        make_client(nvr_config(nvr, password="wrong")).list_channels()
    with pytest.raises(AuthError):
        next(make_client(nvr_config(nvr, password="wrong")).alert_stream())


def test_stream_forwards_with_cooldown(nvr, target):
    tserver, hits, got = target
    config = Config(
        nvrs=[nvr_config(nvr)],
        rules=[
            Rule(
                name="Driveway",
                channels=[1],
                event_types=["VMD"],
                url=f"http://127.0.0.1:{tserver.server_port}/on?cam={{camera_name}}&e={{event_type}}",
                cooldown_seconds=60,
            )
        ],
    )
    client = make_client(config.nvrs[0])
    fwd = Forwarder(config)
    seen = []
    for event in client.alert_stream():
        seen.append(event)
        fwd.handle(event)
        # Run until the stream has cycled past channel 1 at least twice.
        if sum(1 for e in seen if e.channel == 1 and e.active) >= 6:
            break
    client.close()
    fwd.shutdown()

    assert got.wait(2)
    # 6 active VMD events on channel 1, but only one request thanks to the cooldown.
    assert hits == ["/on?cam=Driveway&e=VMD"]
    assert any(e.is_heartbeat for e in seen)
    assert any(e.event_type == "linedetection" and e.target == "human" for e in seen)


@pytest.fixture
def two_nvrs():
    house = fake_nvr.serve(interval=0.02, burst=3, brand="hikvision")
    house.brand = "hikvision"
    garage = fake_nvr.serve(interval=0.02, burst=3, brand="dahua", password="other")
    garage.brand = "dahua"
    yield house, garage
    house.shutdown()
    garage.shutdown()


def test_listen_all_merges_nvrs(two_nvrs, target):
    house, garage = two_nvrs
    tserver, hits, got = target
    config = Config(
        nvrs=[nvr_config(house, name="House"), nvr_config(garage, "other", name="Garage")],
        rules=[
            Rule(
                name="Garage only",
                nvr="Garage",
                channels=[1],
                event_types=["VMD"],
                url=f"http://127.0.0.1:{tserver.server_port}/on?n={{nvr_name}}&cam={{camera_name}}",
                cooldown_seconds=60,
            )
        ],
    )
    fwd = Forwarder(config)
    seen = Counter()
    stop = threading.Event()

    def on_event(event):
        fwd.handle(event)
        if event.active:
            seen[event.nvr] += 1
        if seen["House"] >= 6 and seen["Garage"] >= 6:
            stop.set()

    listen_all(config.nvrs, on_event, stop)
    fwd.shutdown()

    assert stop.is_set()
    assert got.wait(2)
    # Channel 1 moved on both NVRs, but the rule only listens to the garage.
    assert hits == ["/on?n=Garage&cam=Driveway"]


def test_listen_all_keeps_going_when_one_nvr_rejects_login(two_nvrs):
    house, garage = two_nvrs
    nvrs = [nvr_config(house, name="House"), nvr_config(garage, "wrong", name="Garage")]
    seen = Counter()
    stop = threading.Event()

    def on_event(event):
        seen[event.nvr] += 1
        if seen["House"] >= 5:
            stop.set()

    listen_all(nvrs, on_event, stop)
    assert seen["House"] >= 5 and seen["Garage"] == 0

    with pytest.raises(AuthError):
        listen_all([nvr_config(garage, "wrong", name="Garage")], on_event)
