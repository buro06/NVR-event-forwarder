"""A tiny fake Hikvision NVR for testing without hardware.

Serves deviceInfo, the channel list and a multipart alertStream behind HTTP
Digest auth. The stream cycles through the configured channels, emitting a
burst of "active" motion events followed by idle heartbeats.

    python tests/fake_nvr.py --port 8099 --user admin --password secret
"""

from __future__ import annotations

import argparse
import hashlib
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CAMERAS = {1: "Driveway", 2: "Front Door", 3: "Backyard"}
BOUNDARY = "boundary"

_ALERT = """<?xml version="1.0" encoding="UTF-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<ipAddress>127.0.0.1</ipAddress>
<portNo>80</portNo>
<protocol>HTTP</protocol>
<channelID>{channel}</channelID>
<dynChannelID>{channel}</dynChannelID>
<dateTime>{ts}</dateTime>
<activePostCount>1</activePostCount>
<eventType>{event_type}</eventType>
<eventState>{state}</eventState>
<eventDescription>{desc}</eventDescription>
{extra}</EventNotificationAlert>
"""


def alert_xml(channel: int, event_type: str, state: str, target: str | None = None) -> str:
    extra = ""
    if target:
        extra = (
            "<DetectionRegionList><DetectionRegionEntry><regionID>1</regionID>"
            f"<detectionTarget>{target}</detectionTarget></DetectionRegionEntry>"
            "</DetectionRegionList>\n"
        )
    return _ALERT.format(
        channel=channel,
        ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        event_type=event_type,
        state=state,
        desc={"VMD": "Motion alarm", "videoloss": "videoloss alarm"}.get(event_type, event_type),
        extra=extra,
    )


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def make_handler(user: str, password: str, interval: float, burst: int, cameras: dict[int, str]):
    realm = "fake-nvr"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quieter output
            pass

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Digest "):
                return False
            fields = {}
            for part in header[7:].split(","):
                k, _, v = part.strip().partition("=")
                fields[k] = v.strip('"')
            if fields.get("username") != user:
                return False
            ha1 = _md5(f"{user}:{realm}:{password}")
            ha2 = _md5(f"{self.command}:{fields.get('uri', '')}")
            if fields.get("qop"):
                expected = _md5(
                    f"{ha1}:{fields.get('nonce')}:{fields.get('nc')}:{fields.get('cnonce')}:"
                    f"{fields.get('qop')}:{ha2}"
                )
            else:
                expected = _md5(f"{ha1}:{fields.get('nonce')}:{ha2}")
            return secrets.compare_digest(expected, fields.get("response", ""))

        def _challenge(self) -> None:
            self.send_response(401)
            self.send_header(
                "WWW-Authenticate",
                f'Digest realm="{realm}", qop="auth", nonce="{secrets.token_hex(16)}"',
            )
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _xml(self, body: str) -> None:
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._authorized():
                return self._challenge()
            if self.path == "/ISAPI/System/deviceInfo":
                return self._xml(
                    "<DeviceInfo><deviceName>Fake NVR</deviceName><model>DS-FAKE</model>"
                    "<firmwareVersion>V0.0.1</firmwareVersion></DeviceInfo>"
                )
            if self.path == "/ISAPI/ContentMgmt/InputProxy/channels":
                items = "".join(
                    f"<InputProxyChannel><id>{cid}</id><name>{name}</name></InputProxyChannel>"
                    for cid, name in cameras.items()
                )
                return self._xml(
                    '<InputProxyChannelList xmlns="http://www.hikvision.com/ver20/XMLSchema">'
                    f"{items}</InputProxyChannelList>"
                )
            if self.path == "/ISAPI/Event/notification/alertStream":
                return self._stream()
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send_part(self, xml: str) -> None:
            data = xml.encode()
            self.wfile.write(
                f"--{BOUNDARY}\r\nContent-Type: application/xml; charset=\"UTF-8\"\r\n"
                f"Content-Length: {len(data)}\r\n\r\n".encode()
                + data
                + b"\r\n"
            )
            self.wfile.flush()

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/mixed; boundary={BOUNDARY}")
            self.send_header("Connection", "close")
            self.end_headers()
            channels = list(cameras)
            i = 0
            try:
                while True:
                    ch = channels[i % len(channels)]
                    event_type, target = (("VMD", None), ("linedetection", "human"))[i % 2]
                    for _ in range(burst):
                        self._send_part(alert_xml(ch, event_type, "active", target))
                        time.sleep(interval)
                    for _ in range(burst):
                        self._send_part(alert_xml(0, "videoloss", "inactive"))
                        time.sleep(interval)
                    i += 1
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def serve(
    port: int = 0,
    user: str = "admin",
    password: str = "secret",
    interval: float = 1.0,
    burst: int = 3,
    cameras: dict[int, str] | None = None,
) -> ThreadingHTTPServer:
    """Start the fake NVR in a background thread and return the server."""
    handler = make_handler(user, password, interval, burst, cameras or CAMERAS)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--user", default="admin")
    p.add_argument("--password", default="secret")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between messages")
    p.add_argument("--burst", type=int, default=5, help="messages per motion burst / idle period")
    args = p.parse_args()
    server = serve(args.port, args.user, args.password, args.interval, args.burst)
    print(f"Fake NVR on http://127.0.0.1:{server.server_port} (user {args.user!r}, password {args.password!r})")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
