"""
Test axis directions — Move toward each Houdini axis to verify mapping.

From a safe center, extends 250mm along each mapped direction:
  Hou +X  ->  Arm +X  (forward)
  Hou +Y  ->  Arm +Z  (up)
  Hou +Z  ->  Arm +Y  (left)

Watch in UFactory Studio to confirm the directions match Houdini.
"""

import sys
import time
sys.path.insert(0, ".")

from xarm.wrapper import XArmAPI

RPY = (180, 0, 0)
SPEED = 100  # mm/s

# Safe center
CX, CY, CZ = 350, 0, 450

# How far to reach along each axis
REACH = 250  # mm

MOVES = [
    # (label, arm_xyz, what it means in Houdini)
    ("CENTER (start)",     (CX, CY, CZ),           "Reference point"),
    ("Hou +X  (Arm +X)",   (CX + REACH, CY, CZ),   "Should go FORWARD in Houdini"),
    ("CENTER",             (CX, CY, CZ),           "Back to center"),
    ("Hou +Y  (Arm +Z)",   (CX, CY, CZ + REACH),   "Should go UP in Houdini"),
    ("CENTER",             (CX, CY, CZ),           "Back to center"),
    ("Hou +Z  (Arm +Y)",   (CX, CY + REACH, CZ),   "Should go LEFT in Houdini"),
    ("CENTER",             (CX, CY, CZ),           "Back to center"),
]


def main():
    print("Connecting to 127.0.0.1...")
    arm = XArmAPI("127.0.0.1", is_radian=False)
    arm.clean_error()
    arm.clean_warn()
    arm.motion_enable(enable=True)
    arm.set_mode(0)
    arm.set_state(0)
    time.sleep(0.3)

    code, pos = arm.get_position()
    if code == 0:
        print(f"  Current: X={pos[0]:.1f} Y={pos[1]:.1f} Z={pos[2]:.1f}\n")

    print("=" * 50)
    print("  AXIS DIRECTION TEST")
    print(f"  Center: ({CX}, {CY}, {CZ}) mm")
    print(f"  Reach: {REACH} mm per axis")
    print("=" * 50)

    try:
        for label, (x, y, z), desc in MOVES:
            print(f"\n  -> {label}")
            print(f"     Arm ({x}, {y}, {z}) mm  |  {desc}")
            arm.set_position(x, y, z, *RPY, speed=SPEED, wait=True)

            if arm.error_code != 0:
                print(f"     ERROR {arm.error_code} — recovering")
                arm.clean_error()
                arm.motion_enable(enable=True)
                arm.set_mode(0)
                arm.set_state(0)
                time.sleep(0.3)
                continue

            time.sleep(1.5)  # pause so you can see it

        print("\n" + "=" * 50)
        print("  DONE — Check if directions matched Houdini:")
        print("    Hou +X = forward?")
        print("    Hou +Y = up?")
        print("    Hou +Z = left?")
        print("=" * 50)

    finally:
        arm.set_state(4)
        arm.disconnect()
        print("Disconnected.")


if __name__ == "__main__":
    main()
