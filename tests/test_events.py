import json

from fake_nvr import alert_xml

from nvr_event_push.events import AlertStreamSplitter, parse_alert

VMD = b"""<?xml version="1.0" encoding="UTF-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<ipAddress>192.168.1.64</ipAddress>
<channelID>33</channelID>
<dynChannelID>1</dynChannelID>
<dateTime>2026-10-18T21:30:01+01:00</dateTime>
<activePostCount>1</activePostCount>
<eventType>VMD</eventType>
<eventState>active</eventState>
<eventDescription>Motion alarm</eventDescription>
</EventNotificationAlert>"""

HEARTBEAT = b"""<EventNotificationAlert version="1.0" xmlns="http://www.hikvision.com/ver10/XMLSchema">
<ipAddress>192.168.1.64</ipAddress>
<channelID>0</channelID>
<dateTime>2026-10-18T21:30:05+01:00</dateTime>
<activePostCount>0</activePostCount>
<eventType>videoloss</eventType>
<eventState>inactive</eventState>
<eventDescription>videoloss alarm</eventDescription>
</EventNotificationAlert>"""


def test_parse_vmd_prefers_dyn_channel():
    e = parse_alert(VMD)
    assert e.channel == 1
    assert e.event_type == "VMD"
    assert e.active
    assert e.label == "Motion detection"
    assert e.timestamp == "2026-10-18T21:30:01+01:00"
    assert e.target is None
    assert not e.is_heartbeat


def test_parse_heartbeat():
    e = parse_alert(HEARTBEAT)
    assert e.channel is None
    assert e.is_heartbeat
    assert not e.active


def test_parse_smart_event_target():
    e = parse_alert(alert_xml(2, "linedetection", "active", "human"))
    assert e.channel == 2
    assert e.event_type == "linedetection"
    assert e.target == "human"


def test_parse_invalid():
    assert parse_alert(b"<not xml") is None
    assert parse_alert(b"<Other/>") is None


def multipart(*bodies: bytes) -> bytes:
    out = b""
    for body in bodies:
        out += (
            b"--boundary\r\nContent-Type: application/xml\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
            + b"\r\n"
        )
    return out


def test_splitter_whole_stream():
    events = list(AlertStreamSplitter().feed(multipart(VMD, HEARTBEAT, VMD)))
    assert [e.event_type for e in events] == ["VMD", "videoloss", "VMD"]


def test_splitter_byte_by_byte():
    data = multipart(VMD, HEARTBEAT)
    splitter = AlertStreamSplitter()
    events = []
    for i in range(len(data)):
        events.extend(splitter.feed(data[i : i + 1]))
    assert [e.event_type for e in events] == ["VMD", "videoloss"]


def test_splitter_skips_binary_image_parts():
    jpeg = b"\xff\xd8\xff\xe0{\"\x00\x01garbage{ \"x\xff\xd9"
    data = (
        multipart(VMD)
        + b"--boundary\r\nContent-Type: image/jpeg\r\n\r\n"
        + jpeg
        + b"\r\n"
        + multipart(HEARTBEAT)
    )
    events = list(AlertStreamSplitter().feed(data))
    assert [e.event_type for e in events] == ["VMD", "videoloss"]


def test_splitter_json_alert():
    body = json.dumps(
        {
            "ipAddress": "192.168.1.64",
            "channelID": 3,
            "dateTime": "2026-10-18T21:30:01+01:00",
            "eventType": "fielddetection",
            "eventState": "active",
            "DetectionRegionList": [{"targetType": "vehicle"}],
        }
    ).encode()
    splitter = AlertStreamSplitter()
    data = multipart(body, VMD)
    half = len(data) // 3
    events = list(splitter.feed(data[:half])) + list(splitter.feed(data[half:]))
    assert [(e.event_type, e.channel, e.target) for e in events] == [
        ("fielddetection", 3, "vehicle"),
        ("VMD", 1, None),
    ]


def test_splitter_buffer_stays_small_on_noise():
    splitter = AlertStreamSplitter()
    for _ in range(1000):
        assert list(splitter.feed(b"x" * 1024)) == []
    assert len(splitter._buf) <= 64
