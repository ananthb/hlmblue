# Changelog

## 0.3.1

- **A proxy that stops answering no longer stops the polling.** After a power
  cut the ESP32 that bridges the unit can come back able to scan and answer the
  API while every BLE connection times out, and nothing on the old path capped
  discovery: the update never finished, Home Assistant only schedules the next
  poll once it has, so the entity sat **unavailable** until the integration was
  reloaded. Discovery, the descriptor writes and the notify subscriptions now
  give up after 20 s, and the whole update is capped, so a silent link costs one
  poll instead of every poll after it.

## 0.3.0

- **The A/C can now be read and controlled again from a Home Assistant host
  whose Bluetooth adapter cannot carry its status.** The unit pushes its status
  as 41-byte notifications, and a BlueZ link settles at the ATT MTU of 23, which
  is too small: the link connects, the login is accepted, and every status frame
  is dropped, so the entity sat there reporting **off** with no readings and
  nothing to command. The integration now talks to an ESPHome Bluetooth proxy
  instead, where the link negotiates 247.

  This needs no reconfiguration: an ESPHome proxy that Home Assistant already
  connects to is used automatically. **Settings → Devices & services → He A/C →
  Configure** lets you pin one, or force the host's own adapter back.

- The entity is no longer marked **unavailable** whenever status has gone quiet.
  The unit stops pushing within a minute of switching off and does not answer a
  status request while it is off, so time-based freshness made the entity vanish
  exactly when the automations needed it — a relay power cycle left it
  unavailable for good. A link is now considered usable if it is up and its MTU
  can carry a status frame; a running unit that goes quiet is still treated as
  broken, but an idle one stays available and controllable.

- Added a **transport** attribute, saying whether the link is on a proxy
  (`proxy:<name>`) or on this host's adapter, so "is it really using the proxy?"
  is answerable from the UI.

## 0.2.8

- Fix the swing selector: off and vertical were swapped, as were horizontal
  and both. The A/C takes vertical swing inverted, like power, and it was
  being sent the wrong way round.

## 0.2.7

- The A/C now shows as **idle** rather than **cooling** when it is switched on
  but has reached its setpoint. Its on/off datapoint was being read as a
  "working" flag, so anything switched on looked like it was cooling.
- On/off is now read from the A/C itself rather than worked out from its power
  draw, so the state follows the unit more closely — including when it is
  switched with its own remote.

## 0.2.6

- Fix switching from cool to off usually appearing to do nothing. The A/C was
  being switched off, but Home Assistant turned the entity straight back on: the
  status it reads a moment after the command caught the compressor still
  spinning down and took that as the unit running. Going via auto or dry first
  worked because by then the compressor had already stopped. A power command is
  now believed until the A/C's own report agrees with it.
- The action shown is no longer "cooling" when only the fan is turning. The
  threshold was 80 W, inside the 61-91 W this unit draws on the fan alone, so
  the fan run-on after switching off was reported as cooling.

## 0.2.5

- Fix wild temperature readings — the A/C sometimes sends a half-written status
  notification with the next one's bytes behind it, and those bytes were being
  read as a temperature. Home Assistant showed target temperatures like 21930 °C
  and room temperatures in the billions. Status frames are now checked against
  their own length and checksum before they are believed, and a reading outside
  the unit's range is discarded instead of replacing a good one.

## 0.2.4

- The Passkey field is now hidden by default too — reach it from the device
  page (Settings → Devices → the A/C) to view or change the passkey.

## 0.2.3

- The "New random passkey" button is now hidden by default too, so neither
  passkey button clutters dashboards — unhide from the entity settings if needed.

## 0.2.2

- The "Reset passkey to 0000" button is now hidden by default so it doesn't
  clutter dashboards — unhide it from the entity settings if you need it.

## 0.2.1

- Fix the swing selector picking the wrong mode. The A/C's swing read-back
  doesn't map cleanly to vertical/horizontal, which skewed the shown selection;
  the selector is now optimistic and reflects exactly what you chose.

## 0.2.0

- Climate: horizontal swing is now supported alongside vertical — the swing
  control offers off, vertical, horizontal and both, and each is sent to the unit.
- New auto-on and auto-off **timers** (in minutes; 0 cancels).
- Adding an A/C generates and sets a random passkey automatically; the passkey
  field on the add screen is optional, for a unit that already has one you know.
