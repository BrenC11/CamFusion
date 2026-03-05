# CamFusion (Home Assistant Supervisor Add-on)

`CamFusion` is a Docker-based Home Assistant add-on that combines 2-4 camera inputs into one composite panoramic feed.

## Endpoints

- `GET /stream.mjpeg` -> multipart MJPEG stream
- `GET /snapshot.jpg` -> latest JPEG frame
- `GET /healthz` -> runtime stats and status
- `POST /ring/login` -> authenticate and store Ring token
- `GET /ring/accounts` -> list stored Ring accounts
- `GET /ring/devices?account=default` -> list Ring cameras for account

Default port: `8099`

## Features

- Source adapters (camera-agnostic):
  - `rtsp` URL
  - `ha_camera` entity (`camera.some_entity`) via Home Assistant Core API
  - `file` path (looped)
  - `ring` camera via direct Ring login (ring-doorbell auth + snapshot API)
- Modes:
  - `dashboard` (default): low CPU, snapshot-driven, 1-5 FPS on Raspberry Pi
  - `live`: ffmpeg `xstack` compositing, target 10-15 FPS, auto fallback if stream ingestion fails
  - `experimental`: optional OpenCV warp + feather blend (better on NUC/mini-PC)
- Layouts:
  - `hstack`
  - `3x1`
  - `2x2` (for 4 cameras)
- Per-camera crop and scale
- Source failure placeholder (`NO SIGNAL`) while keeping stream alive

## Install As Local Add-on

1. Copy this folder into your Home Assistant add-ons directory, for example:
   - `/addons/camfusion`
2. In Home Assistant, go to **Settings -> Add-ons -> Add-on Store**.
3. Click the menu (top-right) -> **Check for updates**.
4. Open `CamFusion` in the local add-ons list.
5. Configure options and start the add-on.
6. Open stream URLs:
   - `http://<HA_HOST>:8099/stream.mjpeg`
   - `http://<HA_HOST>:8099/snapshot.jpg`
   - `http://<HA_HOST>:8099/healthz`

## Configuration

Core options in add-on config:

- `mode`: `dashboard | live | experimental`
- `fps`: integer
- `layout`: `hstack | 2x2 | 3x1`
- `output_width`: e.g. `1280` or `1920`
- `output_height`: `0` for auto height
- `blend_feather_px`: optional seam blur width (experimental mode)
- `inputs`: list of 2-4 camera definitions

Each input supports:

- `type`: `rtsp | ha_camera | file | ring`
- `url` for RTSP
- `entity_id` for Home Assistant camera entities
- `path` for local file input
- `account` for Ring account alias (default `default`)
- `device_id` or `device_name` for Ring camera selection
- `snapshot_retries` and `snapshot_delay` for Ring snapshot polling
- `crop`: `left/right/top/bottom` as fraction or percent
- `scale`: zoom factor (default `1.0`)
- `warp_affine` (experimental): `a,b,c,d,e,f`
- `warp_perspective` (experimental): `9` comma-separated values

## Example Configs

### Two RTSP cameras (public demo)

```yaml
mode: dashboard
fps: 3
layout: hstack
output_width: 1280
output_height: 0
inputs:
  - type: rtsp
    url: rtsp://wowzaec2demo.streamlock.net/vod/mp4:BigBuckBunny_115k.mov
  - type: rtsp
    url: rtsp://wowzaec2demo.streamlock.net/vod/mp4:BigBuckBunny_115k.mov
```

### Two Home Assistant camera entities (including Ring entities via HA integration)

```yaml
mode: live
fps: 12
layout: hstack
output_width: 1920
output_height: 0
inputs:
  - type: ha_camera
    entity_id: camera.ring_front_door
  - type: ha_camera
    entity_id: camera.garden
```

### Video file loop demo

```yaml
mode: dashboard
fps: 4
layout: hstack
output_width: 1280
inputs:
  - type: file
    path: /media/demo1.mp4
  - type: file
    path: /media/demo2.mp4
```

### Direct Ring login + Ring cameras

1. Authenticate Ring account:

```bash
curl -X POST http://<HA_HOST>:8099/ring/login \\
  -H 'Content-Type: application/json' \\
  -d '{"account":"default","username":"you@example.com","password":"YOUR_PASSWORD"}'
```

If Ring returns 2FA required, call again with `twofa_code`:

```bash
curl -X POST http://<HA_HOST>:8099/ring/login \\
  -H 'Content-Type: application/json' \\
  -d '{"account":"default","username":"you@example.com","password":"YOUR_PASSWORD","twofa_code":"123456"}'
```

2. List available Ring cameras:

```bash
curl "http://<HA_HOST>:8099/ring/devices?account=default"
```

3. Use Ring inputs in add-on config:

```yaml
mode: dashboard
fps: 3
layout: hstack
output_width: 1280
inputs:
  - type: ring
    account: default
    device_name: Front Door
  - type: ring
    account: default
    device_name: Back Garden
```

## Raspberry Pi Performance Notes

- `dashboard` mode is recommended for Pi (`1-5 FPS`) and stays stable under moderate CPU.
- `live` mode can run on Pi for light setups but may need lower output width/FPS.
- `experimental` mode uses OpenCV warp/blend and is best on mini-PC/NUC hardware.

## Troubleshooting

- `ha_camera` needs add-on permissions enabled (`homeassistant_api: true`, `hassio_api: true`).
- For Home Assistant entities, snapshot endpoint used:
  - `http://supervisor/core/api/camera_proxy/<entity_id>`
- LIVE stream endpoint used when available:
  - `http://supervisor/core/api/camera_proxy_stream/<entity_id>`
- If `camera_proxy_stream` fails or stalls, `live` mode automatically falls back to snapshot compositing at higher FPS.
- Ring source notes:
  - If not logged in, source panels show `NO SIGNAL` until `POST /ring/login` succeeds.
  - If Ring needs OTP, `/ring/login` returns `status=requires_2fa` and HTTP `428`.
  - Ring tokens are stored in `/data/ring_accounts.json`.
- If a source fails, that panel shows `NO SIGNAL` and the composite stream remains online.
- Check add-on logs for source errors and `ffmpeg` warnings.

## Security Notes

- Uses Supervisor token from add-on environment (`SUPERVISOR_TOKEN` / `HASSIO_TOKEN`).
- Token values are never printed in logs.
