"""
AI-Driven Arm Control — Full Pipeline

Camera -> VLM Perception -> Continuous Parameters -> Parameterized Motion -> Arm

Includes a web dashboard at http://localhost:7860 for real-time monitoring:
  - Live camera feed (MJPEG)
  - VLM parameter bars
  - Trigger events
  - Pipeline control (pause/resume VLM)

Usage:
    python main.py --persona personas/default.yaml --ip 127.0.0.1
    python main.py --no-camera          # mock perception (no webcam)
    python main.py --keyboard           # manual parameter control (no VLM)
    python main.py --hand-tracking      # MediaPipe hand tracking mode
    python main.py --no-web             # disable dashboard
    python main.py --port 8080          # custom dashboard port
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
from triggers import TriggerEngine, ModeEngine

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


def _dispatch_hands(hands, motion_gen, dashboard=None):
    """Route multi-hand tracking data to motion generator.

    Single hand  -> position control (original behaviour)
    Two hands    -> right hand = position, left hand = RPY orientation
    No hands     -> clear targets (fallback to calm breathing)

    Handedness note (mirrored video):
      MediaPipe "Left"  = user's RIGHT hand  -> position control
      MediaPipe "Right" = user's LEFT hand   -> RPY control
    """
    if not hands:
        motion_gen.set_hand_target(None, None, None)
        motion_gen.set_hand_rpy_input(None, None)
        if dashboard:
            dashboard.push_hand_tracking(None, None, 0.0)
        return

    if len(hands) == 1:
        # Single hand: always controls position, clear RPY
        h = hands[0]
        motion_gen.set_hand_target(h["x"], h["y"], h["z"])
        motion_gen.set_hand_rpy_input(None, None)
        if dashboard:
            dashboard.push_hand_tracking(h["x"], h["y"], h["confidence"])
        return

    # Two hands: identify by MediaPipe handedness label
    pos_hand = None
    rpy_hand = None
    for h in hands:
        if h["label"] == "Left":   # user's right hand (mirrored)
            pos_hand = h
        else:
            rpy_hand = h

    # Fallback if both have the same label
    if pos_hand is None:
        pos_hand = hands[0]
    if rpy_hand is None:
        rpy_hand = hands[1]

    motion_gen.set_hand_target(pos_hand["x"], pos_hand["y"], pos_hand["z"])
    motion_gen.set_hand_rpy_input(rpy_hand["x"], rpy_hand["y"])
    if dashboard:
        dashboard.push_hand_tracking(pos_hand["x"], pos_hand["y"], pos_hand["confidence"])


def main():
    parser = argparse.ArgumentParser(description="AI-Driven Arm Control")
    parser.add_argument("--ip", default="127.0.0.1", help="Arm IP address")
    parser.add_argument("--persona", default="personas/default.yaml", help="Persona YAML config")
    parser.add_argument("--sensitivity", type=int, default=-1,
                        help="Collision sensitivity 0-5, -1=skip (default: -1)")
    parser.add_argument("--keyboard", action="store_true",
                        help="Manual parameter control instead of VLM")
    parser.add_argument("--hand-tracking", action="store_true",
                        help="MediaPipe hand tracking mode (replaces VLM)")
    parser.add_argument("--no-camera", action="store_true",
                        help="Mock camera (gray frames) — VLM still runs but sees nothing")
    parser.add_argument("--no-web", action="store_true",
                        help="Disable web dashboard")
    parser.add_argument("--port", type=int, default=7860,
                        help="Web dashboard port (default: 7860)")
    args = parser.parse_args()

    acquire_lock()

    # Load persona
    config = PersonaConfig(args.persona)
    print(f"Persona: {config.name}")
    print(f"  {config.description}")

    # Init shared state
    state_holder = StateHolder(smoothing=0.3)
    motion_gen = ParametricMotionGenerator(config)
    trigger_engine = TriggerEngine()
    mode_engine = ModeEngine()

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
    hand_tracker = None
    frame_buffer = FrameBuffer(maxlen=20)
    mock_thread_flag = [True]
    dashboard = None

    # Web dashboard
    if not args.no_web:
        from web_server import DashboardServer
        dashboard = DashboardServer(
            frame_buffer=frame_buffer,
            state_holder=state_holder,
            port=args.port,
        )

        _mode_switch_lock = threading.Lock()

        def on_command(cmd, msg=None):
            nonlocal perception, hand_tracker, cam
            if msg is None:
                msg = {}
            if cmd == "pause" and perception:
                perception.running = False
                dashboard.set_status("paused")
                dashboard.push_event("COMMAND", "VLM paused")
                print("[Dashboard] VLM paused", flush=True)
            elif cmd == "resume" and perception:
                perception.running = True
                dashboard.set_status("running")
                dashboard.push_event("COMMAND", "VLM resumed")
                print("[Dashboard] VLM resumed", flush=True)
            elif cmd == "set_input_mode":
                target_mode = msg.get("mode", "vlm")
                # Run in background thread — camera ops block and would
                # freeze the async event loop (MJPEG stream + WebSocket).
                def _do_switch():
                    nonlocal hand_tracker, cam
                    if not _mode_switch_lock.acquire(blocking=False):
                        print("[Dashboard] Mode switch already in progress, ignoring.", flush=True)
                        return
                    try:
                        if target_mode == "hand_tracking":
                            _switch_to_hand_tracking()
                        elif target_mode == "vlm":
                            _switch_to_vlm()
                    except Exception as e:
                        dashboard.push_event("ERROR", f"Mode switch failed: {e}")
                        print(f"[Dashboard] ERROR: Mode switch failed: {e}", flush=True)
                    finally:
                        _mode_switch_lock.release()

                def _switch_to_hand_tracking():
                    nonlocal hand_tracker, cam
                    # Pause VLM perception
                    if perception:
                        perception.running = False
                    # Stop VLM camera (join thread, then release device)
                    if cam:
                        cam.stop()
                        cam = None
                        time.sleep(0.3)  # let OS fully release camera device
                    # Stop previous hand tracker if any
                    if hand_tracker:
                        hand_tracker.stop()
                        hand_tracker = None
                    # Check model file before starting
                    from hand_tracking import HandTrackingThread, MODEL_PATH
                    if not os.path.exists(MODEL_PATH):
                        dashboard.push_event("ERROR",
                            "Hand tracking model not found",
                            f"Download: curl -L -o models/hand_landmarker.task "
                            f"\"https://storage.googleapis.com/mediapipe-models/"
                            f"hand_landmarker/hand_landmarker/float16/latest/"
                            f"hand_landmarker.task\"")
                        print(f"[Dashboard] ERROR: Model not found at {MODEL_PATH}", flush=True)
                        return

                    def on_hand_result(hands):
                        _dispatch_hands(hands, motion_gen, dashboard)

                    hand_tracker = HandTrackingThread(
                        frame_buffer=frame_buffer,
                        device=config.camera_device,
                        resolution=config.camera_resolution,
                        jpeg_quality=config.camera_jpeg_quality,
                        on_result=on_hand_result,
                    )
                    if not hand_tracker.open_camera():
                        dashboard.push_event("ERROR", "Cannot open camera for hand tracking")
                        print("[Dashboard] ERROR: Cannot open camera", flush=True)
                        hand_tracker = None
                        return
                    hand_tracker.start()
                    motion_gen.set_mode("TRACK")
                    dashboard.push_event("COMMAND", "Switched to hand tracking")
                    print("[Dashboard] Switched to hand tracking", flush=True)

                def _switch_to_vlm():
                    nonlocal hand_tracker, cam
                    # Stop hand tracker (join thread, then release camera)
                    if hand_tracker:
                        hand_tracker.stop()
                        hand_tracker = None
                        motion_gen.set_hand_target(None, None, None)
                        time.sleep(0.3)  # let OS fully release camera device
                    # Restart VLM camera
                    cam = CameraThread(
                        device=config.camera_device,
                        resolution=config.camera_resolution,
                        jpeg_quality=config.camera_jpeg_quality,
                        frame_buffer=frame_buffer,
                        target_fps=5.0,
                    )
                    cam.open_camera()
                    cam.start()
                    if perception:
                        perception.running = True
                    motion_gen.set_mode("CALM")
                    dashboard.set_status("running")
                    dashboard.push_event("COMMAND", "Switched to VLM mode")
                    print("[Dashboard] Switched to VLM mode", flush=True)

                threading.Thread(target=_do_switch, daemon=True).start()

        dashboard.on_command = on_command
        dashboard.start()

    try:
        ctrl.connect()
        if dashboard:
            dashboard.push_event("SYSTEM", "Arm connected", f"IP={args.ip}")

        print("\nMoving to center...")
        ctrl.move_to_center()
        time.sleep(0.5)

        # Switch to servo mode for real-time streaming control
        ctrl.enable_servo()

        if args.keyboard:
            # Keyboard mode: no camera, no VLM
            # Still push mock frames so dashboard camera panel shows something
            if dashboard:
                mock_thread = threading.Thread(
                    target=mock_camera_fill,
                    args=(frame_buffer, lambda: mock_thread_flag[0]),
                    daemon=True,
                )
                mock_thread.start()

            driver = threading.Thread(
                target=keyboard_driver,
                args=(state_holder, lambda: ctrl.running),
                daemon=True,
            )
            driver.start()
            print("Keyboard mode active.\n")
            if dashboard:
                dashboard.push_event("SYSTEM", "Keyboard mode active")
        elif args.hand_tracking:
            # Hand tracking mode: MediaPipe hand detection, no VLM
            from hand_tracking import HandTrackingThread

            motion_gen.set_mode("TRACK")

            def on_hand_result(hands):
                _dispatch_hands(hands, motion_gen, dashboard)

            hand_tracker = HandTrackingThread(
                frame_buffer=frame_buffer,
                device=config.camera_device,
                resolution=config.camera_resolution,
                jpeg_quality=config.camera_jpeg_quality,
                on_result=on_hand_result,
            )
            if not hand_tracker.open_camera():
                print("[ERROR] Cannot open camera for hand tracking.")
                ctrl.disconnect()
                sys.exit(1)
            hand_tracker.start()
            print("Hand tracking mode active.\n")
            if dashboard:
                dashboard.push_event("SYSTEM", "Hand tracking started (MediaPipe)")
        else:
            # VLM mode: camera + perception
            provider = GeminiProvider(model=config.vlm_model)

            if args.no_camera:
                mock_thread = threading.Thread(
                    target=mock_camera_fill,
                    args=(frame_buffer, lambda: mock_thread_flag[0]),
                    daemon=True,
                )
                mock_thread.start()
                print("[Camera] Mock mode.\n")
                if dashboard:
                    dashboard.push_event("SYSTEM", "Camera mock mode")
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
                if dashboard:
                    dashboard.push_event("SYSTEM", "Camera opened")

            # VLM result callback -> triggers -> mode -> dashboard
            def on_vlm_result(raw_state, latency, call_count):
                # Check triggers on raw (unsmoothed) state
                newly_fired = trigger_engine.check(raw_state)

                # Update mode from active triggers -> apply to motion generator
                old_mode, new_mode = mode_engine.update(
                    trigger_engine.active_triggers
                )
                motion_gen.set_mode(new_mode)

                # Log mode transitions
                if old_mode != new_mode:
                    trigger_reason = ", ".join(sorted(trigger_engine.active_triggers)) or "(none)"
                    print(f"[MODE] {old_mode} -> {new_mode}  triggers: {trigger_reason}",
                          flush=True)
                    if dashboard:
                        dashboard.push_mode_transition(
                            old_mode, new_mode, trigger_reason, 1.0
                        )

                smoothed = state_holder.get()

                if dashboard:
                    # Push state + motion debug
                    t_now = time.time()
                    x, y, z, _pitch, _yaw = motion_gen.get_target(t_now, smoothed)
                    speed = motion_gen.get_speed(smoothed)
                    dashboard.push_state(
                        raw_state, smoothed,
                        motion_xyz=(x, y, z), speed=speed,
                        vlm_count=call_count, vlm_latency=latency,
                        motion_debug=motion_gen.get_debug_state(),
                    )
                    dashboard.push_triggers(trigger_engine.active_triggers)

                    # Push VLM scene text
                    dashboard.push_vlm_text(
                        perception.last_scene_description,
                        perception.last_primary_action,
                    )

                    # Push arm telemetry
                    telemetry = ctrl.get_telemetry()
                    if telemetry:
                        dashboard.push_arm_telemetry(telemetry)

                    # Push trigger events
                    for name in newly_fired:
                        dashboard.push_event(
                            "FIRED", name,
                            trigger_engine._state_summary(raw_state),
                        )
                    # Check for cleared triggers in recent history
                    for ts, evt, name, details in trigger_engine.trigger_history[-10:]:
                        if evt == "CLEARED" and ts > t_now - 1.0:
                            dashboard.push_event("CLEARED", name, details)
                        elif evt == "BIG_DELTA" and ts > t_now - 1.0:
                            dashboard.push_event("BIG_DELTA", details)

            perception = PerceptionThread(
                provider=provider,
                system_prompt=config.full_system_prompt,
                frame_buffer=frame_buffer,
                state_holder=state_holder,
                rate_hz=config.vlm_rate_hz,
                frame_count=config.frame_count,
                on_result=on_vlm_result,
            )
            perception.start()
            print(f"VLM perception active ({config.vlm_rate_hz} Hz).\n")
            if dashboard:
                dashboard.push_event("SYSTEM", f"VLM started ({config.vlm_model} @ {config.vlm_rate_hz} Hz)")

        # --- Main control loop @ 25 Hz ---
        print("Running (Ctrl+C to stop)\n")

        t = 0.0
        last_log = 0.0

        servo_lost_logged = False
        while ctrl.running:
            if ctrl.arm.has_error or ctrl.arm.state >= 4:
                time.sleep(0.1)
                servo_lost_logged = False
                continue

            # Auto-recover if servo mode was lost
            if ctrl.arm.mode != 1:
                if not servo_lost_logged:
                    print("[MAIN] Servo mode lost, attempting recovery...", flush=True)
                    servo_lost_logged = True
                if ctrl.try_reenable_servo():
                    print("[MAIN] Servo mode restored.", flush=True)
                    servo_lost_logged = False
                else:
                    time.sleep(0.5)
                    continue

            # In hand tracking mode, skip trigger/mode engine (mode stays TRACK)
            if motion_gen.current_mode != "TRACK":
                mode_engine.update(trigger_engine.active_triggers)
                motion_gen.set_mode(mode_engine.current_mode)
                mode_engine.mode_just_changed = False

            state = state_holder.get()
            x, y, z, pitch, yaw = motion_gen.get_target(t, state)
            speed = motion_gen.get_speed(state)

            ret = ctrl.send_servo(x, y, z, speed=speed, pitch=pitch, yaw=yaw, dt=DT)
            if ret == -2:
                time.sleep(0.2)
                continue

            if t - last_log >= 2.0:
                vlm_info = ""
                if perception:
                    vlm_info = f"  vlm#{perception.call_count}"
                pitch_info = f" P={pitch:.0f}" if pitch != 0 else ""
                yaw_info = f" W={yaw:.0f}" if yaw != 0 else ""
                print(f"  [{motion_gen.current_mode}] "
                      f"e={state.energy:.2f} m={state.mood:.2f} "
                      f"ax={state.attention_x:+.2f} ay={state.attention_y:+.2f} "
                      f"X={x:.0f} Y={y:.0f} Z={z:.0f}{pitch_info}{yaw_info} "
                      f"spd={speed:.0f}{vlm_info}")

                # Push state to dashboard periodically (even without VLM updates)
                if dashboard:
                    dashboard.push_state(
                        state, state,
                        motion_xyz=(x, y, z), speed=speed,
                        vlm_count=perception.call_count if perception else 0,
                        vlm_latency=0,
                        motion_debug=motion_gen.get_debug_state(),
                    )
                    dashboard.push_triggers(trigger_engine.active_triggers)
                    telemetry = ctrl.get_telemetry()
                    if telemetry:
                        dashboard.push_arm_telemetry(telemetry)

                last_log = t

            t += DT
            time.sleep(DT)

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        if perception:
            perception.stop()
        if hand_tracker:
            hand_tracker.stop()
        if cam:
            cam.stop()
        mock_thread_flag[0] = False
        ctrl.disconnect()
        if dashboard:
            dashboard.set_status("stopped")
            dashboard.push_event("SYSTEM", "Pipeline stopped")


if __name__ == "__main__":
    main()
