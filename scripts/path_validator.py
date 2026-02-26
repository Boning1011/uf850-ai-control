"""
Path Validator — Automated path feasibility testing and speed optimization.

Given a 3D path (JSON or CSV), validates reachability via IK and uses binary
search on the Docker simulator to find the maximum feasible speed.

Usage:
    python scripts/path_validator.py path_data/example_square.json
    python scripts/path_validator.py path_data/my_path.csv --speed-max 800 --tolerance 5
    python scripts/path_validator.py path_data/my_path.json --output results.json
"""

import argparse
import csv
import json
import math
import os
import sys
import time

from xarm.wrapper import XArmAPI

# ---------- constants ----------

# Fixed end-effector orientation (degrees) — avoids IK singularities
RPY = (180, 0, 0)

# UF850 (XARM6_X12) joint limits in degrees
# Source: xarm SDK x_config.py JOINT_LIMITS[Axis.XARM6][Type.XARM6_X12]
JOINT_LIMITS_DEG = [
    (-360.0, 360.0),     # J1
    (-132.0, 132.0),     # J2
    (-242.0, 3.5),       # J3
    (-360.0, 360.0),     # J4
    (-124.0, 124.0),     # J5
    (-360.0, 360.0),     # J6
]

# Error code names (subset)
ERROR_NAMES = {
    0: "OK", 1: "Emergency Stop", 2: "Emergency IO",
    21: "Kinematic Error", 22: "Self-Collision", 23: "Joint Angle Limit",
    24: "Speed Limit", 25: "Planning Error",
    31: "Collision (abnormal current)", 35: "Safety Boundary Limit",
}


# ---------- path loading ----------

def load_path(filepath):
    """Load a 3D path from JSON or CSV file -> list of [x, y, z]."""
    ext = os.path.splitext(filepath)[1].lower()

    if ext == ".json":
        with open(filepath, "r") as f:
            data = json.load(f)
    elif ext == ".csv":
        data = []
        with open(filepath, "r", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                # Skip header rows (non-numeric first field)
                try:
                    float(row[0])
                except ValueError:
                    continue
                data.append([float(v) for v in row[:3]])
    else:
        raise ValueError(f"Unsupported file format: {ext} (use .json or .csv)")

    # Validate
    for i, pt in enumerate(data):
        if len(pt) < 3:
            raise ValueError(f"Waypoint #{i} has {len(pt)} values, need 3 (x, y, z)")
        data[i] = [float(pt[0]), float(pt[1]), float(pt[2])]

    if len(data) < 2:
        raise ValueError(f"Path needs at least 2 waypoints, got {len(data)}")

    return data


# ---------- IK reachability check ----------

def check_reachability(arm, waypoints):
    """Check IK solvability and joint margins for every waypoint.

    Returns dict with:
        ok: bool — all waypoints reachable
        results: list of per-waypoint dicts
        worst_margin: (joint_index, margin_deg, waypoint_index)
    """
    results = []
    all_ok = True
    worst_margin = None  # (joint_idx, margin_deg, wp_idx)

    for i, wp in enumerate(waypoints):
        pose = [wp[0], wp[1], wp[2], *RPY]

        # IK check
        code, angles = arm.get_inverse_kinematics(pose, input_is_radian=False,
                                                   return_is_radian=False)
        ik_ok = (code == 0)

        # TCP limit check
        tcp_code, tcp_limit = arm.is_tcp_limit(pose, is_radian=False)
        tcp_ok = (tcp_code == 0 and tcp_limit is not None and not tcp_limit)

        wp_result = {
            "index": i,
            "xyz": wp,
            "ik_ok": ik_ok,
            "tcp_ok": tcp_ok,
            "angles": angles if ik_ok else None,
            "margins": None,
        }

        if ik_ok and angles:
            # Compute per-joint margin (degrees to nearest limit)
            margins = []
            for j in range(min(len(angles), len(JOINT_LIMITS_DEG))):
                lo, hi = JOINT_LIMITS_DEG[j]
                angle = angles[j]
                margin = min(abs(angle - lo), abs(angle - hi))
                margins.append(round(margin, 2))

                if worst_margin is None or margin < worst_margin[1]:
                    worst_margin = (j, margin, i)

            wp_result["margins"] = margins

        if not ik_ok or not tcp_ok:
            all_ok = False

        results.append(wp_result)

    return {
        "ok": all_ok,
        "results": results,
        "worst_margin": worst_margin,
    }


# ---------- arm recovery ----------

def recover(arm):
    """Standard 4-step recovery sequence."""
    time.sleep(0.3)
    arm.clean_error()
    arm.clean_warn()
    arm.motion_enable(enable=True)
    arm.set_mode(0)
    arm.set_state(0)
    time.sleep(0.3)


# ---------- path execution ----------

def try_run_path(arm, waypoints, speed):
    """Execute path at given speed. Returns (success, fail_index, error_code).

    Uses wait=True for each waypoint so we get accurate per-point feedback.
    Moves to the first waypoint at a safe speed before starting the timed run.
    """
    # Move to start at safe speed
    wp0 = waypoints[0]
    arm.set_position(wp0[0], wp0[1], wp0[2], *RPY, speed=50, wait=True)
    if arm.error_code != 0:
        return False, 0, arm.error_code

    time.sleep(0.2)

    # Execute path at test speed
    for i, wp in enumerate(waypoints[1:], start=1):
        ret = arm.set_position(wp[0], wp[1], wp[2], *RPY,
                               speed=speed, wait=True)
        if arm.error_code != 0:
            return False, i, arm.error_code
        if ret != 0 and ret != 1:  # 0=success, 1=in_progress
            return False, i, ret

    return True, -1, 0


# ---------- binary search speed optimization ----------

def find_max_speed(arm, waypoints, low=50, high=1000, tolerance=10):
    """Binary search for the maximum speed at which the path executes without error.

    Returns dict with max_speed, iterations, and search history.
    """
    history = []
    best_speed = low
    iteration = 0

    # First: verify the path works at the low speed
    print(f"  Verifying path at minimum speed ({low} mm/s)...")
    ok, fail_idx, err = try_run_path(arm, waypoints, speed=low)
    if not ok:
        err_name = ERROR_NAMES.get(err, f"Unknown({err})")
        print(f"  FAILED even at {low} mm/s! wp#{fail_idx} err={err} ({err_name})")
        recover(arm)
        return {
            "max_speed": 0,
            "iterations": 0,
            "history": [],
            "error": f"Path fails at minimum speed {low} mm/s",
        }

    print(f"  Minimum speed OK. Starting binary search [{low}..{high}] mm/s...\n")

    while high - low > tolerance:
        mid = round((low + high) / 2)
        iteration += 1

        ok, fail_idx, err = try_run_path(arm, waypoints, speed=mid)

        entry = {"iteration": iteration, "speed": mid, "ok": ok}
        if not ok:
            err_name = ERROR_NAMES.get(err, f"Unknown({err})")
            entry["fail_wp"] = fail_idx
            entry["error"] = f"{err} ({err_name})"
            print(f"  [{iteration}] speed={mid} mm/s -> FAIL @wp#{fail_idx} err={err} ({err_name})")
            recover(arm)
            high = mid
        else:
            print(f"  [{iteration}] speed={mid} mm/s -> OK")
            best_speed = mid
            low = mid

        history.append(entry)

    return {
        "max_speed": best_speed,
        "iterations": iteration,
        "history": history,
    }


# ---------- report ----------

def print_report(path_file, waypoints, reachability, speed_result):
    """Print human-readable validation report."""
    n = len(waypoints)
    ik_ok_count = sum(1 for r in reachability["results"] if r["ik_ok"])
    wm = reachability["worst_margin"]

    print("\n" + "=" * 50)
    print("  Path Validation Report")
    print("=" * 50)
    print(f"  Path file:       {os.path.basename(path_file)}")
    print(f"  Waypoints:       {n}")
    print(f"  IK reachability: {ik_ok_count}/{n}", end="")
    if ik_ok_count == n:
        print(" (all OK)")
    else:
        print(" ** SOME UNREACHABLE **")
        for r in reachability["results"]:
            if not r["ik_ok"]:
                print(f"    wp#{r['index']}: {r['xyz']} — IK failed")

    if wm:
        joint_name = f"J{wm[0] + 1}"
        print(f"  Closest joint limit: {joint_name} at wp#{wm[2]} (margin: {wm[1]:.1f} deg)")

    if "error" in speed_result:
        print(f"\n  Speed test:      FAILED - {speed_result['error']}")
    else:
        max_spd = speed_result["max_speed"]
        arm_max = 1000  # UF850 theoretical max mm/s
        utilization = max_spd / arm_max * 100
        print(f"\n  Max speed:       {max_spd} mm/s")
        print(f"  Arm limit:       {arm_max} mm/s")
        print(f"  Utilization:     {utilization:.1f}%")
        print(f"  Search iters:    {speed_result['iterations']}")

    print("=" * 50)


def save_results(output_path, path_file, waypoints, reachability, speed_result):
    """Save results to JSON file."""
    data = {
        "path_file": os.path.basename(path_file),
        "waypoints_count": len(waypoints),
        "rpy": list(RPY),
        "reachability": {
            "all_ok": reachability["ok"],
            "per_waypoint": reachability["results"],
            "worst_margin": {
                "joint": reachability["worst_margin"][0] if reachability["worst_margin"] else None,
                "margin_deg": reachability["worst_margin"][1] if reachability["worst_margin"] else None,
                "waypoint": reachability["worst_margin"][2] if reachability["worst_margin"] else None,
            },
        },
        "speed": speed_result,
    }
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nResults saved to: {output_path}")


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser(
        description="Path Validator — test path feasibility and find max speed")
    parser.add_argument("path_file", help="Path file (JSON or CSV)")
    parser.add_argument("--ip", default="127.0.0.1",
                        help="Arm/simulator IP (default: 127.0.0.1)")
    parser.add_argument("--speed-min", type=int, default=50,
                        help="Binary search lower bound mm/s (default: 50)")
    parser.add_argument("--speed-max", type=int, default=1000,
                        help="Binary search upper bound mm/s (default: 1000)")
    parser.add_argument("--tolerance", type=int, default=10,
                        help="Speed search precision mm/s (default: 10)")
    parser.add_argument("--output", default=None,
                        help="Save results to JSON file")
    parser.add_argument("--ik-only", action="store_true",
                        help="Only run IK reachability check, skip speed test")
    args = parser.parse_args()

    # Load path
    print(f"Loading path: {args.path_file}")
    waypoints = load_path(args.path_file)
    print(f"  {len(waypoints)} waypoints loaded")

    # Connect to arm
    print(f"\nConnecting to {args.ip}...")
    arm = XArmAPI(args.ip, is_radian=False)
    arm.clean_error()
    arm.clean_warn()
    arm.motion_enable(enable=True)
    arm.set_mode(0)
    arm.set_state(0)
    time.sleep(0.3)

    code, pos = arm.get_position()
    if code == 0:
        print(f"  Current pos: X={pos[0]:.1f} Y={pos[1]:.1f} Z={pos[2]:.1f}")

    try:
        # Phase 1: IK reachability
        print(f"\n--- Phase 1: IK Reachability Check ---")
        reachability = check_reachability(arm, waypoints)

        if not reachability["ok"]:
            print("  WARNING: Some waypoints are unreachable!")
            for r in reachability["results"]:
                if not r["ik_ok"]:
                    print(f"    wp#{r['index']}: {r['xyz']}")
            if not args.ik_only:
                print("  Continuing with speed test anyway (unreachable points may cause errors)...")
        else:
            print(f"  All {len(waypoints)} waypoints reachable")
            wm = reachability["worst_margin"]
            if wm:
                print(f"  Tightest joint: J{wm[0]+1} at wp#{wm[2]} (margin: {wm[1]:.1f} deg)")

        # Phase 2: Speed optimization
        speed_result = {"max_speed": 0, "iterations": 0, "history": [],
                        "error": "Skipped (--ik-only)"}

        if not args.ik_only:
            print(f"\n--- Phase 2: Speed Optimization [{args.speed_min}..{args.speed_max}] mm/s ---")
            speed_result = find_max_speed(
                arm, waypoints,
                low=args.speed_min,
                high=args.speed_max,
                tolerance=args.tolerance,
            )

        # Report
        print_report(args.path_file, waypoints, reachability, speed_result)

        # Save
        if args.output:
            save_results(args.output, args.path_file, waypoints,
                         reachability, speed_result)

    finally:
        arm.set_state(4)
        arm.disconnect()
        print("Disconnected.")


if __name__ == "__main__":
    main()
