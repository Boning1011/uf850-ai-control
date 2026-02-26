"""
Loop through Houdini XYZ axis test points to verify direction mapping.

Loads the 3 axis points, transforms via houdini_to_arm, scales from
arm base origin (0,0,0) to preserve true direction, and loops.
"""

import math
import sys
import time
sys.path.insert(0, ".")

from xarm.wrapper import XArmAPI
from scripts.path_validator import load_path, houdini_to_arm, RPY

SPEED = 120  # mm/s
HOME = (300, 0, 400)  # safe rest position between moves


def scale_from_origin(points, factor=0.5):
    """Scale points from arm base origin (0,0,0). Preserves true direction."""
    result = []
    for p in points:
        result.append([round(p[0] * factor, 1),
                       round(p[1] * factor, 1),
                       round(p[2] * factor, 1)])
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

    # Scale from origin — 0.5x keeps direction, gives ~500mm reach
    safe_points = scale_from_origin(arm_points, factor=0.5)

    print(f"\nScaled 0.5x from origin (direction preserved):")
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
        # Go to home first
        print(f"\nMoving to home {HOME}")
        arm.set_position(*HOME, *RPY, speed=80, wait=True)
        time.sleep(0.5)

        print(f"Looping: home -> Hou+X -> home -> Hou+Y -> home -> Hou+Z -> ... (Ctrl+C)\n")

        while True:
            for i, (p, lbl) in enumerate(zip(safe_points, labels)):
                if arm.has_error or arm.state >= 4:
                    arm.clean_error()
                    arm.motion_enable(enable=True)
                    arm.set_mode(0)
                    arm.set_state(0)
                    time.sleep(0.3)

                print(f"    -> {lbl}: ({p[0]}, {p[1]}, {p[2]})")
                ret = arm.set_position(p[0], p[1], p[2], *RPY,
                                       speed=SPEED, wait=True)

                if arm.error_code != 0:
                    print(f"       UNREACHABLE (err={arm.error_code}), recovering")
                    arm.clean_error()
                    arm.motion_enable(enable=True)
                    arm.set_mode(0)
                    arm.set_state(0)
                    time.sleep(0.3)

                time.sleep(1.0)

                # Return to home
                arm.set_position(*HOME, *RPY, speed=SPEED, wait=True)
                time.sleep(0.3)

            loop_count += 1
            print(f"  Loop #{loop_count} done\n")

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        arm.set_state(4)
        arm.disconnect()
        print(f"Disconnected. Loops: {loop_count}")


if __name__ == "__main__":
    main()
