"""
Test dramatic mode transitions on simulator.

Cycles through all 6 modes (CALM -> ALERT -> EXCITED -> PLAYFUL -> TENSE -> DORMANT)
with 8-second hold per mode to verify:
  1. Each mode produces distinct, visible motion
  2. Transitions are fast (flush queue + speed boost)
  3. J5 pitch works in PLAYFUL mode
  4. No kinematic errors at extended positions

Usage:
    python test_dramatic_modes.py --ip 127.0.0.1
"""

import argparse
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from arm_controller import ArmController, acquire_lock
from persona import PersonaConfig, PerceptionState, StateHolder
from motion_gen import ParametricMotionGenerator

DT = 0.04  # 25 Hz

MODE_SEQUENCE = ["CALM", "ALERT", "EXCITED", "PLAYFUL", "TENSE", "DORMANT"]
HOLD_SECONDS = 8  # seconds per mode


def main():
    parser = argparse.ArgumentParser(description="Test dramatic mode transitions")
    parser.add_argument("--ip", default="127.0.0.1")
    parser.add_argument("--persona", default="personas/default.yaml")
    parser.add_argument("--sensitivity", type=int, default=-1)
    parser.add_argument("--hold", type=int, default=HOLD_SECONDS,
                        help="Seconds to hold each mode (default: 8)")
    args = parser.parse_args()

    acquire_lock()

    config = PersonaConfig(args.persona)
    motion_gen = ParametricMotionGenerator(config)

    # Create a default state (moderate energy, centered attention)
    state = PerceptionState(
        energy=0.5, attention_x=0.0, attention_y=0.0,
        mood=0.5, presence=0.5, urgency=0.0,
        timestamp=time.time(),
    )

    ctrl = ArmController(
        ip=args.ip,
        speed=config.speed_base,
        collision_sensitivity=args.sensitivity,
        center=config.center,
        rpy=(180, 0, 0),
        bounds_x=config.bounds_x,
        bounds_y=config.bounds_y,
        bounds_z=config.bounds_z,
    )

    try:
        ctrl.connect()

        print("Moving to center...")
        ctrl.move_to_center()
        time.sleep(1.0)

        print(f"\n=== Dramatic Mode Test ===")
        print(f"Modes: {' -> '.join(MODE_SEQUENCE)}")
        print(f"Hold: {args.hold}s per mode")
        print(f"Safety bounds: X={config.bounds_x} Y={config.bounds_y} Z={config.bounds_z}")
        print()

        t = 0.0

        for mode in MODE_SEQUENCE:
            # Switch mode
            old_mode = motion_gen.current_mode
            motion_gen.set_mode(mode)

            # Flush queue for instant transition
            ctrl.flush_queue()

            print(f">>> [{old_mode}] -> [{mode}]", flush=True)

            hold_start = time.time()
            frame_count = 0
            errors = 0

            while time.time() - hold_start < args.hold:
                if ctrl.arm.has_error or ctrl.arm.state >= 4:
                    errors += 1
                    time.sleep(0.2)
                    if errors > 10:
                        print(f"  [!] Too many errors in {mode}, skipping", flush=True)
                        break
                    continue

                x, y, z, pitch = motion_gen.get_target(t, state)
                speed = motion_gen.get_speed(state)

                ret = ctrl.send_position(x, y, z, speed=speed, pitch=pitch)
                frame_count += 1

                # Log first frame and every 2 seconds
                elapsed = time.time() - hold_start
                if frame_count == 1 or (frame_count % 50 == 0):
                    pitch_str = f" pitch={pitch:.0f}" if pitch != 0 else ""
                    print(f"  [{mode}] {elapsed:.1f}s  "
                          f"X={x:.0f} Y={y:.0f} Z={z:.0f}{pitch_str}  "
                          f"spd={speed:.0f}  ret={ret}", flush=True)

                t += DT
                time.sleep(DT)

            print(f"  <- {mode} done ({frame_count} frames, {errors} errors)\n",
                  flush=True)

        # Return to center
        print("Returning to center...")
        ctrl.flush_queue()
        ctrl.move_to_center(speed=100)
        time.sleep(1.0)

        print("\n=== Test Complete ===")

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        ctrl.disconnect()


if __name__ == "__main__":
    main()
