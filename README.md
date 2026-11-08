# hlmblue

Local control of a Bluetooth air conditioner — a Home Assistant integration and a
web app, no vendor cloud. The wire protocol is documented under [`docs/`](docs/).

## Home Assistant

[![Open your Home Assistant instance and add this repository to HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ananthb&repository=hlmblue&category=integration)

Or add this repo to [HACS](https://hacs.xyz) as a custom repository (category
*Integration*), install **He A/C (BLE)**, then add it from Settings → Devices &
Services.

It reaches the A/C through an ESPHome Bluetooth proxy, which is not optional in
practice: the unit pushes its status as 41-byte notifications, and a Bluetooth
adapter on the Home Assistant host typically settles at an MTU of 23, where
those frames are dropped and the unit looks permanently switched off. A proxied
link negotiates 247. Any ESPHome device with `bluetooth_proxy` enabled that Home
Assistant already connects to is used automatically; **Configure** on the
integration pins one, or forces the host's own adapter.

## Web app

**[hlm.blue](https://hlm.blue)** — a Web Bluetooth remote. Chrome or Edge, with
the A/C in Bluetooth range.

## Licence

GPL-3.0-only. See [`LICENSE`](LICENSE).
