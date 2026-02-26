"""
Test live camera -> VLM perception loop (no arm).

Runs camera capture at ~5fps, sends batches of frames to Gemini at ~1Hz,
prints PerceptionState updates in real time.

Usage:
    python test_perception_live.py [--duration 30] [--persona personas/default.yaml]
    python test_perception_live.py --no-camera   # mock mode: static gray frames
"""

import argparse
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np
from persona import PersonaConfig, StateHolder
from perception import GeminiProvider, FrameBuffer, CameraThread, PerceptionThread


def mock_camera_thread(frame_buffer, running_flag, fps=5.0):
    """Push static gray frames into the buffer (for headless testing)."""
    # Create a simple gray frame with "NO CAMERA" text
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[:] = 128
    cv2.putText(frame, "NO CAMERA - MOCK", (120, 250),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2)
    _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 50])
    mock_bytes = jpeg.tobytes()

    interval = 1.0 / fps
    while running_flag[0]:
        frame_buffer.push(mock_bytes)
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Test live VLM perception loop")
    parser.add_argument("--persona", default="personas/default.yaml")
    parser.add_argument("--duration", type=float, default=30.0,
                        help="Run duration in seconds (default: 30)")
    parser.add_argument("--no-camera", action="store_true",
                        help="Use mock frames instead of real camera")
    args = parser.parse_args()

    config = PersonaConfig(args.persona)
    print(f"Persona: {config.name}")
    print(f"Provider: {config.vlm_provider} / {config.vlm_model}")
    print(f"Duration: {args.duration}s\n")

    # Init components
    frame_buffer = FrameBuffer(maxlen=20)
    state_holder = StateHolder(smoothing=0.3)
    provider = GeminiProvider(model=config.vlm_model)

    # Start camera
    running_flag = [True]
    if args.no_camera:
        import threading
        print("[Camera] Mock mode — sending static frames")
        cam_thread = threading.Thread(
            target=mock_camera_thread,
            args=(frame_buffer, running_flag),
            daemon=True,
        )
        cam_thread.start()
    else:
        cam = CameraThread(
            device=config.camera_device,
            resolution=config.camera_resolution,
            jpeg_quality=config.camera_jpeg_quality,
            frame_buffer=frame_buffer,
            target_fps=5.0,
        )
        if not cam.open_camera():
            print("[ERROR] Cannot open camera. Try --no-camera for mock mode.")
            sys.exit(1)
        cam.start()

    # Start perception
    perception = PerceptionThread(
        provider=provider,
        system_prompt=config.full_system_prompt,
        frame_buffer=frame_buffer,
        state_holder=state_holder,
        rate_hz=config.vlm_rate_hz,
        frame_count=config.frame_count,
    )
    perception.start()

    # Monitor loop — print state every second
    try:
        t0 = time.time()
        while time.time() - t0 < args.duration:
            time.sleep(1.0)
            s = state_holder.get()
            elapsed = time.time() - t0
            print(f"  [{elapsed:5.1f}s] e={s.energy:.2f} ax={s.attention_x:+.2f} "
                  f"ay={s.attention_y:+.2f} m={s.mood:.2f} p={s.presence:.2f} u={s.urgency:.2f}"
                  f"  (vlm calls: {perception.call_count}, errors: {perception.error_count})")
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        perception.stop()
        running_flag[0] = False
        if not args.no_camera:
            cam.stop()

    print(f"\nDone. VLM calls: {perception.call_count}, errors: {perception.error_count}")


if __name__ == "__main__":
    main()
