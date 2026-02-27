"""
Parameterized Motion Generator v2 — per-mode distinct motion patterns.

Each mode has its own motion generator producing qualitatively different movement:
  CALM:     gentle breathing at center (small sinusoidal oscillation)
  ALERT:    reach far forward toward attention (arm stretches to max X)
  EXCITED:  big dramatic sweeping arcs (full workspace lissajous)
  PLAYFUL:  extend forward + rapid J5 pitch oscillation (head nod)
  TENSE:    reach high on Z axis (stretch up, small trembling)
  DORMANT:  contracted low, barely moving (sleeping)

Mode transitions are instant — no blending. Arm snaps to new pattern
with a speed boost during the first 0.8 seconds after switch.
"""

import math
import time


def _lerp(a, b, t):
    """Linear interpolation: a when t=0, b when t=1."""
    return a + (b - a) * t


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class ParametricMotionGenerator:
    """Generate (x, y, z, pitch) targets from mode + continuous state + time.

    Each mode is a distinct motion pattern, not just scaled multipliers.
    Returns pitch for J5 control (default 0 = tool pointing down with roll=180).
    """

    # Speed (mm/s) per mode: (normal, transition_boost)
    MODE_SPEEDS = {
        "CALM":     (120, 600),
        "ALERT":    (200, 700),
        "EXCITED":  (500, 800),
        "PLAYFUL":  (250, 700),
        "TENSE":    (180, 700),
        "DORMANT":  (50, 400),
    }

    TRANSITION_BOOST_DURATION = 0.8  # seconds of high speed after mode switch

    def __init__(self, persona_config):
        self.cfg = persona_config
        self._mode = "CALM"
        self._mode_switch_time = 0.0
        self._debug = {}

    def set_mode(self, mode_name):
        """Switch to a new mode. Resets transition timer for speed boost."""
        if mode_name != self._mode:
            self._mode = mode_name
            self._mode_switch_time = time.monotonic()

    @property
    def current_mode(self):
        return self._mode

    def get_target(self, t, state):
        """Compute (x, y, z, pitch) for current mode.

        Args:
            t: elapsed time in seconds (monotonic phase driver)
            state: PerceptionState with continuous parameters

        Returns:
            (x, y, z, pitch) — x/y/z in arm mm, pitch in degrees
        """
        method = getattr(self, f'_motion_{self._mode.lower()}', self._motion_calm)
        x, y, z, pitch = method(t, state)

        # Safety clamp position (pitch not clamped here — arm_controller handles RPY)
        cfg = self.cfg
        x = _clamp(x, cfg.bounds_x[0], cfg.bounds_x[1])
        y = _clamp(y, cfg.bounds_y[0], cfg.bounds_y[1])
        z = _clamp(z, cfg.bounds_z[0], cfg.bounds_z[1])

        self._debug["mode"] = self._mode
        self._debug["target_xyz"] = [round(x, 1), round(y, 1), round(z, 1)]
        self._debug["pitch"] = round(pitch, 1)

        return x, y, z, pitch

    def get_speed(self, state):
        """Compute arm speed (mm/s). Uses transition boost after mode switch."""
        normal_speed, boost_speed = self.MODE_SPEEDS.get(
            self._mode, (150, 600)
        )

        # Transition boost: high speed right after mode switch
        elapsed = time.monotonic() - self._mode_switch_time
        if elapsed < self.TRANSITION_BOOST_DURATION:
            # Fast linear ramp-down from boost to normal
            blend = elapsed / self.TRANSITION_BOOST_DURATION
            speed = _lerp(boost_speed, normal_speed, blend)
        else:
            speed = normal_speed

        # Energy also modulates speed (subtle, ±30%)
        energy_mult = _lerp(0.7, 1.3, state.energy)
        speed *= energy_mult

        self._debug["speed"] = round(speed, 1)
        self._debug["transition_boost"] = elapsed < self.TRANSITION_BOOST_DURATION
        return speed

    def get_debug_state(self):
        """Return last-computed motion debug state."""
        return self._debug.copy() if self._debug else {}

    # ------------------------------------------------------------------
    # Mode-specific motion generators
    # Each returns (x, y, z, pitch)
    # ------------------------------------------------------------------

    def _motion_calm(self, t, state):
        """Gentle breathing oscillation around center. Small, slow, peaceful."""
        cfg = self.cfg
        cx, cy, cz = cfg.center

        # Attention shifts center slightly
        cx += state.attention_x * cfg.attention_range_x[1] * 0.5
        cy += state.attention_y * cfg.attention_range_y[1] * 0.5

        # Small breathing: ±25mm X, ±15mm Y, ±30mm Z
        amp_x, amp_y, amp_z = 25, 15, 30
        freq = 0.15  # slow breathing ~6.7s cycle

        x = cx + amp_x * math.sin(2 * math.pi * freq * t)
        y = cy + amp_y * math.sin(2 * math.pi * freq * t * 1.3 + 0.5)
        z = cz + amp_z * math.sin(2 * math.pi * freq * t * 0.7)

        self._debug["pattern"] = "breathing"
        self._debug["amp"] = [amp_x, amp_y, amp_z]
        self._debug["freq"] = round(freq, 3)

        return x, y, z, 0

    def _motion_alert(self, t, state):
        """Reach far forward — arm stretches to max X, slowly sweeping Y.

        Like a creature stretching toward something interesting.
        """
        cfg = self.cfg

        # Target: far forward, center height
        # Reach toward attention direction
        target_x = cfg.bounds_x[1]  # max X — full reach
        target_y = state.attention_y * cfg.bounds_y[1] * 0.8  # follow attention in Y
        target_z = cfg.center[2]  # center height

        # Slow sweep while extended: ±40mm Y, ±20mm Z
        sweep_freq = 0.2
        y_sweep = 40 * math.sin(2 * math.pi * sweep_freq * t)
        z_breathe = 20 * math.sin(2 * math.pi * sweep_freq * t * 0.6 + 1.0)

        x = target_x
        y = target_y + y_sweep
        z = target_z + z_breathe

        self._debug["pattern"] = "reach_forward"
        self._debug["reach_x"] = round(target_x, 1)

        return x, y, z, 0

    def _motion_excited(self, t, state):
        """Big dramatic sweeping arcs — workspace lissajous.

        Fast, large, covering most of the reachable space. Amplitude is
        reduced from full bounds to avoid joint-angle-limit errors at
        extreme corner combinations (e.g. close-to-base + far-sideways).
        """
        cfg = self.cfg
        cx, cy, cz = cfg.center

        # Reduced from 0.45/0.45/0.35 to avoid unreachable IK corners
        range_x = (cfg.bounds_x[1] - cfg.bounds_x[0]) * 0.35
        range_y = (cfg.bounds_y[1] - cfg.bounds_y[0]) * 0.30
        range_z = (cfg.bounds_z[1] - cfg.bounds_z[0]) * 0.28

        # Fast lissajous with irrational frequency ratios for non-repeating paths
        freq_base = 0.4  # ~2.5s per cycle
        x = cx + range_x * math.sin(2 * math.pi * freq_base * t)
        y = cy + range_y * math.sin(2 * math.pi * freq_base * t * 1.618)  # golden ratio
        z = cz + range_z * math.sin(2 * math.pi * freq_base * t * 0.713 + 0.3)

        # Add energy modulation: higher energy = faster
        energy_freq = _lerp(0.8, 1.5, state.energy)
        x += 20 * math.sin(2 * math.pi * freq_base * energy_freq * t * 2.1)
        z += 18 * math.sin(2 * math.pi * freq_base * energy_freq * t * 1.7)

        self._debug["pattern"] = "big_sweep"
        self._debug["range"] = [round(range_x, 1), round(range_y, 1), round(range_z, 1)]
        self._debug["freq_base"] = round(freq_base, 3)

        return x, y, z, 0

    def _motion_playful(self, t, state):
        """Extend forward + rapid J5 pitch oscillation (head nod).

        Arm reaches out, then the 'head' (J5) nods rapidly — like a curious
        creature bobbing its head.
        """
        cfg = self.cfg

        # Position: extended forward, small oscillation
        target_x = cfg.bounds_x[1] * 0.85  # ~85% of max reach
        target_y = 0
        target_z = cfg.center[2] + 50  # slightly above center

        # Small position wobble to keep it organic
        wobble_freq = 0.25
        x = target_x + 15 * math.sin(2 * math.pi * wobble_freq * t)
        y = target_y + 20 * math.sin(2 * math.pi * wobble_freq * t * 1.4 + 0.7)
        z = target_z + 10 * math.sin(2 * math.pi * wobble_freq * t * 0.8)

        # J5 pitch: rapid oscillation ±35 degrees
        pitch_freq = 1.5  # fast nod ~1.5 Hz
        pitch = 35 * math.sin(2 * math.pi * pitch_freq * t)

        # Add a slower secondary pitch to vary the pattern
        pitch += 10 * math.sin(2 * math.pi * 0.3 * t + 1.5)

        self._debug["pattern"] = "head_nod"
        self._debug["pitch_freq"] = round(pitch_freq, 2)
        self._debug["pitch_amp"] = 35

        return x, y, z, pitch

    def _motion_tense(self, t, state):
        """Reach high on Z axis — arm stretches up, small rapid trembling.

        Like a creature standing tall, alert and shaking slightly.
        """
        cfg = self.cfg

        # Target: close to base (for stability), very high Z
        target_x = cfg.center[0] * 0.7  # closer to base
        target_y = 0
        target_z = cfg.bounds_z[1] * 0.85  # ~85% of max height

        # Small rapid trembling at the top
        tremble_freq = 3.0  # fast shaking
        tremble_amp = 8  # small amplitude

        x = target_x + tremble_amp * math.sin(2 * math.pi * tremble_freq * t)
        y = target_y + tremble_amp * math.sin(2 * math.pi * tremble_freq * t * 1.3 + 0.4)
        z = target_z + tremble_amp * 0.5 * math.sin(2 * math.pi * tremble_freq * t * 0.9)

        self._debug["pattern"] = "reach_high"
        self._debug["target_z"] = round(target_z, 1)

        return x, y, z, 0

    def _motion_dormant(self, t, state):
        """Contracted, low, barely moving — sleeping posture.

        Arm curls in close to base, very low, occasional tiny movement.
        """
        cfg = self.cfg

        # Low, close, contracted
        target_x = cfg.bounds_x[0] + 50  # close to min X
        target_y = 0
        target_z = cfg.bounds_z[0] + 30  # near floor

        # Barely perceptible breathing
        breathe_freq = 0.08  # very slow ~12.5s cycle
        x = target_x + 5 * math.sin(2 * math.pi * breathe_freq * t)
        y = target_y + 3 * math.sin(2 * math.pi * breathe_freq * t * 1.1)
        z = target_z + 8 * math.sin(2 * math.pi * breathe_freq * t * 0.7)

        self._debug["pattern"] = "sleeping"

        return x, y, z, 0
