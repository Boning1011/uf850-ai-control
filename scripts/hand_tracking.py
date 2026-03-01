"""
Hand Tracking Thread — MediaPipe hand detection running in background.

Captures camera frames, runs MediaPipe HandLandmarker, and delivers
normalised hand coordinates via callback. Pushes JPEG frames to a
shared FrameBuffer so the Dashboard camera panel stays alive.

Usage:
    ht = HandTrackingThread(frame_buffer=fb, on_result=my_callback)
    ht.open_camera()   # must be called from main thread on Windows
    ht.start()
    ...
    ht.stop()
"""

import os
import sys
import time
import threading

import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python.vision import (
    HandLandmarker,
    HandLandmarkerOptions,
    RunningMode,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
MODEL_PATH = os.path.join(PROJECT_ROOT, "models", "hand_landmarker.task")

# Landmark indices
WRIST = 0

# MediaPipe hand skeleton connections (21 landmarks)
HAND_CONNECTIONS = [
    # Thumb
    (0, 1), (1, 2), (2, 3), (3, 4),
    # Index finger
    (0, 5), (5, 6), (6, 7), (7, 8),
    # Middle finger
    (5, 9), (9, 10), (10, 11), (11, 12),
    # Ring finger
    (9, 13), (13, 14), (14, 15), (15, 16),
    # Pinky
    (13, 17), (17, 18), (18, 19), (19, 20),
    # Palm
    (0, 17),
]


class HandTrackingThread:
    """Background thread: webcam -> MediaPipe hand detection -> callback.

    The callback signature is:
        on_result(hand_x, hand_y, hand_z, confidence)
    where hand_x/y are normalised 0-1 (None if no hand detected),
    hand_z is relative depth from MediaPipe (or 0), and confidence
    is detection confidence (0 if no hand).
    """

    def __init__(self, frame_buffer=None, device=0, resolution=(640, 480),
                 jpeg_quality=80, on_result=None):
        self.frame_buffer = frame_buffer
        self.device = device
        self.resolution = resolution
        self.jpeg_quality = jpeg_quality
        self.on_result = on_result
        self.running = True
        self._thread = None
        self._cap = None

    def open_camera(self):
        """Open camera on main thread (Windows requires this). Call before start()."""
        print(f"[HandTrack] Opening camera device {self.device}...", flush=True)
        if sys.platform == "win32":
            self._cap = cv2.VideoCapture(self.device, cv2.CAP_DSHOW)
        else:
            self._cap = cv2.VideoCapture(self.device)
        if not self._cap.isOpened():
            print(f"[HandTrack] Failed to open camera device {self.device}", flush=True)
            return False
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])
        w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"[HandTrack] Camera opened: {w}x{h}", flush=True)
        return True

    def start(self):
        if self._cap is None:
            if not self.open_camera():
                return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._cap:
            self._cap.release()
            self._cap = None
            print("[HandTrack] Camera released.", flush=True)

    def _run(self):
        if not os.path.exists(MODEL_PATH):
            print(f"[HandTrack] ERROR: model not found at {MODEL_PATH}", flush=True)
            print("[HandTrack] Download with:", flush=True)
            print(f'  curl -L -o "{MODEL_PATH}" '
                  '"https://storage.googleapis.com/mediapipe-models/'
                  'hand_landmarker/hand_landmarker/float16/latest/'
                  'hand_landmarker.task"', flush=True)
            return

        options = HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=MODEL_PATH),
            running_mode=RunningMode.VIDEO,
            num_hands=1,
            min_hand_detection_confidence=0.7,
            min_hand_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        landmarker = HandLandmarker.create_from_options(options)
        t_start = time.perf_counter()
        cap = self._cap

        print("[HandTrack] Detection loop started.", flush=True)

        while self.running:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.05)
                continue

            # Mirror horizontally so hand right = video right
            frame = cv2.flip(frame, 1)

            # MediaPipe detection (before drawing overlay)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            timestamp_ms = int((time.perf_counter() - t_start) * 1000)

            result = landmarker.detect_for_video(mp_image, timestamp_ms)

            if result.hand_landmarks:
                landmarks = result.hand_landmarks[0]
                h, w = frame.shape[:2]

                # Draw skeleton connections
                for i, j in HAND_CONNECTIONS:
                    x1, y1 = int(landmarks[i].x * w), int(landmarks[i].y * h)
                    x2, y2 = int(landmarks[j].x * w), int(landmarks[j].y * h)
                    cv2.line(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)

                # Draw landmark dots
                for lm in landmarks:
                    cx, cy = int(lm.x * w), int(lm.y * h)
                    cv2.circle(frame, (cx, cy), 3, (0, 200, 0), -1)

                # Highlight wrist with larger circle
                wrist = landmarks[WRIST]
                wx, wy = int(wrist.x * w), int(wrist.y * h)
                cv2.circle(frame, (wx, wy), 8, (0, 0, 255), 2)

                if self.on_result:
                    self.on_result(wrist.x, wrist.y, wrist.z, 1.0)
            else:
                if self.on_result:
                    self.on_result(None, None, None, 0.0)

            # Push JPEG (with overlay) to frame_buffer for Dashboard
            if self.frame_buffer is not None:
                _, jpeg = cv2.imencode(
                    '.jpg', frame,
                    [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality],
                )
                self.frame_buffer.push(jpeg.tobytes())

            # ~30 Hz target
            time.sleep(0.033)

        landmarker.close()
        print("[HandTrack] Detection loop stopped.", flush=True)
