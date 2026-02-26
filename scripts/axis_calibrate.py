"""
Axis Calibration — Move the arm to diagnostic positions so you can
visually verify the coordinate mapping in Houdini.

Moves to a center point, then extends along each arm axis one at a time.
Watch in UFactory Studio (or Houdini viewport) to see which direction
each axis goes, then confirm the correct Houdini ↔ Arm mapping.

Usage:
    python scripts/axis_calibrate.py
    python scripts/axis_calibrate.py --ip 192.168.1.xxx
"""

import argparse
import time
from xarm.wrapper import XArmAPI

# Fixed orientation
RPY = (180, 0, 0)

# Diagnostic positions (all well within UF850 workspace)
CENTER = (350, 0, 450)
POSITIONS = [
    ("CENTER",  (350,   0, 450), "Arm origin reference point"),
    ("+X (arm forward)", (500,   0, 450), "Arm extends FORWARD from base (+150mm)"),
    ("-X (arm back)",    (200,   0, 450), "Arm retracts TOWARD base (-150mm)"),
    ("+Y (arm left)",    (350, 150, 450), "Arm moves LEFT (+150mm)"),
    ("-Y (arm right)",   (350,-150, 450), "Arm moves RIGHT (-150mm)"),
    ("+Z (arm up)",      (350,   0, 600), "Arm moves UP (+150mm)"),
    ("-Z (arm down)",    (350,   0, 300), "Arm moves DOWN (-150mm)"),
]

SPEED = 80  # mm/s — slow enough to see clearly


def main():
    parser = argparse.ArgumentParser(description="Axis calibration diagnostic")
    parser.add_argument("--ip", default="127.0.0.1")
    args = parser.parse_args()

    print(f"Connecting to {args.ip}...")
    arm = XArmAPI(args.ip, is_radian=False)
    arm.clean_error()
    arm.clean_warn()
    arm.motion_enable(enable=True)
    arm.set_mode(0)
    arm.set_state(0)
    time.sleep(0.3)

    code, pos = arm.get_position()
    if code == 0:
        print(f"  Current pos: X={pos[0]:.1f} Y={pos[1]:.1f} Z={pos[2]:.1f}\n")

    print("=" * 55)
    print("  AXIS CALIBRATION")
    print("  Watch the arm in UFactory Studio / Houdini viewport")
    print("  and note which direction each move goes.")
    print("=" * 55)

    try:
        for label, (x, y, z), desc in POSITIONS:
            input(f"\n  Next: {label} -> ({x}, {y}, {z}) mm\n"
                  f"  {desc}\n"
                  f"  Press Enter to move...")

            arm.set_position(x, y, z, *RPY, speed=SPEED, wait=True)
            time.sleep(0.3)

            code, pos = arm.get_position()
            if code == 0:
                print(f"  Arrived: X={pos[0]:.1f} Y={pos[1]:.1f} Z={pos[2]:.1f}")

            # Return to center after each axis test
            if label != "CENTER":
                time.sleep(1.0)
                arm.set_position(*CENTER, *RPY, speed=SPEED, wait=True)

        print("\n" + "=" * 55)
        print("  CALIBRATION DONE")
        print()
        print("  Now tell me: in your Houdini scene, which Houdini")
        print("  axis did each arm motion correspond to?")
        print()
        print("  Example answers:")
        print("    Arm +X (forward) = Houdini -Z")
        print("    Arm +Y (left)    = Houdini +X")
        print("    Arm +Z (up)      = Houdini +Y")
        print("=" * 55)

    finally:
        arm.set_state(4)
        arm.disconnect()
        print("Disconnected.")


if __name__ == "__main__":
    main()
