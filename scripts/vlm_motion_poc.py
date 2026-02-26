"""
VLM Motion Switcher PoC — 4 hardcoded motion patterns with state switching.

Demonstrates the control flow: VLM state command -> smooth transition ->
procedural arm motion. Four patterns:
  0 = IDLE    — slow breathing (up/down float)
  1 = CURIOUS — figure-8 scanning
  2 = ALERT   — fast triangular waypoint circuit
  3 = DANCE   — erratic high-frequency shaking

Usage:
    python vlm_motion_poc.py [--ip 127.0.0.1] [--speed 150] [--mode auto|keyboard] [--dwell 8]
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
            try:
                os.kill(old_pid, 0)
                print(f"[ERROR] Another instance is already running (PID {old_pid}).")
                print(f"  If this is wrong, delete: {LOCK_FILE}")
                sys.exit(1)
            except OSError:
                pass
        except (ValueError, IOError):
            pass

    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(_release_lock)


def _release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass

# ---------- constants ----------

# Safe center position (mm) — raised high to keep joints mid-range
CX, CY, CZ = 300, 0, 350
# Fixed orientation (degrees) — NEVER changed, avoids IK dead zones
ROLL, PITCH, YAW = 180, 0, 0
# Workspace clamp bounds (default — IDLE / CURIOUS / ALERT)
SAFE_X = (220, 420)
SAFE_Y = (-80, 80)
SAFE_Z = (290, 420)
# DANCE workspace — arm raised high, much wider range
DANCE_X = (100, 450)
DANCE_Y = (-150, 150)
DANCE_Z = (280, 950)
# Blend duration (seconds)
BLEND_DURATION = 0.5
# Command rate
DT = 0.04  # ~25 Hz

STATE_NAMES = {0: "IDLE", 1: "CURIOUS", 2: "ALERT", 3: "DANCE"}
# Speed multiplier per state (applied to --speed)
SPEED_MULT = {0: 0.4, 1: 1.0, 2: 2.0, 3: 3.0}

# Error code lookup (subset)
ERROR_NAMES = {
    0: "OK", 1: "Emergency Stop", 2: "Emergency IO",
    10: "Servo 1 error", 11: "Servo 2 error", 12: "Servo 3 error",
    13: "Servo 4 error", 14: "Servo 5 error", 15: "Servo 6 error",
    21: "Kinematic Error", 22: "Self-Collision", 23: "Joint Angle Limit",
    24: "Speed Limit", 25: "Planning Error",
    31: "Collision (abnormal current)", 35: "Safety Boundary Limit",
}


# ---------- motion patterns (return x, y, z only — RPY always fixed) ----------

def pattern_idle(t):
    """Breathing — small, slow, calm drift. Clearly 'asleep'."""
    x = CX + 15 * math.sin(2 * math.pi * 0.04 * t)
    y = CY + 10 * math.sin(2 * math.pi * 0.05 * t)
    z = CZ + 20 * math.sin(2 * math.pi * 0.03 * t)
    return x, y, z


def pattern_curious(t):
    """Figure-8 (Lissajous) — wide scan."""
    freq = 0.16
    phase = 2 * math.pi * freq * t
    x = CX + 80 * math.sin(phase)
    y = CY + 60 * math.sin(2 * phase)
    z = CZ + 50 * math.cos(phase)
    return x, y, z


# ALERT waypoints — forward-biased triangle
_ALERT_WP = [
    (CX + 20,  CY,       CZ + 50),   # high center
    (CX + 80,  CY + 50,  CZ - 30),   # forward-right
    (CX + 80,  CY - 50,  CZ - 30),   # forward-left
]

def pattern_alert(t):
    """Triangular waypoint circuit — fast snappy jumps."""
    segment = int(t / 0.5) % 3
    return _ALERT_WP[segment]


def pattern_dance(t):
    """Wild dance — arm raised high, large fast overlapping oscillations."""
    # Raised center — arm reaches up into the air
    dcx, dcy, dcz = 200, 0, 650
    x = dcx + (80 * math.sin(2 * math.pi * 1.0 * t)
               + 50 * math.sin(2 * math.pi * 2.6 * t)
               + 20 * math.sin(2 * math.pi * 4.5 * t))
    y = dcy + (60 * math.sin(2 * math.pi * 1.3 * t)
               + 35 * math.sin(2 * math.pi * 3.1 * t)
               + 15 * math.sin(2 * math.pi * 5.3 * t))
    z = dcz + (140 * math.sin(2 * math.pi * 0.7 * t)
               + 70 * math.sin(2 * math.pi * 1.9 * t)
               + 30 * math.sin(2 * math.pi * 3.7 * t))
    return x, y, z


PATTERNS = [pattern_idle, pattern_curious, pattern_alert, pattern_dance]


# ---------- workspace safety ----------

def clamp_position(x, y, z, state=0):
    if state == 3:  # DANCE — wider bounds
        bx, by, bz = DANCE_X, DANCE_Y, DANCE_Z
    else:
        bx, by, bz = SAFE_X, SAFE_Y, SAFE_Z
    x = max(bx[0], min(bx[1], x))
    y = max(by[0], min(by[1], y))
    z = max(bz[0], min(bz[1], z))
    return x, y, z


# ---------- motion dispatcher ----------

class MotionDispatcher:
    """Manages current pattern and smooth XYZ blending between patterns."""

    def __init__(self, get_arm_xyz):
        self.current_state = 0
        self._get_arm_xyz = get_arm_xyz
        self._blend_alpha = 1.0
        self._blend_start_xyz = None
        self._blend_start_time = None
        self._lock = threading.Lock()

    def request_state(self, state_id, t):
        with self._lock:
            if state_id == self.current_state:
                return
            if state_id not in STATE_NAMES:
                return
            self._blend_start_xyz = self._get_arm_xyz()
            self._blend_start_time = t
            self._blend_alpha = 0.0
            self.current_state = state_id
            sp = self._blend_start_xyz
            print(f"  >> Switching to {STATE_NAMES[state_id]}"
                  f"  (blend from X={sp[0]:.0f} Y={sp[1]:.0f} Z={sp[2]:.0f})")

    def get_target(self, t):
        """Return blended (x, y, z). RPY is always fixed."""
        new_xyz = PATTERNS[self.current_state](t)

        with self._lock:
            if self._blend_alpha >= 1.0:
                return new_xyz

            elapsed = t - self._blend_start_time
            raw = min(1.0, elapsed / BLEND_DURATION)
            a = raw * raw * (3 - 2 * raw)
            self._blend_alpha = a

            sp = self._blend_start_xyz
            x = sp[0] + a * (new_xyz[0] - sp[0])
            y = sp[1] + a * (new_xyz[1] - sp[1])
            z = sp[2] + a * (new_xyz[2] - sp[2])

            if raw >= 1.0:
                self._blend_alpha = 1.0

            return x, y, z


# ---------- VLM interface ----------

class VLMInterface:
    """Thin abstraction — queue-based state channel. Last-write-wins."""

    def __init__(self):
        self._queue = queue.Queue(maxsize=1)

    def push_state(self, state_id):
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


# ---------- arm controller ----------

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

    def get_xyz(self):
        """Return current (x, y, z) only — RPY always fixed at constants."""
        code, pos = self.arm.get_position()
        if code == 0:
            return (pos[0], pos[1], pos[2])
        return (CX, CY, CZ)

    def _print_position(self):
        pos = self.get_xyz()
        print(f"  Current pos: X={pos[0]:.1f} Y={pos[1]:.1f} Z={pos[2]:.1f}")

    def _on_error_warn(self, data):
        err = data["error_code"]
        warn = data["warn_code"]
        if err != 0:
            name = ERROR_NAMES.get(err, f"Unknown({err})")
            print(f"\n[ERROR] code={err} ({name}) -- auto-recovering...")
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
            # Move to center with fixed RPY — safe from any position
            self.arm.set_position(CX, CY, CZ, ROLL, PITCH, YAW,
                                  speed=60, wait=True)
            print(f"[RECOVER] OK -> center (total: {self.error_count})")
        except Exception as e:
            print(f"[RECOVER] partial ({e}), total: {self.error_count}")
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
    cycle = [0, 1, 2, 3]
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
    print("\nControls: [0]=IDLE  [1]=CURIOUS  [2]=ALERT  [3]=DANCE  [q]=quit")
    while running_flag():
        try:
            line = input("> ").strip()
        except EOFError:
            break
        if line == "q":
            break
        if line in ("0", "1", "2", "3"):
            state = int(line)
            print(f"[VLM-key] -> {STATE_NAMES[state]}")
            vlm.push_state(state)
        else:
            print("  (type 0, 1, 2, 3, or q)")


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
    dispatcher = MotionDispatcher(ctrl.get_xyz)

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
        log_interval = 2.0
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

            # 3. Get blended XYZ target
            x, y, z = dispatcher.get_target(t)

            # 4. Safety clamp (DANCE uses wider bounds)
            x, y, z = clamp_position(x, y, z, state=dispatcher.current_state)

            # 5. Send command (speed varies per mode)
            spd = args.speed * SPEED_MULT[dispatcher.current_state]
            ret = ctrl.arm.set_position(
                x, y, z, ROLL, PITCH, YAW,
                speed=spd, wait=False)
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
