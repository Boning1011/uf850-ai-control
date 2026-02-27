"""
Test parameterized motion on simulator — keyboard controls fake PerceptionState.

Controls:
    0-9     Set energy (0.0 - 0.9)
    a / d   Attention left / right
    w / s   Attention up / down
    m       Cycle mood (tense -> neutral -> playful)
    u       Toggle urgency (0 / 0.8)
    p       Toggle presence (0 / 1)
    q       Quit

Usage:
    python test_motion_params.py [--ip 127.0.0.1]
"""

import argparse
import sys
import time
import threading

# Add scripts dir to path for imports
sys.path.insert(0, __file__ and __import__('os').path.dirname(__import__('os').path.abspath(__file__)))

from arm_controller import ArmController, acquire_lock
from persona import PersonaConfig, PerceptionState, StateHolder
from motion_gen import ParametricMotionGenerator

DT = 0.04  # 25 Hz


def keyboard_thread(state_holder, running_flag):
    """Read keyboard input to adjust PerceptionState."""
    energy = 0.0
    att_x = 0.0
    att_y = 0.0
    mood = 0.5
    urgency = 0.0
    presence = 0.0

    print("\n--- Keyboard Control ---")
    print("  0-9: energy    a/d: attention L/R    w/s: attention U/D")
    print("  m: cycle mood  u: toggle urgency     p: toggle presence")
    print("  q: quit\n")

    while running_flag():
        try:
            line = input("> ").strip().lower()
        except EOFError:
            break

        if not line:
            continue

        for ch in line:
            if ch == 'q':
                return
            elif ch.isdigit():
                energy = int(ch) / 9.0
                print(f"  energy = {energy:.2f}")
            elif ch == 'a':
                att_x = max(-1.0, att_x - 0.3)
                print(f"  attention_x = {att_x:.2f}")
            elif ch == 'd':
                att_x = min(1.0, att_x + 0.3)
                print(f"  attention_x = {att_x:.2f}")
            elif ch == 'w':
                att_y = min(1.0, att_y + 0.3)
                print(f"  attention_y = {att_y:.2f}")
            elif ch == 's':
                att_y = max(-1.0, att_y - 0.3)
                print(f"  attention_y = {att_y:.2f}")
            elif ch == 'm':
                mood = (mood + 0.5) % 1.5  # cycles 0.0 -> 0.5 -> 1.0 -> 0.0
                if mood > 1.0:
                    mood = 0.0
                labels = {0.0: "tense", 0.5: "neutral", 1.0: "playful"}
                print(f"  mood = {mood:.1f} ({labels.get(mood, '')})")
            elif ch == 'u':
                urgency = 0.0 if urgency > 0 else 0.8
                print(f"  urgency = {urgency:.1f}")
            elif ch == 'p':
                presence = 0.0 if presence > 0 else 1.0
                print(f"  presence = {presence:.1f}")

        state_holder.update(PerceptionState(
            energy=energy,
            attention_x=att_x,
            attention_y=att_y,
            mood=mood,
            urgency=urgency,
            presence=presence,
            timestamp=time.time(),
        ))


def main():
    parser = argparse.ArgumentParser(description="Test parameterized motion on simulator")
    parser.add_argument("--ip", default="127.0.0.1")
    parser.add_argument("--persona", default="personas/default.yaml")
    parser.add_argument("--sensitivity", type=int, default=-1)
    args = parser.parse_args()

    acquire_lock()

    # Load persona
    config = PersonaConfig(args.persona)
    print(f"Persona: {config.name}")

    # Init components
    state_holder = StateHolder(smoothing=0.2)
    motion_gen = ParametricMotionGenerator(config)
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

        # Move to center
        print("Moving to center...")
        ctrl.move_to_center()
        time.sleep(0.5)

        # Start keyboard thread
        kb = threading.Thread(
            target=keyboard_thread,
            args=(state_holder, lambda: ctrl.running),
            daemon=True,
        )
        kb.start()

        print("Running at 25 Hz (Ctrl+C to stop)\n")

        t = 0.0
        last_log = 0.0

        while ctrl.running:
            if ctrl.arm.has_error or ctrl.arm.state >= 4:
                time.sleep(0.1)
                continue

            state = state_holder.get()
            x, y, z, pitch = motion_gen.get_target(t, state)
            speed = motion_gen.get_speed(state)

            ret = ctrl.send_position(x, y, z, speed=speed, pitch=pitch)
            if ret == -2:
                time.sleep(0.2)
                continue

            if t - last_log >= 2.0:
                print(f"  [e={state.energy:.1f} m={state.mood:.1f} ax={state.attention_x:+.1f}] "
                      f"X={x:.0f} Y={y:.0f} Z={z:.0f} spd={speed:.0f}")
                last_log = t

            t += DT
            time.sleep(DT)

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        ctrl.disconnect()


if __name__ == "__main__":
    main()
