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
