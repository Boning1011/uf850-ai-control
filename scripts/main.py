"""
AI-Driven Arm Control — Full Pipeline

Camera -> VLM Perception -> Continuous Parameters -> Parameterized Motion -> Arm

Usage:
    python main.py --persona personas/default.yaml --ip 127.0.0.1
    python main.py --no-camera          # mock perception (no webcam)
    python main.py --keyboard           # manual parameter control (no VLM)
"""

import argparse
import sys
import os
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np
from arm_controller import ArmController, acquire_lock
from persona import PersonaConfig, PerceptionState, StateHolder
from motion_gen import ParametricMotionGenerator
from perception import GeminiProvider, FrameBuffer, CameraThread, PerceptionThread

DT = 0.04  # 25 Hz


def keyboard_driver(state_holder, running_flag):
    """Manual parameter control via keyboard (replaces VLM)."""
    energy = 0.0
    att_x = 0.0
    att_y = 0.0
    mood = 0.5
    urgency = 0.0
    presence = 0.0

    print("\n--- Keyboard Mode ---")
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
            elif ch == 'a':
                att_x = max(-1.0, att_x - 0.3)
            elif ch == 'd':
                att_x = min(1.0, att_x + 0.3)
            elif ch == 'w':
                att_y = min(1.0, att_y + 0.3)
            elif ch == 's':
                att_y = max(-1.0, att_y - 0.3)
            elif ch == 'm':
                mood = round((mood + 0.5) % 1.5, 1)
                if mood > 1.0:
                    mood = 0.0
            elif ch == 'u':
                urgency = 0.0 if urgency > 0 else 0.8
            elif ch == 'p':
                presence = 0.0 if presence > 0 else 1.0

        state_holder.update(PerceptionState(
            energy=energy, attention_x=att_x, attention_y=att_y,
            mood=mood, urgency=urgency, presence=presence,
            timestamp=time.time(),
        ))
        print(f"  -> e={energy:.1f} ax={att_x:+.1f} ay={att_y:+.1f} "
              f"m={mood:.1f} u={urgency:.1f} p={presence:.1f}")


def mock_camera_fill(frame_buffer, running_flag):
    """Push gray mock frames for --no-camera mode."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[:] = 128
    cv2.putText(frame, "NO CAMERA", (180, 250),
                cv2.FONT_HERSHEY_SIMPLEX, 1.5, (255, 255, 255), 2)
    _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 50])
    mock_bytes = jpeg.tobytes()

    while running_flag():
        frame_buffer.push(mock_bytes)
        time.sleep(0.2)


def main():
    parser = argparse.ArgumentParser(description="AI-Driven Arm Control")
    parser.add_argument("--ip", default="127.0.0.1", help="Arm IP address")
    parser.add_argument("--persona", default="personas/default.yaml", help="Persona YAML config")
    parser.add_argument("--sensitivity", type=int, default=-1,
                        help="Collision sensitivity 0-5, -1=skip (default: -1)")
    parser.add_argument("--keyboard", action="store_true",
                        help="Manual parameter control instead of VLM")
    parser.add_argument("--no-camera", action="store_true",
                        help="Mock camera (gray frames) — VLM still runs but sees nothing")
    args = parser.parse_args()

    acquire_lock()

    # Load persona
    config = PersonaConfig(args.persona)
    print(f"Persona: {config.name}")
    print(f"  {config.description}")

    # Init shared state
    state_holder = StateHolder(smoothing=0.3)
    motion_gen = ParametricMotionGenerator(config)

    # Init arm
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

    # Perception components (unless keyboard mode)
    cam = None
    perception = None
    frame_buffer = None
    mock_thread_flag = [True]

    try:
        ctrl.connect()
        print("\nMoving to center...")
        ctrl.move_to_center()
        time.sleep(0.5)

        if args.keyboard:
            # Keyboard mode: no camera, no VLM
            driver = threading.Thread(
                target=keyboard_driver,
                args=(state_holder, lambda: ctrl.running),
                daemon=True,
            )
            driver.start()
            print("Keyboard mode active.\n")
        else:
            # VLM mode: camera + perception
            frame_buffer = FrameBuffer(maxlen=20)
            provider = GeminiProvider(model=config.vlm_model)

            if args.no_camera:
                mock_thread = threading.Thread(
                    target=mock_camera_fill,
                    args=(frame_buffer, lambda: mock_thread_flag[0]),
                    daemon=True,
                )
                mock_thread.start()
                print("[Camera] Mock mode.\n")
            else:
                cam = CameraThread(
                    device=config.camera_device,
                    resolution=config.camera_resolution,
                    jpeg_quality=config.camera_jpeg_quality,
                    frame_buffer=frame_buffer,
                    target_fps=5.0,
                )
                if not cam.open_camera():
                    print("[ERROR] Cannot open camera. Use --no-camera or --keyboard.")
                    ctrl.disconnect()
                    sys.exit(1)
                cam.start()

            perception = PerceptionThread(
                provider=provider,
                system_prompt=config.full_system_prompt,
                frame_buffer=frame_buffer,
                state_holder=state_holder,
                rate_hz=config.vlm_rate_hz,
                frame_count=config.frame_count,
            )
            perception.start()
            print(f"VLM perception active ({config.vlm_rate_hz} Hz).\n")

        # --- Main control loop @ 25 Hz ---
        print("Running (Ctrl+C to stop)\n")

        t = 0.0
        last_log = 0.0

        while ctrl.running:
            if ctrl.arm.has_error or ctrl.arm.state >= 4:
                time.sleep(0.1)
                continue

            state = state_holder.get()
            x, y, z = motion_gen.get_target(t, state)
            speed = motion_gen.get_speed(state)

            ret = ctrl.send_position(x, y, z, speed=speed)
            if ret == -2:
                time.sleep(0.2)
                continue

            if t - last_log >= 2.0:
                vlm_info = ""
                if perception:
                    vlm_info = f"  vlm#{perception.call_count}"
                print(f"  [e={state.energy:.2f} m={state.mood:.2f} "
                      f"ax={state.attention_x:+.2f} ay={state.attention_y:+.2f}] "
                      f"X={x:.0f} Y={y:.0f} Z={z:.0f} spd={speed:.0f}{vlm_info}")
                last_log = t

            t += DT
            time.sleep(DT)

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        if perception:
            perception.stop()
        if cam:
            cam.stop()
        mock_thread_flag[0] = False
        ctrl.disconnect()


if __name__ == "__main__":
    main()
