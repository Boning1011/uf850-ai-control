"""
Test VLM perception with static image(s) or a live webcam snapshot.

Usage:
    python test_perception_static.py                   # capture from webcam
    python test_perception_static.py photo.jpg         # single image file
    python test_perception_static.py f1.jpg f2.jpg ... # multiple frames

Tests that Gemini returns valid structured PerceptionState.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2
from persona import PersonaConfig, PerceptionState
from perception import GeminiProvider


def capture_webcam_frames(device=0, n=5, fps=5.0):
    """Capture n frames from webcam at given fps. Returns list of JPEG bytes."""
    cap = cv2.VideoCapture(device)
    if not cap.isOpened():
        print(f"[ERROR] Cannot open camera device {device}")
        sys.exit(1)

    print(f"Capturing {n} frames from webcam (device {device})...")
    frames = []
    interval = 1.0 / fps
    for i in range(n):
        ret, frame = cap.read()
        if not ret:
            print(f"  Frame {i}: capture failed, retrying...")
            time.sleep(0.2)
            ret, frame = cap.read()
            if not ret:
                continue
        _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        frames.append(jpeg.tobytes())
        print(f"  Frame {i}: {len(frames[-1])} bytes")
        if i < n - 1:
            time.sleep(interval)

    cap.release()
    print(f"Captured {len(frames)} frames.\n")
    return frames


def load_image_files(paths):
    """Load image files as JPEG bytes."""
    frames = []
    for p in paths:
        if not os.path.exists(p):
            print(f"[WARN] File not found: {p}")
            continue
        with open(p, "rb") as f:
            data = f.read()
        frames.append(data)
        print(f"  Loaded {p}: {len(data)} bytes")
    return frames


def main():
    persona_path = "personas/default.yaml"
    config = PersonaConfig(persona_path)
    print(f"Persona: {config.name}")
    print(f"Provider: {config.vlm_provider} / {config.vlm_model}\n")

    # Get frames: from args or webcam
    if len(sys.argv) > 1:
        image_paths = sys.argv[1:]
        frames = load_image_files(image_paths)
    else:
        frames = capture_webcam_frames(
            device=config.camera_device,
            n=config.frame_count,
        )

    if not frames:
        print("[ERROR] No frames to analyze.")
        sys.exit(1)

    # Init provider and call
    print(f"Sending {len(frames)} frame(s) to {config.vlm_model}...")
    provider = GeminiProvider(model=config.vlm_model)

    t0 = time.time()
    result = provider.perceive(frames, config.full_system_prompt)
    latency = time.time() - t0

    print(f"\n--- Result ({latency:.1f}s) ---")
    state = PerceptionState(**{k: result[k] for k in
                               ["energy", "mood", "presence", "urgency"]},
                            timestamp=time.time())
    print(f"  energy:      {state.energy:.3f}")
    print(f"  mood:        {state.mood:.3f}")
    print(f"  presence:    {state.presence:.3f}")
    print(f"  urgency:     {state.urgency:.3f}")
    print(f"\nRaw: {result}")


if __name__ == "__main__":
    main()
