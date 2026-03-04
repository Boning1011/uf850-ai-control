"""
Parameterized Motion Generator v2 — per-mode distinct motion patterns.

Each mode has its own motion generator producing qualitatively different movement:
  CALM:     gentle breathing at center (small sinusoidal oscillation)
  ALERT:    reach far forward (arm stretches to max X, sweeps Y)
  EXCITED:  big dramatic sweeping arcs (full workspace lissajous)
  PLAYFUL:  extend forward + rapid J5 pitch oscillation (head nod)
  TRACK:    real-time hand tracking via MediaPipe (follow hand position)

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
    """Generate (x, y, z, pitch, yaw) targets from mode + continuous state + time.

    Each mode is a distinct motion pattern, not just scaled multipliers.
    Returns pitch/yaw for orientation control (default 0 = tool pointing down with roll=180).
    """

    # Speed (mm/s) per mode: (normal, transition_boost)
    MODE_SPEEDS = {
        "CALM":     (120, 600),
        "ALERT":    (200, 700),
        "EXCITED":  (250, 500),
        "PLAYFUL":  (250, 700),
        "TRACK":    (200, 700),
    }

    TRANSITION_BOOST_DURATION = 0.8  # seconds of high speed after mode switch

    # EMA smoothing factor for hand tracking input (0 = no smoothing, 1 = frozen).
    # Higher values = smoother but laggier. 0.85 feels responsive yet stable.
    HAND_SMOOTH = 0.85
    HAND_RPY_SMOOTH = 0.88  # RPY needs heavier smoothing (amplified noise)

    # Grace period (seconds) to hold last known position when hand detection
    # is briefly lost. Prevents jumps from single-frame MediaPipe drops.
    HAND_LOST_GRACE = 0.5

    def __init__(self, persona_config):
        self.cfg = persona_config
        self._mode = "CALM"
        self._mode_switch_time = 0.0
        self._debug = {}
        self._hand_target = None  # (x, y, z) in arm mm, set by hand tracking
        self._hand_rpy_target = None  # (pitch, yaw) from second hand, or None
        self._hand_smooth = None  # EMA-smoothed (x, y, z)
        self._hand_rpy_smooth = None  # EMA-smoothed (pitch, yaw)
        self._hand_lost_time = None  # monotonic timestamp when hand was last lost

    def set_mode(self, mode_name):
        """Switch to a new mode. Resets transition timer for speed boost."""
        if mode_name != self._mode:
            self._mode = mode_name
            self._mode_switch_time = time.monotonic()

    @property
    def current_mode(self):
        return self._mode

    def get_target(self, t, state):
        """Compute (x, y, z, pitch, yaw) for current mode.

        Args:
            t: elapsed time in seconds (monotonic phase driver)
            state: PerceptionState with continuous parameters

        Returns:
            (x, y, z, pitch, yaw) — x/y/z in arm mm, pitch/yaw in degrees
        """
        method = getattr(self, f'_motion_{self._mode.lower()}', self._motion_calm)
        result = method(t, state)
        if len(result) == 4:
            x, y, z, pitch = result
            yaw = 0
        else:
            x, y, z, pitch, yaw = result

        # Safety clamp position (pitch/yaw not clamped here — arm_controller handles RPY)
        cfg = self.cfg
        x = _clamp(x, cfg.bounds_x[0], cfg.bounds_x[1])
        y = _clamp(y, cfg.bounds_y[0], cfg.bounds_y[1])
        z = _clamp(z, cfg.bounds_z[0], cfg.bounds_z[1])

        self._debug["mode"] = self._mode
        self._debug["target_xyz"] = [round(x, 1), round(y, 1), round(z, 1)]
        self._debug["pitch"] = round(pitch, 1)
        self._debug["yaw"] = round(yaw, 1)

        return x, y, z, pitch, yaw

    def get_debug_state(self):
        """Return last-computed motion debug state."""
        return self._debug.copy() if self._debug else {}

    def set_hand_target(self, hand_x, hand_y, hand_z):
        """Set hand tracking target from normalised coords (0-1).

        Maps camera pixel space (mirrored video) to arm Cartesian workspace:
          hand_x (horizontal, mirrored) -> arm Y (direct: user right = viewer right = +Y)
          hand_y (vertical, 0=top) -> arm Z (inverted: top = high Z)
          arm X: fixed at comfortable forward reach

        Raw position is EMA-smoothed to eliminate MediaPipe jitter.
        """
        if hand_x is None:
            # Grace period: hold last known position for brief detection drops.
            # Only clear after HAND_LOST_GRACE seconds of continuous loss.
            if self._hand_smooth is not None:
                if self._hand_lost_time is None:
                    self._hand_lost_time = time.monotonic()
                elapsed = time.monotonic() - self._hand_lost_time
                if elapsed < self.HAND_LOST_GRACE:
                    return  # keep _hand_target at last smooth position
            # Grace expired or no prior smooth state — clear everything
            self._hand_target = None
            self._hand_smooth = None
            self._hand_lost_time = None
            return
        # Hand re-detected — reset lost timer
        self._hand_lost_time = None

        cfg = self.cfg
        # Amplify horizontal movement: center 50% of frame covers full Y range
        hand_x_scaled = _clamp(0.5 + (hand_x - 0.5) * 2.0, 0.0, 1.0)
        # Video is mirrored, so hand_x=1 (user's right) -> +Y (viewer's right)
        arm_y = _lerp(cfg.bounds_y[0], cfg.bounds_y[1], hand_x_scaled)
        arm_z = _lerp(cfg.bounds_z[1], cfg.bounds_z[0], hand_y)
        arm_x = _lerp(cfg.bounds_x[0], cfg.bounds_x[1], 0.7)
        raw = (arm_x, arm_y, arm_z)

        # EMA low-pass filter: smoothed = α * old + (1-α) * new
        if self._hand_smooth is None:
            self._hand_smooth = raw
        else:
            a = self.HAND_SMOOTH
            self._hand_smooth = (
                a * self._hand_smooth[0] + (1 - a) * raw[0],
                a * self._hand_smooth[1] + (1 - a) * raw[1],
                a * self._hand_smooth[2] + (1 - a) * raw[2],
            )
        self._hand_target = self._hand_smooth

    def set_hand_rpy_input(self, hand_x, hand_y):
        """Set RPY control from second hand (normalised 0-1 coords).

        Only vertical position (hand_y) is used — left hand controls pitch only.
        This avoids left/right hand crossing in front of camera.

          hand_y (vertical, 0=top) -> pitch: center(0.5) = 0°, range ±50°

        Pass None to clear (reverts to single-hand boundary lean).
        Raw value is EMA-smoothed to eliminate jitter.
        """
        if hand_x is None:
            self._hand_rpy_target = None
            self._hand_rpy_smooth = None
            return
        pitch = -(hand_y - 0.5) * 2.0 * 50.0      # ±50°, inverted (top=positive)
        raw = (pitch, 0)

        if self._hand_rpy_smooth is None:
            self._hand_rpy_smooth = raw
        else:
            a = self.HAND_RPY_SMOOTH
            self._hand_rpy_smooth = (
                a * self._hand_rpy_smooth[0] + (1 - a) * raw[0],
                a * self._hand_rpy_smooth[1] + (1 - a) * raw[1],
            )
        self._hand_rpy_target = self._hand_rpy_smooth

    # ------------------------------------------------------------------
    # Mode-specific motion generators
    # Each returns (x, y, z, pitch) — TRACK mode returns (x, y, z, pitch, yaw)
    # ------------------------------------------------------------------

    def _motion_calm(self, t, state):
        """Gentle breathing oscillation around center. Small, slow, peaceful."""
        cfg = self.cfg
        cx, cy, cz = cfg.center

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
        target_x = cfg.bounds_x[1]  # max X — full reach
        target_z = cfg.center[2]  # center height

        # Slow sweep while extended: ±60mm Y, ±20mm Z
        sweep_freq = 0.2
        y_sweep = 60 * math.sin(2 * math.pi * sweep_freq * t)
        z_breathe = 20 * math.sin(2 * math.pi * sweep_freq * t * 0.6 + 1.0)

        x = target_x
        y = y_sweep
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

        # Lissajous with irrational frequency ratios for non-repeating paths
        freq_base = 0.2  # ~5s per cycle (slowed for real hardware safety)
        x = cx + range_x * math.sin(2 * math.pi * freq_base * t)
        y = cy + range_y * math.sin(2 * math.pi * freq_base * t * 1.618)  # golden ratio
        z = cz + range_z * math.sin(2 * math.pi * freq_base * t * 0.713 + 0.3)

        # Add energy modulation: higher energy = faster
        energy_freq = _lerp(0.8, 1.3, state.energy)
        x += 12 * math.sin(2 * math.pi * freq_base * energy_freq * t * 2.1)
        z += 10 * math.sin(2 * math.pi * freq_base * energy_freq * t * 1.7)

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

    def _motion_track(self, t, state):
        """Direct hand tracking — follow hand position in real time.

        Single-hand mode: position tracking + boundary RPY lean (original).
        Two-hand mode:  right hand = position, left hand = RPY orientation.
        Falls back to calm breathing if no hand detected.
        """
        if self._hand_target is None:
            return self._motion_calm(t, state)

        x, y, z = self._hand_target
        cfg = self.cfg

        if self._hand_rpy_target is not None:
            # ── Two-hand mode: second hand directly controls pitch & yaw ──
            pitch, yaw = self._hand_rpy_target
            self._debug["pattern"] = "hand_track_2h"
            self._debug["rpy_input"] = [round(pitch, 1), round(yaw, 1)]
        else:
            # ── Single-hand mode: RPY lean at boundaries (original) ──
            pitch = 0
            yaw = 0
            margin = 40  # mm from boundary where lean starts

            z_headroom_hi = cfg.bounds_z[1] - z
            z_headroom_lo = z - cfg.bounds_z[0]
            if z_headroom_hi < margin:
                lean = (1.0 - z_headroom_hi / margin)
                pitch += lean * 20
            elif z_headroom_lo < margin:
                lean = (1.0 - z_headroom_lo / margin)
                pitch -= lean * 15

            y_headroom_pos = cfg.bounds_y[1] - y
            y_headroom_neg = y - cfg.bounds_y[0]
            if y_headroom_pos < margin:
                lean = (1.0 - y_headroom_pos / margin)
                pitch += lean * 10
            elif y_headroom_neg < margin:
                lean = (1.0 - y_headroom_neg / margin)
                pitch -= lean * 10

            self._debug["pattern"] = "hand_track"
            self._debug["lean_pitch"] = round(pitch, 1)

        self._debug["hand_target"] = [round(self._hand_target[0], 1),
                                       round(self._hand_target[1], 1),
                                       round(self._hand_target[2], 1)]

        return x, y, z, pitch, yaw

    def get_speed(self, state):
        """Compute arm speed (mm/s). Uses transition boost after mode switch.

        In TRACK mode, applies soft margin deceleration near workspace boundaries.
        """
        normal_speed, boost_speed = self.MODE_SPEEDS.get(
            self._mode, (150, 600)
        )

        # Transition boost: high speed right after mode switch
        elapsed = time.monotonic() - self._mode_switch_time
        if elapsed < self.TRANSITION_BOOST_DURATION:
            blend = elapsed / self.TRANSITION_BOOST_DURATION
            speed = _lerp(boost_speed, normal_speed, blend)
        else:
            speed = normal_speed

        # Energy also modulates speed (subtle, +/-30%)
        energy_mult = _lerp(0.7, 1.3, state.energy)
        speed *= energy_mult

        # ── Soft margin deceleration (TRACK mode only) ──
        if self._mode == "TRACK" and self._hand_target is not None:
            cfg = self.cfg
            x, y, z = self._hand_target
            margin_width = 50  # mm from boundary where deceleration starts
            min_speed_frac = 0.3  # slow to 30% at boundary

            def _margin_factor(val, lo, hi):
                d = min(val - lo, hi - val)
                if d >= margin_width:
                    return 1.0
                if d <= 0:
                    return min_speed_frac
                t = d / margin_width
                # smoothstep: 3t^2 - 2t^3
                smooth = t * t * (3 - 2 * t)
                return min_speed_frac + (1.0 - min_speed_frac) * smooth

            f = min(
                _margin_factor(x, cfg.bounds_x[0], cfg.bounds_x[1]),
                _margin_factor(y, cfg.bounds_y[0], cfg.bounds_y[1]),
                _margin_factor(z, cfg.bounds_z[0], cfg.bounds_z[1]),
            )
            speed *= f
            self._debug["soft_margin_factor"] = round(f, 2)

        self._debug["speed"] = round(speed, 1)
        self._debug["transition_boost"] = elapsed < self.TRANSITION_BOOST_DURATION
        return speed
