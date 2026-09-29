"""The Python codec must reproduce the golden frames in proto/vectors.json,
exactly like the JS codec and tools/ac_frames.js."""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "hlmblue"))

import protocol as p  # noqa: E402

VECTORS = json.loads((ROOT / "proto" / "vectors.json").read_text())["frames"]

BUILDERS = {
    "power_on": lambda: p.frame_power(True),
    "power_off": lambda: p.frame_power(False),
    "temp_24": lambda: p.frame_temp(24),
    "temp_16": lambda: p.frame_temp(16),
    "mode_cool": lambda: p.frame_mode("cool"),
    "mode_auto": lambda: p.frame_mode("auto"),
    "mode_dry": lambda: p.frame_mode("dry"),
    "fan_high": lambda: p.frame_fan("high"),
    "fan_low": lambda: p.frame_fan("low"),
    "status_request": lambda: p.frame_status(),
    "passkey_4271": lambda: p.frame_login("4271"),
}


def test_frames_match_vectors():
    for name, build in BUILDERS.items():
        assert build().hex() == VECTORS[name], f"{name} mismatch"


def test_swing_payloads():
    # Vertical is inverted like power; horizontal is not.
    assert p.frame_swing(True)[-1:] == b"\x00"
    assert p.frame_swing(False)[-1:] == b"\x01"
    assert p.frame_swing_h(True)[-2:] == b"\x00\x01"
    assert p.frame_swing_h(False)[-2:] == b"\x00\x00"


def test_decode_status():
    # A 'Poll' notification: 55aa 03 07 len | dp(02) type(02) len(0004) 00000018 | sum
    frame = b"Poll:42:55aa030700080202000400000018" + b"31" + b"\x00"
    dps = p.decode_notify(frame)
    assert dps.get(p.DP_TEMP) == 0x18  # 24 C setpoint


def test_decode_garbage():
    assert p.decode_notify(b"not a poll") == {}
    assert p.decode_notify(b"Diag:1:") == {}


# Real notifications captured from HELM__9869 (0xB003). Every frame is padded
# with "00" to a fixed 15-byte slot.
def test_decode_padded_slot():
    assert p.decode_notify(b"Poll:1925:55aa030700086a0200040000001e9f\x00") == {p.DP_ROOM_TEMP: 30}
    assert p.decode_notify(b"Poll:1934:55aa03070008020200040000001932\x00") == {p.DP_TEMP: 25}
    assert p.decode_notify(b"Poll:1919:55aa030700051a010001002a000000\x00") == {0x1A: 0}


# The unit tears that slot: a part-written frame with the start of the next one
# behind it. Before the length+checksum check these decoded as header bytes and
# Home Assistant showed them as temperatures (0x55aa = 21930 C, and worse).
def test_decode_rejects_torn_frames():
    torn = [
        b"Poll:1926:55aa030700086a02000455aa0307",  # value = 55aa0307 = 1437205255
        b"Poll:1927:55aa0307000802020004000055aa",  # value = 000055aa = 21930
        b"Poll:1928:55aa030700080202" + b"55aa03070008" + b"16",  # 13002344470
    ]
    for frame in torn:
        assert p.decode_notify(frame) == {}, frame


def test_plausible_filters_out_of_range():
    assert p.plausible(p.DP_TEMP, 24)
    assert not p.plausible(p.DP_TEMP, 21930)
    assert p.plausible(p.DP_ROOM_TEMP, 30)
    assert not p.plausible(p.DP_ROOM_TEMP, 1437205255)
    assert p.plausible(p.DP_MODE, 1)  # no range for this DPID
    assert p.plausible(p.DP_TEMP, None)
