"""
Servo Mode (Mode 1) Stress & Stability Test

Exercises the servo pipeline under various challenging conditions:
  Phase 1: Sustained 25Hz streaming — baseline stability (30s)
  Phase 2: Rapid mode switching — all 6 modes cycled every 2s (30s)
  Phase 3: Extreme positions — hit all 8 corners of the safety bounds (24s)
  Phase 4: Speed extremes — DORMANT (50mm/s) then EXCITED (800mm/s boost) (20s)
  Phase 5: Pitch oscillation — PLAYFUL rapid J5 nod (15s)
  Phase 6: Random state bombardment — random params every 0.1s (20s)
  Phase 7: Boundary soak — hold at bounds for position-clamp drift check (15s)
  Phase 8: Recovery test — force error and verify re-enable (10s)

Collects statistics: frame count, error count, Hz, position drift, timing jitter.

Usage:
    python test_servo_stress.py --ip 127.0.0.1
    python test_servo_stress.py --ip 127.0.0.1 --quick   # 50% shorter phases
"""

import argparse
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from arm_controller import ArmController, acquire_lock
from persona import PersonaConfig, PerceptionState
from motion_gen import ParametricMotionGenerator

DT = 0.04  # 25 Hz target


class StressStats:
    """Collects per-phase statistics."""

    def __init__(self, phase_name):
        self.name = phase_name
        self.frames = 0
        self.errors = 0           # SDK return != 0
        self.arm_errors = 0       # arm error_code != 0
        self.recoveries = 0
        self.start_time = None
        self.end_time = None
        self.loop_times = []      # per-frame wall time
        self.ret_codes = {}       # histogram of return codes

    def start(self):
        self.start_time = time.monotonic()

    def stop(self):
        self.end_time = time.monotonic()

    def record_frame(self, ret, loop_dt):
        self.frames += 1
        self.loop_times.append(loop_dt)
        self.ret_codes[ret] = self.ret_codes.get(ret, 0) + 1
        if ret != 0:
            self.errors += 1

    @property
    def duration(self):
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return 0

    @property
    def avg_hz(self):
        if self.duration > 0 and self.frames > 0:
            return self.frames / self.duration
        return 0

    @property
    def timing_jitter_ms(self):
        """Standard deviation of loop times in ms."""
        if len(self.loop_times) < 2:
            return 0
        mean = sum(self.loop_times) / len(self.loop_times)
        variance = sum((t - mean) ** 2 for t in self.loop_times) / len(self.loop_times)
        return math.sqrt(variance) * 1000

    @property
    def max_loop_ms(self):
        return max(self.loop_times) * 1000 if self.loop_times else 0

    def summary(self):
        lines = [f"  Phase: {self.name}"]
        lines.append(f"    Frames:    {self.frames}")
        lines.append(f"    Duration:  {self.duration:.1f}s")
        lines.append(f"    Avg Hz:    {self.avg_hz:.1f}")
        lines.append(f"    Errors:    {self.errors} SDK, {self.arm_errors} arm")
        lines.append(f"    Recoveries:{self.recoveries}")
        lines.append(f"    Jitter:    {self.timing_jitter_ms:.1f}ms (max loop: {self.max_loop_ms:.1f}ms)")
        if self.ret_codes:
            codes = ", ".join(f"{k}:{v}" for k, v in sorted(self.ret_codes.items()))
            lines.append(f"    Ret codes: {codes}")
        status = "PASS" if (self.errors == 0 and self.arm_errors == 0) else "FAIL"
        lines.append(f"    Result:    {status}")
        return "\n".join(lines)


def wait_for_ready(ctrl, timeout=5.0):
    """Wait until arm is not in error state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not ctrl.arm.has_error and ctrl.arm.state < 4:
            return True
        time.sleep(0.1)
    return False


def run_servo_loop(ctrl, motion_gen, state, duration, stats, log_interval=2.0):
    """Run the servo control loop for `duration` seconds, collecting stats."""
    stats.start()
    t = 0.0
    last_log = 0.0
    initial_error_count = ctrl.error_count
    deadline = time.monotonic() + duration

    while time.monotonic() < deadline and ctrl.running:
        loop_start = time.monotonic()

        # Skip during error recovery
        if ctrl.arm.has_error or ctrl.arm.state >= 4:
            stats.arm_errors += 1
            time.sleep(0.1)
            continue

        x, y, z, pitch = motion_gen.get_target(t, state)
        speed = motion_gen.get_speed(state)

        ret = ctrl.send_servo(x, y, z, speed=speed, pitch=pitch, dt=DT)

        loop_dt = time.monotonic() - loop_start
        stats.record_frame(ret, loop_dt)

        # Periodic log
        if t - last_log >= log_interval:
            pitch_s = f" P={pitch:.0f}" if abs(pitch) > 0.1 else ""
            print(f"    [{motion_gen.current_mode}] "
                  f"X={x:.0f} Y={y:.0f} Z={z:.0f}{pitch_s} "
                  f"spd={speed:.0f} ret={ret} "
                  f"hz={stats.avg_hz:.1f}", flush=True)
            last_log = t

        t += DT
        # Compensate sleep for loop computation time
        sleep_time = DT - loop_dt
        if sleep_time > 0:
            time.sleep(sleep_time)

    stats.recoveries = ctrl.error_count - initial_error_count
    stats.stop()


# ============================================================
# Test phases
# ============================================================

def phase_1_sustained(ctrl, motion_gen, duration):
    """Phase 1: Sustained 25Hz streaming in CALM mode — baseline stability."""
    print(f"\n{'='*60}")
    print(f"Phase 1: Sustained streaming ({duration}s)")
    print(f"{'='*60}", flush=True)

    stats = StressStats("Sustained 25Hz (CALM)")
    motion_gen.set_mode("CALM")
    state = PerceptionState(energy=0.3, mood=0.5, timestamp=time.time())
    run_servo_loop(ctrl, motion_gen, state, duration, stats)
    return stats


def phase_2_rapid_mode_switch(ctrl, motion_gen, duration):
    """Phase 2: Rapid mode switching — cycle all modes every 2s."""
    print(f"\n{'='*60}")
    print(f"Phase 2: Rapid mode switching ({duration}s)")
    print(f"{'='*60}", flush=True)

    modes = ["CALM", "ALERT", "EXCITED", "PLAYFUL", "TENSE", "DORMANT"]
    stats = StressStats("Rapid mode switch")
    stats.start()

    state = PerceptionState(energy=0.6, attention_x=0.3, mood=0.5,
                            presence=0.7, timestamp=time.time())
    t = 0.0
    initial_errors = ctrl.error_count
    mode_idx = 0
    switch_interval = 2.0
    next_switch = switch_interval
    mode_switches = 0

    motion_gen.set_mode(modes[0])
    deadline = time.monotonic() + duration

    while time.monotonic() < deadline and ctrl.running:
        loop_start = time.monotonic()

        if ctrl.arm.has_error or ctrl.arm.state >= 4:
            stats.arm_errors += 1
            time.sleep(0.1)
            continue

        # Switch mode every 2s
        if t >= next_switch:
            mode_idx = (mode_idx + 1) % len(modes)
            old = motion_gen.current_mode
            motion_gen.set_mode(modes[mode_idx])
            mode_switches += 1
            print(f"    SWITCH #{mode_switches}: {old} -> {modes[mode_idx]}", flush=True)
            next_switch += switch_interval

        x, y, z, pitch = motion_gen.get_target(t, state)
        speed = motion_gen.get_speed(state)
        ret = ctrl.send_servo(x, y, z, speed=speed, pitch=pitch, dt=DT)

        loop_dt = time.monotonic() - loop_start
        stats.record_frame(ret, loop_dt)

        t += DT
        sleep_time = DT - loop_dt
        if sleep_time > 0:
            time.sleep(sleep_time)

    stats.recoveries = ctrl.error_count - initial_errors
    stats.stop()
    print(f"    Total mode switches: {mode_switches}", flush=True)
    return stats


def phase_3_extreme_positions(ctrl, motion_gen, duration_per_corner):
    """Phase 3: Drive to all 8 corners of the safety bounds."""
    print(f"\n{'='*60}")
    print(f"Phase 3: Extreme positions — 8 corners ({duration_per_corner}s each)")
    print(f"{'='*60}", flush=True)

    bx = ctrl.bounds_x
    by = ctrl.bounds_y
    bz = ctrl.bounds_z

    corners = [
        (bx[0], by[0], bz[0], "near-left-low"),
        (bx[1], by[0], bz[0], "far-left-low"),
        (bx[0], by[1], bz[0], "near-right-low"),
        (bx[1], by[1], bz[0], "far-right-low"),
        (bx[0], by[0], bz[1], "near-left-high"),
        (bx[1], by[0], bz[1], "far-left-high"),
        (bx[0], by[1], bz[1], "near-right-high"),
        (bx[1], by[1], bz[1], "far-right-high"),
    ]

    stats = StressStats("Extreme positions (8 corners)")
    stats.start()
    initial_errors = ctrl.error_count

    t = 0.0
    for cx, cy, cz, label in corners:
        print(f"    Corner: {label} -> X={cx} Y={cy} Z={cz}", flush=True)
        corner_deadline = time.monotonic() + duration_per_corner

        while time.monotonic() < corner_deadline and ctrl.running:
            loop_start = time.monotonic()

            if ctrl.arm.has_error or ctrl.arm.state >= 4:
                stats.arm_errors += 1
                time.sleep(0.1)
                continue

            speed = 300
            ret = ctrl.send_servo(cx, cy, cz, speed=speed, dt=DT)

            loop_dt = time.monotonic() - loop_start
            stats.record_frame(ret, loop_dt)

            t += DT
            sleep_time = DT - loop_dt
            if sleep_time > 0:
                time.sleep(sleep_time)

        # Check actual position vs target after each corner
        if wait_for_ready(ctrl, timeout=1.0):
            actual = ctrl.get_xyz()
            dx = abs(actual[0] - cx)
            dy = abs(actual[1] - cy)
            dz = abs(actual[2] - cz)
            dist = math.sqrt(dx*dx + dy*dy + dz*dz)
            status = "OK" if dist < 50 else f"DRIFT={dist:.0f}mm"
            print(f"      Actual: X={actual[0]:.0f} Y={actual[1]:.0f} Z={actual[2]:.0f} "
                  f"({status})", flush=True)

    stats.recoveries = ctrl.error_count - initial_errors
    stats.stop()
    return stats


def phase_4_speed_extremes(ctrl, motion_gen, duration):
    """Phase 4: Alternate between DORMANT (slowest) and EXCITED (fastest)."""
    print(f"\n{'='*60}")
    print(f"Phase 4: Speed extremes ({duration}s)")
    print(f"{'='*60}", flush=True)

    stats = StressStats("Speed extremes")
    stats.start()
    initial_errors = ctrl.error_count

    t = 0.0
    deadline = time.monotonic() + duration
    toggle_interval = 5.0
    next_toggle = toggle_interval

    # Start dormant
    motion_gen.set_mode("DORMANT")
    state = PerceptionState(energy=0.0, mood=0.5, timestamp=time.time())

    while time.monotonic() < deadline and ctrl.running:
        loop_start = time.monotonic()

        if ctrl.arm.has_error or ctrl.arm.state >= 4:
            stats.arm_errors += 1
            time.sleep(0.1)
            continue

        # Toggle between extremes
        if t >= next_toggle:
            if motion_gen.current_mode == "DORMANT":
                motion_gen.set_mode("EXCITED")
                state = PerceptionState(energy=1.0, mood=0.5, timestamp=time.time())
                print(f"    -> EXCITED (max speed)", flush=True)
            else:
                motion_gen.set_mode("DORMANT")
                state = PerceptionState(energy=0.0, mood=0.5, timestamp=time.time())
                print(f"    -> DORMANT (min speed)", flush=True)
            next_toggle += toggle_interval

        x, y, z, pitch = motion_gen.get_target(t, state)
        speed = motion_gen.get_speed(state)
        ret = ctrl.send_servo(x, y, z, speed=speed, pitch=pitch, dt=DT)

        loop_dt = time.monotonic() - loop_start
        stats.record_frame(ret, loop_dt)

        t += DT
        sleep_time = DT - loop_dt
        if sleep_time > 0:
            time.sleep(sleep_time)

    stats.recoveries = ctrl.error_count - initial_errors
    stats.stop()
    return stats


def phase_5_pitch_oscillation(ctrl, motion_gen, duration):
    """Phase 5: PLAYFUL mode — rapid J5 pitch oscillation."""
    print(f"\n{'='*60}")
    print(f"Phase 5: Pitch oscillation / PLAYFUL ({duration}s)")
    print(f"{'='*60}", flush=True)

    stats = StressStats("Pitch oscillation (PLAYFUL)")
    motion_gen.set_mode("PLAYFUL")
    state = PerceptionState(energy=0.7, mood=1.0, timestamp=time.time())
    run_servo_loop(ctrl, motion_gen, state, duration, stats)
    return stats


def phase_6_random_state(ctrl, motion_gen, duration):
    """Phase 6: Random state bombardment — random params every 0.1s."""
    print(f"\n{'='*60}")
    print(f"Phase 6: Random state bombardment ({duration}s)")
    print(f"{'='*60}", flush=True)

    modes = ["CALM", "ALERT", "EXCITED", "PLAYFUL", "TENSE", "DORMANT"]
    stats = StressStats("Random state bombardment")
    stats.start()
    initial_errors = ctrl.error_count

    t = 0.0
    deadline = time.monotonic() + duration
    state_change_interval = 0.1
    next_change = state_change_interval
    state = PerceptionState(timestamp=time.time())

    while time.monotonic() < deadline and ctrl.running:
        loop_start = time.monotonic()

        if ctrl.arm.has_error or ctrl.arm.state >= 4:
            stats.arm_errors += 1
            time.sleep(0.1)
            continue

        # Randomize state very frequently
        if t >= next_change:
            state = PerceptionState(
                energy=random.random(),
                attention_x=random.uniform(-1, 1),
                attention_y=random.uniform(-1, 1),
                mood=random.random(),
                presence=random.random(),
                urgency=random.random(),
                timestamp=time.time(),
            )
            # Also randomly switch modes
            if random.random() < 0.3:  # 30% chance per 0.1s
                new_mode = random.choice(modes)
                motion_gen.set_mode(new_mode)
            next_change += state_change_interval

        x, y, z, pitch = motion_gen.get_target(t, state)
        speed = motion_gen.get_speed(state)
        ret = ctrl.send_servo(x, y, z, speed=speed, pitch=pitch, dt=DT)

        loop_dt = time.monotonic() - loop_start
        stats.record_frame(ret, loop_dt)

        t += DT
        sleep_time = DT - loop_dt
        if sleep_time > 0:
            time.sleep(sleep_time)

    stats.recoveries = ctrl.error_count - initial_errors
    stats.stop()
    return stats


def phase_7_boundary_soak(ctrl, motion_gen, duration):
    """Phase 7: Hold at boundaries — check for position clamp drift."""
    print(f"\n{'='*60}")
    print(f"Phase 7: Boundary soak ({duration}s)")
    print(f"{'='*60}", flush=True)

    stats = StressStats("Boundary soak")
    stats.start()
    initial_errors = ctrl.error_count

    # Try to push PAST bounds (send_servo should clamp)
    targets = [
        (ctrl.bounds_x[1] + 100, 0, ctrl.center[2], "X overshoot"),
        (ctrl.center[0], ctrl.bounds_y[0] - 100, ctrl.center[2], "Y undershoot"),
        (ctrl.center[0], 0, ctrl.bounds_z[1] + 100, "Z overshoot"),
        (ctrl.bounds_x[0] - 100, ctrl.bounds_y[1] + 100, ctrl.bounds_z[0] - 100, "All undershoot"),
    ]

    t = 0.0
    per_target = duration / len(targets)

    for tx, ty, tz, label in targets:
        print(f"    Pushing: {label} -> raw ({tx:.0f},{ty:.0f},{tz:.0f})", flush=True)
        target_deadline = time.monotonic() + per_target

        while time.monotonic() < target_deadline and ctrl.running:
            loop_start = time.monotonic()

            if ctrl.arm.has_error or ctrl.arm.state >= 4:
                stats.arm_errors += 1
                time.sleep(0.1)
                continue

            ret = ctrl.send_servo(tx, ty, tz, speed=300, dt=DT)
            loop_dt = time.monotonic() - loop_start
            stats.record_frame(ret, loop_dt)

            t += DT
            sleep_time = DT - loop_dt
            if sleep_time > 0:
                time.sleep(sleep_time)

        # Verify arm stayed within bounds
        if wait_for_ready(ctrl, timeout=1.0):
            actual = ctrl.get_xyz()
            in_bounds = (
                ctrl.bounds_x[0] - 5 <= actual[0] <= ctrl.bounds_x[1] + 5
                and ctrl.bounds_y[0] - 5 <= actual[1] <= ctrl.bounds_y[1] + 5
                and ctrl.bounds_z[0] - 5 <= actual[2] <= ctrl.bounds_z[1] + 5
            )
            status = "IN BOUNDS" if in_bounds else "OUT OF BOUNDS!"
            print(f"      Actual: X={actual[0]:.0f} Y={actual[1]:.0f} Z={actual[2]:.0f} "
                  f"({status})", flush=True)
            if not in_bounds:
                stats.arm_errors += 1

    stats.recoveries = ctrl.error_count - initial_errors
    stats.stop()
    return stats


def phase_8_recovery(ctrl, motion_gen, duration):
    """Phase 8: Trigger error via out-of-range joint angles, verify recovery."""
    print(f"\n{'='*60}")
    print(f"Phase 8: Recovery test ({duration}s)")
    print(f"{'='*60}", flush=True)

    stats = StressStats("Recovery test")
    stats.start()
    initial_errors = ctrl.error_count

    # First, move to a normal position
    state = PerceptionState(energy=0.3, mood=0.5, timestamp=time.time())
    motion_gen.set_mode("CALM")

    print("    Streaming normally for 3s...", flush=True)
    t = 0.0
    normal_deadline = time.monotonic() + min(3.0, duration * 0.3)

    while time.monotonic() < normal_deadline and ctrl.running:
        loop_start = time.monotonic()
        if not ctrl.arm.has_error and ctrl.arm.state < 4:
            x, y, z, pitch = motion_gen.get_target(t, state)
            speed = motion_gen.get_speed(state)
            ret = ctrl.send_servo(x, y, z, speed=speed, dt=DT)
            loop_dt = time.monotonic() - loop_start
            stats.record_frame(ret, loop_dt)
        t += DT
        sleep_time = DT - (time.monotonic() - loop_start)
        if sleep_time > 0:
            time.sleep(sleep_time)

    # Try to provoke an error by sending extreme RPY directly
    # (bypassing velocity clamp by calling SDK directly)
    print("    Attempting to provoke error (extreme position)...", flush=True)
    try:
        # Send a position that's physically unreachable (very far + high)
        # The firmware should reject this and trigger error callback
        ctrl.arm.set_servo_cartesian(
            [700, 300, 1000, 180, 0, 0], is_radian=False
        )
        time.sleep(0.5)
    except Exception as e:
        print(f"    Provoke exception: {e}", flush=True)

    # Wait for recovery
    print("    Waiting for auto-recovery...", flush=True)
    recovery_deadline = time.monotonic() + 5.0
    recovered = False
    while time.monotonic() < recovery_deadline:
        if not ctrl.arm.has_error and ctrl.arm.state < 4:
            recovered = True
            break
        time.sleep(0.2)

    if recovered:
        print(f"    Recovery successful! (errors recovered: {ctrl.error_count})", flush=True)

        # Verify servo mode is restored and working
        print("    Verifying servo mode restored...", flush=True)
        verify_deadline = time.monotonic() + min(3.0, duration * 0.3)
        verify_ok = 0
        while time.monotonic() < verify_deadline and ctrl.running:
            loop_start = time.monotonic()
            if not ctrl.arm.has_error and ctrl.arm.state < 4:
                x, y, z, pitch = motion_gen.get_target(t, state)
                speed = motion_gen.get_speed(state)
                ret = ctrl.send_servo(x, y, z, speed=speed, dt=DT)
                if ret == 0:
                    verify_ok += 1
                loop_dt = time.monotonic() - loop_start
                stats.record_frame(ret, loop_dt)
            t += DT
            sleep_time = DT - (time.monotonic() - loop_start)
            if sleep_time > 0:
                time.sleep(sleep_time)

        print(f"    Post-recovery: {verify_ok} successful servo commands", flush=True)
    else:
        print("    Recovery FAILED — arm still in error state", flush=True)
        stats.arm_errors += 1

    stats.recoveries = ctrl.error_count - initial_errors
    stats.stop()
    return stats


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Servo Mode Stress Test")
    parser.add_argument("--ip", default="127.0.0.1")
    parser.add_argument("--persona", default="personas/default.yaml")
    parser.add_argument("--sensitivity", type=int, default=-1)
    parser.add_argument("--quick", action="store_true",
                        help="Run shorter phases (50%% duration)")
    args = parser.parse_args()

    acquire_lock()

    scale = 0.5 if args.quick else 1.0

    config = PersonaConfig(args.persona)
    motion_gen = ParametricMotionGenerator(config)

    ctrl = ArmController(
        ip=args.ip,
        speed=config.speed_base,
        collision_sensitivity=args.sensitivity,
        center=config.center,
        rpy=(180, 0, 0),
        bounds_x=config.bounds_x,
        bounds_y=config.bounds_y,
        bounds_z=config.bounds_z,
    )

    all_stats = []

    try:
        ctrl.connect()

        print("\nMoving to center...")
        ctrl.move_to_center()
        time.sleep(0.5)

        ctrl.enable_servo()
        time.sleep(0.3)

        print(f"\n{'#'*60}")
        print(f"# SERVO MODE STRESS TEST")
        print(f"# IP: {args.ip}")
        print(f"# Bounds: X={config.bounds_x} Y={config.bounds_y} Z={config.bounds_z}")
        print(f"# Center: {config.center}")
        print(f"# Scale: {'quick' if args.quick else 'full'}")
        print(f"{'#'*60}")

        # Phase 1: Sustained streaming
        s = phase_1_sustained(ctrl, motion_gen, 30 * scale)
        all_stats.append(s)
        print(s.summary(), flush=True)

        # Return to center between phases
        _recenter(ctrl)

        # Phase 2: Rapid mode switching
        s = phase_2_rapid_mode_switch(ctrl, motion_gen, 30 * scale)
        all_stats.append(s)
        print(s.summary(), flush=True)

        _recenter(ctrl)

        # Phase 3: Extreme positions
        s = phase_3_extreme_positions(ctrl, motion_gen, 3 * scale)
        all_stats.append(s)
        print(s.summary(), flush=True)

        _recenter(ctrl)

        # Phase 4: Speed extremes
        s = phase_4_speed_extremes(ctrl, motion_gen, 20 * scale)
        all_stats.append(s)
        print(s.summary(), flush=True)

        _recenter(ctrl)

        # Phase 5: Pitch oscillation
        s = phase_5_pitch_oscillation(ctrl, motion_gen, 15 * scale)
        all_stats.append(s)
        print(s.summary(), flush=True)

        _recenter(ctrl)

        # Phase 6: Random state
        s = phase_6_random_state(ctrl, motion_gen, 20 * scale)
        all_stats.append(s)
        print(s.summary(), flush=True)

        _recenter(ctrl)

        # Phase 7: Boundary soak
        s = phase_7_boundary_soak(ctrl, motion_gen, 16 * scale)
        all_stats.append(s)
        print(s.summary(), flush=True)

        _recenter(ctrl)

        # Phase 8: Recovery
        s = phase_8_recovery(ctrl, motion_gen, 10 * scale)
        all_stats.append(s)
        print(s.summary(), flush=True)

        # ---- Final report ----
        print(f"\n{'#'*60}")
        print(f"# FINAL REPORT")
        print(f"{'#'*60}")

        total_frames = sum(s.frames for s in all_stats)
        total_errors = sum(s.errors for s in all_stats)
        total_arm_errors = sum(s.arm_errors for s in all_stats)
        total_recoveries = sum(s.recoveries for s in all_stats)
        total_duration = sum(s.duration for s in all_stats)

        print(f"\n  Total frames:     {total_frames}")
        print(f"  Total duration:   {total_duration:.1f}s")
        print(f"  Overall Hz:       {total_frames / total_duration:.1f}" if total_duration > 0 else "")
        print(f"  SDK errors:       {total_errors}")
        print(f"  Arm errors:       {total_arm_errors}")
        print(f"  Recoveries:       {total_recoveries}")

        # Per-phase summary
        print(f"\n  Per-phase results:")
        fails = []
        for s in all_stats:
            status = "PASS" if (s.errors == 0 and s.arm_errors == 0) else "FAIL"
            label = f"    {status}: {s.name}"
            if s.errors > 0 or s.arm_errors > 0:
                label += f" ({s.errors} SDK err, {s.arm_errors} arm err)"
                fails.append(s.name)
            print(label)

        # Overall verdict
        overall = "PASS" if not fails else "FAIL"
        print(f"\n  {'='*40}")
        print(f"  OVERALL: {overall}")
        if fails:
            print(f"  Failed phases: {', '.join(fails)}")
        print(f"  {'='*40}")

    except KeyboardInterrupt:
        print("\n\nInterrupted by user.", flush=True)
    finally:
        # Always return to center and disconnect
        try:
            if ctrl.arm and not ctrl.arm.has_error:
                ctrl.arm.set_mode(0)
                ctrl.arm.set_state(0)
                time.sleep(0.3)
                ctrl.move_to_center(speed=60)
                time.sleep(0.5)
        except Exception:
            pass
        ctrl.disconnect()


def _recenter(ctrl):
    """Return to center in Mode 0, then re-enable servo."""
    try:
        if ctrl.arm.has_error:
            ctrl.arm.clean_error()
            ctrl.arm.clean_warn()
            ctrl.arm.motion_enable(enable=True)
        ctrl.arm.set_mode(0)
        ctrl.arm.set_state(0)
        time.sleep(0.5)
        ctrl.move_to_center(speed=100)
        time.sleep(0.5)
        ctrl.enable_servo()
        time.sleep(0.2)
    except Exception as e:
        print(f"  [recenter] {e}", flush=True)


if __name__ == "__main__":
    main()
