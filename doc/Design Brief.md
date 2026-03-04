# UFactory 850 AI-Driven Interactive Control — Design Brief

## Project Context

We are building an interactive new-media art installation where a UFactory 850 (6-axis) robotic arm holds a tool above a screen, responding to audience presence and behavior in real time. The screen displays live-computed content via TouchDesigner.

## Previous Approach (Being Replaced)

Blender rig → Python script → CSV animation data → PyBullet collision check → UFactory Python API playback. State machine in TouchDesigner selects which pre-rendered animation to play based on sensor triggers.

Problems: every new behavior requires a full CG pipeline round-trip, animations are static with zero variation, collision checking is offline, and the arm has no intelligence — it is purely a playback device.

## New Architecture: Three-Layer Design

### Layer 1 — AI Perception & Decision

**Two parallel modes:**

**A. VLM Mode** — Google Gemini 2.5 Flash analyzes camera frames at ~1 Hz and outputs structured scene understanding via JSON schema (energy, mood, presence, urgency + gesture + scene_description). Multiple frames per call (~5 frames, 0.2s apart) allow detection of dynamic gestures. VLM runs asynchronously; PerceptionState is smoothed with exponential moving average (EMA factor 0.3) to avoid jitter.

**B. Hand Tracking Mode (Default)** — MediaPipe HandLandmarker runs locally in a background thread at full camera speed, with no cloud API dependency. Right hand controls arm XYZ position; left hand controls pitch orientation (pitch-only, ±50°). Camera feed is mirrored horizontally for natural interaction; MediaPipe hand labels are swapped accordingly.

Both modes feed into the same PerceptionState → TriggerEngine → ModeEngine → MotionGenerator pipeline. Modes can be switched at runtime from the web dashboard.

### Layer 2 — Procedural Motion Generation

All arm motion is computed in real-time as Cartesian coordinates (XYZ position + RPY orientation). No pre-rendered animations, no CSV files, no CG software in the loop.

The UFactory SDK's built-in IK solver converts Cartesian targets to joint angles. We never directly control individual joints.

Each behavioral state maps to a procedural motion recipe — parametric functions of time that define how the tool tip moves and how its orientation shifts. Layered on top: Perlin noise for organic micro-tremor, sinusoidal breathing, proximity-driven lean/tilt. This is the same procedural thinking as Houdini VEX — parameters and math generating motion, not keyframes.

State transitions use smooth blending (interpolation with easing) so the arm never snaps between behaviors.

**Body expression with 6 axes**: True null-space motion (tool tip locked, body moves freely) requires 7+ axes. On the 6-axis 850, we achieve a similar visual effect by locking tool XYZ position but varying RPY orientation — the IK solver produces different arm postures, creating visible "body language" while the tool stays roughly in place.

### Layer 3 — Safety & Execution

Three lines of defense, all independent of each other:

1. **Code-level coordinate clamping** — every target position is clamped before reaching the SDK (e.g. Z must be above screen surface + margin)
2. **UFactory firmware safety boundary** — a hardware-level failsafe configured in UFactory Studio; the controller refuses commands that violate the boundary regardless of what the software sends
3. **Built-in self-collision avoidance** — the UFactory controller firmware prevents the arm's own links from colliding; no configuration needed

This means even if the AI outputs a dangerous decision or the code has a bug, the arm physically cannot hit the screen or itself.

## Communication with TouchDesigner

The Python control script sends current state, tool position, orientation, and audience data to TouchDesigner via OSC. TD uses this to drive reactive screen content. The arm control and visual content are synchronized through shared state, not through TD controlling the arm.

## What This Architecture Is NOT

- It is **not** a VLA (Vision-Language-Action) end-to-end model. We do not use a single neural network to go from camera pixels to joint angles. That approach lacks the controllability and safety guarantees needed for a physical installation.
- It is **not** imitation learning (yet). Phase 4 may introduce learned motion policies via LeRobot/ACT/Diffusion Policy for specific gestures that are hard to express procedurally, but the core system is deterministic and math-driven.
- It is **not** reinforcement learning. No simulation training, no reward functions, no GPU clusters.

## Key Technical Constraints

- UFactory 850 is 6-axis — sufficient for Cartesian control but no true null-space redundancy
- The tool must never contact the screen — this is a hard physical constraint, not a soft preference
- The installation must be safe around the public — conservative speed/acceleration limits are mandatory
- Internet dependency for cloud VLM — system must fall back to safe idle behavior if API is unreachable
- Control loop runs at ~50 Hz locally; VLM perception runs at ~0.5–1 Hz asynchronously

## Development Phases

1. ✅ **~1 day** — Wire VLM perception (Gemini 2.5 Flash) to procedural arm control as PoC
2. ✅ **~1 day** — Replace CSV playback with procedural Cartesian control; servo mode; error recovery; safety layers
3. ✅ **~1 week** — Polish motion personalities (7 modes), web dashboard, MediaPipe hand tracking as default mode, two-hand control, tab UI
4. ✅ **2026-03-04** — First real hardware testing. Identified and fixed motion jitter (EMA smoothing on hand tracking input).
5. **Ongoing** — TouchDesigner OSC integration, tune motion on real hardware, optional: imitation learning (LeRobot) for organic gestures

## Real Hardware Testing Notes (since 2026-03-04)

Prior to 2026-03-04, all development and testing was done exclusively on the Docker simulator. Real hardware introduces considerations that the simulator does not surface:

- **Physical vibration & inertia** — Jitter that is invisible in simulation causes visible shaking on real hardware (table vibration, mechanical resonance). All motion targets must be smoothed; raw sensor input should never drive the arm directly.
- **Speed & acceleration tuning** — Simulator has no mass/friction; real arm dynamics require conservative speed limits and acceleration profiles tuned on actual hardware.
- **Safety margins** — Real hardware testing means physical consequences. Always verify on simulator first, then test on real arm with reduced speed.
- **Sensor noise** — Camera/MediaPipe detection noise is amplified through coordinate mapping. EMA or similar low-pass filtering is mandatory on all real-time sensor inputs.

## Real-Time Tracking Mode (Implemented — Default)

The arm directly tracks hand position via MediaPipe, bypassing the VLM perception layer entirely. This is now the **default mode** when launching `main.py`.

**Pipeline:**

```
Camera → MediaPipe HandLandmarker (background thread)
  → Right hand: XY pixel → arm XYZ Cartesian (mirrored, 2× horizontal scale)
  → Left hand: wrist Y pixel → pitch angle (±50°, pitch-only)
  → Clamp to safety bounds
  → EMA smoothing on clamped output
  → Servo mode (velocity-clamped set_servo_cartesian)
```

**Two-hand design:**
- **Right hand** — controls arm tip position (X, Y). Horizontal movement scaled 2× for responsive feel. Camera mirrored, so "natural" left/right maps correctly.
- **Left hand** — controls pitch only (J5 wrist tilt). Range ±50°. Decoupled from position so each hand has a single clear role.

**Key implementation notes:**
- Runs as `HandTrackingThread` alongside existing pipeline; pushes JPEG frames to shared buffer for dashboard
- TRACK motion mode in `ParametricMotionGenerator` — reuses same servo pipeline as other modes
- Mode switchable at runtime from web dashboard without restarting
- Camera resources handed off cleanly when switching between VLM and hand tracking modes

## Key Dependencies

- `xarm-python-sdk` — UFactory arm control (Cartesian + servo mode)
- `google-genai` — Gemini 2.5 Flash VLM (structured JSON output)
- `mediapipe` — Local hand landmark detection (no cloud API)
- `opencv-contrib-python` — Camera capture, frame processing
- `fastapi` + `uvicorn` + `websockets` — Real-time web dashboard
- `python-osc` — TouchDesigner OSC communication (planned)
- `pyyaml` — Persona config files
- `tenacity` — Retry logic for VLM API calls