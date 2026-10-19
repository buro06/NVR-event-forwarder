# nvr-event-push

A small middle-man that listens to a **Hikvision-based NVR** (Hikvision, Annke, and other ISAPI-compatible rebrands) for motion and smart events. When an event matches one of your rules, it sends a customizable **HTTP GET** to another server, such as a home-automation hub that turns on the outdoor lights.

```
NVR alertStream ──► nvr-event-push ──► GET http://your-server/motion?camera=Driveway&event=VMD
```

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .            # or: pip install -e '.[dev]' for tests
```

## Prepare the NVR

For each camera you want to forward, open its **Motion** (or Smart Event: Line Crossing / Intrusion / Region Entrance / Region Exiting) settings. Under *Linkage Method* / *Actions*, enable **Notify Surveillance Center** (it may be labeled "Notify NVR/CMS"). If you skip this, the NVR won't put the event on its event stream.

Use an NVR user that has remote access. The standard Hikvision Digest authentication is supported, and Basic auth is available as a fallback.

## Use

```bash
nvr-event-push            # interactive menu: set up NVR, rules, test, watch, run
nvr-event-push run        # headless: listen and forward (for services)
nvr-event-push watch      # print live events without forwarding (--raw for XML)
nvr-event-push cameras    # list channel numbers and names
nvr-event-push test-url "Driveway lights"   # fire one test request for a rule (--off for its off URL)
```

Every command accepts `-c/--config PATH` (default `./config.yaml` or `$NVR_EVENT_PUSH_CONFIG`) and `-v` for debug logging. The password can come from `$NVR_PASSWORD` instead of the file.

The menu saves the config after every change. See `config.example.yaml` for the format if you'd rather edit it by hand.

### Rules

Each rule has:
- **cameras**: NVR channel numbers. An empty list means all cameras.
- **event types**: `VMD` (motion), `linedetection`, `fielddetection` (intrusion), `regionEntrance`, `regionExiting`. Other event names your NVR sends (check with `watch`) can be typed into the config.
- **targets** (optional): `human` / `vehicle`. This only works on cameras that report the target type (AcuSense). If a rule has a target filter, events that don't report a target are skipped.
- **url**: the GET to send. Values are URL-encoded automatically. The URL can contain these placeholders:

  | Placeholder | Value |
  |---|---|
  | `{camera_name}` | Camera name from the NVR |
  | `{channel}` | Channel number |
  | `{event_type}` | Raw type, e.g. `VMD` |
  | `{event_label}` | Readable type, e.g. `Motion detection` |
  | `{target}` | `human` / `vehicle` / empty |
  | `{timestamp}` | Event time reported by the NVR |
  | `{nvr_host}` | NVR address |
  | `{rule_name}` | Rule name |

- **cooldown**: The NVR repeats "active" about once a second while motion continues. Each rule sends at most one request per camera per cooldown window. Requests are only sent when motion **starts**, never when it ends.
- **off_url** (optional) and **off_delay_seconds** (default 120): a second GET sent once no matching event has arrived for `off_delay_seconds`. Every matching event restarts the countdown, including events inside the cooldown, so the light stays on while motion continues. It accepts the same placeholders as `url`. When the off request is sent, the cooldown is reset so the next motion turns the light back on right away. Cameras whose off URL comes out the same (one light watched by several cameras) share one countdown, so the light stays on until all of them are quiet. Putting `{camera_name}` or `{channel}` in the off URL gives each camera its own countdown. On shutdown, any pending off requests are sent right away. Test one with `test-url --off "Rule name"`.

### Retries

If a request fails with a connection error, a timeout, or HTTP 5xx/429, it is retried `defaults.retries` times (default 2). The wait between attempts starts at 1 second and doubles each time. Other 4xx responses, such as a wrong URL or a rejected token, are not retried, because they would fail the same way again. Set `retries: 0` to turn retries off.

## Run as a service

`run` reconnects automatically with backoff if the NVR reboots or the network drops. It also exits cleanly on SIGTERM.

**systemd** (`/etc/systemd/system/nvr-event-push.service`):
```ini
[Unit]
Description=NVR event forwarder
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/nvr-event-push
ExecStart=/opt/nvr-event-push/.venv/bin/nvr-event-push run -c /opt/nvr-event-push/config.yaml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Troubleshooting

- **Nothing shows in `watch`**: check that *Notify Surveillance Center* is enabled for that camera's event. Then walk in front of the camera.
- **Wrong channel numbers**: on NVRs the tool uses `dynChannelID` (it matches the D1, D2… numbering in `cameras`). Run `watch --raw` to see exactly what your NVR sends.
- **HTTP 401**: wrong credentials. Too many failures can lock the NVR account for a while, which is why auth errors aren't retried.

## Development

```bash
pip install -e '.[dev]'
pytest
python tests/fake_nvr.py --port 8099     # fake NVR (admin / secret) for manual testing
```
