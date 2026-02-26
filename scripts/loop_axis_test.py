"""
Loop through Houdini XYZ axis test points to verify direction mapping.

Loads the 3 axis points, transforms via houdini_to_arm, then scales
them to reachable positions (preserving direction) and loops.
"""

import sys
import time
import threading
sys.path.insert(0, ".")

from xarm.wrapper import XArmAPI
from scripts.path_validator import load_path, houdini_to_arm, RPY

SPEED = 120  # mm/s
CENTER = (350, 0, 450)  # safe center to return to between points


def scale_to_reachable(points, max_dist=400, center=CENTER):
    """Scale transformed points toward center so they're reachable.
    Preserves direction from center, caps distance."""
    import math
    result = []
    for p in points:
        dx = p[0] - center[0]
        dy = p[1] - center[1]
        dz = p[2] - center[2]
        dist = math.sqrt(dx*dx + dy*dy + dz*dz)
        if dist > max_dist:
            s = max_dist / dist
            dx *= s
            dy *= s
            dz *= s
        result.append([
            round(center[0] + dx, 1),
            round(center[1] + dy, 1),
            round(center[2] + dz, 1),
        ])
    return result


def main():
    # Load and transform
    path_file = "path_data/hou_test_xyz_axis.json"
    print(f"Loading: {path_file}")
    points, is_houdini = load_path(path_file)

    print(f"\nHoudini points (file order):")
    for i, p in enumerate(points):
        print(f"  #{i}: ({p[0]}, {p[1]}, {p[2]})")

    arm_points = houdini_to_arm(points)

    print(f"\nAfter houdini_to_arm:")
    for i, p in enumerate(arm_points):
        print(f"  #{i}: Arm ({p[0]}, {p[1]}, {p[2]}) mm")

    safe_points = scale_to_reachable(arm_points)

    print(f"\nScaled to reachable (direction preserved):")
    labels = ["Hou +X (arm forward)", "Hou +Y (arm up)", "Hou +Z (arm left)"]
    for i, (p, lbl) in enumerate(zip(safe_points, labels)):
        print(f"  #{i}: Arm ({p[0]}, {p[1]}, {p[2]}) mm  <- {lbl}")

    # Connect
    print(f"\nConnecting...")
    arm = XArmAPI("127.0.0.1", is_radian=False)
    arm.clean_error()
    arm.clean_warn()
    arm.motion_enable(enable=True)
    arm.set_mode(0)
    arm.set_state(0)
    time.sleep(0.3)

    loop_count = 0

    try:
        # Go to center first
        print(f"\nMoving to center ({CENTER[0]}, {CENTER[1]}, {CENTER[2]})")
        arm.set_position(*CENTER, *RPY, speed=80, wait=True)
        time.sleep(0.5)

        print(f"Looping: center -> Hou+X -> center -> Hou+Y -> center -> Hou+Z -> ... (Ctrl+C)\n")

        while True:
            for i, (p, lbl) in enumerate(zip(safe_points, labels)):
                if arm.has_error or arm.state >= 4:
                    arm.clean_error()
                    arm.motion_enable(enable=True)
                    arm.set_mode(0)
                    arm.set_state(0)
                    time.sleep(0.3)

                # Move to axis point
                arm.set_position(p[0], p[1], p[2], *RPY, speed=SPEED, wait=True)
                time.sleep(0.8)

                # Return to center
                arm.set_position(*CENTER, *RPY, speed=SPEED, wait=True)
                time.sleep(0.3)

            loop_count += 1
            print(f"  Loop #{loop_count} done")

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        arm.set_state(4)
        arm.disconnect()
        print(f"Disconnected. Loops: {loop_count}")


if __name__ == "__main__":
    main()
