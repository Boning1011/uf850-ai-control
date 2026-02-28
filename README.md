# UFactory 850 AI-Driven Interactive Control

New-media art installation: a 6-axis robotic arm responds to audience behavior in real time.

**Pipeline**: Camera → VLM Perception (Gemini) → Continuous Parameters → Parametric Motion → Arm

See [doc/Design Brief.md](doc/Design%20Brief.md) for architecture details, [doc/Devlog.md](doc/Devlog.md) for development history.

---

## Quick Start

### 1. Environment Setup (first time only)

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
uv sync
```

This creates `.venv/` and installs all dependencies from `uv.lock`. No manual Python install needed.

Set up API key:

```bash
cp .env.example .env   # then fill in GEMINI_API_KEY
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

### 3. Run

```bash
uv run python scripts/main.py
```

This starts the full pipeline: camera capture → VLM perception → parametric motion → arm control, plus a web dashboard at http://localhost:7860.

Common launch modes:

```bash
uv run python scripts/main.py --no-camera          # mock camera (no webcam needed)
uv run python scripts/main.py --keyboard            # manual parameter control (no VLM)
uv run python scripts/main.py --no-web              # disable web dashboard
uv run python scripts/main.py --persona personas/default.yaml --ip 192.168.1.xxx
```

| Flag | Default | Description |
|------|---------|-------------|
| `--ip` | `127.0.0.1` | Arm IP (simulator or real) |
| `--persona` | `personas/default.yaml` | Persona YAML config |
| `--sensitivity` | `-1` | Collision sensitivity 0-5, -1=skip |
| `--keyboard` | off | Manual parameter control instead of VLM |
| `--no-camera` | off | Mock camera (gray frames) |
| `--no-web` | off | Disable web dashboard |
| `--port` | `7860` | Web dashboard port |

---

## Architecture

```
┌─────────┐     ┌──────────────┐     ┌────────────┐     ┌────────────────┐     ┌─────┐
│ Camera   │────>│ VLM (Gemini) │────>│ Parameters │────>│ Motion Gen     │────>│ Arm │
│ 5 fps    │     │ ~1 Hz async  │     │ smoothed   │     │ 25 Hz servo    │     │     │
└─────────┘     └──────────────┘     └────────────┘     └────────────────┘     └─────┘
                                           │
                                     ┌─────┴──────┐
                                     │ Triggers / │
                                     │ Mode Engine│
                                     └────────────┘
```

Key modules (all under `scripts/`):

| Module | Role |
|--------|------|
| `main.py` | **Entry point** — orchestrates the full pipeline |
| `perception.py` | Camera capture + VLM perception (Gemini) |
| `motion_gen.py` | Parametric motion generation from perception state |
| `arm_controller.py` | xArm SDK wrapper, servo control, safety bounds |
| `triggers.py` | Event triggers + mode engine (idle/engaged/dramatic) |
| `persona.py` | Persona config loader (YAML → parameters) |
| `web_server.py` | Real-time web dashboard (FastAPI + SSE) |

---

## Utility Scripts

| Script | Purpose |
|--------|---------|
| `scripts/test_sim_connection.py` | Verify SDK-to-simulator connection |
| `scripts/loop_motion.py` | Continuous figure-8 + automatic error recovery |
| `scripts/path_validator.py` | Houdini → arm coordinate validation |

---

## Error Recovery

When the arm hits a collision, speed limit, or any error, the SDK auto-recovers:

```
clean_error() → motion_enable(True) → set_mode(0) → set_state(0)
```

Key error codes: 22 (self-collision), 23 (joint limit), 24 (speed limit), 31 (collision current), 35 (safety boundary).
