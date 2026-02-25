"""
Continuous figure-8 loop motion with automatic error recovery.

Generates a smooth 3D Lissajous curve (figure-8) in Cartesian space
and sends position commands via the xArm SDK. When the arm hits a
collision / error state, it auto-recovers (clean_error → motion_enable
→ set_state) without needing to click the web UI dialog.

Usage:
    python loop_motion.py [--ip 127.0.0.1] [--speed 100] [--sensitivity 0]
"""

import argparse
import math
import time
import threading
from xarm.wrapper import XArmAPI

# --- Error code lookup (subset) ---
ERROR_NAMES = {
    0: "OK",
    1: "Emergency Stop",
    2: "Emergency IO",
    10: "Servo 1 error", 11: "Servo 2 error", 12: "Servo 3 error",
    13: "Servo 4 error", 14: "Servo 5 error", 15: "Servo 6 error",
    21: "Kinematic Error",
    22: "Self-Collision",
    23: "Joint Angle Limit",
    24: "Speed Limit",
    25: "Planning Error",
    31: "Collision (abnormal current)",
    35: "Safety Boundary Limit",
}


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
        self.arm.set_mode(0)  # position control mode
        self.arm.set_state(0)  # ready

        # Collision settings — skip on simulator (blocks indefinitely)
        if self.collision_sensitivity >= 0:
            try:
                print("  Setting collision sensitivity (may hang on simulator)...",
                      end="", flush=True)
                # Use a timeout wrapper: run in thread, give up after 3s
                result = [None]
                def _set_collision():
                    result[0] = self.arm.set_collision_sensitivity(
                        self.collision_sensitivity)
                t = threading.Thread(target=_set_collision, daemon=True)
                t.start()
                t.join(timeout=3.0)
                if t.is_alive():
                    print(" SKIPPED (simulator doesn't support it)")
                else:
                    print(f" OK (ret={result[0]})")
                    self.arm.set_collision_rebound(True)
            except Exception as e:
                print(f" FAILED ({e})")

        # Register callbacks for automatic recovery
        self.arm.register_error_warn_changed_callback(self._on_error_warn)
        self.arm.register_state_changed_callback(self._on_state_changed)

        print(f"Connected to {self.ip}")
        print(f"  Collision sensitivity: {self.collision_sensitivity} (0=off, 5=max)")
        print(f"  Speed: {self.speed} mm/s")
        self._print_position()

    def _print_position(self):
        code, pos = self.arm.get_position()
        if code == 0:
            print(f"  Current pos: X={pos[0]:.1f} Y={pos[1]:.1f} Z={pos[2]:.1f}")

    def _on_error_warn(self, data):
        err = data["error_code"]
        warn = data["warn_code"]
        if err != 0:
            name = ERROR_NAMES.get(err, f"Unknown({err})")
            print(f"\n[ERROR] code={err} ({name}) — auto-recovering...")
            self._recover()
        if warn != 0:
            print(f"[WARN] code={warn} — clearing")
            self.arm.clean_warn()

    def _on_state_changed(self, data):
        state = data["state"]
        if state >= 4:
            print(f"[STATE] arm entered state {state} (stopped/error)")

    def _recover(self):
        """Three-step recovery: clean_error → motion_enable → set_state(0)."""
        if not self.recovering.acquire(blocking=False):
            return  # another thread is already recovering
        try:
            self.error_count += 1
            time.sleep(0.3)  # let the arm settle

            self.arm.clean_error()
            self.arm.clean_warn()
            self.arm.motion_enable(enable=True)
            self.arm.set_mode(0)
            self.arm.set_state(0)

            time.sleep(0.2)
            code, state = self.arm.get_state()
            if state == 0 or state == 1:
                print(f"[RECOVER] Success (total errors: {self.error_count})")
            else:
                print(f"[RECOVER] Arm still in state {state}, retrying...")
                time.sleep(1)
                self.arm.clean_error()
                self.arm.motion_enable(enable=True)
                self.arm.set_mode(0)
                self.arm.set_state(0)
        finally:
            self.recovering.release()

    def run_figure8(self):
        """
        Generate a 3D Lissajous figure-8 and send positions in a loop.

        The curve is centered around a safe "home" position and sized to
        stay well within the arm's reachable workspace.
        """
        # Safe center position for the 850 (mm)
        cx, cy, cz = 300, 0, 300
        # Amplitude of the figure-8 (mm)
        ax, ay, az = 120, 120, 60
        # Fixed orientation (roll, pitch, yaw in degrees)
        roll, pitch, yaw = 180, 0, 0

        # First move to center slowly
        print("\nMoving to center position...")
        self.arm.set_position(cx, cy, cz, roll, pitch, yaw,
                              speed=50, wait=True)
        time.sleep(0.5)

        print("Starting figure-8 loop (Ctrl+C to stop)\n")
        t = 0.0
        dt = 0.04  # ~25 Hz command rate
        freq = 0.15  # cycles per second — slow and smooth

        while self.running:
            # Skip sending commands if arm is in error state
            if self.arm.has_error or self.arm.state >= 4:
                time.sleep(0.1)
                continue

            # Lissajous figure-8: x=sin(t), y=sin(2t), z=cos(t)
            phase = 2 * math.pi * freq * t
            x = cx + ax * math.sin(phase)
            y = cy + ay * math.sin(2 * phase)
            z = cz + az * math.cos(phase)

            ret = self.arm.set_position(
                x, y, z, roll, pitch, yaw,
                speed=self.speed,
                wait=False,  # non-blocking for smooth streaming
            )

            if ret == -2:
                # APIState.NOT_READY — arm in error state, will be
                # caught by callback and recovered
                time.sleep(0.2)
                continue

            t += dt
            time.sleep(dt)

    def disconnect(self):
        self.running = False
        if self.arm:
            self.arm.set_state(4)  # stop
            self.arm.disconnect()
            print(f"\nDisconnected. Total errors auto-recovered: {self.error_count}")


def main():
    parser = argparse.ArgumentParser(description="Continuous figure-8 with auto error recovery")
    parser.add_argument("--ip", default="127.0.0.1", help="xArm IP (default: 127.0.0.1)")
    parser.add_argument("--speed", type=int, default=100, help="Movement speed mm/s (default: 100)")
    parser.add_argument("--sensitivity", type=int, default=0, choices=range(6),
                        help="Collision sensitivity 0-5 (default: 0=off)")
    args = parser.parse_args()

    ctrl = ArmController(args.ip, args.speed, args.sensitivity)
    try:
        ctrl.connect()
        ctrl.run_figure8()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        ctrl.disconnect()


if __name__ == "__main__":
    main()
