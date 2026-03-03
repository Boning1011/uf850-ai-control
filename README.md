# UFactory 850 AI-Driven Interactive Control

New-media art installation: a 6-axis robotic arm responds to audience behavior in real time.

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

Default mode is **Hand Tracking** (MediaPipe): camera captures hand positions and the arm follows in real time.

Common launch modes:

```bash
uv run python scripts/main.py                      # hand tracking (default)
uv run python scripts/main.py --vlm                 # VLM perception mode (Gemini)
uv run python scripts/main.py --vlm --no-camera     # VLM with mock camera
uv run python scripts/main.py --keyboard            # manual parameter control (no VLM)
uv run python scripts/main.py --no-web              # disable web dashboard
uv run python scripts/main.py --persona personas/default.yaml --ip 192.168.1.xxx
```

| Flag | Default | Description |
|------|---------|-------------|
| `--ip` | `127.0.0.1` | Arm IP (simulator or real) |
| `--persona` | `personas/default.yaml` | Persona YAML config |
| `--sensitivity` | `-1` | Collision sensitivity 0-5, -1=skip |
| `--vlm` | off | VLM perception mode (Gemini) instead of hand tracking |
| `--keyboard` | off | Manual parameter control instead of camera input |
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

┌─────────┐     ┌──────────────┐
│ Camera   │────>│ MediaPipe    │──── (hand tracking mode) ────> Motion Gen ────> Arm
│          │     │ HandLandmark │
└─────────┘     └──────────────┘
```

Two input modes, switchable at launch:

- **Hand Tracking** (default): MediaPipe detects hand positions at camera framerate, arm follows directly
- **VLM Perception** (`--vlm`): Gemini analyzes camera frames at ~1 Hz, outputs 6 continuous behavioral parameters, trigger engine maps to motion modes

### Hand Tracking Pipeline Details

Beyond the core screen-to-arm coordinate mapping, the hand tracking pipeline includes:

- **Two-Hand Role Split** — Right hand controls XYZ position; left hand controls pitch. Single hand defaults to position control with automatic boundary lean.
- **Center-Weighted Amplification** — The center 50% of the camera frame maps to the arm's full left-right range, making small movements in the natural interaction zone cover the entire workspace.
- **Boundary Lean** — When the arm approaches workspace edges, it tilts toward that direction, visually mimicking a "reaching" gesture.
- **Soft Margin Deceleration** — Speed gradually reduces to 30% near boundaries using a smoothstep curve, preventing abrupt stops.
- **Per-Frame Velocity Clamping** — Each servo frame enforces max displacement by Euclidean distance (position) and per-axis with angle wrapping (rotation).
- **Mode-Switch Speed Boost** — A brief speed burst on entering tracking mode so the arm quickly catches up to the hand.
- **Organic Micro-Motion** — Tiny sinusoidal offsets on X/Z keep the arm feeling alive even when the hand is still.
- **No-Hand Fallback** — When hands leave the frame, the arm smoothly transitions into a calm breathing motion at center.
- **Hard Safety Clamping** — All coordinates are hard-clamped to the workspace envelope before reaching the SDK.
- **Auto Error Recovery** — Servo errors trigger automatic recovery: clear error, re-enable, return to center, re-enter servo mode with retries.

---

## Core Modules

All source code under `scripts/`. Here's what each module does:

### `main.py` — Entry Point

Orchestrates the full pipeline. Parses CLI args, loads persona config, initializes all modules, and runs the 25 Hz servo loop. Supports three input modes: hand tracking (default), VLM perception (`--vlm`), and keyboard (`--keyboard`).

### `arm_controller.py` — Arm Control

Low-level xArm SDK wrapper. Handles:
- Connection and initialization (with single-instance file lock)
- Servo mode (Mode 1) for 25 Hz streaming position commands
- Velocity clamping: limits frame-to-frame displacement to enforce speed bounds
- Safety boundary clamping: clips XYZ to safe workspace cube
- Automatic error recovery via callback: `clean_error()` → `motion_enable()` → `set_mode()` → `set_state()`

All arm operations in the project go through this module.

### `perception.py` — VLM Perception

Camera capture and cloud VLM integration. Components:
- **`FrameBuffer`**: thread-safe ring buffer for JPEG frames (shared between camera and dashboard)
- **`CameraThread`**: background capture at configurable FPS (handles Windows DirectShow quirks)
- **`VLMProvider`**: abstract interface for VLM backends
- **`GeminiProvider`**: Gemini 2.5 Flash implementation — sends multi-frame batches, receives structured JSON output via Pydantic schema (`VLMOutput`: energy, mood, presence, urgency, gesture, scene_description)

### `motion_gen.py` — Motion Generation

Parametric motion pattern generator. 5 distinct modes, each with qualitatively different movement:

| Mode | Behavior |
|------|----------|
| `CALM` | Gentle breathing at center, small sinusoidal oscillation |
| `ALERT` | Reach far forward, slow Y sweep |
| `EXCITED` | Big dramatic sweeping arcs, full workspace Lissajous |
| `PLAYFUL` | Extend forward + rapid pitch oscillation (head nod effect) |
| `TRACK` | Real-time hand following via MediaPipe coordinates |

Mode transitions are instant with a speed boost during the first 0.8s after switch. Takes perception state as input, outputs `(x, y, z, pitch, yaw, speed)` target per frame.

### `triggers.py` — Trigger & Mode Engine

Two components that sit between perception and motion:
- **`TriggerEngine`**: detects behavioral state transitions — 4 numeric triggers (`HIGH_ENERGY`, `SUDDEN_MOVEMENT`, `PLAYFUL_MOOD`, `HIGH_PRESENCE`) based on threshold crossings, plus 10 gesture triggers (e.g. `GESTURE_HEART`, `GESTURE_WAVE`) from VLM gesture detection
- **`ModeEngine`**: maps active triggers to motion modes via priority-ordered rules (gesture triggers have highest priority, e.g. heart gesture → PLAYFUL, rock gesture → EXCITED)

### `persona.py` — Config & State

Three components:
- **`PerceptionState`**: dataclass with 4 continuous behavioral parameters (energy, mood, presence, urgency), all auto-clamped to valid ranges
- **`StateHolder`**: thread-safe wrapper with exponential moving average smoothing (default α=0.3) for graceful transitions between VLM updates
- **`PersonaConfig`**: loads `personas/*.yaml` — defines VLM system prompt, personality hints, motion parameters (center, amplitude, frequency, speed), safety bounds, and camera settings

### `hand_tracking.py` — Hand Tracking

MediaPipe HandLandmarker running in a background thread. Captures camera frames, detects hand landmarks (21 points), normalizes wrist coordinates to 0–1 range, and delivers results via callback. Also pushes JPEG frames to the shared `FrameBuffer` so the dashboard camera feed stays alive. Used as the default input mode for TRACK motion.

### `web_server.py` — Dashboard

FastAPI-based real-time monitoring server, runs in a background thread alongside the main pipeline:
- `/` — HTML dashboard (served from `scripts/static/index.html`)
- `/video_feed` — MJPEG camera stream
- `/ws` — WebSocket pushing real-time state: VLM parameters, smoothed state, motion targets, active triggers, arm telemetry
- REST endpoints for pipeline control (pause/resume, mode switching, input mode selection)

Default: http://localhost:7860

---

## Persona System

Behavior is configured via YAML files in `personas/`. The default persona (`personas/default.yaml`) defines:

- **VLM prompt**: system prompt and personality hints that shape how Gemini interprets the scene
- **Motion parameters**: center position, amplitude/frequency ranges, speed scaling, attention range
- **Safety bounds**: XYZ workspace limits for boundary clamping
- **Camera settings**: device index, resolution, JPEG quality

Create new personas by copying `default.yaml` and adjusting parameters. Load with `--persona personas/my_persona.yaml`.

---

## Utility Scripts

Offline tools for workspace analysis and calibration (not part of the runtime pipeline):

### `scripts/path_validator.py` — Path Feasibility Testing

Loads a 3D path (JSON, CSV, or Houdini `.geo` format), validates reachability via IK checks, and uses binary search on the simulator to find maximum feasible speed. Also contains the `houdini_to_arm()` coordinate mapping function.

```bash
uv run python scripts/path_validator.py path_data/example.json
uv run python scripts/path_validator.py path_data/houdini_path.json --ik-only
uv run python scripts/path_validator.py path_data/my_path.csv --speed-max 800
```

### `scripts/probe_workspace.py` — Workspace Envelope Probing

Systematically tests IK feasibility across a grid of positions and orientations (no physical movement — pure IK computation). Outputs structured reference data to `doc/arm_workspace_data.json`.

```bash
uv run python scripts/probe_workspace.py              # standard grid
uv run python scripts/probe_workspace.py --fine        # 25mm grid (slower)
uv run python scripts/probe_workspace.py --presets-only
```

### `scripts/axis_calibrate.py` — Coordinate Axis Calibration

Interactive tool that moves the arm to 7 reference positions (center, ±X, ±Y, ±Z) one at a time, for visual verification of coordinate directions in UFactory Studio.

```bash
uv run python scripts/axis_calibrate.py
uv run python scripts/axis_calibrate.py --ip 192.168.1.xxx
```

---

## Error Recovery

When the arm hits a collision, speed limit, or any error, the SDK auto-recovers:

```
clean_error() → motion_enable(True) → set_mode(0) → set_state(0)
```

Key error codes: 22 (self-collision), 23 (joint limit), 24 (speed limit), 31 (collision current), 35 (safety boundary).

---

## Reference Documents

| Document | Content |
|----------|---------|
| [doc/Design Brief.md](doc/Design%20Brief.md) | Architecture details and development phases |
| [doc/arm_reference.md](doc/arm_reference.md) | Arm workspace envelope, presets, RPY freedom (IK-verified) |
| [doc/Devlog.md](doc/Devlog.md) | Development timeline and decision history |
| [doc/Environment.md](doc/Environment.md) | Hardware, accounts, tooling details |
| [doc/UFactory Studio Docker Setup.md](doc/UFactory%20Studio%20Docker%20Setup.md) | Docker simulator setup and commands |
