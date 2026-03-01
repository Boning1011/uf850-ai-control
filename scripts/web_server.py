"""
Dashboard Web Server — real-time monitoring for the UF850 pipeline.

Provides:
- MJPEG camera stream at /video_feed
- WebSocket at /ws for real-time VLM state, triggers, and events
- Static dashboard page at /
- REST endpoints for pipeline control

Runs in a background thread alongside the main pipeline.
"""

import asyncio
import json
import threading
import time
from collections import deque
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse


class DashboardServer:
    """Lightweight web dashboard for real-time pipeline monitoring."""

    def __init__(self, frame_buffer, state_holder, host="0.0.0.0", port=7860):
        self.frame_buffer = frame_buffer
        self.state_holder = state_holder
        self.host = host
        self.port = port

        # Thread-safe shared state (written from pipeline threads)
        self._lock = threading.Lock()
        self._latest_raw = None
        self._latest_smoothed = None
        self._latest_motion = None  # {"x": ..., "y": ..., "z": ..., "speed": ...}
        self._vlm_count = 0
        self._vlm_latency = 0.0
        self._active_triggers = []
        self._pipeline_status = "running"
        self._scene_description = ""
        self._primary_action = "idle"
        self._motion_debug = None
        self._arm_telemetry = None
        self._current_mode = "IDLE"
        self._mode_history = []  # [{time, from_mode, to_mode, action, confidence}]
        self._hand_tracking = {"x": None, "y": None, "confidence": 0.0, "active": False}
        self._input_mode = "hand_tracking"  # "vlm" | "hand_tracking" | "keyboard"

        # Event log (thread-safe deque)
        self._events = deque(maxlen=500)
        self._event_counter = 0

        # Async event loop (set when server starts)
        self._loop = None
        self._thread = None

        # Pipeline control callback: called with command string
        self.on_command = None

        self.app = self._create_app()

    def _create_app(self):
        app = FastAPI(title="UF850 Dashboard")
        server = self

        @app.get("/", response_class=HTMLResponse)
        async def index():
            html_path = Path(__file__).parent / "static" / "index.html"
            return HTMLResponse(html_path.read_text(encoding="utf-8"))

        @app.get("/video_feed")
        async def video_feed():
            return StreamingResponse(
                server._mjpeg_stream(),
                media_type="multipart/x-mixed-replace; boundary=frame",
            )

        @app.websocket("/ws")
        async def ws_endpoint(websocket: WebSocket):
            await websocket.accept()

            async def sender():
                """Push state + events to client at ~10 Hz."""
                last_event_id = 0
                last_state_hash = None

                # Send event history on connect
                with server._lock:
                    history = list(server._events)
                if history:
                    await websocket.send_json({"type": "init", "events": history})
                    last_event_id = history[-1]["id"]

                while True:
                    await asyncio.sleep(0.1)

                    # Send new events
                    with server._lock:
                        new_events = [e for e in server._events if e["id"] > last_event_id]
                    for evt in new_events:
                        await websocket.send_json(evt)
                        last_event_id = evt["id"]

                    # Send state update
                    with server._lock:
                        if server._latest_smoothed is None:
                            continue
                        state_msg = {
                            "type": "state",
                            "smoothed": server._latest_smoothed,
                            "raw": server._latest_raw,
                            "motion": server._latest_motion,
                            "motion_debug": server._motion_debug,
                            "arm": server._arm_telemetry,
                            "scene_description": server._scene_description,
                            "primary_action": server._primary_action,
                            "vlm_count": server._vlm_count,
                            "vlm_latency": server._vlm_latency,
                            "active_triggers": server._active_triggers,
                            "current_mode": server._current_mode,
                            "mode_history": server._mode_history[-10:],
                            "hand_tracking": server._hand_tracking,
                            "input_mode": server._input_mode,
                            "status": server._pipeline_status,
                        }

                    # Simple change detection: skip if identical
                    h = json.dumps(state_msg, sort_keys=True)
                    if h != last_state_hash:
                        await websocket.send_json(state_msg)
                        last_state_hash = h

            async def receiver():
                """Listen for commands from client."""
                while True:
                    data = await websocket.receive_text()
                    try:
                        msg = json.loads(data)
                        cmd = msg.get("command")
                        if cmd and server.on_command:
                            server.on_command(cmd, msg)
                    except (json.JSONDecodeError, Exception):
                        pass

            try:
                await asyncio.gather(sender(), receiver())
            except (WebSocketDisconnect, Exception):
                pass

        return app

    async def _mjpeg_stream(self):
        """Yield MJPEG frames from the shared FrameBuffer."""
        while True:
            frames = self.frame_buffer.get_latest(1)
            if frames:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + frames[-1]
                    + b"\r\n"
                )
            await asyncio.sleep(0.1)  # ~10 fps max

    # ── Public API (called from pipeline threads) ──

    def push_state(self, raw_state, smoothed_state, motion_xyz=None, speed=None,
                   vlm_count=0, vlm_latency=0.0, motion_debug=None):
        """Push VLM state update. Called from perception/main threads."""
        raw_dict = {
            "energy": round(raw_state.energy, 3),
            "attention_x": round(raw_state.attention_x, 3),
            "attention_y": round(raw_state.attention_y, 3),
            "mood": round(raw_state.mood, 3),
            "presence": round(raw_state.presence, 3),
            "urgency": round(raw_state.urgency, 3),
        }
        smoothed_dict = {
            "energy": round(smoothed_state.energy, 3),
            "attention_x": round(smoothed_state.attention_x, 3),
            "attention_y": round(smoothed_state.attention_y, 3),
            "mood": round(smoothed_state.mood, 3),
            "presence": round(smoothed_state.presence, 3),
            "urgency": round(smoothed_state.urgency, 3),
        }
        motion_dict = None
        if motion_xyz is not None:
            motion_dict = {
                "x": round(motion_xyz[0], 1),
                "y": round(motion_xyz[1], 1),
                "z": round(motion_xyz[2], 1),
                "speed": round(speed, 1) if speed else 0,
            }

        with self._lock:
            self._latest_raw = raw_dict
            self._latest_smoothed = smoothed_dict
            self._latest_motion = motion_dict
            self._vlm_count = vlm_count
            self._vlm_latency = round(vlm_latency, 2)
            if motion_debug is not None:
                self._motion_debug = motion_debug
                if "mode" in motion_debug:
                    self._current_mode = motion_debug["mode"]

    def push_vlm_text(self, scene_description, primary_action):
        """Push VLM scene description text. Called from perception callback."""
        with self._lock:
            self._scene_description = scene_description
            self._primary_action = primary_action

    def push_arm_telemetry(self, telemetry):
        """Push arm physical telemetry dict. Called from main loop."""
        with self._lock:
            self._arm_telemetry = telemetry

    def push_input_mode(self, mode):
        """Set the active input mode: 'vlm', 'hand_tracking', or 'keyboard'."""
        with self._lock:
            self._input_mode = mode

    def push_hand_tracking(self, hand_x, hand_y, confidence):
        """Push hand tracking state. Called from hand tracking callback."""
        with self._lock:
            if hand_x is not None:
                self._hand_tracking = {
                    "x": round(hand_x, 3),
                    "y": round(hand_y, 3),
                    "confidence": round(confidence, 2),
                    "active": True,
                }
            else:
                self._hand_tracking = {"x": None, "y": None, "confidence": 0.0, "active": False}

    def push_triggers(self, active_triggers):
        """Update active trigger list. Called after TriggerEngine.check()."""
        with self._lock:
            self._active_triggers = list(active_triggers)

    def push_mode_transition(self, from_mode, to_mode, action, confidence):
        """Record a mode transition. Called from action detection loop."""
        entry = {
            "time": time.time(),
            "from_mode": from_mode,
            "to_mode": to_mode,
            "action": action,
            "confidence": round(confidence, 2),
        }
        with self._lock:
            self._current_mode = to_mode
            self._mode_history.append(entry)
            # Keep last 50 transitions
            if len(self._mode_history) > 50:
                self._mode_history = self._mode_history[-50:]
        self.push_event("MODE_SWITCH",
                        f"{action} (conf {confidence:.2f}) → {to_mode}",
                        f"from {from_mode}")

    def push_event(self, event_type, message, details=""):
        """Push an event to the log. Thread-safe."""
        with self._lock:
            self._event_counter += 1
            self._events.append({
                "id": self._event_counter,
                "type": "event",
                "event": event_type,
                "message": message,
                "details": details,
                "timestamp": time.time(),
            })

    def set_status(self, status):
        """Set pipeline status: 'running', 'paused', 'stopped'."""
        with self._lock:
            self._pipeline_status = status

    # ── Server lifecycle ──

    def start(self):
        """Start the web server in a background thread."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print(f"[Dashboard] http://{self.host}:{self.port}", flush=True)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            loop="asyncio",
        )
        server = uvicorn.Server(config)
        self._loop.run_until_complete(server.serve())
