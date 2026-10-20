from fake_nvr import dahua_record

from nvr_event_push.dahua import DahuaStreamParser, parse_record


def part(body: str, length: bool = True) -> bytes:
    data = body.encode()
    head = "--myboundary\r\nContent-Type: text/plain\r\n"
    if length:
        head += f"Content-Length: {len(data)}\r\n"
    return head.encode() + b"\r\n" + data + b"\r\n\r\n"


HEARTBEAT = part("Heartbeat")

CROSS_REGION = """Code=CrossRegionDetection;action=Start;index=2;data={
   "Direction" : "Enter",
   "LocaleTime" : "2026-10-18 21:30:01",
   "Object" : { "ObjectType" : "MotorVehicle", "ObjectID" : 7 }
}"""


def events(*chunks: bytes) -> list:
    parser = DahuaStreamParser()
    return [e for c in chunks for e in parser.feed(c)]


def test_video_motion_start_stop():
    start, stop = events(
        part("Code=VideoMotion;action=Start;index=0"),
        part("Code=VideoMotion;action=Stop;index=0"),
    )
    assert start.channel == 1  # Dahua index is 0-based
    assert start.event_type == "VMD"
    assert start.active
    assert start.description == "VideoMotion"
    assert start.target is None
    assert start.timestamp  # filled with the receive time when the NVR sends none
    assert stop.event_type == "VMD" and not stop.active


def test_pulse_with_target_data():
    (e,) = events(part(dahua_record(2, "CrossLineDetection", "Pulse", "human")))
    assert e.channel == 2
    assert e.event_type == "linedetection"
    assert e.active
    assert e.target == "human"
    assert e.raw.startswith("Code=CrossLineDetection")


def test_cross_region_direction_and_timestamp():
    (e,) = events(part(CROSS_REGION))
    assert e.channel == 3
    assert e.event_type == "regionEntrance"
    assert e.target == "vehicle"
    assert e.timestamp == "2026-10-18 21:30:01"
    leave = parse_record(CROSS_REGION.replace("Enter", "Leave"))[1]
    assert leave.event_type == "regionExiting"
    intrusion = parse_record(CROSS_REGION.replace('"Direction" : "Enter",', ""))[1]
    assert intrusion.event_type == "fielddetection"


def test_smart_motion_and_unknown_codes():
    human, vehicle, other = events(
        part("Code=SmartMotionHuman;action=Start;index=0"),
        part("Code=SmartMotionVehicle;action=Start;index=1"),
        part("Code=VideoBlind;action=Start;index=4"),
    )
    assert (human.event_type, human.target) == ("VMD", "human")
    assert (vehicle.event_type, vehicle.target) == ("VMD", "vehicle")
    assert (other.event_type, other.channel) == ("VideoBlind", 5)


def test_heartbeat_repeats_open_events_until_stop():
    parser = DahuaStreamParser()
    feed = lambda b: list(parser.feed(b))  # noqa: E731

    (start,) = feed(part("Code=VideoMotion;action=Start;index=0"))
    beat, repeat = feed(HEARTBEAT)
    assert beat.is_heartbeat and not beat.active
    assert repeat.active and repeat.channel == 1 and repeat.event_type == "VMD"
    assert repeat.raw == ""
    assert not repeat.is_heartbeat

    feed(part("Code=VideoMotion;action=Stop;index=0"))
    (beat,) = feed(HEARTBEAT)
    assert beat.is_heartbeat


def test_pulse_is_not_repeated():
    parser = DahuaStreamParser()
    list(parser.feed(part("Code=CrossLineDetection;action=Pulse;index=0")))
    assert [e.event_type for e in parser.feed(HEARTBEAT)] == ["heartbeat"]


def test_byte_by_byte_matches_whole():
    stream = (
        part("Code=VideoMotion;action=Start;index=0")
        + HEARTBEAT
        + part(CROSS_REGION)
        + part("Code=VideoMotion;action=Stop;index=0")
    )
    whole = events(stream)
    split = events(*(stream[i : i + 1] for i in range(len(stream))))
    assert [(e.event_type, e.state, e.channel) for e in split] == [
        (e.event_type, e.state, e.channel) for e in whole
    ]
    assert [e.event_type for e in whole] == ["VMD", "heartbeat", "VMD", "regionEntrance", "VMD"]


def test_part_without_content_length():
    first, second = events(
        part("Code=VideoMotion;action=Start;index=0", length=False),
        part("Code=VideoMotion;action=Stop;index=0", length=False),
        b"--myboundary\r\n",
    )
    assert first.active and not second.active


def test_several_records_in_one_part():
    a, b = events(part("Code=VideoMotion;action=Start;index=0\r\nCode=VideoMotion;action=Start;index=1"))
    assert (a.channel, b.channel) == (1, 2)


def test_garbage_is_skipped():
    got = events(
        b"\xff\xd8 not an event \r\n",
        part("no code here"),
        part("Code=VideoMotion;action=Start;index=0;data={broken"),
    )
    assert [e.event_type for e in got] == ["VMD"]
