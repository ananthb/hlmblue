"""Wire codec for the broadcast BLE A/C — pure, no Home Assistant imports.

Mirrors web/src/Codec.elm and tools/ac_frames.js, and is checked against
proto/vectors.json by the tests. See docs/protocol.md for the format:

* Commands are a 25-byte header + payload written to the command characteristic.
* Status arrives on the notify characteristic as ASCII text "Poll:<seq>:<hex>",
  where <hex> is a Tuya datapoint frame (55aa | ver | cmd | len | dp-units | crc).
"""

from __future__ import annotations

# GATT
SERVICE_UUID = "0000a00a-0000-1000-8000-00805f9b34fb"
CHAR_CMD = "0000b002-0000-1000-8000-00805f9b34fb"
CHAR_NOTIFY = "0000b003-0000-1000-8000-00805f9b34fb"
CHAR_INDICATE = "0000b004-0000-1000-8000-00805f9b34fb"
NAME_PREFIX = "HELM"

# command_id
CMD_STATUS_DATA = 500
CMD_BLE_PASSKEY = 600
CMD_AC_CTRL = 1003

# AC command index (goes in level[0] of an AC_CTRL frame)
AC_POWER = 0
AC_SPEED = 1
AC_TEMP = 2
AC_MODE = 3
AC_SWING = 4  # vertical swing
AC_OFF_TIMER = 18
AC_ON_TIMER = 19
AC_SWING_H = 21  # horizontal swing

POWER_ON = 0
POWER_OFF = 1
MODE = {"dry": 0, "cool": 1, "auto": 2, "fan": 3, "heat": 4, "wind": 5, "wet": 6}
# The A/C reports mode with a different value map than it takes commands with
# (verified live): status 0=auto, 1=cool, 2=heat, 3=dry, 4=fan.
STATUS_MODE = {0: "auto", 1: "cool", 2: "heat", 3: "dry", 4: "fan"}
FAN = {"auto": 0, "low": 1, "medium": 2, "high": 3}
FAN_REV = {v: k for k, v in FAN.items()}

# status DPIDs
# 0x01 is the unit's on/off state, 1 = on -- the inverse of the POWER command
# above, where ON is 0. Verified live: on with only the fan turning, drawing
# 24 W, it reports 1. See power.py.
DP_POWER = 0x01
DP_TEMP = 0x02
DP_MODE = 0x04
DP_FAN = 0x05
DP_POWER_W = 0x1C
DP_SWING_V = 0x6E
DP_SWING_H = 0x6F
DP_ROOM_TEMP = 0x6A
DP_PASSKEY_ACK = 0x79

TEMP_MIN = 16
TEMP_MAX = 30

# Ranges a reading has to fall in to be believed. Anything else is a torn frame
# that got past the checksum; the DPIDs not listed here are unbounded.
RANGES = {
    DP_TEMP: (TEMP_MIN, TEMP_MAX),
    DP_ROOM_TEMP: (-20, 70),
    DP_POWER_W: (0, 20000),
}


def plausible(dpid: int, value: int | None) -> bool:
    """Is `value` in range for `dpid`? True for a DPID with no range."""
    lo, hi = RANGES.get(dpid, (None, None))
    if lo is None or value is None:
        return True
    return lo <= value <= hi


def serialize(cmd_id: int, payload: bytes = b"", *, total_level: int = 0, level0: int = 0) -> bytes:
    """CommandPacketBuilder.serialize(): 25-byte header + payload."""
    length = len(payload)
    buf = bytearray(25 + length)
    buf[0] = 0xFF
    buf[1:3] = cmd_id.to_bytes(2, "big")
    buf[3:5] = length.to_bytes(2, "big")
    buf[5:7] = (1).to_bytes(2, "big")  # seqNum
    buf[11] = total_level
    buf[12] = level0  # level[0]; level[1..4] and checksum/params stay zero
    buf[17:21] = length.to_bytes(4, "big")  # totalSize
    buf[25:] = payload
    return bytes(buf)


def _ac(ac_cmd: int, value: int) -> bytes:
    return serialize(CMD_AC_CTRL, bytes([value & 0xFF]), total_level=2, level0=ac_cmd)


def _ac_bytes(ac_cmd: int, payload: bytes) -> bytes:
    return serialize(CMD_AC_CTRL, payload, total_level=2, level0=ac_cmd)


def frame_login(pin: str) -> bytes:
    p = str(pin).zfill(4)[:4].encode("ascii")
    return serialize(CMD_BLE_PASSKEY, p, total_level=1, level0=p[0])


def frame_status() -> bytes:
    return serialize(CMD_STATUS_DATA)


def frame_power(on: bool) -> bytes:
    return _ac(AC_POWER, POWER_ON if on else POWER_OFF)


def frame_temp(celsius: int) -> bytes:
    return _ac(AC_TEMP, int(celsius))


def frame_mode(mode: str) -> bytes:
    return _ac(AC_MODE, MODE.get(mode, MODE["cool"]))


def frame_fan(fan: str) -> bytes:
    return _ac(AC_SPEED, FAN.get(fan, FAN["auto"]))


def frame_swing(on: bool) -> bytes:
    """Vertical swing. Inverted like power: on is 0 (verified live)."""
    return _ac(AC_SWING, 0 if on else 1)


def frame_swing_h(on: bool) -> bytes:
    """Horizontal swing (2-byte payload: [0, on?1:0])."""
    return _ac_bytes(AC_SWING_H, bytes([0, 1 if on else 0]))


def frame_off_timer(minutes: int) -> bytes:
    """Auto-off after `minutes` (0 cancels). Payload is u32 BE minutes."""
    return _ac_bytes(AC_OFF_TIMER, max(0, int(minutes)).to_bytes(4, "big"))


def frame_on_timer(minutes: int) -> bytes:
    """Auto-on after `minutes` (0 cancels). Payload is u32 BE minutes."""
    return _ac_bytes(AC_ON_TIMER, max(0, int(minutes)).to_bytes(4, "big"))


def decode_notify(raw: bytes) -> dict[int, int | None]:
    """Decode one 0xB003 notification into {dpid: value}.

    The value is ASCII text "Poll:<seq>:<hexframe>" (or "Diag:..."). The hexframe
    is a Tuya datapoint frame, right-padded with "00" to a fixed 15-byte slot.

    The unit tears that slot: a notification can carry a part-written frame with
    the start of the next one behind it, so the declared body length and the
    checksum are both verified before any datapoint is believed. Returns an
    empty dict for anything unrecognised or unverified.
    """
    text = raw.decode("ascii", "replace").rstrip("\x00")
    parts = text.split(":")
    if len(parts) < 3 or parts[0] != "Poll":
        return {}
    try:
        b = bytes.fromhex(parts[-1])
    except ValueError:
        return {}
    if len(b) < 7 or b[0] != 0x55 or b[1] != 0xAA:
        return {}
    end = 6 + ((b[4] << 8) | b[5])  # body runs [6, end); b[end] is the checksum
    if len(b) <= end or sum(b[:end]) & 0xFF != b[end]:
        return {}
    out: dict[int, int | None] = {}
    i = 6
    while i < end:
        dlen = (b[i + 2] << 8) | b[i + 3] if i + 4 <= end else 0
        if i + 4 + dlen > end:
            return {}  # a datapoint overruns the body: the whole frame is torn
        val = b[i + 4 : i + 4 + dlen]
        out[b[i]] = int.from_bytes(val, "big") if val else None
        i += 4 + dlen
    return out


def random_pin() -> str:
    """A random 4-digit passkey, never 0000 (which the app forbids as the default)."""
    import secrets

    while True:
        n = f"{secrets.randbelow(10000):04d}"
        if n != "0000":
            return n
