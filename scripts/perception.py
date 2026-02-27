"""
VLM Perception — provider abstraction, Gemini implementation, and camera thread.

Captures camera frames, sends multi-frame batches to a VLM provider,
and updates a shared StateHolder with continuous behavioral parameters.
"""

import collections
import json
import os
import time
import threading
from abc import ABC, abstractmethod
from pathlib import Path

import cv2
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from persona import PerceptionState, StateHolder

# Load .env from project root
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


# ---------- VLM output schema (Pydantic — used by Gemini for structured output) ----------

class VLMOutput(BaseModel):
    """Structured perception output from VLM. All fields enforced by schema."""
    energy: float = Field(description="Activation level. 0.0=dormant/sleeping, 1.0=maximum excitement")
    attention_x: float = Field(description="Horizontal focus. -1.0=far left, 0.0=center, 1.0=far right")
    attention_y: float = Field(description="Vertical focus. -1.0=bottom, 0.0=center, 1.0=top")
    mood: float = Field(description="Emotional coloring. 0.0=tense/wary, 0.5=neutral, 1.0=playful/joyful")
    presence: float = Field(description="Audience amount. 0.0=empty room, 1.0=many people very close")
    urgency: float = Field(description="Sudden change. 0.0=stable scene, 1.0=dramatic sudden change")
    scene_description: str = Field(default="", description="Brief scene description in Chinese, max 30 chars. E.g. '一个人坐在桌前看手机'")
    primary_action: str = Field(default="idle", description="Primary detected action: idle, sitting, standing, waving, approaching, leaving, pointing, leaning, drinking, talking, unknown")


# ---------- provider abstraction ----------

class VLMProvider(ABC):
    """Abstract interface for vision-language model providers."""

    @abstractmethod
    def perceive(self, frames_jpeg: list[bytes], system_prompt: str) -> dict:
        """Send frame(s) and system prompt, return dict matching PerceptionState fields.
        Blocking call. Raises on error."""
        ...


class GeminiProvider(VLMProvider):
    """Gemini 2.5 Flash via google-genai standard generateContent API."""

    def __init__(self, model: str = "gemini-2.5-flash"):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set. Add it to .env or environment.")
        self.client = genai.Client(api_key=api_key)
        self.model = model

    def perceive(self, frames_jpeg: list[bytes], system_prompt: str) -> dict:
        # Build contents: system prompt + image parts + instruction
        contents = []

        # Add each frame as an image part
        n = len(frames_jpeg)
        for i, frame_bytes in enumerate(frames_jpeg):
            contents.append(
                types.Part.from_bytes(data=frame_bytes, mime_type="image/jpeg")
            )

        # Add text instruction
        if n > 1:
            contents.append(
                f"Above are {n} sequential camera frames (oldest to newest, ~0.2s apart). "
                f"Analyze the scene and any dynamic changes between frames."
            )
        else:
            contents.append("Analyze this camera frame.")

        response = self.client.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=VLMOutput,
            ),
        )

        # Use the auto-parsed Pydantic object
        parsed = response.parsed
        if parsed is not None:
            return parsed.model_dump()

        # Fallback: parse from text
        return json.loads(response.text)


# ---------- frame buffer ----------

class FrameBuffer:
    """Thread-safe ring buffer for camera frames (JPEG bytes)."""

    def __init__(self, maxlen: int = 10):
        self._buffer = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def push(self, jpeg_bytes: bytes):
        with self._lock:
            self._buffer.append(jpeg_bytes)

    def get_latest(self, n: int) -> list[bytes]:
        """Return the last n frames (oldest first). May return fewer if not enough frames yet."""
        with self._lock:
            frames = list(self._buffer)
        return frames[-n:] if len(frames) >= n else frames


# ---------- camera capture thread ----------

class CameraThread:
    """Captures frames from a camera at a target fps and pushes JPEG bytes into a FrameBuffer.

    IMPORTANT: On Windows, cv2.VideoCapture() must be called from the main thread.
    Use open_camera() before start().
    """

    def __init__(self, device: int, resolution: tuple, jpeg_quality: int,
                 frame_buffer: FrameBuffer, target_fps: float = 5.0):
        self.device = device
        self.resolution = resolution
        self.jpeg_quality = jpeg_quality
        self.frame_buffer = frame_buffer
        self.target_fps = target_fps
        self.running = True
        self._thread = None
        self._cap = None

    def open_camera(self):
        """Open camera on main thread (Windows requires this). Call before start()."""
        print(f"[Camera] Opening device {self.device}...", flush=True)
        self._cap = cv2.VideoCapture(self.device)
        if not self._cap.isOpened():
            print(f"[Camera] Failed to open device {self.device}", flush=True)
            return False
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])
        w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"[Camera] Opened device {self.device} at {w}x{h}", flush=True)
        return True

    def start(self):
        if self._cap is None:
            if not self.open_camera():
                return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        if self._cap:
            self._cap.release()
            print("[Camera] Released.", flush=True)

    def _run(self):
        interval = 1.0 / self.target_fps
        cap = self._cap

        while self.running:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.1)
                continue

            _, jpeg = cv2.imencode('.jpg', frame,
                                   [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
            self.frame_buffer.push(jpeg.tobytes())
            time.sleep(interval)


# ---------- perception thread ----------

class PerceptionThread:
    """Background thread: grabs frames from buffer -> VLM -> updates StateHolder."""

    def __init__(self, provider: VLMProvider, system_prompt: str,
                 frame_buffer: FrameBuffer, state_holder: StateHolder,
                 rate_hz: float = 1.0, frame_count: int = 5,
                 on_result=None):
        self.provider = provider
        self.system_prompt = system_prompt
        self.frame_buffer = frame_buffer
        self.state_holder = state_holder
        self.rate_hz = rate_hz
        self.frame_count = frame_count
        self.on_result = on_result  # callback(raw_state, latency, call_count)
        self.running = True
        self._thread = None
        self.call_count = 0
        self.error_count = 0
        self.last_scene_description = ""
        self.last_primary_action = "idle"

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False

    def _run(self):
        interval = 1.0 / self.rate_hz
        print(f"[VLM] Perception loop started ({self.rate_hz} Hz, {self.frame_count} frames/call)", flush=True)

        while self.running:
            frames = self.frame_buffer.get_latest(self.frame_count)
            if not frames or len(frames) < self.frame_count:
                time.sleep(0.5)
                continue

            try:
                t0 = time.time()
                result = self.provider.perceive(frames, self.system_prompt)
                latency = time.time() - t0
                self.call_count += 1

                state = PerceptionState(
                    energy=result.get("energy", 0.0),
                    attention_x=result.get("attention_x", 0.0),
                    attention_y=result.get("attention_y", 0.0),
                    mood=result.get("mood", 0.5),
                    presence=result.get("presence", 0.0),
                    urgency=result.get("urgency", 0.0),
                    timestamp=time.time(),
                )
                self.state_holder.update(state)

                # Cache scene text (display-only, not used for motion)
                self.last_scene_description = str(result.get("scene_description", ""))[:50]
                self.last_primary_action = str(result.get("primary_action", "idle"))

                scene_tag = f" [{self.last_primary_action}] {self.last_scene_description}" if self.last_scene_description else ""
                print(f"[VLM] #{self.call_count} ({latency:.1f}s) "
                      f"e={state.energy:.2f} ax={state.attention_x:+.2f} ay={state.attention_y:+.2f} "
                      f"m={state.mood:.2f} p={state.presence:.2f} u={state.urgency:.2f}"
                      f"{scene_tag}", flush=True)

                if self.on_result:
                    try:
                        self.on_result(state, latency, self.call_count)
                    except Exception:
                        pass

            except Exception as e:
                self.error_count += 1
                print(f"[VLM] Error #{self.error_count}: {e}", flush=True)

            time.sleep(interval)
