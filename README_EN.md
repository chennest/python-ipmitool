[中文文档](./README.md)

# python-ipmitool

Server fan control via IPMI. The repository contains two parts:

| Component | Location | Status | Description |
|---|---|---|---|
| **GPU Fan Console** | `app/` + `frontend/` | Current main project | Web console that regulates chassis fans in a closed loop by GPU temperature (FastAPI + Vue3) |
| **Fan Control Scripts** | `fancontroller.py` etc. | Legacy, still usable | Command-line scripts, CPU-temperature-based fan control (Dell 730 etc.) |

## GPU Fan Console

### What it does

Target platform: **ASRock Rack EPYCD8** (BMC firmware 2.20). Passive cards like the
Tesla T10 rely entirely on chassis fans for cooling, so the console reads GPU
temperature, computes a duty cycle from an editable step curve with hysteresis,
and writes it to the BMC via `ipmitool raw 0x3a 0x01`.

- **Data sources**: GPU temperature from a local DCGM exporter (`:9400`), fan speed
  from ipmi_exporter (`:9290`), CPU temperature from node_exporter (`:9100`, requires
  `--collector.hwmon`) — all read directly from the exporters; historical trends go
  through Prometheus. Falls back to `nvidia-smi` when DCGM is unavailable
- **Control curve**: piecewise curve + hysteresis band (rising temperature applies
  immediately; falling temperature must leave the hysteresis band before downshift,
  avoiding the "helicopter effect"). Editable in the UI with a **live preview**
  (`/api/curve/preview` runs the real algorithm)
- **Three modes**: auto (curve) / manual (fixed duty from the UI) / BMC auto
- **GPU ↔ fan-slot assignments**: each fan slot is bound to a GPU; the highest
  temperature of the bound GPU drives the speed. Unassigned slots go back to the BMC
- **State lives in SQLite**: mode, curve, assignments and audit log all persist in
  `app/data/fan-console.db`, the single source of truth; `app/config.yaml` is only
  a first-run seed
- **Safety net (three layers)**:
  1. On shutdown / crash / SIGTERM, `SafetyGuard` hands all managed fan slots back to BMC auto
  2. In-process `atexit` second layer
  3. **Independent heartbeat watchdog** (systemd timer, every 2 min): if the heartbeat
     expires it force-writes `8×0x00` — covering even SIGKILL / power loss
- **Dashboard**: GPU cards (temperature / SM clock), fan speeds, separate temperature
  and fan-speed trend charts, audit log

### Repository layout

```
app/
  main.py          # Entry: one process = control loop + API + static hosting
  api.py           # REST + WebSocket routes
  controller.py    # Control loop (15 s per tick)
  curve.py         # Piecewise curve + hysteresis
  ipmi.py          # raw 0x3a 0x01 command family
  sensors.py       # DCGM / ipmi_exporter / nvidia-smi / Prometheus readers
  safety.py        # Safety guard
  store.py         # SQLite persistence
  runtime.py       # Resource paths (source run = app/ dir; frozen = exe dir)
  config.yaml      # Configuration (template with example addresses; seed for first run)
  deploy/          # systemd units (main service + watchdog) and watchdog script
  data/            # fan-console.db (generated at runtime, never overwrite)
  static/          # Frontend build output (npm run build lands here)
  tests/           # Unit tests
frontend/          # Vue3 + TS + Vite + Tailwind (Dashboard / Fans / Settings)
deploy.sh          # One-click deploy script
DEPLOY.md          # Deployment manual (pitfalls, rollback, verification checklist)
```

### Run locally

```bash
# 1) Build the frontend (output goes straight into app/static/)
cd frontend && npm install && npm run build && cd ..

# 2) Install backend dependencies
pip install -r app/requirements.txt

# 3) Start (one process serves pages + API + control loop)
python -m uvicorn app.main:app --host 0.0.0.0 --port 8765
# Open http://127.0.0.1:8765
```

Frontend development with hot reload:

```bash
cd frontend && npm run dev    # vite on :5173, /api proxied to 127.0.0.1:8765
```

Run tests:

```bash
python -m unittest discover -s app/tests -t . -v
```

### API overview

| Method | Path | Description |
|---|---|---|
| GET | `/api/status` | Current state (mode, managed slots, temperatures, duty) |
| GET | `/api/gpus` / `/api/fans` | GPU / fan lists |
| GET/PUT | `/api/curve` | Read/write the control curve |
| POST | `/api/curve/preview` | Curve preview (real algorithm) |
| GET | `/api/history` | Historical trends (from Prometheus) |
| GET/PUT | `/api/assignments` | GPU ↔ fan-slot assignments |
| POST | `/api/mode` | Switch auto / manual / bmc-auto |
| POST | `/api/manual` | Manually set duty cycle |
| POST | `/api/restore-auto` | Hand everything back to BMC auto |
| GET | `/api/audit` | Audit log |
| GET | `/api/health` | Health check |
| WS | `/ws` | Live push |

### Deployment

Production target is Linux + systemd (in-band `/dev/ipmi0` access requires root).
**Daily updates are one command**:

```bash
CONN=<ssh-connection> ./deploy.sh                 # full: backend + frontend, restart + self-check
CONN=<ssh-connection> ./deploy.sh --static-only   # frontend only (no backend change, no restart)
```

First-time deployment, pitfalls, rollback and the verification checklist live in
**[DEPLOY.md](./DEPLOY.md)**. Last-resort fallback: send
`ipmitool raw 0x3a 0x01 0x00 0x00 0x00 0x00 0x00 0x00 0x00 0x00` to the BMC to hand
all fan slots back to BMC auto (can be done from the BMC's own management address,
no host OS required).

### CI & releases (GitHub Actions)

Every push / PR runs backend tests, frontend type-checked build and produces a
deployable bundle artifact. Pushes to main additionally build **standalone
executables for Windows and Linux** (PyInstaller); pushing a `v*` tag automatically
publishes a GitHub Release with all artifacts. See
[.github/workflows/ci.yml](./.github/workflows/ci.yml).

---

## Fan Control Scripts (Legacy CLI)

Interface-free command-line version: monitors CPU temperature and adjusts fan speeds
per predefined ranges. Works on Windows and Linux. Fan speed and temperature readings
are unified through Prometheus while write commands remain per-model, with
multi-server, multi-threading, daily log rotation and optional e-mail alerting
(off by default).

### Compatible servers

| Brand | Model | Compatible | Type Name |
|:---:|:---:|:---:|:---:|
| Dell | 730XD | Y | `dell730` |
| Dell | 730 | Y | `dell730` |
| ASRock Rack | EPYCD8 | Y | `epycd8` |

### How to use

> On Linux install `ipmitool` first: Debian-based `apt install -y ipmitool`,
> Red Hat-based `yum install -y ipmitool`. On Windows use the bundled
> `ipmitool/ipmitool.exe`.

```bash
git clone https://github.com/chennest/python-ipmitool.git
cd python-ipmitool
pip install -r requirements.txt

# Copy and edit the config (IP addresses only, no domains; use "local" for in-band)
cp fan_settings.yaml.template fan_settings.yaml
```

Key fields of `fan_settings.yaml` (full example in the template file):

```yaml
auto: true                  # true = auto, false = manual
interval: 60                # control interval (seconds)
log_backup_count: 30        # log retention (days)
windows_ipmi_tool_path: ".\\ipmitool\\ipmitool.exe"
alert:                      # e-mail alerts (optional, off by default)
  enabled: false
  fan_speed_threshold: 10000
  max_failed_attempts: 3
  email: { ... }            # SMTP config, multiple recipients, 1 h anti-spam
prometheus:
  base_url: "http://<your-prometheus>:9090"
servers:
  - type: dell730
    ip: "192.0.2.10"        # example address, replace with yours
    user: root
    password: "your-password"
    temperature_ranges:     # temperature range → per-fan percentages
      - { min_temp: 0,  max_temp: 60, fan_speeds: [20, 20, 20, 20, 20, 20] }
      - { min_temp: 61, max_temp: 80, fan_speeds: [25, 25, 25, 25, 25, 25] }
```

Two run modes:

```bash
# Mode 1: loop control (recommended for long-running background service)
python fancontroller.py

# Mode 2: run once (recommended for cron / systemd timer / Task Scheduler)
python fancontroller_once.py
# crontab example: every 10 minutes
# */10 * * * * /usr/bin/python3 /path/to/python-ipmitool/fancontroller_once.py
```

For long-running Linux setups configure a systemd service
(`/etc/systemd/system/fancontroller.service`, `After=network.target` +
`Restart=always`; logs via `journalctl -u fancontroller -f`).

> Alert triggers: fan speed above the threshold (default 10000 RPM) or consecutive
> failures reaching the limit (default 3); per-server alert interval is at least
> 1 hour. Gmail requires an app password. Controller implementation details are in
> [CLAUDE.md](./CLAUDE.md).

---

## Documentation index

- [DEPLOY.md](./DEPLOY.md) — GPU Fan Console deployment manual (one-click script, pitfalls, rollback, verification checklist)
- [CLAUDE.md](./CLAUDE.md) — repository architecture (both generations, how to add a new model)
- [README.md](./README.md) — Chinese documentation

## Contributing

Issues and pull requests are welcome. For questions or suggestions, please open a
GitHub issue.

## License

This project is licensed under **GPL-3.0** — see the [LICENSE](./LICENSE) file.

## Credits

[perryclements/r410-fancontroller: Python fan controller for Dell R410 server (GitHub.com)](https://github.com/perryclements/r410-fancontroller)

[ipmitool/ipmitool: An open-source tool for controlling IPMI-enabled systems (GitHub.com)](https://github.com/ipmitool/ipmitool)
