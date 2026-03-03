"""
Persona system — PerceptionState dataclass, StateHolder, and YAML config loader.
"""

import dataclasses
import threading
import time
from pathlib import Path

import yaml


@dataclasses.dataclass
class PerceptionState:
    """Continuous behavioral parameters from VLM perception.
    All floats, clamped to valid ranges on creation."""
    energy: float = 0.0          # 0.0 = dormant, 1.0 = maximum excitement
    mood: float = 0.5            # 0.0 = tense/wary, 0.5 = neutral, 1.0 = playful
    presence: float = 0.0        # 0.0 = nobody, 1.0 = many people / close
    urgency: float = 0.0        # 0.0 = calm scene, 1.0 = sudden dramatic change
    timestamp: float = 0.0       # time.time() when produced

    def __post_init__(self):
        self.energy = max(0.0, min(1.0, float(self.energy)))
        self.mood = max(0.0, min(1.0, float(self.mood)))
        self.presence = max(0.0, min(1.0, float(self.presence)))
        self.urgency = max(0.0, min(1.0, float(self.urgency)))


class StateHolder:
    """Thread-safe container for PerceptionState with exponential smoothing."""

    def __init__(self, smoothing=0.3):
        self._state = PerceptionState()
        self._lock = threading.Lock()
        self._smoothing = smoothing  # 0 = no smoothing, higher = more lag

    def update(self, new_state: PerceptionState):
        with self._lock:
            a = self._smoothing
            self._state.energy = a * self._state.energy + (1 - a) * new_state.energy
            self._state.mood = a * self._state.mood + (1 - a) * new_state.mood
            self._state.presence = a * self._state.presence + (1 - a) * new_state.presence
            self._state.urgency = a * self._state.urgency + (1 - a) * new_state.urgency
            self._state.timestamp = new_state.timestamp

    def get(self) -> PerceptionState:
        with self._lock:
            return dataclasses.replace(self._state)

    def reset(self):
        """Reset to default state (all zeros, mood=0.5). Called on mode switches."""
        with self._lock:
            self._state = PerceptionState()


class PersonaConfig:
    """Loads and holds persona configuration from YAML."""

    def __init__(self, yaml_path: str):
        path = Path(yaml_path)
        if not path.exists():
            raise FileNotFoundError(f"Persona config not found: {yaml_path}")
        with open(path, "r", encoding="utf-8") as f:
            self._raw = yaml.safe_load(f)

        # Top level
        self.name = self._raw.get("name", "Unnamed")
        self.description = self._raw.get("description", "")

        # VLM section
        vlm = self._raw.get("vlm", {})
        self.system_prompt = vlm.get("system_prompt", "")
        self.personality_hint = vlm.get("personality_hint", "")
        self.vlm_rate_hz = vlm.get("rate_hz", 1.0)
        self.vlm_provider = vlm.get("provider", "gemini")
        self.vlm_model = vlm.get("model", "gemini-2.5-flash")
        self.frame_count = vlm.get("frame_count", 5)

        # Motion section
        motion = self._raw.get("motion", {})
        self.center = tuple(motion.get("center", [300, 0, 400]))
        self.amplitude_low = tuple(motion.get("amplitude", {}).get("low", [15, 10, 20]))
        self.amplitude_high = tuple(motion.get("amplitude", {}).get("high", [120, 100, 200]))
        self.frequency_low = tuple(motion.get("frequency", {}).get("low", [0.03, 0.04, 0.05]))
        self.frequency_high = tuple(motion.get("frequency", {}).get("high", [1.2, 1.5, 0.8]))
        self.speed_base = motion.get("speed", {}).get("base", 150)
        self.speed_energy_mult = tuple(motion.get("speed", {}).get("energy_mult", [0.3, 3.0]))
        self.blend_duration = motion.get("blend_duration", 0.5)

        # Safety section
        safety = self._raw.get("safety", {})
        self.bounds_x = tuple(safety.get("x", [100, 450]))
        self.bounds_y = tuple(safety.get("y", [-150, 150]))
        self.bounds_z = tuple(safety.get("z", [250, 950]))

        # Camera section
        camera = self._raw.get("camera", {})
        self.camera_device = camera.get("device", 0)
        self.camera_resolution = tuple(camera.get("resolution", [640, 480]))
        self.camera_jpeg_quality = camera.get("jpeg_quality", 80)

    @property
    def full_system_prompt(self) -> str:
        parts = [self.system_prompt.strip()]
        if self.personality_hint:
            parts.append(self.personality_hint.strip())
        return "\n\n".join(parts)

    def __repr__(self):
        return (f"PersonaConfig(name={self.name!r}, provider={self.vlm_provider}, "
                f"model={self.vlm_model}, rate={self.vlm_rate_hz}Hz, "
                f"center={self.center}, frames={self.frame_count})")


# ---------- CLI self-test ----------

if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "personas/default.yaml"
    config = PersonaConfig(path)
    print(config)
    print(f"\nSystem prompt ({len(config.full_system_prompt)} chars):")
    print(config.full_system_prompt[:300] + "..." if len(config.full_system_prompt) > 300 else config.full_system_prompt)
    print(f"\nMotion: amplitude {config.amplitude_low} -> {config.amplitude_high}")
    print(f"Motion: frequency {config.frequency_low} -> {config.frequency_high}")
    print(f"Motion: center {config.center}")
    print(f"Safety: X={config.bounds_x} Y={config.bounds_y} Z={config.bounds_z}")

    # Quick StateHolder test
    holder = StateHolder(smoothing=0.3)
    holder.update(PerceptionState(energy=1.0, mood=0.8, timestamp=time.time()))
    s = holder.get()
    print(f"\nStateHolder test: energy={s.energy:.2f} mood={s.mood:.2f} (after 1 update from defaults)")
