# UFactory 850 AI-Driven Interactive Control

New-media art installation: a 6-axis robotic arm responds to audience behavior in real time.

See [doc/Design Brief.md](doc/Design%20Brief.md) for architecture details, [doc/Devlog.md](doc/Devlog.md) for development history.

---

## Quick Start

### 1. Environment Setup (first time only)

```bash
python -m venv .venv
```

Activate venv:

```bash
# Git Bash / Linux / macOS
source .venv/bin/activate

# Windows CMD
.venv\Scripts\activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install xarm-python-sdk
```

### 2. Start Docker Simulator

```bash
docker run -d --name uf_software \
  -p 18333:18333 \
  -p 502:502 -p 503:503 -p 504:504 \
  -p 30000:30000 -p 30001:30001 -p 30002:30002 -p 30003:30003 \
  --entrypoint //bin/bash \
  danielwang123321/uf-ubuntu-docker \
  -c "//xarm_scripts/xarm_start.sh 6 12; sleep infinity"
```

> Web UI: http://localhost:18333

If container already exists:

```bash
docker start uf_software
```

Stop simulator:

```bash
docker stop uf_software
```

### 3. Run Scripts

Test connection (one-shot move):

```bash
.venv/Scripts/python.exe scripts/test_sim_connection.py
```

Continuous figure-8 motion with auto error recovery:

```bash
.venv/Scripts/python.exe scripts/loop_motion.py --speed 80
```

Full options:

```bash
.venv/Scripts/python.exe scripts/loop_motion.py --ip 127.0.0.1 --speed 100 --sensitivity 0
```

| Flag | Default | Description |
|------|---------|-------------|
| `--ip` | `127.0.0.1` | Arm IP (simulator or real) |
| `--speed` | `100` | Movement speed (mm/s) |
| `--sensitivity` | `0` | Collision sensitivity 0-5 (0=off, real arm only) |

> Linux / macOS: replace `.venv/Scripts/python.exe` with `.venv/bin/python`.

---

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/test_sim_connection.py` | Verify SDK-to-simulator connection |
| `scripts/loop_motion.py` | Continuous figure-8 + automatic error recovery |

---

## Error Recovery

When the arm hits a collision, speed limit, or any error, the SDK can auto-recover without clicking the web UI popup:

```
clean_error() → motion_enable(True) → set_mode(0) → set_state(0)
```

`loop_motion.py` implements this via `register_error_warn_changed_callback` — errors are caught and cleared automatically.

Key error codes: 22 (self-collision), 23 (joint limit), 24 (speed limit), 31 (collision current), 35 (safety boundary).
