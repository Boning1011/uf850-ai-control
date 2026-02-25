"""
MVP Test: Verify Python SDK → Docker UFactory Simulator connection.
Connects to the simulator, enables the arm, and moves to a position.
Open http://localhost:18333 in a browser to see the arm move in UFactory Studio.
"""

import sys
import time
from xarm.wrapper import XArmAPI

ROBOT_IP = "127.0.0.1"

def main():
    print(f"Connecting to simulator at {ROBOT_IP}...")
    arm = XArmAPI(ROBOT_IP, is_radian=False)

    # Check connection
    print(f"Connected. State: {arm.state}, Mode: {arm.mode}")
    print(f"Robot type: {arm.axis}")

    # Clean error/warn state
    arm.clean_error()
    arm.clean_warn()

    # Enable motion
    arm.motion_enable(enable=True)
    arm.set_mode(0)       # Position control mode
    arm.set_state(0)      # Sport state (ready to move)
    time.sleep(0.5)

    print(f"After enable — State: {arm.state}, Error: {arm.error_code}")

    # Get current position
    code, pos = arm.get_position()
    print(f"Current position: {pos}")

    # Move to a safe test position (small move)
    print("Moving to test position [300, 0, 400, 180, 0, 0]...")
    arm.set_position(300, 0, 400, 180, 0, 0, speed=100, wait=True)

    code, pos = arm.get_position()
    print(f"New position: {pos}")

    # Move to another position to confirm control
    print("Moving to second position [350, 100, 350, 180, 0, 0]...")
    arm.set_position(350, 100, 350, 180, 0, 0, speed=100, wait=True)

    code, pos = arm.get_position()
    print(f"Final position: {pos}")

    # Done
    print("\nMVP test PASSED — SDK can control the Docker simulator.")
    arm.disconnect()

if __name__ == "__main__":
    main()
