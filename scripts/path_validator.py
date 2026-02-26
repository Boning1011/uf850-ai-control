"""
Path Validator — Automated path feasibility testing and speed optimization.

Given a 3D path (JSON, CSV, or Houdini .geo), validates reachability via IK
and uses binary search on the Docker simulator to find the maximum feasible speed.

Supported formats:
  - Simple JSON: [[x, y, z], ...]
  - CSV: x,y,z per row (with optional header)
  - Houdini .geo JSON: exported via Geometry ROP with "JSON" format

Usage:
    python scripts/path_validator.py path_data/example_square.json
    python scripts/path_validator.py path_data/hou_parametric_celeis_01.json --ik-only
    python scripts/path_validator.py path_data/my_path.csv --speed-max 800 --tolerance 5
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


# ---------- Houdini .geo parser ----------

def _geo_array_to_dict(arr):
    """Convert Houdini's alternating [key, value, key, value, ...] array to dict.
    If input is already a dict, return it as-is."""
    if isinstance(arr, dict):
        return arr
    if not isinstance(arr, list):
        return {}
    d = {}
    i = 0
    while i < len(arr) - 1:
        key = arr[i]
        val = arr[i + 1]
        if isinstance(key, str):
            d[key] = val
            i += 2
        else:
            i += 1
    return d


def _is_houdini_geo(data):
    """Detect if JSON data is Houdini .geo format."""
    return (isinstance(data, list)
            and len(data) >= 2
            and data[0] == "fileversion")


def parse_houdini_geo(data):
    """Extract point positions from Houdini .geo JSON -> list of [x, y, z]."""
    geo = _geo_array_to_dict(data)

    pointcount = geo.get("pointcount", 0)
    if pointcount == 0:
        raise ValueError("Houdini .geo file has 0 points")

    # Navigate: attributes > pointattributes > [attr_entry] > values > tuples
    attrs_raw = geo.get("attributes")
    if not attrs_raw:
        raise ValueError("No 'attributes' in Houdini .geo")

    attrs = _geo_array_to_dict(attrs_raw)
    point_attrs = attrs.get("pointattributes")
    if not point_attrs:
        raise ValueError("No 'pointattributes' in Houdini .geo")

    # Find the "P" (position) attribute
    tuples_data = None
    for attr_entry in point_attrs:
        # Each attr_entry is [metadata_array, data_array]
        if not isinstance(attr_entry, list) or len(attr_entry) < 2:
            continue
        meta = _geo_array_to_dict(attr_entry[0])
        if meta.get("name") == "P":
            values_dict = _geo_array_to_dict(attr_entry[1])
            values_inner = values_dict.get("values")
            if values_inner:
                inner = _geo_array_to_dict(values_inner)
                tuples_data = inner.get("tuples")
            break

    if not tuples_data:
        raise ValueError("Could not find point position data (attribute 'P') in .geo")

    # Validate
    points = []
    for i, pt in enumerate(tuples_data):
        if len(pt) < 3:
            raise ValueError(f"Point #{i} has {len(pt)} components, need 3")
        points.append([float(pt[0]), float(pt[1]), float(pt[2])])

    info = _geo_array_to_dict(geo.get("info", []))
    software = info.get("software", "unknown")
    print(f"  Houdini .geo detected ({software}, {pointcount} points)")

    return points


# ---------- coordinate mapping ----------

def houdini_to_arm(points, scale=1000.0):
    """Direct coordinate transform from Houdini scene to arm coordinates.

    No auto-fit, no scaling-to-fit — preserves the exact spatial relationship
    the user set up in Houdini relative to the arm model.

    Axis mapping (Houdini Y-up right-hand, arm faces +X in Houdini):
      Houdini X  ->  Arm X  (forward, away from base)
      Houdini Y  ->  Arm Z  (up)
      Houdini Z  ->  Arm Y  (left, right-hand rule)

    Scale: Houdini units (meters) × 1000 -> mm
    """
    if not points:
        return points

    # Print source bounds
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    zs = [p[2] for p in points]
    print(f"  Houdini bounds:")
    print(f"    X: [{min(xs):.4f}, {max(xs):.4f}] (-> Arm X, forward)")
    print(f"    Y: [{min(ys):.4f}, {max(ys):.4f}] (-> Arm Z, up)")
    print(f"    Z: [{min(zs):.4f}, {max(zs):.4f}] (-> Arm Y, left)")
    print(f"  Scale: x{scale} (Houdini unit -> mm)")

    mapped = []
    for p in points:
        arm_x = p[0] * scale
        arm_z = p[1] * scale
        arm_y = p[2] * scale
        mapped.append([round(arm_x, 2), round(arm_y, 2), round(arm_z, 2)])

    # Print mapped bounds
    arm_xs = [p[0] for p in mapped]
    arm_ys = [p[1] for p in mapped]
    arm_zs = [p[2] for p in mapped]
    print(f"  Arm bounds (mm):")
    print(f"    X: [{min(arm_xs):.1f}, {max(arm_xs):.1f}]  (forward/back)")
    print(f"    Y: [{min(arm_ys):.1f}, {max(arm_ys):.1f}]  (left/right)")
    print(f"    Z: [{min(arm_zs):.1f}, {max(arm_zs):.1f}]  (up/down)")

    return mapped


# ---------- subsampling ----------

def subsample_path(points, step):
    """Take every Nth point, always including first and last."""
    if step <= 1 or len(points) <= step:
        return points
    sampled = points[::step]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled


# ---------- path loading ----------

def load_path(filepath):
    """Load a 3D path from JSON (simple or Houdini .geo) or CSV -> list of [x, y, z].

    Returns (points, is_houdini) where is_houdini indicates if coordinate mapping is needed.
    """
    ext = os.path.splitext(filepath)[1].lower()

    if ext == ".json":
        with open(filepath, "r") as f:
            data = json.load(f)

        # Detect format
        if _is_houdini_geo(data):
            return parse_houdini_geo(data), True
        else:
            # Simple [[x,y,z], ...] format
            points = data

    elif ext == ".csv":
        points = []
        with open(filepath, "r", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                try:
                    float(row[0])
                except ValueError:
                    continue
                points.append([float(v) for v in row[:3]])
    else:
        raise ValueError(f"Unsupported file format: {ext} (use .json or .csv)")

    # Validate simple format
    for i, pt in enumerate(points):
        if len(pt) < 3:
            raise ValueError(f"Waypoint #{i} has {len(pt)} values, need 3 (x, y, z)")
        points[i] = [float(pt[0]), float(pt[1]), float(pt[2])]

    if len(points) < 2:
        raise ValueError(f"Path needs at least 2 waypoints, got {len(points)}")

    return points, False


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
                print(f"    wp#{r['index']}: {r['xyz']} - IK failed")

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
    parser.add_argument("path_file", help="Path file (JSON, Houdini .geo, or CSV)")
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
    parser.add_argument("--subsample", type=int, default=1,
                        help="Take every Nth point (default: 1 = all points)")
    parser.add_argument("--no-fit", action="store_true",
                        help="Skip auto-fit mapping for Houdini files (use raw coords)")
    args = parser.parse_args()

    # Load path
    print(f"Loading path: {args.path_file}")
    waypoints, is_houdini = load_path(args.path_file)
    print(f"  {len(waypoints)} points loaded")

    # Transform Houdini coordinates to arm space
    if is_houdini and not args.no_fit:
        print(f"\n  Mapping Houdini -> arm coordinates...")
        waypoints = houdini_to_arm(waypoints)

    # Subsample
    if args.subsample > 1:
        orig_count = len(waypoints)
        waypoints = subsample_path(waypoints, args.subsample)
        print(f"  Subsampled: {orig_count} -> {len(waypoints)} points (every {args.subsample}th)")

    if len(waypoints) < 2:
        print("ERROR: Need at least 2 waypoints after processing")
        sys.exit(1)

    print(f"  Final path: {len(waypoints)} waypoints")

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
            fail_count = sum(1 for r in reachability["results"] if not r["ik_ok"])
            print(f"  {fail_count}/{len(waypoints)} unreachable")
            # Show first few
            shown = 0
            for r in reachability["results"]:
                if not r["ik_ok"]:
                    print(f"    wp#{r['index']}: [{r['xyz'][0]:.1f}, {r['xyz'][1]:.1f}, {r['xyz'][2]:.1f}]")
                    shown += 1
                    if shown >= 5:
                        remaining = fail_count - shown
                        if remaining > 0:
                            print(f"    ... and {remaining} more")
                        break
            if not args.ik_only:
                print("  Continuing with speed test anyway...")
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
