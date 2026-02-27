"""
Trigger Engine — detects behavioral state transitions from VLM perception.

Fires/clears triggers based on threshold crossings, tracks big deltas,
and maintains event history for logging and dashboard display.
"""

import time
from persona import PerceptionState


class TriggerEngine:
    """Detects behavioral triggers from VLM state changes."""

    TRIGGERS = {
        "HEAD_TURN_LEFT":   {"field": "attention_x", "op": "<",  "val": -0.4},
        "HEAD_TURN_RIGHT":  {"field": "attention_x", "op": ">",  "val": 0.4},
        "LOOKING_UP":       {"field": "attention_y", "op": ">",  "val": 0.4},
        "LOOKING_DOWN":     {"field": "attention_y", "op": "<",  "val": -0.4},
        "HIGH_ENERGY":      {"field": "energy",      "op": ">",  "val": 0.6},
        "LOW_ENERGY":       {"field": "energy",      "op": "<",  "val": 0.15},
        "SUDDEN_MOVEMENT":  {"field": "urgency",     "op": ">",  "val": 0.5},
        "PLAYFUL_MOOD":     {"field": "mood",        "op": ">",  "val": 0.7},
        "TENSE_MOOD":       {"field": "mood",        "op": "<",  "val": 0.3},
        "HIGH_PRESENCE":    {"field": "presence",    "op": ">",  "val": 0.5},
        "LOW_PRESENCE":     {"field": "presence",    "op": "<",  "val": 0.15},
    }

    def __init__(self):
        self.active_triggers = set()
        self.trigger_history = []  # (timestamp, event_type, trigger_name, details)
        self.prev_state = None

    def check(self, state: PerceptionState) -> list[str]:
        """Check state against all triggers, return list of newly fired triggers."""
        newly_fired = []
        current_active = set()

        for name, rule in self.TRIGGERS.items():
            val = getattr(state, rule["field"])
            if rule["op"] == ">" and val > rule["val"]:
                current_active.add(name)
            elif rule["op"] == "<" and val < rule["val"]:
                current_active.add(name)

        # Detect transitions
        for name in current_active - self.active_triggers:
            newly_fired.append(name)
            self.trigger_history.append((
                time.time(), "FIRED", name,
                self._state_summary(state),
            ))

        for name in self.active_triggers - current_active:
            self.trigger_history.append((
                time.time(), "CLEARED", name,
                self._state_summary(state),
            ))

        self.active_triggers = current_active

        # Track big deltas
        if self.prev_state is not None:
            deltas = self._compute_deltas(self.prev_state, state)
            big_deltas = {k: v for k, v in deltas.items() if abs(v) > 0.2}
            if big_deltas:
                self.trigger_history.append((
                    time.time(), "BIG_DELTA", "",
                    ", ".join(f"{k}={v:+.2f}" for k, v in big_deltas.items()),
                ))

        self.prev_state = state
        return newly_fired

    def _state_summary(self, s):
        return (f"e={s.energy:.2f} ax={s.attention_x:+.2f} ay={s.attention_y:+.2f} "
                f"m={s.mood:.2f} p={s.presence:.2f} u={s.urgency:.2f}")

    def _compute_deltas(self, old, new):
        return {
            "energy": new.energy - old.energy,
            "att_x": new.attention_x - old.attention_x,
            "att_y": new.attention_y - old.attention_y,
            "mood": new.mood - old.mood,
            "presence": new.presence - old.presence,
            "urgency": new.urgency - old.urgency,
        }


class ModeEngine:
    """Maps active triggers to a motion mode with smooth parameter blending.

    Each mode provides multipliers (amp, freq, speed) and a center Z offset
    that are applied to the parametric motion generator. Transitions between
    modes are smoothed via exponential interpolation.
    """

    # Mode parameters: multipliers applied to base parametric motion
    MODE_PARAMS = {
        "CALM":     {"amp_mult": 1.0, "freq_mult": 1.0, "speed_mult": 1.0, "center_z_offset": 0},
        "ALERT":    {"amp_mult": 1.5, "freq_mult": 1.2, "speed_mult": 1.4, "center_z_offset": 30},
        "EXCITED":  {"amp_mult": 2.5, "freq_mult": 1.8, "speed_mult": 2.5, "center_z_offset": 50},
        "PLAYFUL":  {"amp_mult": 2.0, "freq_mult": 0.8, "speed_mult": 1.5, "center_z_offset": 20},
        "TENSE":    {"amp_mult": 1.3, "freq_mult": 2.0, "speed_mult": 1.8, "center_z_offset": -20},
        "DORMANT":  {"amp_mult": 0.3, "freq_mult": 0.5, "speed_mult": 0.4, "center_z_offset": -40},
    }

    # Priority-ordered rules: first match wins (highest priority first)
    TRIGGER_RULES = [
        ({"HIGH_ENERGY", "SUDDEN_MOVEMENT"}, "EXCITED"),   # any of these
        ({"HIGH_ENERGY"},                     "EXCITED"),
        ({"SUDDEN_MOVEMENT"},                 "EXCITED"),
        ({"PLAYFUL_MOOD"},                    "PLAYFUL"),
        ({"TENSE_MOOD"},                      "TENSE"),
        ({"HIGH_PRESENCE"},                   "ALERT"),
        ({"HEAD_TURN_LEFT"},                  "ALERT"),
        ({"HEAD_TURN_RIGHT"},                 "ALERT"),
        ({"LOOKING_UP"},                      "ALERT"),
        ({"LOOKING_DOWN"},                    "ALERT"),
        ({"LOW_ENERGY", "LOW_PRESENCE"},      "DORMANT"),   # both required
    ]

    def __init__(self, blend_speed=0.15):
        """
        Args:
            blend_speed: exponential blend factor per update (0-1).
                         Lower = smoother/slower transitions.
        """
        self.current_mode = "CALM"
        self.blend_speed = blend_speed
        # Current blended params (start at CALM)
        self._current = dict(self.MODE_PARAMS["CALM"])
        self._target = dict(self.MODE_PARAMS["CALM"])

    def update(self, active_triggers: set) -> tuple[str, dict]:
        """Determine mode from active triggers, blend params smoothly.

        Args:
            active_triggers: set of trigger names currently active

        Returns:
            (mode_name, blended_params_dict)
        """
        # Determine target mode (first matching rule wins)
        new_mode = "CALM"
        for required_triggers, mode in self.TRIGGER_RULES:
            if required_triggers <= active_triggers:  # subset check
                new_mode = mode
                break

        # Special case: DORMANT requires BOTH LOW_ENERGY and LOW_PRESENCE
        if new_mode == "CALM" and {"LOW_ENERGY", "LOW_PRESENCE"} <= active_triggers:
            new_mode = "DORMANT"

        old_mode = self.current_mode
        self.current_mode = new_mode
        self._target = dict(self.MODE_PARAMS[new_mode])

        # Exponential blend toward target
        a = self.blend_speed
        for key in self._current:
            self._current[key] = self._current[key] * (1 - a) + self._target[key] * a

        return old_mode, new_mode, dict(self._current)
