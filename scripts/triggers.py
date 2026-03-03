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
        "HIGH_ENERGY":      {"field": "energy",      "op": ">",  "val": 0.6},
        "SUDDEN_MOVEMENT":  {"field": "urgency",     "op": ">",  "val": 0.5},
        "PLAYFUL_MOOD":     {"field": "mood",        "op": ">",  "val": 0.7},
        "HIGH_PRESENCE":    {"field": "presence",    "op": ">",  "val": 0.5},
    }

    # Gesture string -> trigger name mapping. Any gesture not "none" fires a trigger.
    GESTURE_TRIGGERS = {
        "heart":      "GESTURE_HEART",
        "thumbs_up":  "GESTURE_THUMBS_UP",
        "rock":       "GESTURE_ROCK",
        "peace":      "GESTURE_PEACE",
        "wave":       "GESTURE_WAVE",
        "pointing":   "GESTURE_POINTING",
        "open_palm":  "GESTURE_OPEN_PALM",
        "fist":       "GESTURE_FIST",
        "ok":         "GESTURE_OK",
        "other":      "GESTURE_OTHER",
    }

    def __init__(self):
        self.active_triggers = set()
        self.trigger_history = []  # (timestamp, event_type, trigger_name, details)
        self.prev_state = None
        self._active_gesture_trigger = None  # currently active gesture trigger name

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

        # Preserve gesture triggers (managed by check_gesture, not numeric thresholds)
        gesture_triggers = {t for t in self.active_triggers if t.startswith("GESTURE_")}
        current_active |= gesture_triggers

        # Detect transitions (only for numeric triggers)
        for name in current_active - self.active_triggers:
            if not name.startswith("GESTURE_"):
                newly_fired.append(name)
                self.trigger_history.append((
                    time.time(), "FIRED", name,
                    self._state_summary(state),
                ))

        for name in self.active_triggers - current_active:
            if not name.startswith("GESTURE_"):
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

    def check_gesture(self, gesture: str) -> list[str]:
        """Check gesture string, fire/clear gesture triggers. Returns newly fired."""
        newly_fired = []
        trigger_name = self.GESTURE_TRIGGERS.get(gesture)

        # Clear previous gesture trigger if gesture changed
        if self._active_gesture_trigger and self._active_gesture_trigger != trigger_name:
            old = self._active_gesture_trigger
            self.active_triggers.discard(old)
            self.trigger_history.append((
                time.time(), "CLEARED", old, f"gesture={gesture}",
            ))
            self._active_gesture_trigger = None

        # Fire new gesture trigger
        if trigger_name and trigger_name not in self.active_triggers:
            self.active_triggers.add(trigger_name)
            self._active_gesture_trigger = trigger_name
            newly_fired.append(trigger_name)
            self.trigger_history.append((
                time.time(), "FIRED", trigger_name, f"gesture={gesture}",
            ))
        elif trigger_name:
            self._active_gesture_trigger = trigger_name

        return newly_fired

    def _state_summary(self, s):
        return (f"e={s.energy:.2f} m={s.mood:.2f} "
                f"p={s.presence:.2f} u={s.urgency:.2f}")

    def _compute_deltas(self, old, new):
        return {
            "energy": new.energy - old.energy,
            "mood": new.mood - old.mood,
            "presence": new.presence - old.presence,
            "urgency": new.urgency - old.urgency,
        }


class ModeEngine:
    """Maps active triggers to a motion mode — instant switching, no blending.

    Mode transitions are immediate. The motion generator handles each mode
    as a completely distinct pattern. A `mode_just_changed` flag tells the
    main loop to flush the arm command queue for a snappy transition.
    """

    VALID_MODES = {"CALM", "ALERT", "EXCITED", "PLAYFUL", "TRACK"}

    # Priority-ordered rules: first match wins (highest priority first)
    # Gesture triggers are HIGHEST priority — they always override numeric triggers.
    TRIGGER_RULES = [
        # --- Gesture triggers (top priority) ---
        ({"GESTURE_ROCK"},       "EXCITED"),   # rock/metal → wild energy
        ({"GESTURE_FIST"},       "EXCITED"),   # fist pump → high energy
        ({"GESTURE_THUMBS_UP"},  "EXCITED"),   # thumbs up → excited approval
        ({"GESTURE_HEART"},      "PLAYFUL"),   # heart → warm and playful
        ({"GESTURE_PEACE"},      "PLAYFUL"),   # peace → playful and happy
        ({"GESTURE_OK"},         "PLAYFUL"),   # ok → positive playful
        ({"GESTURE_WAVE"},       "ALERT"),     # wave → full attention
        ({"GESTURE_POINTING"},   "ALERT"),     # pointing → directional attention
        ({"GESTURE_OPEN_PALM"},  "ALERT"),     # open palm → "look at me"
        ({"GESTURE_OTHER"},      "ALERT"),     # unknown gesture → curious attention
        # --- Numeric triggers ---
        ({"HIGH_ENERGY", "SUDDEN_MOVEMENT"}, "EXCITED"),
        ({"HIGH_ENERGY"},                     "EXCITED"),
        ({"SUDDEN_MOVEMENT"},                 "EXCITED"),
        ({"PLAYFUL_MOOD"},                    "PLAYFUL"),
        ({"HIGH_PRESENCE"},                   "ALERT"),
    ]

    def __init__(self, **_kwargs):
        self.current_mode = "CALM"
        self.mode_just_changed = False
        self._forced_mode = None  # manual override from dashboard

    def force_mode(self, mode):
        """Force a specific mode, bypassing trigger evaluation.
        Pass None to release the override."""
        if mode is not None and mode not in self.VALID_MODES:
            return
        self._forced_mode = mode

    @property
    def is_forced(self):
        return self._forced_mode is not None

    def update(self, active_triggers: set) -> tuple[str, str]:
        """Determine mode from active triggers — instant switch.

        Args:
            active_triggers: set of trigger names currently active

        Returns:
            (old_mode, new_mode)
        """
        # Manual override takes precedence
        if self._forced_mode is not None:
            new_mode = self._forced_mode
        else:
            # Determine target mode (first matching rule wins)
            new_mode = "CALM"
            for required_triggers, mode in self.TRIGGER_RULES:
                if required_triggers <= active_triggers:
                    new_mode = mode
                    break

        old_mode = self.current_mode
        if new_mode != old_mode:
            self.mode_just_changed = True
        self.current_mode = new_mode

        return old_mode, new_mode
