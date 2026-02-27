#!/usr/bin/env python3
"""
Hand Tracking PoC — MediaPipe HandLandmarker standalone test.

Opens the webcam, runs MediaPipe hand detection (Tasks API),
draws landmarks, and prints normalised wrist / index-finger-tip
coordinates + FPS.

No arm code, no integration — pure detection verification.

Requires:
    pip install mediapipe opencv-python
    Model: models/hand_landmarker.task
        curl -L -o models/hand_landmarker.task \
          "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task"

Usage:
    python scripts/test_hand_tracking.py
    Press q or ESC to quit.
"""

import os
import sys
import time

import cv2
import mediapipe as mp
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python.vision import (
    HandLandmarker,
    HandLandmarkerOptions,
    RunningMode,
)

# ── Paths ──────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
MODEL_PATH = os.path.join(PROJECT_ROOT, "models", "hand_landmarker.task")

# Landmark indices
WRIST = 0
INDEX_TIP = 8


def open_camera(device: int = 0, width: int = 640, height: int = 480):
    """Open camera with DirectShow on Windows (avoids hanging)."""
    if sys.platform == "win32":
        cap = cv2.VideoCapture(device, cv2.CAP_DSHOW)
    else:
        cap = cv2.VideoCapture(device)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    if not cap.isOpened():
        print("ERROR: cannot open camera", flush=True)
        sys.exit(1)
    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"Camera opened: {actual_w}x{actual_h}", flush=True)
    return cap


def draw_landmarks(frame, hand_landmarks, w, h):
    """Draw hand landmarks and connections on frame."""
    # Connections for drawing lines between landmarks
    CONNECTIONS = [
        (0, 1), (1, 2), (2, 3), (3, 4),       # thumb
        (0, 5), (5, 6), (6, 7), (7, 8),       # index
        (0, 9), (9, 10), (10, 11), (11, 12),  # middle
        (0, 13), (13, 14), (14, 15), (15, 16),  # ring
        (0, 17), (17, 18), (18, 19), (19, 20),  # pinky
        (5, 9), (9, 13), (13, 17),             # palm
    ]

    points = []
    for lm in hand_landmarks:
        px, py = int(lm.x * w), int(lm.y * h)
        points.append((px, py))

    # Draw connections
    for i, j in CONNECTIONS:
        if i < len(points) and j < len(points):
            cv2.line(frame, points[i], points[j], (200, 200, 200), 1)

    # Draw landmark dots
    for px, py in points:
        cv2.circle(frame, (px, py), 3, (255, 255, 0), -1)

    return points


def main():
    if not os.path.exists(MODEL_PATH):
        print(f"ERROR: model not found at {MODEL_PATH}", flush=True)
        print("Download it with:", flush=True)
        print(f'  curl -L -o "{MODEL_PATH}" '
              '"https://storage.googleapis.com/mediapipe-models/'
              'hand_landmarker/hand_landmarker/float16/latest/'
              'hand_landmarker.task"', flush=True)
        sys.exit(1)

    cap = open_camera()

    # ── HandLandmarker (VIDEO mode — synchronous, frame-by-frame) ──
    options = HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=MODEL_PATH),
        running_mode=RunningMode.VIDEO,
        num_hands=1,
        min_hand_detection_confidence=0.7,
        min_hand_presence_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    landmarker = HandLandmarker.create_from_options(options)

    fps_smooth = 0.0
    t_prev = time.perf_counter()
    frame_count = 0
    t_start = time.perf_counter()

    print("Hand tracking running. Press q/ESC to quit.", flush=True)

    while True:
        ok, frame = cap.read()
        if not ok:
            print("WARNING: cap.read() failed, retrying...", flush=True)
            continue

        h, w = frame.shape[:2]

        # Convert to MediaPipe Image (RGB)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

        # Timestamp in milliseconds (monotonic from start)
        timestamp_ms = int((time.perf_counter() - t_start) * 1000)

        result = landmarker.detect_for_video(mp_image, timestamp_ms)

        # ── Draw landmarks ─────────────────────────────────────────
        if result.hand_landmarks:
            for hand_lm in result.hand_landmarks:
                points = draw_landmarks(frame, hand_lm, w, h)

                # Highlight wrist and index tip
                if len(points) > INDEX_TIP:
                    wrist_pt = points[WRIST]
                    index_pt = points[INDEX_TIP]

                    cv2.circle(frame, wrist_pt, 10, (0, 255, 0), -1)
                    cv2.circle(frame, index_pt, 10, (0, 0, 255), -1)

                    cv2.putText(frame, "WRIST", (wrist_pt[0] + 12, wrist_pt[1]),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
                    cv2.putText(frame, "INDEX", (index_pt[0] + 12, index_pt[1]),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

                    # Normalised coordinates
                    wrist_lm = hand_lm[WRIST]
                    index_lm = hand_lm[INDEX_TIP]

                    # Console output (throttled to ~5 Hz)
                    if frame_count % 6 == 0:
                        print(
                            f"  wrist=({wrist_lm.x:.3f}, {wrist_lm.y:.3f})  "
                            f"index=({index_lm.x:.3f}, {index_lm.y:.3f})  "
                            f"FPS={fps_smooth:.1f}",
                            flush=True,
                        )
        else:
            cv2.putText(frame, "No hand detected", (20, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)

        # ── FPS counter ────────────────────────────────────────────
        t_now = time.perf_counter()
        dt = t_now - t_prev
        t_prev = t_now
        if dt > 0:
            fps_instant = 1.0 / dt
            fps_smooth = fps_smooth * 0.9 + fps_instant * 0.1

        cv2.putText(frame, f"FPS: {fps_smooth:.1f}", (20, h - 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

        frame_count += 1

        # ── Show ───────────────────────────────────────────────────
        cv2.imshow("Hand Tracking PoC", frame)
        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), 27):  # q or ESC
            break

    landmarker.close()
    cap.release()
    cv2.destroyAllWindows()
    print(f"Done. {frame_count} frames processed.", flush=True)


if __name__ == "__main__":
    main()
