# UF850 Arm Capability Reference

> **Data source**: IK probe on Docker simulator, 2026-02-28
> **Probe script**: `scripts/probe_workspace.py` (re-run to update)
> **Raw data**: `doc/arm_workspace_data.json`

## Coordinate System

| Axis | Direction        | Positive = |
|------|------------------|------------|
| X    | Forward/back     | Away from base |
| Y    | Lateral          | Left (viewer's right) |
| Z    | Vertical         | Up |

Origin at arm base. Units: mm, degrees. Default RPY = (180, 0, 0) = tool pointing straight down.

---

## Workspace Envelope (RPY=180,0,0)

### Key Limits

| Metric              | Value    | Condition         |
|---------------------|----------|-------------------|
| Max reach radius    | 838 mm   | at Z=200-350      |
| Max forward (X)     | 800 mm   | at Z=50-500       |
| Max lateral (Y)     | >600 mm  | at Z=0-550 (clipped by probe range) |
| Max height (Z)      | 1100 mm  | tiny area, X/Y <150 |
| Min height (Z)      | 0 mm     | reachable but near ground |

### Per-Height Summary

| Z (mm) | X range       | Y range       | Reach radius | Notes                           |
|---------|---------------|---------------|-------------|---------------------------------|
| 0       | -200 to 750   | +/-600+       | 791 mm      | Ground level                    |
| 100     | -200 to 800   | +/-600+       | 820 mm      |                                 |
| 200     | -200 to 800   | +/-600+       | **838 mm**  | **Peak reach**                  |
| 300     | -200 to 800   | +/-600+       | 838 mm      | Peak reach (tied)               |
| 400     | -200 to 800   | +/-600+       | 828 mm      | Default center height           |
| 500     | -200 to 800   | +/-600+       | 808 mm      |                                 |
| 600     | -200 to 750   | +/-600+       | 765 mm      |                                 |
| 700     | -200 to 700   | +/-600+       | 721 mm      | Lateral starts narrowing        |
| 800     | -200 to 650   | +/-600+       | 652 mm      |                                 |
| 900     | -200 to 550   | +/-550        | 559 mm      | Boundary visible in all axes    |
| 1000    | -200 to 400   | +/-400        | 424 mm      | Narrowing fast                  |
| 1050    | -200 to 300   | +/-300        | 320 mm      |                                 |
| 1100    | -150 to 150   | +/-150        | 158 mm      | Near arm limit, tiny workspace  |

> Y ranges marked "+/-600+" are clipped by the probe sweep range (+/-600mm). Actual lateral reach is wider and follows the reach radius sphere.

### Cross-Section at Key Heights

**Z=400 (default center height)** — the "comfortable zone":
```
X=-200: Y=+/-600+     (behind base, full lateral)
X=   0: Y=+/-600+     (directly above base)
X= 200: Y=+/-600+     (near reach)
X= 400: Y=+/-600+     (forward)
X= 600: Y=+/-550      (far forward, lateral narrows)
X= 800: Y=+/-200      (max forward, very narrow)
```

**Z=900 (high reach)** — workspace narrows significantly:
```
X=   0: Y=+/-550
X= 200: Y=+/-500
X= 400: Y=+/-350
X= 550: Y=+/-100      (max forward at this height)
```

**Z=1050 (near ceiling)** — small area:
```
X=   0: Y=+/-300
X= 200: Y=+/-250
X= 300: Y=+/-100      (max forward at this height)
```

---

## RPY Freedom

### Pitch (J5 control, "nodding")

**Near-full freedom across most of the workspace.** Pitch = +/-90 deg at almost all positions inside the main working area (X=100-600, Z=100-800).

Constrained zones (pitch span < 180 deg):

| Position (X, Y, Z) | Pitch range    | Span   | Cause               |
|---------------------|----------------|--------|---------------------|
| (700, +/-200, 100)  | [-90, +65]     | 155 deg | Far forward + low   |
| (100, 0, 400)       | [-70, +90]     | 160 deg | Close to base       |
| (700, +/-200, 600)  | [-90, +40]     | 130 deg | Far forward + high  |
| (700, +/-100, 700)  | [-90, +10]     | 100 deg | Far forward + very high |

**Rule of thumb**: Keep X < 650mm for full pitch freedom. At X > 650mm, pitch tilting upward (positive pitch) becomes restricted, especially at Z > 500mm.

### Yaw (J6 control, "looking left/right")

**Full 360 deg freedom everywhere tested.** J6 has +/-360 deg range, so yaw is never a constraint.

### Roll

Fixed at 180 deg (tool pointing down). Varying roll is possible via J4 (+/-360 deg) but not currently used in motion generation.

---

## Preset Positions

### Safe Presets (margin > 25 deg)

| Name            | X    | Y    | Z    | RPY            | Margin  | Description            |
|-----------------|------|------|------|----------------|---------|------------------------|
| **home**        | 300  | 0    | 400  | (180, 0, 0)    | 28.9 deg | Default center         |
| forward_far     | 600  | 0    | 400  | (180, 0, 0)    | 77.3 deg | Far forward reach      |
| excited_high    | 350  | 0    | 700  | (180, -20, 0)  | 61.4 deg | Excited, reaching up   |
| alert_forward   | 500  | 0    | 500  | (180, -15, 0)  | 61.1 deg | Alert, leaning forward |
| curious_tilt    | 400  | 0    | 500  | (180, 20, 15)  | 53.7 deg | Curious, head tilted   |
| scan_left       | 400  | 200  | 450  | (180, 0, -30)  | 53.3 deg | Scanning leftward      |
| scan_right      | 400  | -200 | 450  | (180, 0, 30)   | 53.3 deg | Scanning rightward     |
| forward_mid     | 450  | 0    | 400  | (180, 0, 0)    | 51.1 deg | Moderate forward       |
| high_forward    | 400  | 0    | 700  | (180, 0, 0)    | 48.4 deg | High and forward       |
| present_left    | 350  | 150  | 500  | (180, 0, -20)  | 47.1 deg | Presenting leftward    |
| present_right   | 350  | -150 | 500  | (180, 0, 20)   | 47.1 deg | Presenting rightward   |
| left_wide       | 300  | 200  | 450  | (180, 0, 0)    | 40.5 deg | Extended left          |
| right_wide      | 300  | -200 | 450  | (180, 0, 0)    | 40.5 deg | Extended right         |
| high_center     | 200  | 0    | 800  | (180, 0, 0)    | 27.7 deg | Raised high, centered  |

### Tight-Margin Presets (margin < 25 deg — use with caution)

| Name            | X    | Y    | Z    | RPY            | Margin  | Bottleneck | Notes                        |
|-----------------|------|------|------|----------------|---------|------------|------------------------------|
| low_center      | 250  | 0    | 200  | (180, 0, 0)    | 19.2 deg | J3         | Low centered                 |
| shy_low         | 200  | 0    | 300  | (180, 25, 0)   | 16.0 deg | J3         | Shy, looking down            |
| overhead        | 100  | 0    | 900  | (180, 0, 0)    | 10.1 deg | J5         | Near-vertical above base     |
| dormant         | 150  | 0    | 200  | (180, 0, 0)    | 5.5 deg  | J3         | Near J3 upper limit (3.5 deg) |
| low_close       | 150  | 0    | 250  | (180, 0, 0)    | 3.1 deg  | J3         | Near J3 upper limit (3.5 deg) |

> **J3 upper limit = 3.5 deg** is the main constraint at low-close positions. The elbow hits its extension limit. Increase Z or X to gain margin.

> **J5 limit = +/-124 deg** constrains the `overhead` position where the wrist needs extreme angles.

---

## Practical Guidelines

### Motion Design Rules of Thumb

1. **Sweet spot**: X=200-500, Y=+/-200, Z=300-700 — generous margins, full pitch/yaw freedom
2. **Forward reach**: up to X=800 is possible at Z=200-400, but Y narrows and margins tighten
3. **Height**: Z=200-800 has broad workspace; Z>900 narrows rapidly
4. **Low positions** (Z < 250): keep X > 200 to avoid J3 limit
5. **Overhead** (Z > 900): small workspace, keep within X=+/-200, Y=+/-200
6. **Pitch expression**: freely use +/-90 deg inside the sweet spot; at far-forward (X>650), positive pitch (looking up) is restricted

### Current Safety Bounds vs Actual Reach

| Axis | Current bounds | Actual reachable | Utilization |
|------|----------------|------------------|-------------|
| X    | 100 — 550      | -200 — 800       | 56%         |
| Y    | -200 — 200     | -600+ — 600+     | 33%         |
| Z    | 200 — 850      | 0 — 1100         | 59%         |

The current safety bounds use roughly **1/3 to 1/2** of the available workspace. This is conservative (good for safety) but leaves room for expansion when needed.

### Margin Zones

| Margin     | Meaning                       | Recommendation                    |
|------------|-------------------------------|-----------------------------------|
| > 40 deg   | Comfortable, smooth motion    | Use freely                        |
| 20-40 deg  | Adequate                      | Fine for normal operation         |
| 10-20 deg  | Tight                         | Slower speeds recommended         |
| < 10 deg   | Near joint limit              | Avoid in continuous motion; use only as endpoints |

---

## How to Re-probe

```bash
# Standard probe (50mm grid, ~3 seconds)
uv run python scripts/probe_workspace.py

# Fine grid (25mm, more detail)
uv run python scripts/probe_workspace.py --fine

# Presets only (instant)
uv run python scripts/probe_workspace.py --presets-only

# Against real arm
uv run python scripts/probe_workspace.py --ip 192.168.1.xxx
```

Results are saved to `doc/arm_workspace_data.json`. This reference should be updated when:
- End-effector tool changes (different tool length shifts the workspace)
- Mounting orientation changes
- New preset positions are added to the script
