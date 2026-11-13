"""Constants for the A/C Remote (BLE) integration."""

DOMAIN = "hlmblue"

CONF_ADDRESS = "address"
CONF_PIN = "pin"
DEFAULT_PIN = "0000"
CONF_CONFIGURED = "configured"

# Where the link to the A/C comes from. "auto" prefers an ESPHome Bluetooth
# proxy and falls back to this host's adapter when there is none; "local"
# pins the adapter; "proxy" refuses to fall back.
CONF_TRANSPORT = "transport"
TRANSPORT_AUTO = "auto"
TRANSPORT_LOCAL = "local"
TRANSPORT_PROXY = "proxy"
CONF_PROXY = "proxy"

# The unit is request/response and drops idle connections; poll on this cadence.
POLL_INTERVAL = 30  # seconds
# hvac_action is "cooling"/"heating" above this power draw, else "idle".
# Measured on a live unit: 19-24 W idle or on the fan alone, 61-91 W while the
# fan runs on after a power-off, and 316 W upwards once the inverter compressor
# is working. 150 W sits in the gap, where 80 W sat inside the fan band and read
# fan run-on as cooling.
RUNNING_WATTS = 150
# How long a power command outranks the unit's own report of DPID 0x01, which is
# what on/off is actually read from. Only the status read that follows a command
# by a fraction of a second needs covering -- it may predate the unit acting --
# and the hold lifts as soon as the report agrees, so one poll is plenty.
POWER_SETTLE = POLL_INTERVAL  # seconds
# A link can be up and still carry nothing. That is exactly how a BlueZ link
# stuck at ATT MTU 23 behaves: it connects, the login and the status request are
# written, and the unit's 41-byte status frames never arrive, so the entity used
# to sit there reporting "off" with no readings. Below this MTU a status frame
# cannot fit (41 bytes of payload plus the 3-byte ATT header), so a link that
# negotiated less cannot be used whatever it claims.
STATUS_MTU = 44
# An idle unit is silent: it stops pushing within a minute of switching off and
# does not answer a status request while off. Silence only means fault while the
# unit is on, where status arrives every second or two.
STATUS_TIMEOUT = 3 * POLL_INTERVAL  # seconds
# A proxy that stops answering must cost one poll, not every poll after it:
# nothing in Home Assistant times a coordinator update out, so it is capped here.
# Comfortably above the per-step timeouts in proxy.py.
UPDATE_TIMEOUT = 4 * POLL_INTERVAL  # seconds
