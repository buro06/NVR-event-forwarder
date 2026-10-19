"""End-to-end: fake NVR -> NvrClient -> Forwarder -> local HTTP target."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import fake_nvr
from nvr_event_push.config import Config, NvrConfig, Rule
from nvr_event_push.forwarder import Forwarder
from nvr_event_push.isapi import AuthError, NvrClient


@pytest.fixture
def nvr():
    server = fake_nvr.serve(interval=0.02, burst=3)
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


def nvr_config(server, password="secret"):
    return NvrConfig(host="127.0.0.1", port=server.server_port, password=password)


def test_device_info_and_channels(nvr):
    client = NvrClient(nvr_config(nvr))
    assert client.device_info()["model"] == "DS-FAKE"
    assert client.list_channels() == fake_nvr.CAMERAS


def test_bad_password(nvr):
    with pytest.raises(AuthError):
        NvrClient(nvr_config(nvr, password="wrong")).list_channels()
    with pytest.raises(AuthError):
        next(NvrClient(nvr_config(nvr, password="wrong")).alert_stream())


def test_stream_forwards_with_cooldown(nvr, target):
    tserver, hits, got = target
    config = Config(
        nvr=nvr_config(nvr),
        rules=[
            Rule(
                name="Driveway",
                channels=[1],
                event_types=["VMD"],
                url=f"http://127.0.0.1:{tserver.server_port}/on?cam={{camera_name}}&e={{event_type}}",
                cooldown_seconds=60,
            )
        ],
        cameras=fake_nvr.CAMERAS,
    )
    client = NvrClient(config.nvr)
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
