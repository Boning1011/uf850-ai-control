"""
Loop Path Player — Continuously loop a Houdini .geo path on the simulator.

Loads a path file, auto-fits to arm workspace, prints coordinate mapping
for visual verification, and loops the path at a given speed.

Usage:
    python scripts/loop_path.py path_data/hou_parametric_lissajous_01_resampled.json
    python scripts/loop_path.py path_data/some_path.json --speed 300 --no-fit
"""

import argparse
import math
import sys
import time
import threading

from xarm.wrapper import XArmAPI

# Re-use path loading and mapping from path_validator
sys.path.insert(0, ".")
from scripts.path_validator import (
    load_path, houdini_to_arm, subsample_path, RPY,
)

ERROR_NAMES = {
    0: "OK", 1: "Emergency Stop", 21: "Kinematic Error",
    22: "Self-Collision", 23: "Joint Angle Limit", 24: "Speed Limit",
    25: "Planning Error", 31: "Collision (abnormal current)",
    35: "Safety Boundary Limit",
}


def compute_path_length(points):
    """Total Euclidean path length in mm."""
    total = 0.0
    for i in range(1, len(points)):
        dx = points[i][0] - points[i-1][0]
        dy = points[i][1] - points[i-1][1]
        dz = points[i][2] - points[i-1][2]
        total += math.sqrt(dx*dx + dy*dy + dz*dz)
    return total


def compute_segment_lengths(points):
    """Per-segment lengths."""
    segs = []
    for i in range(1, len(points)):
        dx = points[i][0] - points[i-1][0]
        dy = points[i][1] - points[i-1][1]
        dz = points[i][2] - points[i-1][2]
        segs.append(math.sqrt(dx*dx + dy*dy + dz*dz))
    return segs


class PathLooper:
    def __init__(self, ip, waypoints, speed):
        self.ip = ip
        self.waypoints = waypoints
        self.speed = speed
        self.arm = None
        self.recovering = threading.Lock()
        self.error_count = 0
        self.loop_count = 0
        self.running = True

    def connect(self):
        self.arm = XArmAPI(self.ip, is_radian=False)
        self.arm.clean_error()
        self.arm.clean_warn()
        self.arm.motion_enable(enable=True)
        self.arm.set_mode(0)
        self.arm.set_state(0)
        time.sleep(0.3)

        self.arm.register_error_warn_changed_callback(self._on_error)
        code, pos = self.arm.get_position()
        if code == 0:
            print(f"  Current pos: X={pos[0]:.1f} Y={pos[1]:.1f} Z={pos[2]:.1f}")

    def _on_error(self, data):
        err = data["error_code"]
        if err != 0:
            name = ERROR_NAMES.get(err, f"Unknown({err})")
            print(f"\n[ERROR] code={err} ({name}) — recovering...")
            self._recover()
        warn = data.get("warn_code", 0)
        if warn != 0:
            self.arm.clean_warn()

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
            time.sleep(0.3)
            print(f"[RECOVER] done (total errors: {self.error_count})")
        finally:
            self.recovering.release()

    def run(self):
        wps = self.waypoints
        n = len(wps)

        # Move to start slowly
        print(f"\nMoving to start: [{wps[0][0]:.1f}, {wps[0][1]:.1f}, {wps[0][2]:.1f}]")
        self.arm.set_position(wps[0][0], wps[0][1], wps[0][2], *RPY,
                              speed=80, wait=True)
        time.sleep(0.3)

        print(f"Looping {n} waypoints at {self.speed} mm/s (Ctrl+C to stop)\n")

        while self.running:
            # Skip if arm in error
            if self.arm.has_error or self.arm.state >= 4:
                time.sleep(0.2)
                continue

            t0 = time.time()
            ok = True

            for i, wp in enumerate(wps):
                if not self.running:
                    break
                if self.arm.has_error or self.arm.state >= 4:
                    ok = False
                    time.sleep(0.2)
                    break

                ret = self.arm.set_position(
                    wp[0], wp[1], wp[2], *RPY,
                    speed=self.speed, wait=True,
                )
                if ret not in (0, 1):
                    ok = False
                    time.sleep(0.2)
                    break

            if ok:
                self.loop_count += 1
                elapsed = time.time() - t0
                print(f"  Loop #{self.loop_count} done ({elapsed:.1f}s)")

    def disconnect(self):
        self.running = False
        if self.arm:
            self.arm.set_state(4)
            self.arm.disconnect()
            print(f"\nDisconnected. Loops: {self.loop_count}, Errors: {self.error_count}")


def main():
    parser = argparse.ArgumentParser(description="Loop a path on the arm/simulator")
    parser.add_argument("path_file", help="Path file (JSON/geo/CSV)")
    parser.add_argument("--ip", default="127.0.0.1")
    parser.add_argument("--speed", type=int, default=300,
                        help="Movement speed mm/s (default: 300)")
    parser.add_argument("--no-fit", action="store_true",
                        help="Skip auto-fit (use raw coordinates as mm)")
    parser.add_argument("--subsample", type=int, default=1,
                        help="Take every Nth point")
    args = parser.parse_args()

    # Load
    print(f"Loading: {args.path_file}")
    waypoints, is_houdini = load_path(args.path_file)
    print(f"  {len(waypoints)} points loaded")

    # Houdini -> arm coordinate transform
    if is_houdini and not args.no_fit:
        print(f"\n  === Houdini -> Arm mapping ===")
        print(f"  Hou X -> Arm X (forward), Hou Y -> Arm Z (up), Hou Z -> Arm Y (left)")
        waypoints = houdini_to_arm(waypoints)

    # Subsample
    if args.subsample > 1:
        orig = len(waypoints)
        waypoints = subsample_path(waypoints, args.subsample)
        print(f"  Subsampled: {orig} -> {len(waypoints)}")

    # Print mapped bounds
    arm_xs = [p[0] for p in waypoints]
    arm_ys = [p[1] for p in waypoints]
    arm_zs = [p[2] for p in waypoints]
    print(f"\n  === Final arm coords ===")
    print(f"  Arm X: [{min(arm_xs):.1f} .. {max(arm_xs):.1f}] mm  (forward/back)")
    print(f"  Arm Y: [{min(arm_ys):.1f} .. {max(arm_ys):.1f}] mm  (left/right)")
    print(f"  Arm Z: [{min(arm_zs):.1f} .. {max(arm_zs):.1f}] mm  (up/down)")

    # Path stats
    path_len = compute_path_length(waypoints)
    segs = compute_segment_lengths(waypoints)
    print(f"\n  Path length: {path_len:.1f} mm")
    print(f"  Segments: {len(segs)}, avg={path_len/len(segs):.1f} mm, "
          f"min={min(segs):.1f} mm, max={max(segs):.1f} mm")
    print(f"  Est. loop time at {args.speed} mm/s: {path_len/args.speed:.1f}s")

    # Connect and loop
    looper = PathLooper(args.ip, waypoints, args.speed)
    try:
        print(f"\nConnecting to {args.ip}...")
        looper.connect()
        looper.run()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        looper.disconnect()


if __name__ == "__main__":
    main()
