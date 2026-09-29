# A/C BLE protocol

The wire protocol for the broadcast BLE air conditioner. Machine-readable GATT
map in [`gatt.json`](gatt.json); golden command frames in
[`../proto/vectors.json`](../proto/vectors.json); a reference codec in
[`../tools/ac_frames.js`](../tools/ac_frames.js).

## Advertisement

- Service UUID `0xA00A`, connectable, public address.
- The local name is `HELM:` followed by the device name, then two NUL bytes; the
  `0x2A00` GAP characteristic reports the same name without the `HELM:` prefix.
  Scan by name prefix `HELM`.
- No manufacturer or service data — all state and control live behind GATT.

## GATT

MTU negotiates to 247.

| Service | Char | Properties | Notes |
|---|---|---|---|
| `0x1800` Generic Access | `0x2A00` | read | device name |
| **`0xA00A`** vendor | **`0xB001`** | read | device info blob |
| | **`0xB002`** | write, write-no-response | **command channel** |
| | **`0xB003`** | notify | status stream |
| | **`0xB004`** | indicate | second stream, purpose unknown |
| `0x180F` Battery | `0x2A19` | read, notify | reads 90%, vestigial on a mains unit |

`0xB003` and `0xB004` each have a `0x2902` CCCD (handles 20 and 23).

## The BLE frame

Every write to `0xB002` is a fixed 25-byte header followed by a variable payload:

```
off  size  field         encoding   notes
 0    1    header        u8         always 0xFF
 1    2    cmdId         u16 BE     see command_id
 3    2    len           u16 BE     payload length
 5    2    seqNum        u16 BE     1
 7    4    checksum      u32 BE     0 (not validated by the unit)
11    1    total_level   u8         2 for AC control, 1 for passkey
12    5    level         bytes      level[0] carries the command / dpid; rest 0
17    4    totalSize     u32 BE     = payload length
21    4    params        u32 BE     0
25    …    payload       bytes      value bytes
```

`command_id`:

| name | value | | name | value |
|---|---|---|---|---|
| `FIRMWARE_UPDATE` | 100 | | `STATUS_DATA` | 500 |
| `VFS_UPDATE` | 101 | | `BLE_PASSKEY` | 600 |
| `SAVE_DEVICE_NAME` | 103 | | `WIFI` | 700 |
| `USER_ID` | 105 | | **`AC_CTRL`** | **1003** (`0x03EB`) |
| `FACTORY_RESET` | 109 | | | |

## AC control

An AC command is an `AC_CTRL` (1003) frame with `total_level = 2`, the command in
`level[0]`, and the value in the payload.

`level[0]` — the command index:

| cmd | `level[0]` | payload | cmd | `level[0]` | payload |
|---|---|---|---|---|---|
| POWER | 0 | `[on?0:1]` | DISPLAY | 10 | `[on?0:1]` |
| SPEED (fan) | 1 | `[0-3]` | REMOTE_DIAG | 11 | `[0]` |
| TEMP | 2 | `[°C]` | COMPRESSOR | 12 | |
| MODE | 3 | `[mode]` | ODU | 13 | |
| SWING (V) | 4 | `[on?0:1]` | IDU | 14 | |
| TURBO | 5 | | CONVERTIBLE | 17 | `[val]` |
| SLEEP | 6 | | OFF_TIMER | 18 | `[min u32 BE]` |
| TIMER | 7 | | ON_TIMER | 19 | `[min u32 BE]` |
| CONDA | 8 | | SILENT | 20 | `[on?1:0]` |
| ECO | 9 | | SWING_H | 21 | `[0, on?1:0]` |

Value encodings:

- **power** — `ON = 0`, `OFF = 1` (note ON is 0).
- **mode** — `DRY 0 · COOL 1 · AUTO 2 · FAN 3 · HEAT 4 · WIND 5 · WET 6 · CONVERTIBLE 17`.
- **fan** — `auto 0 · low 1 · medium 2 · high 3`.
- **temperature** — one byte of °C.
- **swing** — vertical is `SWING (V)` (`level[0] = 4`, payload `[on?0:1]`,
  inverted like power), horizontal is `SWING_H`
  (`level[0] = 21`, payload `[0, on?1:0]`).

Worked frames (hex, spaces for reading only; full set in
[`../proto/vectors.json`](../proto/vectors.json)):

```
POWER ON    ff03eb 0001 0001 00000000 02 0000000000 00000001 00000000 00
POWER OFF   ff03eb 0001 0001 00000000 02 0000000000 00000001 00000000 01
TEMP 24 °C  ff03eb 0001 0001 00000000 02 0200000000 00000001 00000000 18
MODE COOL   ff03eb 0001 0001 00000000 02 0300000000 00000001 00000000 01
FAN HIGH    ff03eb 0001 0001 00000000 02 0100000000 00000001 00000000 03
```

## Passkey

Control is gated behind a 4-digit passkey, and reading status requires it too.
After connecting, send a `BLE_PASSKEY` (600) frame, `total_level = 1`, payload =
the four ASCII digits, `level[0]` = the first digit's byte. The unit answers on
`0xB003` with dpid `0x79` (passkey ack); only then are `AC_CTRL` commands
honoured. The factory default is `0000`; a never-paired or reset unit accepts it.

## Status: the `0xB003` notify stream

To receive status, write the CCCDs explicitly — `0x0100` to the `0xB003` CCCD
(handle 20) and `0x0200` to the `0xB004` CCCD (handle 23); a generic
`start_notify` is not enough. Then a write to `0xB002` (a `STATUS_DATA` request
works) triggers the unit to dump its state.

Each notification's value is ASCII text:

```
Poll:1160:55aa03070005 01 01 0001 00 11
Diag:1171:->100149c4 1
```

`Poll:<seq>:<hexframe>` carries state; `Diag:<seq>:…` carries diagnostics. The
`<hexframe>` is a standard Tuya datapoint frame:

```
55aa            header
03              version
07              command (0x07 = status report)
00 XX           body length, 2 bytes BE
  <dp unit>…    one or more datapoints
XX              checksum (sum of the preceding bytes, mod 256)
```

Each datapoint unit is `dpid(1) · type(1) · len(2 BE) · value(len)` — Tuya's
`0x01` bool, `0x02` 4-byte int, `0x04` enum. DPIDs seen on a live unit:

| dpid | meaning | dpid | meaning |
|---|---|---|---|
| `0x01` | power (`1`=on, `0`=off) | `0x69` | silent |
| `0x02` | temperature setpoint °C | `0x6A` | room temperature °C |
| `0x04` | mode (enum, below) | `0x6B` | coil temp *(inferred)* |
| `0x05` | fan speed (enum) | `0x6D` | display |
| `0x08` | eco *(inferred)* | `0x6E` | swing vertical |
| `0x19` | sleep | `0x6F` | swing horizontal |
| `0x1A` | health/ionizer *(inferred)* | `0x73` | defrost *(inferred)* |
| `0x1C` | **power draw (W)** | `0x75` | error/fault *(inferred)* |
| `0x67` | turbo | `0x79` | passkey ack |

The **status mode enum differs from the command map**: in status,
`0=auto · 1=cool · 2=heat · 3=dry · 4=fan`.

**Power is inverted the same way.** The `POWER` command takes `ON = 0`, but
status DPID `0x01` reports `1` for on and `0` for off. Verified live: in
fan-only mode, drawing 24 W with the compressor necessarily stopped, the unit
reported `0x01 = 1`; switched off at 19 W it reports `0`. So `0x01` is the
unit's own on/off state and not a compressor flag — whether it is *working* has
to come from the power draw (DPID `0x1C`), because a unit idling at its setpoint
is still on.

### The frame slot, and tearing

`<hexframe>` is always **30 hex characters — a fixed 15-byte slot**, right-padded
with `00` when the frame is shorter. One datapoint per notification, so a 5-byte
body is padded by three bytes and an 8-byte body fills the slot exactly:

```
Poll:1919:55aa03070005 1a010001 00 2a 000000   <- padded
Poll:1925:55aa03070008 6a020004 0000001e 9f    <- exact fit, room temp = 30 C
```

**The unit tears that slot.** A notification can carry a part-written frame with
the start of the next one behind it, e.g. `55aa030700086a02000455aa0307`: a room
temperature datapoint header whose four value bytes are the next frame's
`55 aa 03 07`. A decoder that trusts the declared length reports that as a
reading of 1437205255.

So **verify both the declared body length and the checksum** before believing any
datapoint, and treat a datapoint that overruns the body as a torn frame:

* the frame must hold `6 + length + 1` bytes, and
* `sum(bytes[:6 + length]) & 0xFF` must equal the checksum byte at `6 + length`.

The padding is outside the body and outside the checksum, so it needs no special
handling once the length is respected.


## Reaching the unit through an ESPHome proxy

An ESP32 running [`bluetooth_proxy`](https://esphome.io/components/bluetooth_proxy.html)
works as a remote GATT client via `aioesphomeapi` (see
[`../tools/dump_gatt.py`](../tools/dump_gatt.py)). Two non-obvious traps:

- **The ESP routes the connection response only to the API connection that
  subscribed to advertisements.** Call
  `subscribe_bluetooth_le_raw_advertisements()` before
  `bluetooth_device_connect()`, or the response goes to whichever client
  subscribed first (usually Home Assistant) and your client times out.
- **`feature_flags` is not read from `device_info()`.** Pass
  `info.bluetooth_proxy_feature_flags` into `bluetooth_device_connect()`
  explicitly, or it defaults to 0 and the call raises `ValueError`.

Taking the advertisement subscription takes BLE proxying away from Home Assistant
and does not always hand back; keep such sessions short, and reload the ESPHome
config entry if the scanner stays stuck.
