"""
VLM Motion Switcher PoC — 3 hardcoded motion patterns with state switching.

Demonstrates the control flow: VLM state command → smooth transition →
procedural arm motion. Three patterns:
  0 = IDLE    — slow breathing (up/down float)
  1 = CURIOUS — compact figure-8 scanning
  2 = ALERT   — triangular waypoint circuit

Usage:
    python vlm_motion_poc.py [--ip 127.0.0.1] [--speed 60] [--mode auto|keyboard] [--dwell 8]
"""

import argparse
import atexit
import math
import os
import queue
import sys
import time
import threading
from xarm.wrapper import XArmAPI

# ---------- single-instance lock ----------

LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".vlm_motion.lock")


def acquire_lock():
    """Ensure only one instance runs at a time (file-based lock with PID check)."""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                old_pid = int(f.read().strip())
            # Check if that process is still alive
            try:
                os.kill(old_pid, 0)  # signal 0 = existence check
                print(f"[ERROR] Another instance is already running (PID {old_pid}).")
                print(f"  If this is wrong, delete: {LOCK_FILE}")
                sys.exit(1)
            except OSError:
                pass  # process is dead, stale lock file
        except (ValueError, IOError):
            pass  # corrupt lock file, overwrite it

    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(_release_lock)


def _release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass

# ---------- constants ----------

# Safe center position (mm)
CX, CY, CZ = 300, 0, 300
# Fixed orientation (degrees)
ROLL, PITCH, YAW = 180, 0, 0
# Workspace clamp bounds (wide X/Z for J2/J3 reach)
SAFE_X = (150, 530)
SAFE_Y = (-150, 150)
SAFE_Z = (130, 450)
# Blend duration (seconds)
BLEND_DURATION = 1.0
# Command rate
DT = 0.04  # ~25 Hz

STATE_NAMES = {0: "IDLE", 1: "CURIOUS", 2: "ALERT"}

# Error code lookup (subset)
ERROR_NAMES = {
    0: "OK", 1: "Emergency Stop", 2: "Emergency IO",
    10: "Servo 1 error", 11: "Servo 2 error", 12: "Servo 3 error",
    13: "Servo 4 error", 14: "Servo 5 error", 15: "Servo 6 error",
    21: "Kinematic Error", 22: "Self-Collision", 23: "Joint Angle Limit",
    24: "Speed Limit", 25: "Planning Error",
    31: "Collision (abnormal current)", 35: "Safety Boundary Limit",
}


# ---------- motion patterns ----------

def pattern_idle(t):
    """Breathing — slow reaching forward/back and up/down."""
    x = CX + 80 * math.sin(2 * math.pi * 0.06 * t)   # forward/back reach
    y = CY + 40 * math.sin(2 * math.pi * 0.08 * t)
    z = CZ + 100 * math.sin(2 * math.pi * 0.10 * t)   # big vertical swing
    return x, y, z


def pattern_curious(t):
    """Figure-8 (Lissajous) — wide reaching scan."""
    freq = 0.16
    phase = 2 * math.pi * freq * t
    x = CX + 150 * math.sin(phase)             # long forward reach
    y = CY + 100 * math.sin(2 * phase)
    z = CZ + 90 * math.cos(phase)              # big vertical component
    return x, y, z


# ALERT waypoints — exaggerated triangle emphasizing reach
_ALERT_WP = [
    (CX + 50,  CY,       CZ + 130),   # high reach forward
    (CX + 180, CY + 80,  CZ - 100),   # far forward-right-low
    (CX - 80,  CY - 80,  CZ - 100),   # retracted-left-low
]

def pattern_alert(t):
    """Triangular waypoint circuit — sharp, reactive feel."""
    segment = int(t / 1.5) % 3   # 1.5s per waypoint
    return _ALERT_WP[segment]


PATTERNS = [pattern_idle, pattern_curious, pattern_alert]


# ---------- workspace safety ----------

def clamp_position(x, y, z):
    x = max(SAFE_X[0], min(SAFE_X[1], x))
    y = max(SAFE_Y[0], min(SAFE_Y[1], y))
    z = max(SAFE_Z[0], min(SAFE_Z[1], z))
    return x, y, z


# ---------- motion dispatcher ----------

class MotionDispatcher:
    """Manages current pattern and smooth blending between patterns."""

    def __init__(self, get_arm_position):
        self.current_state = 0  # start with IDLE
        self._get_arm_pos = get_arm_position
        self._blend_alpha = 1.0  # 1.0 = fully settled
        self._blend_start_pos = None
        self._blend_start_time = None
        self._lock = threading.Lock()

    def request_state(self, state_id, t):
        """Switch to a new state with smooth blending."""
        with self._lock:
            if state_id == self.current_state:
                return
            if state_id not in STATE_NAMES:
                return
            # Capture actual arm position as blend start
            self._blend_start_pos = self._get_arm_pos()
            self._blend_start_time = t
            self._blend_alpha = 0.0
            self.current_state = state_id
            print(f"  >> Switching to {STATE_NAMES[state_id]}"  # no unicode
                  f"  (blend from {self._fmt(self._blend_start_pos)})")

    def get_target(self, t):
        """Return blended (x, y, z) for the current moment."""
        new_target = PATTERNS[self.current_state](t)

        with self._lock:
            if self._blend_alpha >= 1.0:
                return new_target

            elapsed = t - self._blend_start_time
            raw = min(1.0, elapsed / BLEND_DURATION)
            # Smoothstep easing: 3t^2 - 2t^3
            a = raw * raw * (3 - 2 * raw)
            self._blend_alpha = a

            sp = self._blend_start_pos
            x = sp[0] + a * (new_target[0] - sp[0])
            y = sp[1] + a * (new_target[1] - sp[1])
            z = sp[2] + a * (new_target[2] - sp[2])

            if raw >= 1.0:
                self._blend_alpha = 1.0

            return x, y, z

    @staticmethod
    def _fmt(pos):
        if pos is None:
            return "unknown"
        return f"X={pos[0]:.0f} Y={pos[1]:.0f} Z={pos[2]:.0f}"


# ---------- VLM interface ----------

class VLMInterface:
    """Thin abstraction — queue-based state channel. Last-write-wins."""

    def __init__(self):
        self._queue = queue.Queue(maxsize=1)

    def push_state(self, state_id):
        # Drain old value if present, then push new
        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass
        self._queue.put_nowait(state_id)

    def pop_state(self):
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None


# ---------- arm controller (from loop_motion.py) ----------

class ArmController:
    def __init__(self, ip, speed, collision_sensitivity):
        self.ip = ip
        self.speed = speed
        self.collision_sensitivity = collision_sensitivity
        self.arm = None
        self.recovering = threading.Lock()
        self.error_count = 0
        self.running = True

    def connect(self):
        self.arm = XArmAPI(self.ip, is_radian=False)
        self.arm.clean_error()
        self.arm.clean_warn()
        self.arm.motion_enable(enable=True)
        self.arm.set_mode(0)
        self.arm.set_state(0)

        if self.collision_sensitivity >= 0:
            try:
                print("  Setting collision sensitivity...", end="", flush=True)
                result = [None]
                def _set_collision():
                    result[0] = self.arm.set_collision_sensitivity(
                        self.collision_sensitivity)
                t = threading.Thread(target=_set_collision, daemon=True)
                t.start()
                t.join(timeout=3.0)
                if t.is_alive():
                    print(" SKIPPED (simulator)")
                else:
                    print(f" OK (ret={result[0]})")
                    self.arm.set_collision_rebound(True)
            except Exception as e:
                print(f" FAILED ({e})")

        self.arm.register_error_warn_changed_callback(self._on_error_warn)
        self.arm.register_state_changed_callback(self._on_state_changed)

        print(f"Connected to {self.ip}, speed={self.speed} mm/s")
        self._print_position()

    def get_position(self):
        """Return current (x, y, z) or fallback to center."""
        code, pos = self.arm.get_position()
        if code == 0:
            return (pos[0], pos[1], pos[2])
        return (CX, CY, CZ)

    def _print_position(self):
        pos = self.get_position()
        print(f"  Current pos: X={pos[0]:.1f} Y={pos[1]:.1f} Z={pos[2]:.1f}")

    def _on_error_warn(self, data):
        err = data["error_code"]
        warn = data["warn_code"]
        if err != 0:
            name = ERROR_NAMES.get(err, f"Unknown({err})")
            print(f"\n[ERROR] code={err} ({name}) — auto-recovering...")
            self._recover()
        if warn != 0:
            self.arm.clean_warn()

    def _on_state_changed(self, data):
        state = data["state"]
        if state >= 4:
            print(f"[STATE] arm entered state {state}")

    def _recover(self):
        if not self.recovering.acquire(blocking=False):
            return
        try:
            self.error_count += 1
            time.sleep(0.3)
            self.arm.clean_error()
            self.arm.clean_warn()
            self.arm.motion_enable(enable=True)
            self.arm.set_mode(0)
            self.arm.set_state(0)
            time.sleep(0.2)
            code, state = self.arm.get_state()
            if state in (0, 1):
                print(f"[RECOVER] OK (total: {self.error_count})")
            else:
                print(f"[RECOVER] state={state}, retrying...")
                time.sleep(1)
                self.arm.clean_error()
                self.arm.motion_enable(enable=True)
                self.arm.set_mode(0)
                self.arm.set_state(0)
        finally:
            self.recovering.release()

    def disconnect(self):
        self.running = False
        if self.arm:
            self.arm.set_state(4)
            self.arm.disconnect()
            print(f"\nDisconnected. Errors recovered: {self.error_count}")


# ---------- test drivers ----------

def auto_driver(vlm, dwell, running_flag):
    """Cycle through states automatically."""
    cycle = [0, 1, 2]
    idx = 0
    while running_flag():
        state = cycle[idx % len(cycle)]
        print(f"\n[VLM-auto] -> {STATE_NAMES[state]}")
        vlm.push_state(state)
        for _ in range(int(dwell / 0.5)):
            if not running_flag():
                return
            time.sleep(0.5)
        idx += 1


def keyboard_driver(vlm, running_flag):
    """Read stdin for state switches."""
    print("\nControls: [0]=IDLE  [1]=CURIOUS  [2]=ALERT  [q]=quit")
    while running_flag():
        try:
            line = input("> ").strip()
        except EOFError:
            break
        if line == "q":
            break
        if line in ("0", "1", "2"):
            state = int(line)
            print(f"[VLM-key] -> {STATE_NAMES[state]}")
            vlm.push_state(state)
        else:
            print("  (type 0, 1, 2, or q)")


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser(description="VLM Motion Switcher PoC")
    parser.add_argument("--ip", default="127.0.0.1")
    parser.add_argument("--speed", type=int, default=150)
    parser.add_argument("--sensitivity", type=int, default=-1, choices=range(-1, 6),
                        help="Collision sensitivity 0-5, -1=skip (default: -1)")
    parser.add_argument("--mode", choices=["auto", "keyboard"], default="auto")
    parser.add_argument("--dwell", type=float, default=8.0,
                        help="Seconds per state in auto mode (default: 8)")
    args = parser.parse_args()

    acquire_lock()

    ctrl = ArmController(args.ip, args.speed, args.sensitivity)
    vlm = VLMInterface()
    dispatcher = MotionDispatcher(ctrl.get_position)

    try:
        ctrl.connect()

        # Move to center
        print("\nMoving to center...")
        ctrl.arm.set_position(CX, CY, CZ, ROLL, PITCH, YAW,
                              speed=50, wait=True)
        time.sleep(0.5)

        # Start test driver thread
        if args.mode == "auto":
            driver = threading.Thread(
                target=auto_driver,
                args=(vlm, args.dwell, lambda: ctrl.running),
                daemon=True)
        else:
            driver = threading.Thread(
                target=keyboard_driver,
                args=(vlm, lambda: ctrl.running),
                daemon=True)
        driver.start()

        print(f"Running ({args.mode} mode, Ctrl+C to stop)\n")

        # Main control loop @ 25 Hz
        t = 0.0
        log_interval = 2.0  # print position every 2s
        last_log = 0.0

        while ctrl.running:
            # 1. Check for VLM state change
            new_state = vlm.pop_state()
            if new_state is not None:
                dispatcher.request_state(new_state, t)

            # 2. Skip if arm in error
            if ctrl.arm.has_error or ctrl.arm.state >= 4:
                time.sleep(0.1)
                continue

            # 3. Get blended target
            x, y, z = dispatcher.get_target(t)

            # 4. Safety clamp
            x, y, z = clamp_position(x, y, z)

            # 5. Send command
            ret = ctrl.arm.set_position(
                x, y, z, ROLL, PITCH, YAW,
                speed=args.speed, wait=False)
            if ret == -2:
                time.sleep(0.2)
                continue

            # 6. Periodic log
            if t - last_log >= log_interval:
                state_name = STATE_NAMES[dispatcher.current_state]
                print(f"  [{state_name}] X={x:.0f} Y={y:.0f} Z={z:.0f}")
                last_log = t

            t += DT
            time.sleep(DT)

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        ctrl.disconnect()


if __name__ == "__main__":
    main()
