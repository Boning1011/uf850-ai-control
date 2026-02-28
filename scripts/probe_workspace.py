"""
probe_workspace.py — Probe UF850 reachable workspace via IK checks.

Connects to the xArm controller/simulator and systematically tests IK feasibility
across a grid of positions and orientations. No physical movement occurs — all
checks are pure inverse kinematics computations.

Phases:
  1. XYZ envelope at default RPY (180,0,0) — find reachable boundary per Z layer
  2. Pitch range at coarser grid — how much pitch freedom at each position
  3. Yaw range at coarser grid — how much yaw freedom at each position
  4. Preset validation — verify named positions + their RPY ranges

Output: doc/arm_workspace_data.json (structured reference data)

Usage:
    uv run python scripts/probe_workspace.py
    uv run python scripts/probe_workspace.py --fine       # 25mm grid (slower)
    uv run python scripts/probe_workspace.py --presets-only
"""

import argparse
import json
import math
import os
import sys
import time

from xarm.wrapper import XArmAPI

# ---------- UF850 joint limits ----------

JOINT_LIMITS_DEG = [
    (-360.0, 360.0),   # J1
    (-132.0, 132.0),   # J2
    (-242.0, 3.5),     # J3
    (-360.0, 360.0),   # J4
    (-124.0, 124.0),   # J5
    (-360.0, 360.0),   # J6
]

DEFAULT_RPY = (180, 0, 0)


# ---------- semantic preset positions ----------

PRESETS = {
    # Core positions
    "home":            {"xyz": [300, 0, 400],    "rpy": [180, 0, 0],    "desc": "Default center position"},
    "high_center":     {"xyz": [200, 0, 800],    "rpy": [180, 0, 0],    "desc": "Raised high, centered"},
    "high_forward":    {"xyz": [400, 0, 700],    "rpy": [180, 0, 0],    "desc": "High and reaching forward"},
    "overhead":        {"xyz": [100, 0, 900],    "rpy": [180, 0, 0],    "desc": "Near-vertical above base"},
    "forward_far":     {"xyz": [600, 0, 400],    "rpy": [180, 0, 0],    "desc": "Far forward reach"},
    "forward_mid":     {"xyz": [450, 0, 400],    "rpy": [180, 0, 0],    "desc": "Moderate forward"},
    "low_center":      {"xyz": [250, 0, 200],    "rpy": [180, 0, 0],    "desc": "Low position, centered"},
    "low_close":       {"xyz": [150, 0, 250],    "rpy": [180, 0, 0],    "desc": "Close to base, low (rest)"},
    "left_wide":       {"xyz": [300, 200, 450],  "rpy": [180, 0, 0],    "desc": "Extended to the left"},
    "right_wide":      {"xyz": [300, -200, 450], "rpy": [180, 0, 0],    "desc": "Extended to the right"},
    # Expression-oriented
    "alert_forward":   {"xyz": [500, 0, 500],    "rpy": [180, -15, 0],  "desc": "Alert, leaning forward"},
    "curious_tilt":    {"xyz": [400, 0, 500],    "rpy": [180, 20, 15],  "desc": "Curious, head tilted"},
    "present_left":    {"xyz": [350, 150, 500],  "rpy": [180, 0, -20],  "desc": "Presenting leftward"},
    "present_right":   {"xyz": [350, -150, 500], "rpy": [180, 0, 20],   "desc": "Presenting rightward"},
    "shy_low":         {"xyz": [200, 0, 300],    "rpy": [180, 25, 0],   "desc": "Shy, looking down"},
    "excited_high":    {"xyz": [350, 0, 700],    "rpy": [180, -20, 0],  "desc": "Excited, reaching up"},
    "dormant":         {"xyz": [150, 0, 200],    "rpy": [180, 0, 0],    "desc": "Dormant/sleeping pose"},
    "scan_left":       {"xyz": [400, 200, 450],  "rpy": [180, 0, -30],  "desc": "Scanning to the left"},
    "scan_right":      {"xyz": [400, -200, 450], "rpy": [180, 0, 30],   "desc": "Scanning to the right"},
}


# ---------- IK check ----------

def check_ik(arm, x, y, z, roll=180, pitch=0, yaw=0):
    """Check IK feasibility. Returns (reachable, angles, margins)."""
    pose = [x, y, z, roll, pitch, yaw]
    code, angles = arm.get_inverse_kinematics(
        pose, input_is_radian=False, return_is_radian=False
    )
    if code != 0:
        return False, None, None

    margins = []
    if angles:
        for j in range(min(len(angles), len(JOINT_LIMITS_DEG))):
            lo, hi = JOINT_LIMITS_DEG[j]
            margin = min(abs(angles[j] - lo), abs(angles[j] - hi))
            margins.append(round(margin, 2))

    return True, angles, margins


# ---------- Phase 1: XYZ Envelope ----------

def probe_envelope(arm, step=50):
    """Sweep XYZ grid at default RPY, find reachable boundary per Z layer."""
    print("\n=== Phase 1: XYZ Envelope (RPY=180,0,0) ===")
    print(f"  Grid step: {step}mm")

    r, p, w = DEFAULT_RPY
    z_values = list(range(0, 1101, step))
    x_values = list(range(-200, 851, step))
    y_values = list(range(-600, 601, step))

    total = len(z_values) * len(x_values) * len(y_values)
    checked = 0
    reachable_total = 0
    t0 = time.monotonic()

    layers = {}

    for zi, z in enumerate(z_values):
        boundary = {}  # x -> [y_min, y_max]
        layer_reachable = 0

        for x in x_values:
            y_min_found = None
            y_max_found = None

            for y in y_values:
                checked += 1
                ok, _, _ = check_ik(arm, x, y, z, r, p, w)
                if ok:
                    layer_reachable += 1
                    if y_min_found is None or y < y_min_found:
                        y_min_found = y
                    if y_max_found is None or y > y_max_found:
                        y_max_found = y

            if y_min_found is not None:
                boundary[x] = [y_min_found, y_max_found]

        reachable_total += layer_reachable

        if boundary:
            x_keys = sorted(boundary.keys())
            x_range = [x_keys[0], x_keys[-1]]
            all_y_min = min(v[0] for v in boundary.values())
            all_y_max = max(v[1] for v in boundary.values())
            y_range = [all_y_min, all_y_max]

            # Max reach radius from Z-axis in XY plane
            max_reach = 0
            for bx, (ylo, yhi) in boundary.items():
                r1 = math.sqrt(bx**2 + ylo**2)
                r2 = math.sqrt(bx**2 + yhi**2)
                max_reach = max(max_reach, r1, r2)

            layers[str(z)] = {
                "reachable_count": layer_reachable,
                "x_range": x_range,
                "y_range": y_range,
                "max_reach_radius": round(max_reach, 1),
                "boundary": {str(bx): bv for bx, bv in sorted(boundary.items())},
            }

            elapsed = time.monotonic() - t0
            rate = checked / elapsed if elapsed > 0 else 0
            eta = (total - checked) / rate if rate > 0 else 0
            print(f"  Z={z:5d}: X=[{x_range[0]:4d},{x_range[1]:4d}] "
                  f"Y=[{y_range[0]:4d},{y_range[1]:4d}] "
                  f"reach={max_reach:.0f}mm  "
                  f"[{checked}/{total}, ETA {eta:.0f}s]")
        else:
            layers[str(z)] = {"reachable_count": 0}
            elapsed = time.monotonic() - t0
            rate = checked / elapsed if elapsed > 0 else 0
            eta = (total - checked) / rate if rate > 0 else 0
            print(f"  Z={z:5d}: unreachable  [{checked}/{total}, ETA {eta:.0f}s]")

    elapsed = time.monotonic() - t0
    print(f"\n  Done: {reachable_total}/{checked} reachable "
          f"({reachable_total/max(checked,1)*100:.1f}%) in {elapsed:.1f}s")

    return layers


# ---------- Phase 2: Pitch range ----------

def probe_pitch_ranges(arm, step=100):
    """At a coarser grid, find feasible pitch range at each position."""
    print("\n=== Phase 2: Pitch Range Map ===")

    z_values = list(range(100, 901, step))
    x_values = list(range(100, 701, step))
    y_values = list(range(-200, 201, step))
    pitch_values = list(range(-90, 91, 5))

    results = {}
    tested = 0
    t0 = time.monotonic()

    for z in z_values:
        for x in x_values:
            for y in y_values:
                ok, _, _ = check_ik(arm, x, y, z)
                if not ok:
                    continue

                tested += 1
                p_min = p_max = None

                for pitch in pitch_values:
                    ok, _, _ = check_ik(arm, x, y, z, 180, pitch, 0)
                    if ok:
                        if p_min is None:
                            p_min = pitch
                        p_max = pitch

                key = f"{x},{y},{z}"
                results[key] = {
                    "xyz": [x, y, z],
                    "pitch_min": p_min,
                    "pitch_max": p_max,
                    "pitch_span": (p_max - p_min) if p_min is not None else 0,
                }

    elapsed = time.monotonic() - t0
    print(f"  {tested} reachable positions tested in {elapsed:.1f}s")

    # Summary by Z
    by_z = {}
    for data in results.values():
        z = data["xyz"][2]
        if z not in by_z:
            by_z[z] = []
        by_z[z].append(data["pitch_span"])

    for z in sorted(by_z.keys()):
        spans = by_z[z]
        avg = sum(spans) / len(spans)
        print(f"  Z={z:4d}: avg pitch span={avg:.0f} deg "
              f"(min={min(spans)}, max={max(spans)}, n={len(spans)})")

    return results


# ---------- Phase 2b: Yaw range ----------

def probe_yaw_ranges(arm, step=100):
    """At coarser grid (Y=0 only), find feasible yaw range."""
    print("\n=== Phase 2b: Yaw Range Map ===")

    z_values = list(range(200, 801, step))
    x_values = list(range(100, 601, step))
    yaw_values = list(range(-180, 181, 5))

    results = {}
    tested = 0
    t0 = time.monotonic()

    for z in z_values:
        for x in x_values:
            ok, _, _ = check_ik(arm, x, 0, z)
            if not ok:
                continue

            tested += 1
            yaw_min = yaw_max = None

            for yaw in yaw_values:
                ok, _, _ = check_ik(arm, x, 0, z, 180, 0, yaw)
                if ok:
                    if yaw_min is None:
                        yaw_min = yaw
                    yaw_max = yaw

            key = f"{x},0,{z}"
            results[key] = {
                "xyz": [x, 0, z],
                "yaw_min": yaw_min,
                "yaw_max": yaw_max,
                "yaw_span": (yaw_max - yaw_min) if yaw_min is not None else 0,
            }

    elapsed = time.monotonic() - t0
    print(f"  {tested} positions tested in {elapsed:.1f}s")

    for key, data in sorted(results.items(), key=lambda kv: (kv[1]["xyz"][2], kv[1]["xyz"][0])):
        x, _, z = data["xyz"]
        span = data["yaw_span"]
        if data["yaw_min"] is not None:
            print(f"  X={x:4d} Z={z:4d}: yaw=[{data['yaw_min']},{data['yaw_max']}] ({span} deg)")

    return results


# ---------- Phase 3: Preset validation ----------

def validate_presets(arm):
    """Validate all presets with pitch and yaw range testing."""
    print("\n=== Phase 3: Preset Validation ===")

    results = {}

    for name, preset in PRESETS.items():
        x, y, z = preset["xyz"]
        r, p, w = preset["rpy"]

        ok, angles, margins = check_ik(arm, x, y, z, r, p, w)

        # Pitch range
        pitch_min = pitch_max = None
        for pitch in range(-90, 91, 5):
            pok, _, _ = check_ik(arm, x, y, z, 180, pitch, w)
            if pok:
                if pitch_min is None:
                    pitch_min = pitch
                pitch_max = pitch

        # Yaw range
        yaw_min = yaw_max = None
        for yaw in range(-180, 181, 5):
            yok, _, _ = check_ik(arm, x, y, z, 180, p, yaw)
            if yok:
                if yaw_min is None:
                    yaw_min = yaw
                yaw_max = yaw

        results[name] = {
            **preset,
            "reachable": ok,
            "angles": [round(a, 2) for a in angles] if angles else None,
            "margins": margins,
            "min_margin": round(min(margins), 1) if margins else None,
            "pitch_range": [pitch_min, pitch_max] if pitch_min is not None else None,
            "yaw_range": [yaw_min, yaw_max] if yaw_min is not None else None,
        }

        status = "OK" if ok else "FAIL"
        p_str = f"pitch=[{pitch_min},{pitch_max}]" if pitch_min is not None else "pitch=N/A"
        y_str = f"yaw=[{yaw_min},{yaw_max}]" if yaw_min is not None else "yaw=N/A"
        m_str = f"margin={min(margins):.1f} deg" if margins else ""
        print(f"  {name:20s} -> {status:4s}  {p_str:20s} {y_str:20s} {m_str}")

    return results


# ---------- Summary ----------

def compute_summary(layers, presets):
    """Extract key metrics from probe data."""
    s = {}

    reachable_z = [int(z) for z, d in layers.items() if d["reachable_count"] > 0]
    if reachable_z:
        s["z_min"] = min(reachable_z)
        s["z_max"] = max(reachable_z)

    # Max forward reach (X)
    max_x = 0
    max_x_z = 0
    for z_str, d in layers.items():
        if "x_range" in d and d["x_range"] and d["x_range"][1] > max_x:
            max_x = d["x_range"][1]
            max_x_z = int(z_str)
    s["max_forward_x"] = max_x
    s["max_forward_at_z"] = max_x_z

    # Max lateral reach (|Y|)
    max_y = 0
    max_y_z = 0
    for z_str, d in layers.items():
        if "y_range" in d and d["y_range"]:
            extent = max(abs(d["y_range"][0]), abs(d["y_range"][1]))
            if extent > max_y:
                max_y = extent
                max_y_z = int(z_str)
    s["max_lateral_y"] = max_y
    s["max_lateral_at_z"] = max_y_z

    # Max reach radius
    max_r = 0
    max_r_z = 0
    for z_str, d in layers.items():
        if "max_reach_radius" in d and d["max_reach_radius"] > max_r:
            max_r = d["max_reach_radius"]
            max_r_z = int(z_str)
    s["max_reach_radius"] = max_r
    s["max_reach_at_z"] = max_r_z

    # Presets
    s["presets_ok"] = sum(1 for p in presets.values() if p["reachable"])
    s["presets_total"] = len(presets)

    return s


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser(
        description="Probe UF850 workspace via IK checks (no physical movement)")
    parser.add_argument("--ip", default="127.0.0.1",
                        help="Arm/simulator IP (default: 127.0.0.1)")
    parser.add_argument("--output", default="doc/arm_workspace_data.json",
                        help="Output JSON path")
    parser.add_argument("--fine", action="store_true",
                        help="Finer grid: 25mm step (slower, more detail)")
    parser.add_argument("--presets-only", action="store_true",
                        help="Only validate presets, skip envelope/pitch probes")
    args = parser.parse_args()

    grid_step = 25 if args.fine else 50

    print(f"Connecting to {args.ip}...")
    arm = XArmAPI(args.ip, is_radian=False)
    arm.clean_error()
    arm.motion_enable(enable=True)
    arm.set_mode(0)
    arm.set_state(0)
    time.sleep(0.3)

    code, pos = arm.get_position()
    if code == 0:
        print(f"  Current pos: X={pos[0]:.1f} Y={pos[1]:.1f} Z={pos[2]:.1f}")

    results = {
        "probe_date": time.strftime("%Y-%m-%d %H:%M"),
        "arm_model": "UF850 (XARM6_X12)",
        "default_rpy": list(DEFAULT_RPY),
        "grid_step_mm": grid_step,
        "joint_limits_deg": JOINT_LIMITS_DEG,
    }

    try:
        if not args.presets_only:
            results["envelope"] = probe_envelope(arm, step=grid_step)
            results["pitch_map"] = probe_pitch_ranges(arm, step=100)
            results["yaw_map"] = probe_yaw_ranges(arm, step=100)

        results["presets"] = validate_presets(arm)

        if "envelope" in results:
            results["summary"] = compute_summary(
                results["envelope"], results["presets"])

    finally:
        arm.set_state(4)
        arm.disconnect()
        print("\nDisconnected.")

    # Save
    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nData saved to: {args.output}")
    print(f"File size: {os.path.getsize(args.output) / 1024:.1f} KB")

    # Print summary
    if "summary" in results:
        s = results["summary"]
        print(f"\n{'='*55}")
        print(f"  UF850 Workspace Summary (RPY=180,0,0)")
        print(f"{'='*55}")
        print(f"  Reachable Z:      {s.get('z_min', '?')} — {s.get('z_max', '?')} mm")
        print(f"  Max forward (X):  {s.get('max_forward_x', '?')} mm "
              f"(at Z={s.get('max_forward_at_z', '?')})")
        print(f"  Max lateral (Y):  +/-{s.get('max_lateral_y', '?')} mm "
              f"(at Z={s.get('max_lateral_at_z', '?')})")
        print(f"  Max reach radius: {s.get('max_reach_radius', '?')} mm "
              f"(at Z={s.get('max_reach_at_z', '?')})")
        print(f"  Presets:          {s.get('presets_ok', '?')}/{s.get('presets_total', '?')} "
              f"reachable")
        print(f"{'='*55}")


if __name__ == "__main__":
    main()
