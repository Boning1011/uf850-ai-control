# UFactory 850 AI-Driven Interactive Control — Design Brief

## Project Context

We are building an interactive new-media art installation where a UFactory 850 (6-axis) robotic arm holds a tool above a screen, responding to audience presence and behavior in real time. The screen displays live-computed content via TouchDesigner.

## Previous Approach (Being Replaced)

Blender rig → Python script → CSV animation data → PyBullet collision check → UFactory Python API playback. State machine in TouchDesigner selects which pre-rendered animation to play based on sensor triggers.

Problems: every new behavior requires a full CG pipeline round-trip, animations are static with zero variation, collision checking is offline, and the arm has no intelligence — it is purely a playback device.

## New Architecture: Three-Layer Design

### Layer 1 — AI Perception & Decision

A cloud VLM (e.g. Claude Vision, GPT-4V) analyzes camera frames at ~0.5–1 Hz and outputs structured scene understanding: audience count, position, distance, gesture, and a recommended behavioral state (idle, curious, tracking, excited, etc.).

This replaces the state machine. The arm no longer reacts to binary sensor triggers — it "sees" and "interprets" the scene.

Optional inputs: audio via speech-to-text, local lightweight models (YOLO/MediaPipe) as a pre-filter to reduce API cost.

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

1. **Afternoon**: Wire VLM perception to existing animation library as proof of concept
2. **1–2 days**: Replace CSV playback with procedural Cartesian control; configure safety boundaries
3. **1–2 weeks**: Polish motion personalities, integrate TouchDesigner via OSC, add local pre-filter
4. **1 month (optional)**: Introduce imitation learning via LeRobot for organic gesture quality

## Key Dependencies

- UFactory Python SDK (`xArm-Python-SDK`)
- Anthropic or OpenAI API (for VLM perception layer)
- `python-osc` (for TouchDesigner communication)
- OpenCV (camera capture)
- Standard math/noise libraries for procedural motion