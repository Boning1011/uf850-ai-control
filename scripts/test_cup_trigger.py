"""
Multi-action trigger test: detect various actions → switch arm mode on simulator.

Each detected action switches to a new arm motion mode.
Modes persist until the next action is detected (no auto-return to IDLE).

Actions & modes:
  IDLE     — gentle breathing (starting mode only)
  DRINKING — fast rise + wobble at top
  WAVING   — side-to-side mirror wave
  LEANING  — arm leans forward curiously
  POINTING — arm extends toward pointed direction
  RESTING  — slow descend to low relaxed pose

Uses gemini-2.5-flash with thinking=OFF for ~1.3s latency.
Connects to Docker simulator arm at 127.0.0.1.

Usage:
    PYTHONUTF8=1 python scripts/test_cup_trigger.py [--duration 180]
"""

import argparse
import datetime
import json
import math
import os
import platform
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pathlib import Path
from pydantic import BaseModel, Field

from arm_controller import ArmController, acquire_lock
from perception import FrameBuffer
from persona import PerceptionState, StateHolder
from web_server import DashboardServer

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


# ── VLM schema ────────────────────────────────────────────────────────────

class ActionDetection(BaseModel):
    """What the VLM returns each frame."""
    action: str = Field(
        description=(
            "The most prominent action happening RIGHT NOW. Must be one of: "
            "'drinking' (cup/bottle raised to mouth, actively sipping), "
            "'waving' (hand raised and moving side-to-side), "
            "'leaning_forward' (person leaning toward camera, curious/engaged), "
            "'pointing' (arm extended, finger pointing in a direction), "
            "'resting' (leaning back, relaxed, arms down or crossed), "
            "'none' (just sitting normally, no distinct action)"
        )
    )
    confidence: float = Field(ge=0, le=1, description="How confident are you in this action detection")
    energy: float = Field(ge=0, le=1, description="Person's overall activity level. 0=still, 1=very active")
    description: str = Field(description="One-sentence description of what the person is doing")


SYSTEM_PROMPT = (
    "You are a fast visual action detector for a robotic arm installation. "
    "You receive sequential camera frames and must classify the person's current action.\n\n"
    "Classify into exactly ONE of these actions:\n"
    "- 'drinking': cup/bottle/glass raised to mouth, actively taking a sip\n"
    "- 'waving': hand raised and moving side-to-side (greeting gesture)\n"
    "- 'leaning_forward': person noticeably leaning toward the camera\n"
    "- 'pointing': arm extended, finger pointing somewhere\n"
    "- 'resting': leaning back, relaxed posture, arms down or crossed\n"
    "- 'none': normal sitting, no distinct action\n\n"
    "Only report an action if you are confident (>0.6). "
    "When action is ambiguous, prefer 'none'. Be decisive and fast."
)

# Map action names to arm modes
ACTION_TO_MODE = {
    "drinking": "DRINKING",
    "waving": "WAVING",
    "leaning_forward": "LEANING",
    "pointing": "POINTING",
    "resting": "RESTING",
    "none": None,  # no mode change
}

VALID_ACTIONS = set(ACTION_TO_MODE.keys())


# ── Arm motion modes ─────────────────────────────────────────────────────

CENTER = (300, 0, 400)


def mode_idle(t, phase_t):
    """Gentle breathing around center."""
    x = CENTER[0] + 15 * math.sin(0.3 * t)
    y = CENTER[1] + 10 * math.sin(0.4 * t + 1.0)
    z = CENTER[2] + 20 * math.sin(0.25 * t + 0.5)
    return x, y, z, 80


def mode_drinking(t, phase_t):
    """Excited rise + wobble at top."""
    p = phase_t
    if p < 1.0:
        z_lift = 200 * p
        x, y = CENTER[0], CENTER[1]
        z = CENTER[2] + z_lift
    else:
        wt = p - 1.0
        x = CENTER[0] + 40 * math.sin(3.0 * wt)
        y = CENTER[1] + 30 * math.sin(4.0 * wt + 0.7)
        z = CENTER[2] + 200 + 30 * math.sin(2.5 * wt)
    return x, y, z, 250


def mode_waving(t, phase_t):
    """Side-to-side mirror wave."""
    p = phase_t
    wave_amp = min(p / 0.5, 1.0) * 100  # ramp up amplitude
    x = CENTER[0] + 20 * math.sin(0.5 * t)
    y = CENTER[1] + wave_amp * math.sin(3.5 * p)
    z = CENTER[2] + 150 + 20 * math.sin(2.0 * p + 0.3)
    return x, y, z, 300


def mode_leaning(t, phase_t):
    """Arm leans forward curiously."""
    p = phase_t
    approach = min(p / 1.5, 1.0)
    x = CENTER[0] + 120 * approach + 10 * math.sin(1.5 * p)
    y = CENTER[1] + 15 * math.sin(2.0 * p)
    z = CENTER[2] + 50 * approach + 10 * math.sin(1.8 * p + 0.5)
    return x, y, z, 180


def mode_pointing(t, phase_t):
    """Arm extends outward, then tracks slowly."""
    p = phase_t
    extend = min(p / 1.0, 1.0)
    x = CENTER[0] + 100 * extend
    y = CENTER[1] + 80 * extend * math.sin(0.3 * p)
    z = CENTER[2] + 80 * extend + 15 * math.sin(0.8 * p)
    return x, y, z, 200


def mode_resting(t, phase_t):
    """Slow descend to low relaxed pose."""
    p = phase_t
    descend = min(p / 2.0, 1.0)
    x = CENTER[0] + 8 * math.sin(0.15 * t)
    y = CENTER[1] + 5 * math.sin(0.2 * t + 0.5)
    z = CENTER[2] - 100 * descend + 10 * math.sin(0.1 * t)
    return x, y, z, 60


MODE_FUNCTIONS = {
    "IDLE": mode_idle,
    "DRINKING": mode_drinking,
    "WAVING": mode_waving,
    "LEANING": mode_leaning,
    "POINTING": mode_pointing,
    "RESTING": mode_resting,
}


# ── Logger ────────────────────────────────────────────────────────────────

class TestLogger:
    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.f.write(f"# Multi-Action Trigger Test — {now}\n\n")
        self.f.write("Camera → Gemini VLM (thinking=OFF) → Action Detection → Arm Simulator\n\n")
        self.f.write("Modes: IDLE → DRINKING / WAVING / LEANING / POINTING / RESTING (no auto-return)\n\n")
        self.f.write("| Time | Latency | Action | Conf | Energy | Mode | Arm XYZ | Description |\n")
        self.f.write("|------|---------|--------|------|--------|------|---------|-------------|\n")
        self.f.flush()

    def log(self, latency, det, mode, xyz, mode_changed=False):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        action_str = f"**{det.action.upper()}**" if det.action != "none" else "none"
        mode_str = f"**→{mode}**" if mode_changed else mode
        desc = det.description[:50]
        self.f.write(f"| {now} | {latency:.1f}s | {action_str} | {det.confidence:.2f} "
                     f"| {det.energy:.2f} | {mode_str} "
                     f"| ({xyz[0]:.0f}, {xyz[1]:.0f}, {xyz[2]:.0f}) | {desc} |\n")
        self.f.flush()

    def log_event(self, event):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self.f.write(f"| {now} | — | — | — | — | **{event}** | — | — |\n")
        self.f.flush()

    def close(self):
        self.f.close()


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Multi-action trigger arm test")
    parser.add_argument("--duration", type=int, default=180,
                        help="Test duration seconds (default: 180)")
    parser.add_argument("--arm-ip", default="127.0.0.1",
                        help="xArm IP (default: 127.0.0.1 for simulator)")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="Disable web dashboard")
    parser.add_argument("--port", type=int, default=7860,
                        help="Dashboard port (default: 7860)")
    args = parser.parse_args()

    acquire_lock()

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "doc", f"action_test_{ts}.md"))
    logger = TestLogger(log_path)

    print("=== Multi-Action Trigger Test ===", flush=True)
    print(f"  Duration: {args.duration}s", flush=True)
    print(f"  Arm: {args.arm_ip}", flush=True)
    print(f"  Log: {log_path}", flush=True)
    print(f"  Modes: DRINKING / WAVING / LEANING / POINTING / RESTING", flush=True)
    print(flush=True)

    # Arm
    arm = ArmController(
        ip=args.arm_ip, speed=80,
        center=CENTER, rpy=(180, 0, 0),
        bounds_x=(100, 450), bounds_y=(-150, 150), bounds_z=(250, 950),
    )
    arm.connect()
    arm.move_to_center(speed=100)
    logger.log_event("ARM_CONNECTED")
    print("[Arm] Connected and at center.", flush=True)

    # Camera
    if platform.system() == "Windows":
        cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    else:
        cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    time.sleep(0.3)
    if not cap.isOpened():
        print("[ERROR] Cannot open camera.", flush=True)
        return
    print("[Camera] Ready.", flush=True)
    logger.log_event("CAMERA_READY")

    # Dashboard
    frame_buffer = FrameBuffer(maxlen=10)
    state_holder = StateHolder(smoothing=0.3)
    dashboard = None
    if not args.no_dashboard:
        dashboard = DashboardServer(frame_buffer, state_holder,
                                    host="0.0.0.0", port=args.port)
        dashboard.start()
        logger.log_event("DASHBOARD_READY")

    # VLM
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    print("[VLM] Ready (gemini-2.5-flash, thinking=OFF).", flush=True)
    logger.log_event("VLM_READY")

    # State
    mode = "IDLE"
    mode_start = time.time()
    t_start = time.time()
    call_count = 0
    mode_history = []  # (time, mode)

    print("\n[Running] Do any action to trigger a mode switch!\n", flush=True)

    try:
        while True:
            elapsed = time.time() - t_start
            if elapsed >= args.duration:
                break

            # Grab 3 frames
            frames_jpeg = []
            for _ in range(3):
                ret, frame = cap.read()
                if ret:
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    jpeg_bytes = buf.tobytes()
                    frames_jpeg.append(jpeg_bytes)
                    frame_buffer.push(jpeg_bytes)  # feed dashboard camera
                time.sleep(0.02)

            if not frames_jpeg:
                time.sleep(0.1)
                continue

            # VLM call
            parts = [types.Part.from_bytes(data=f, mime_type="image/jpeg") for f in frames_jpeg]
            parts.append(f"{len(frames_jpeg)} sequential camera frames. Classify the person's action.")

            t0 = time.time()
            try:
                resp = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=parts,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        response_mime_type="application/json",
                        response_schema=ActionDetection,
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                    ),
                )
                latency = time.time() - t0
                call_count += 1

                det = resp.parsed
                if det is None:
                    data = json.loads(resp.text)
                    det = ActionDetection(**data)

            except Exception as e:
                latency = time.time() - t0
                print(f"  [VLM error] {str(e)[:80]}", flush=True)
                time.sleep(0.5)
                continue

            # Normalize action
            action = det.action.lower().strip()
            if action not in VALID_ACTIONS:
                action = "none"

            # Mode transition: only switch if action is detected with confidence
            mode_changed = False
            new_mode = ACTION_TO_MODE.get(action)
            if new_mode and new_mode != mode and det.confidence >= 0.6:
                old_mode = mode
                mode = new_mode
                mode_start = time.time()
                mode_changed = True
                mode_history.append((time.time() - t_start, mode))
                print(f"  >>> {action.upper()} detected (conf={det.confidence:.2f}) "
                      f"→ mode: {mode} <<<", flush=True)
                logger.log_event(f"MODE SWITCH → {mode}")
                if dashboard:
                    dashboard.push_mode_transition(old_mode, mode, action, det.confidence)

            # Compute arm position
            t_arm = time.time() - t_start
            phase_t = time.time() - mode_start
            mode_fn = MODE_FUNCTIONS[mode]
            x, y, z, spd = mode_fn(t_arm, phase_t)

            arm.send_position(x, y, z, speed=spd)

            # Push to dashboard
            if dashboard:
                raw_state = PerceptionState(
                    energy=det.energy, presence=det.confidence,
                    mood=0.5, urgency=0.0,
                    attention_x=0.0, attention_y=0.0,
                    timestamp=time.time(),
                )
                state_holder.update(raw_state)
                smoothed = state_holder.get()
                dashboard.push_state(raw_state, smoothed,
                                     motion_xyz=(x, y, z), speed=spd,
                                     vlm_count=call_count, vlm_latency=latency)
                dashboard.push_vlm_text(det.description, action)

            # Log
            logger.log(latency, det, mode, (x, y, z), mode_changed)

            # Console
            action_display = action.upper() if action != "none" else "      "
            marker = " ***" if mode_changed else ""
            print(f"  #{call_count:3d} [{latency:.1f}s] {action_display:16s} "
                  f"conf={det.confidence:.1f} e={det.energy:.1f} "
                  f"mode={mode:9s} -> ({x:.0f},{y:.0f},{z:.0f}){marker}",
                  flush=True)

    except KeyboardInterrupt:
        print("\nStopping...", flush=True)

    finally:
        cap.release()
        arm.move_to_center(speed=100)
        arm.disconnect()

        # Summary
        logger.log_event(f"DONE — {call_count} VLM calls in {time.time()-t_start:.0f}s")
        if mode_history:
            logger.f.write("\n---\n\n## Mode Transitions\n\n")
            logger.f.write("| Time (s) | Mode |\n|----------|------|\n")
            for t, m in mode_history:
                logger.f.write(f"| {t:.1f}s | {m} |\n")
            logger.f.flush()
        logger.close()

        print(f"\n=== Done ===", flush=True)
        print(f"  Calls: {call_count}, Duration: {time.time()-t_start:.0f}s", flush=True)
        print(f"  Mode switches: {len(mode_history)}", flush=True)
        if mode_history:
            print(f"  Transitions: IDLE", end="", flush=True)
            for _, m in mode_history:
                print(f" → {m}", end="", flush=True)
            print(flush=True)
        print(f"  Log: {log_path}", flush=True)


if __name__ == "__main__":
    main()
