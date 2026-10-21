# nvr-event-push

A small middle-man that listens to one or more NVRs for motion and smart events. It supports **Hikvision-based NVRs** (Hikvision, Annke, and other ISAPI-compatible rebrands) and **Dahua-based NVRs** (Dahua, Amcrest, and other Dahua rebrands), and you can mix brands. When an event matches one of your rules, it sends a customizable **HTTP GET** to another server, such as a home-automation hub that turns on the outdoor lights.

```
NVR alertStream ──► nvr-event-push ──► GET http://your-server/motion?camera=Driveway&event=VMD
```

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .            # or: pip install -e '.[dev]' for tests
```

**Windows**: install Python 3.10+ from python.org, then double-click `nvr-event-push.cmd` to open the menu. The first run creates `.venv` and installs everything. From a Command Prompt, pass the usual arguments, e.g. `nvr-event-push.cmd run`. The config is kept in `config.yaml` next to the `.cmd` file unless you pass `-c` or set `NVR_EVENT_PUSH_CONFIG`.

## Prepare the NVR

Each NVR has a `brand`: `hikvision` (the default) or `dahua`. The interactive menu asks for it when you add the NVR.

**Hikvision / Annke**: for each camera you want to forward, open its **Motion** (or Smart Event: Line Crossing / Intrusion / Region Entrance / Region Exiting) settings. Under *Linkage Method* / *Actions*, enable **Notify Surveillance Center** (it may be labeled "Notify NVR/CMS"). If you skip this, the NVR won't put the event on its event stream.

**Dahua / Amcrest**: enable **Motion Detection** (or an IVS rule such as Tripwire / Intrusion, or Smart Motion Detection) for each camera you want to forward. Every enabled detection is sent on the event stream, so no extra linkage setting is needed.

Use an NVR user that has remote access. Standard Digest authentication is supported, and Basic auth is available as a fallback.

## Use

```bash
nvr-event-push            # interactive menu: set up NVRs, rules, test, watch, run
nvr-event-push run        # headless: listen and forward (for services)
nvr-event-push watch      # print live events without forwarding (--raw for XML)
nvr-event-push watch --nvr Garage --event VMD,linedetection   # only these NVRs / event types
nvr-event-push cameras    # list channel numbers and names on each NVR
nvr-event-push test-url "Driveway lights"   # fire one test request for a rule (--off for its off URL)
```

`watch` shows everything by default. `--nvr NAME` limits it to certain NVRs (only those are connected to), and `--event TYPE` limits it to certain event types. Both can be repeated or comma-separated. The menu's *Watch live events* asks the same questions and remembers your last answers.

Every command accepts `-c/--config PATH` (default `./config.yaml` or `$NVR_EVENT_PUSH_CONFIG`) and `-v` for debug logging.

The menu saves the config after every change. See `config.example.yaml` for the format if you'd rather edit it by hand.

### Multiple NVRs

Add as many NVRs as you like under **Manage NVRs** in the menu, or under `nvrs:` in the config. Each one needs a unique `name`, which is used in rules and log lines. `run` and `watch` listen to all of them at once. If one NVR is offline, the others keep working while it reconnects. If one NVR rejects the login, it is skipped and logged, and the others keep running.

Passwords can come from environment variables instead of the file. `NVR_PASSWORD_<NAME>` sets the password for one NVR: the name is uppercased and any other characters become `_`, so "Back garage" uses `NVR_PASSWORD_BACK_GARAGE`. `NVR_PASSWORD` sets it for every NVR that has no variable of its own.

Configs from earlier versions, with a single `nvr:` section, still load as one NVR called `NVR`. The menu writes them back in the new `nvrs:` format.

### Rules

Each rule has:
- **nvr** (optional): the name of the NVR to listen to. If it's left out, the rule listens to every NVR.
- **channels**: channel numbers on that NVR. An empty list means all cameras. If the rule has no `nvr`, the numbers apply to every NVR.
- **event types**: `VMD` (motion), `linedetection`, `fielddetection` (intrusion), `regionEntrance`, `regionExiting`. Other event names your NVR sends (check with `watch`) can be typed into the config.
- **targets** (optional): `human` / `vehicle`. This only works on cameras that report the target type (Hikvision AcuSense, Dahua/Amcrest Smart Motion Detection or IVS). If a rule has a target filter, events that don't report a target are skipped.
- **url**: the GET to send. Values are URL-encoded automatically. The URL can contain these placeholders:

  | Placeholder | Value |
  |---|---|
  | `{camera_name}` | Camera name from the NVR |
  | `{channel}` | Channel number |
  | `{event_type}` | Raw type, e.g. `VMD` |
  | `{event_label}` | Readable type, e.g. `Motion detection` |
  | `{target}` | `human` / `vehicle` / empty |
  | `{timestamp}` | Event time reported by the NVR |
  | `{nvr_name}` | Name of the NVR the event came from |
  | `{nvr_host}` | That NVR's address |
  | `{rule_name}` | Rule name |

- **cooldown**: A Hikvision NVR repeats "active" about once a second while motion continues. A Dahua NVR only reports when motion starts and stops, so ongoing events are repeated on each 5-second heartbeat instead. Each rule sends at most one request per camera per cooldown window. Requests are only sent when motion **starts**, never when it ends.
- **off_url** (optional) and **off_delay_seconds** (default 120): a second GET sent once no matching event has arrived for `off_delay_seconds`. Every matching event restarts the countdown, including events inside the cooldown, so the light stays on while motion continues. On Dahua NVRs, keep `off_delay_seconds` above about 10 seconds, because repeats only arrive every 5 seconds. It accepts the same placeholders as `url`. When the off request is sent, the cooldown is reset so the next motion turns the light back on right away. Cameras whose off URL comes out the same (one light watched by several cameras) share one countdown, so the light stays on until all of them are quiet. Putting `{camera_name}` or `{channel}` in the off URL gives each camera its own countdown. With several NVRs, add `{nvr_name}` as well if the same channel number on two NVRs should count separately. On shutdown, any pending off requests are sent right away. Test one with `test-url --off "Rule name"`.

Cooldowns are kept per camera, so channel 1 on one NVR and channel 1 on another are counted separately.

Dahua event codes are mapped to the same names, so a rule works with either brand. Codes that aren't listed here keep their Dahua name, for example `VideoBlind`:

| Dahua code | Event type | Target |
|---|---|---|
| `VideoMotion` | `VMD` | |
| `SmartMotionHuman` / `SmartMotionVehicle` | `VMD` | `human` / `vehicle` |
| `CrossLineDetection` | `linedetection` | from the event data |
| `CrossRegionDetection` | `regionEntrance` / `regionExiting` (direction Enter / Leave), otherwise `fielddetection` | from the event data |

### Retries

If a request fails with a connection error, a timeout, or HTTP 5xx/429, it is retried `defaults.retries` times (default 2). The wait between attempts starts at 1 second and doubles each time. Other 4xx responses, such as a wrong URL or a rejected token, are not retried, because they would fail the same way again. Set `retries: 0` to turn retries off.

## Run as a service

`run` reconnects to each NVR on its own, with backoff, if that NVR reboots or the network drops. It also exits cleanly on SIGTERM.

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

- **Nothing shows in `watch`**: on Hikvision, check that *Notify Surveillance Center* is enabled for that camera's event. On Dahua, check that the detection itself is enabled. Then walk in front of the camera.
- **Wrong channel numbers**: on Hikvision NVRs the tool uses `dynChannelID` (it matches the D1, D2… numbering in `cameras`). Dahua reports channels from 0, and the tool adds 1 so the numbers match the NVR's UI. Run `watch --raw` to see exactly what your NVR sends.
- **HTTP 401**: wrong credentials. Too many failures can lock the NVR account for a while, which is why auth errors aren't retried.

## Development

```bash
pip install -e '.[dev]'
pytest
python tests/fake_nvr.py --port 8099     # fake NVR (admin / secret) for manual testing
python tests/fake_nvr.py --brand dahua   # ...or a fake Dahua NVR
```
