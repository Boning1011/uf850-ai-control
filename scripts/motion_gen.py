"""
Parameterized Motion Generator — continuous PerceptionState drives sinusoidal XYZ motion.

Replaces discrete pattern_idle/curious/alert/dance with a single parameterized function
whose amplitude, frequency, center, and harmonic content are modulated by 6 continuous floats.
"""

import math


def _lerp(a, b, t):
    """Linear interpolation: a when t=0, b when t=1."""
    return a + (b - a) * t


class ParametricMotionGenerator:
    """Generate XYZ targets from continuous behavioral parameters + time.

    The motion is a sum of sinusoids whose amplitude and frequency are
    continuously modulated by PerceptionState. This means:
    - energy=0: tiny, slow breathing oscillation (like idle)
    - energy=1: large, fast sweeping arcs (like dance)
    - attention shifts the orbit center
    - mood modulates harmonic content (tense = jerky, playful = smooth)
    - urgency adds temporary startle jitter
    """

    def __init__(self, persona_config):
        self.cfg = persona_config
        self._debug = {}  # cached debug state from last get_target call

    def get_target(self, t, state):
        """Compute (x, y, z) for time t given current PerceptionState.

        Args:
            t: elapsed time in seconds (monotonic, drives oscillation phase)
            state: PerceptionState with continuous parameters

        Returns:
            (x, y, z) tuple in arm mm coordinates
        """
        cfg = self.cfg
        e = state.energy

        # 1. Center position + attention offset
        cx = cfg.center[0] + state.attention_x * cfg.attention_range_x[1]
        cy = cfg.center[1] + state.attention_y * cfg.attention_range_y[1]
        cz = cfg.center[2]

        # 2. Amplitude: lerp between low (dormant) and high (excited)
        amp_x = _lerp(cfg.amplitude_low[0], cfg.amplitude_high[0], e)
        amp_y = _lerp(cfg.amplitude_low[1], cfg.amplitude_high[1], e)
        amp_z = _lerp(cfg.amplitude_low[2], cfg.amplitude_high[2], e)

        # 3. Frequency: lerp between slow and fast
        freq_x = _lerp(cfg.frequency_low[0], cfg.frequency_high[0], e)
        freq_y = _lerp(cfg.frequency_low[1], cfg.frequency_high[1], e)
        freq_z = _lerp(cfg.frequency_low[2], cfg.frequency_high[2], e)

        # 4. Primary oscillation (base Lissajous-like pattern)
        #    Use slightly different phase relationships to avoid straight-line motion
        phase_x = 2 * math.pi * freq_x * t
        phase_y = 2 * math.pi * freq_y * t
        phase_z = 2 * math.pi * freq_z * t

        x = cx + amp_x * math.sin(phase_x)
        y = cy + amp_y * math.sin(phase_y * 1.1)  # slight detuning for organic feel
        z = cz + amp_z * math.sin(phase_z * 0.7)

        # 5. Mood modulates harmonic content
        #    mood=0 (tense): sharp higher harmonics → jerky motion
        #    mood=1 (playful): gentle secondary → flowing motion
        tense = 1.0 - state.mood  # 0 = relaxed, 1 = tense
        harmonic = tense * 0.3
        x += harmonic * amp_x * 0.4 * math.sin(3.7 * phase_x)
        y += harmonic * amp_y * 0.3 * math.sin(2.3 * phase_y)
        z += harmonic * amp_z * 0.3 * math.sin(4.1 * phase_z)

        # Playful: add gentle secondary oscillation
        playful = state.mood * 0.2
        x += playful * amp_x * 0.3 * math.sin(0.37 * phase_x + 1.2)
        y += playful * amp_y * 0.4 * math.sin(0.53 * phase_y + 0.8)

        # 6. Urgency: temporary startle jitter
        jitter_active = state.urgency > 0.3
        jitter_amp = state.urgency * 15 if jitter_active else 0
        if jitter_active:
            x += jitter_amp * math.sin(2 * math.pi * 8.0 * t)
            z += jitter_amp * math.cos(2 * math.pi * 7.3 * t)

        # Cache debug info (cheap dict assignment, no extra math)
        self._debug = {
            "amp": [round(amp_x, 1), round(amp_y, 1), round(amp_z, 1)],
            "freq": [round(freq_x, 3), round(freq_y, 3), round(freq_z, 3)],
            "phase": [
                round((phase_x % (2 * math.pi)) / (2 * math.pi), 2),
                round((phase_y % (2 * math.pi)) / (2 * math.pi), 2),
                round((phase_z % (2 * math.pi)) / (2 * math.pi), 2),
            ],
            "mood_harmonic": round(harmonic, 2),
            "playful_mod": round(playful, 2),
            "jitter_active": jitter_active,
            "jitter_amp": round(jitter_amp, 1),
        }

        return x, y, z

    def get_speed(self, state):
        """Compute arm speed (mm/s) from energy level."""
        cfg = self.cfg
        mult = _lerp(cfg.speed_energy_mult[0], cfg.speed_energy_mult[1], state.energy)
        self._debug["speed_mult"] = round(mult, 2)
        return cfg.speed_base * mult

    def get_debug_state(self):
        """Return last-computed motion debug state."""
        return self._debug.copy() if self._debug else {}
