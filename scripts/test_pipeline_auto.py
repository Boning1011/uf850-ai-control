"""
Full-pipeline autonomous test: Camera → VLM → Triggers → Motion (simulated).

Runs in background. Captures from webcam, sends to Gemini VLM, detects behavioral
triggers, computes motion targets, and logs EVERYTHING to a markdown file so you
can review what happened while you were away.

Usage:
    python scripts/test_pipeline_auto.py [--duration 600] [--rate 0.5]
"""

import argparse
import math
import os
import sys
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from persona import PersonaConfig, PerceptionState, StateHolder
from motion_gen import ParametricMotionGenerator
from perception import GeminiProvider, FrameBuffer, CameraThread, PerceptionThread, VLMOutput


# ── Trigger definitions ──────────────────────────────────────────────────

class TriggerEngine:
    """Detects behavioral triggers from VLM state changes and logs them."""

    # Thresholds
    TRIGGERS = {
        "HEAD_TURN_LEFT":   {"field": "attention_x", "op": "<",  "val": -0.4},
        "HEAD_TURN_RIGHT":  {"field": "attention_x", "op": ">",  "val": 0.4},
        "LOOKING_UP":       {"field": "attention_y", "op": ">",  "val": 0.4},
        "LOOKING_DOWN":     {"field": "attention_y", "op": "<",  "val": -0.4},
        "HIGH_ENERGY":      {"field": "energy",      "op": ">",  "val": 0.6},
        "LOW_ENERGY":       {"field": "energy",      "op": "<",  "val": 0.15},
        "SUDDEN_MOVEMENT":  {"field": "urgency",     "op": ">",  "val": 0.5},
        "PLAYFUL_MOOD":     {"field": "mood",        "op": ">",  "val": 0.7},
        "TENSE_MOOD":       {"field": "mood",        "op": "<",  "val": 0.3},
        "HIGH_PRESENCE":    {"field": "presence",    "op": ">",  "val": 0.5},
        "LOW_PRESENCE":     {"field": "presence",    "op": "<",  "val": 0.15},
    }

    def __init__(self):
        self.active_triggers = set()      # currently active trigger names
        self.trigger_history = []          # (timestamp, event_type, trigger_name, details)
        self.prev_state = None
        self.state_history = []            # (timestamp, PerceptionState) for delta tracking

    def check(self, state: PerceptionState) -> list[str]:
        """Check state against all triggers, return list of newly fired triggers."""
        newly_fired = []
        newly_cleared = []
        current_active = set()

        for name, rule in self.TRIGGERS.items():
            val = getattr(state, rule["field"])
            if rule["op"] == ">" and val > rule["val"]:
                current_active.add(name)
            elif rule["op"] == "<" and val < rule["val"]:
                current_active.add(name)

        # Detect transitions
        for name in current_active - self.active_triggers:
            newly_fired.append(name)
            self.trigger_history.append((
                time.time(), "FIRED", name,
                self._state_summary(state),
            ))

        for name in self.active_triggers - current_active:
            newly_cleared.append(name)
            self.trigger_history.append((
                time.time(), "CLEARED", name,
                self._state_summary(state),
            ))

        self.active_triggers = current_active

        # Track big deltas
        if self.prev_state is not None:
            deltas = self._compute_deltas(self.prev_state, state)
            big_deltas = {k: v for k, v in deltas.items() if abs(v) > 0.2}
            if big_deltas:
                self.trigger_history.append((
                    time.time(), "BIG_DELTA", "",
                    f"Δ {', '.join(f'{k}={v:+.2f}' for k, v in big_deltas.items())}",
                ))

        self.prev_state = state
        return newly_fired

    def _state_summary(self, s):
        return (f"e={s.energy:.2f} ax={s.attention_x:+.2f} ay={s.attention_y:+.2f} "
                f"m={s.mood:.2f} p={s.presence:.2f} u={s.urgency:.2f}")

    def _compute_deltas(self, old, new):
        return {
            "energy": new.energy - old.energy,
            "att_x": new.attention_x - old.attention_x,
            "att_y": new.attention_y - old.attention_y,
            "mood": new.mood - old.mood,
            "presence": new.presence - old.presence,
            "urgency": new.urgency - old.urgency,
        }


# ── Logging ──────────────────────────────────────────────────────────────

class PipelineLogger:
    """Writes structured log to a markdown file for human review."""

    def __init__(self, filepath):
        self.filepath = filepath
        self.f = open(filepath, "w", encoding="utf-8")
        self._write_header()

    def _write_header(self):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.f.write(f"# Pipeline Test Log — {now}\n\n")
        self.f.write("Autonomous full-pipeline test: Camera → Gemini VLM → Triggers → Motion (simulated)\n\n")
        self.f.write("---\n\n")
        self.f.flush()

    def log_config(self, config, rate_hz, duration):
        self.f.write("## Configuration\n\n")
        self.f.write(f"- **Persona**: {config.name}\n")
        self.f.write(f"- **VLM**: {config.vlm_model} @ {rate_hz} Hz\n")
        self.f.write(f"- **Duration**: {duration}s\n")
        self.f.write(f"- **Motion center**: {config.center}\n")
        self.f.write(f"- **Safety bounds**: X={config.bounds_x} Y={config.bounds_y} Z={config.bounds_z}\n\n")
        self.f.write("---\n\n")
        self.f.write("## Event Log\n\n")
        self.f.write("| Time | Event | Details |\n")
        self.f.write("|------|-------|---------|\n")
        self.f.flush()

    def log_event(self, event_type, details):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self.f.write(f"| {now} | **{event_type}** | {details} |\n")
        self.f.flush()

    def log_section(self, title, content):
        self.f.write(f"\n### {title}\n\n{content}\n\n")
        self.f.flush()

    def log_vlm_call(self, call_num, latency, raw_state, smoothed_state, motion_xyz, speed):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        raw = (f"e={raw_state.energy:.2f} ax={raw_state.attention_x:+.2f} "
               f"ay={raw_state.attention_y:+.2f} m={raw_state.mood:.2f} "
               f"p={raw_state.presence:.2f} u={raw_state.urgency:.2f}")
        smoothed = (f"e={smoothed_state.energy:.2f} ax={smoothed_state.attention_x:+.2f} "
                    f"ay={smoothed_state.attention_y:+.2f} m={smoothed_state.mood:.2f} "
                    f"p={smoothed_state.presence:.2f} u={smoothed_state.urgency:.2f}")
        mx, my, mz = motion_xyz
        self.f.write(f"| {now} | VLM #{call_num} ({latency:.1f}s) | "
                     f"raw: [{raw}] → smoothed: [{smoothed}] → "
                     f"motion: X={mx:.0f} Y={my:.0f} Z={mz:.0f} spd={speed:.0f} |\n")
        self.f.flush()

    def log_trigger(self, event_type, trigger_name, details):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        icon = "🔴" if event_type == "FIRED" else "⚪" if event_type == "CLEARED" else "📊"
        self.f.write(f"| {now} | {icon} {event_type}: {trigger_name} | {details} |\n")
        self.f.flush()

    def log_summary(self, trigger_engine, total_vlm_calls, total_errors, elapsed):
        self.f.write("\n---\n\n## Summary\n\n")
        self.f.write(f"- **Total duration**: {elapsed:.0f}s\n")
        self.f.write(f"- **VLM calls**: {total_vlm_calls}\n")
        self.f.write(f"- **VLM errors**: {total_errors}\n")
        self.f.write(f"- **Total trigger events**: {len(trigger_engine.trigger_history)}\n\n")

        # Count trigger fires
        fire_counts = {}
        for _, evt, name, _ in trigger_engine.trigger_history:
            if evt == "FIRED":
                fire_counts[name] = fire_counts.get(name, 0) + 1
        if fire_counts:
            self.f.write("### Trigger Frequency\n\n")
            self.f.write("| Trigger | Times Fired |\n")
            self.f.write("|---------|-------------|\n")
            for name, count in sorted(fire_counts.items(), key=lambda x: -x[1]):
                self.f.write(f"| {name} | {count} |\n")
        self.f.write("\n")
        self.f.flush()

    def close(self):
        self.f.close()


# ── Main pipeline ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Autonomous pipeline test")
    parser.add_argument("--duration", type=int, default=600,
                        help="Test duration in seconds (default: 600 = 10 min)")
    parser.add_argument("--rate", type=float, default=0.33,
                        help="VLM calls per second (default: 0.33 = once per 3s)")
    parser.add_argument("--persona", default="personas/default.yaml")
    args = parser.parse_args()

    # Setup
    config = PersonaConfig(args.persona)
    state_holder = StateHolder(smoothing=0.3)
    motion_gen = ParametricMotionGenerator(config)
    trigger_engine = TriggerEngine()

    # Log file
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "doc", f"pipeline_test_{timestamp}.md")
    log_path = os.path.normpath(log_path)
    logger = PipelineLogger(log_path)
    logger.log_config(config, args.rate, args.duration)

    print(f"=== Autonomous Pipeline Test ===", flush=True)
    print(f"  Persona: {config.name}", flush=True)
    print(f"  VLM rate: {args.rate} Hz ({1/args.rate:.1f}s per call)", flush=True)
    print(f"  Duration: {args.duration}s", flush=True)
    print(f"  Log: {log_path}", flush=True)
    print(f"  Arm: SIMULATED (no physical arm)", flush=True)
    print(flush=True)

    # Camera
    frame_buffer = FrameBuffer(maxlen=20)
    cam = CameraThread(
        device=config.camera_device,
        resolution=config.camera_resolution,
        jpeg_quality=config.camera_jpeg_quality,
        frame_buffer=frame_buffer,
        target_fps=5.0,
    )

    # On Windows, use DirectShow backend for faster camera init
    import platform
    if platform.system() == "Windows":
        import cv2
        print("[Camera] Using DirectShow backend (Windows)...", flush=True)
        cam._cap = cv2.VideoCapture(config.camera_device, cv2.CAP_DSHOW)
        if cam._cap.isOpened():
            cam._cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.camera_resolution[0])
            cam._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.camera_resolution[1])
            w = int(cam._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cam._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            print(f"[Camera] Opened device {config.camera_device} at {w}x{h}", flush=True)
        else:
            print("[ERROR] Cannot open camera. Aborting.", flush=True)
            logger.log_event("ERROR", "Cannot open camera")
            logger.close()
            sys.exit(1)
    elif not cam.open_camera():
        print("[ERROR] Cannot open camera. Aborting.", flush=True)
        logger.log_event("ERROR", "Cannot open camera")
        logger.close()
        sys.exit(1)
    cam.start()
    logger.log_event("STARTED", "Camera opened and capturing")
    print("[Camera] Capturing...", flush=True)

    # VLM provider
    provider = GeminiProvider(model=config.vlm_model)
    logger.log_event("STARTED", f"VLM provider ready: {config.vlm_model}")

    # Wait for enough frames
    print("[Pipeline] Waiting for frame buffer to fill...", flush=True)
    while len(frame_buffer.get_latest(config.frame_count)) < config.frame_count:
        time.sleep(0.5)
    logger.log_event("READY", f"Frame buffer has {config.frame_count} frames")
    print("[Pipeline] Ready. Starting VLM loop.\n", flush=True)

    # Main loop — we do VLM calls manually here (not via PerceptionThread)
    # so we can intercept raw results for logging and trigger detection.
    vlm_interval = 1.0 / args.rate
    call_count = 0
    error_count = 0
    t_start = time.time()
    t_motion = 0.0

    try:
        while True:
            elapsed = time.time() - t_start
            if elapsed >= args.duration:
                logger.log_event("FINISHED", f"Duration reached ({args.duration}s)")
                break

            # Grab frames
            frames = frame_buffer.get_latest(config.frame_count)
            if len(frames) < config.frame_count:
                time.sleep(0.5)
                continue

            # VLM call
            try:
                t0 = time.time()
                result = provider.perceive(frames, config.full_system_prompt)
                latency = time.time() - t0
                call_count += 1

                # Raw state from VLM
                raw_state = PerceptionState(
                    energy=result.get("energy", 0.0),
                    attention_x=result.get("attention_x", 0.0),
                    attention_y=result.get("attention_y", 0.0),
                    mood=result.get("mood", 0.5),
                    presence=result.get("presence", 0.0),
                    urgency=result.get("urgency", 0.0),
                    timestamp=time.time(),
                )

                # Update smoothed state
                state_holder.update(raw_state)
                smoothed = state_holder.get()

                # Compute motion target (simulated)
                t_motion += vlm_interval
                x, y, z = motion_gen.get_target(t_motion, smoothed)
                speed = motion_gen.get_speed(smoothed)

                # Log VLM result + motion
                logger.log_vlm_call(call_count, latency, raw_state, smoothed, (x, y, z), speed)

                # Check triggers
                newly_fired = trigger_engine.check(raw_state)
                for entry in trigger_engine.trigger_history[-(len(newly_fired) + 5):]:
                    ts, evt, name, details = entry
                    if ts >= t0:
                        logger.log_trigger(evt, name, details)

                # Console output
                active_str = ", ".join(sorted(trigger_engine.active_triggers)) or "(none)"
                print(f"[#{call_count} {latency:.1f}s] "
                      f"e={raw_state.energy:.2f} ax={raw_state.attention_x:+.2f} "
                      f"ay={raw_state.attention_y:+.2f} m={raw_state.mood:.2f} "
                      f"p={raw_state.presence:.2f} u={raw_state.urgency:.2f} "
                      f"→ X={x:.0f} Y={y:.0f} Z={z:.0f} spd={speed:.0f} "
                      f"| triggers: {active_str}", flush=True)

            except Exception as e:
                error_count += 1
                logger.log_event("VLM_ERROR", str(e))
                print(f"[VLM ERROR #{error_count}] {e}", flush=True)

            time.sleep(vlm_interval)

    except KeyboardInterrupt:
        logger.log_event("INTERRUPTED", "Ctrl+C received")
        print("\nStopping...", flush=True)

    finally:
        cam.stop()
        elapsed = time.time() - t_start
        logger.log_summary(trigger_engine, call_count, error_count, elapsed)
        logger.close()
        print(f"\n=== Test complete ===", flush=True)
        print(f"  Duration: {elapsed:.0f}s", flush=True)
        print(f"  VLM calls: {call_count}, errors: {error_count}", flush=True)
        print(f"  Log saved: {log_path}", flush=True)


if __name__ == "__main__":
    main()
