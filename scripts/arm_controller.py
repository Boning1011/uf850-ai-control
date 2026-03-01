"""
Reusable ArmController — extracted from vlm_motion_poc.py.

Handles connection, error recovery, position safety clamping, and command queue flushing.
"""

import atexit
import math
import os
import sys
import time
import threading
from xarm.wrapper import XArmAPI


# ---------- single-instance lock ----------

LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".arm_controller.lock")


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


# ---------- error code lookup ----------

ERROR_NAMES = {
    0: "OK", 1: "Emergency Stop", 2: "Emergency IO",
    10: "Servo 1 error", 11: "Servo 2 error", 12: "Servo 3 error",
    13: "Servo 4 error", 14: "Servo 5 error", 15: "Servo 6 error",
    21: "Kinematic Error", 22: "Self-Collision", 23: "Joint Angle Limit",
    24: "Speed Limit", 25: "Planning Error",
    31: "Collision (abnormal current)", 35: "Safety Boundary Limit",
    60: "Servo Cartesian Out of Range",
}


# ---------- arm controller ----------

class ArmController:
    """Manages xArm connection, error recovery, and position safety."""

    def __init__(self, ip, speed=150, collision_sensitivity=-1,
                 center=(300, 0, 400), rpy=(180, 0, 0),
                 bounds_x=(100, 450), bounds_y=(-150, 150), bounds_z=(250, 950)):
        self.ip = ip
        self.speed = speed
        self.collision_sensitivity = collision_sensitivity
        self.center = center
        self.rpy = rpy
        self.bounds_x = bounds_x
        self.bounds_y = bounds_y
        self.bounds_z = bounds_z
        self.arm = None
        self.recovering = threading.Lock()
        self.error_count = 0
        self.running = True
        self._servo_mode = False
        self._last_servo_pos = None  # [x, y, z, roll, pitch, yaw]

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

    def get_telemetry(self):
        """Read cached arm telemetry. Non-blocking (reads from SDK report buffer)."""
        if not self.arm:
            return None
        try:
            return {
                "angles": [round(a, 1) for a in (self.arm.angles or [0]*7)[:6]],
                "temperatures": list((self.arm.temperatures or [0]*7)[:6]),
                "currents": [round(c, 2) for c in (self.arm.currents or [0]*7)[:6]],
                "tcp_speed": round(self.arm.realtime_tcp_speed or 0, 1),
                "cmd_num": self.arm.cmd_num or 0,
                "state": self.arm.state or 0,
                "mode": self.arm.mode or 0,
                "error_code": self.arm.error_code or 0,
                "warn_code": self.arm.warn_code or 0,
            }
        except Exception:
            return None

    def get_xyz(self):
        code, pos = self.arm.get_position()
        if code == 0:
            return (pos[0], pos[1], pos[2])
        return self.center

    def clamp_position(self, x, y, z):
        x = max(self.bounds_x[0], min(self.bounds_x[1], x))
        y = max(self.bounds_y[0], min(self.bounds_y[1], y))
        z = max(self.bounds_z[0], min(self.bounds_z[1], z))
        return x, y, z

    def send_position(self, x, y, z, speed=None, pitch=None):
        """Send a clamped position command. Returns SDK return code.

        Args:
            x, y, z: target position in mm
            speed: mm/s (default: self.speed)
            pitch: tool pitch in degrees (default: from self.rpy[1]).
                   Used for J5 control — e.g. ±35° for head nod.
        """
        x, y, z = self.clamp_position(x, y, z)
        spd = speed if speed is not None else self.speed
        r, p, w = self.rpy
        if pitch is not None:
            p = pitch
        return self.arm.set_position(x, y, z, r, p, w, speed=spd, wait=False)

    def move_to_center(self, speed=50):
        r, p, w = self.rpy
        cx, cy, cz = self.center
        self.arm.set_position(cx, cy, cz, r, p, w, speed=speed, wait=True)

    def flush_queue(self):
        if self._servo_mode:
            return  # no command queue in servo mode
        self.arm.set_state(4)
        self.arm.set_mode(0)
        self.arm.set_state(0)

    # ---------- servo mode (Mode 1) ----------

    def enable_servo(self):
        """Switch to servo mode (Mode 1). Call after move_to_center().

        Retries up to 3 times if set_mode(1) fails (common after error recovery).
        Returns True if servo mode confirmed, False otherwise.
        """
        # Clear any residual errors first
        if self.arm.has_error:
            self.arm.clean_error()
            time.sleep(0.1)

        for attempt in range(3):
            ret = self.arm.set_mode(1)
            if ret == 0:
                break
            print(f"[ARM] set_mode(1) failed (ret={ret}), retry {attempt + 1}/3",
                  flush=True)
            # Full reset cycle before retry
            self.arm.clean_error()
            self.arm.clean_warn()
            self.arm.motion_enable(enable=True)
            self.arm.set_mode(0)
            self.arm.set_state(0)
            time.sleep(0.5)

        self.arm.set_state(0)

        # Wait for mode report to update (SDK reports asynchronously)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if self.arm.mode == 1:
                break
            time.sleep(0.05)

        # Verify mode actually changed
        if self.arm.mode != 1:
            print(f"[ARM] WARNING: servo mode NOT confirmed (mode={self.arm.mode})",
                  flush=True)
            self._servo_mode = False
            return False

        self._seed_servo_pos()
        self._servo_mode = True
        print("[ARM] Servo mode enabled")
        return True

    def try_reenable_servo(self):
        """Try to restore servo mode if lost. Returns True if servo confirmed."""
        if self._servo_mode and self.arm.mode == 1:
            return True
        if self.arm.has_error or self.arm.state >= 4:
            return False  # let error recovery handle it
        if self.recovering.locked():
            return False  # recovery in progress
        print("[ARM] Re-enabling servo mode...", flush=True)
        self.move_to_center(speed=60)
        time.sleep(0.3)
        return self.enable_servo()

    def _seed_servo_pos(self):
        """Read current arm position as servo starting point."""
        code, pos = self.arm.get_position()
        if code == 0:
            self._last_servo_pos = list(pos[:6])
        else:
            r, p, w = self.rpy
            cx, cy, cz = self.center
            self._last_servo_pos = [cx, cy, cz, r, p, w]

    def send_servo(self, x, y, z, speed=None, pitch=None, dt=0.04):
        """Send velocity-clamped servo command.

        Speed is used for velocity clamping: max_mm_per_frame = speed * dt.
        This means the existing speed/boost system maps directly to how fast
        the arm actually moves in servo mode.

        Args:
            x, y, z: target position in mm
            speed: desired speed in mm/s (for velocity clamping)
            pitch: tool pitch in degrees (default: from self.rpy[1])
            dt: control loop timestep in seconds
        Returns:
            SDK return code, -2 if arm not in servo mode
        """
        # Guard: skip if arm not in servo mode (prevents SDK warning spam)
        if self.arm.mode != 1:
            if self._servo_mode:
                self._servo_mode = False
                print(f"[ARM] Servo mode lost (arm.mode={self.arm.mode})", flush=True)
            return -2

        x, y, z = self.clamp_position(x, y, z)
        r, p, w = self.rpy
        if pitch is not None:
            p = pitch

        target = [x, y, z, r, p, w]

        if self._last_servo_pos is None:
            self._seed_servo_pos()

        # Velocity limits from speed
        spd = speed if speed is not None else self.speed
        max_mm = spd * dt
        max_deg = spd * dt * 0.3  # rotation scaling (conservative)

        clamped = self._velocity_clamp(self._last_servo_pos, target, max_mm, max_deg)
        ret = self.arm.set_servo_cartesian(clamped, is_radian=False)
        if ret == 0:
            self._last_servo_pos = list(clamped)
        return ret

    def _velocity_clamp(self, current, target, max_mm, max_deg):
        """Clamp step from current to target within per-frame limits.

        Position (XYZ): clamp Euclidean distance to max_mm.
        Rotation (RPY): clamp each axis independently to max_deg.
        """
        result = list(target)

        # Position — Euclidean clamp
        dx = target[0] - current[0]
        dy = target[1] - current[1]
        dz = target[2] - current[2]
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)

        if dist > max_mm and dist > 0:
            scale = max_mm / dist
            result[0] = current[0] + dx * scale
            result[1] = current[1] + dy * scale
            result[2] = current[2] + dz * scale

        # Rotation — per-axis clamp with angle wrapping
        for i in range(3, 6):
            delta = target[i] - current[i]
            # Normalize to [-180, 180] to avoid wrapping issues (e.g. -180 vs +180)
            delta = (delta + 180) % 360 - 180
            if abs(delta) > max_deg:
                delta = max_deg * (1 if delta > 0 else -1)
            result[i] = current[i] + delta

        return result

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
            was_servo = self._servo_mode
            time.sleep(0.3)
            self.arm.clean_error()
            self.arm.clean_warn()
            self.arm.motion_enable(enable=True)
            # Move to center in Mode 0 (blocking, safe)
            self.arm.set_mode(0)
            self.arm.set_state(0)
            time.sleep(0.5)
            self.move_to_center(speed=60)
            time.sleep(0.5)
            # Restore servo mode if it was active
            if was_servo:
                ok = self.enable_servo()
                if ok:
                    print(f"[RECOVER] OK -> center + servo (total: {self.error_count})")
                else:
                    print(f"[RECOVER] OK -> center, servo FAILED (total: {self.error_count})",
                          flush=True)
            else:
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
