#!/usr/bin/env python3
"""Convert joint-angle CSV to USD animation layer for UF850 arm.

Reads a CSV with columns: frame, time_s, j1_deg..j6_deg, speed_pct
Outputs:
  1. Animation .usda  — time-sampled rotateXYZ (over prims, composable)
  2. Scene .usda       — sublayers animation + model for direct viewing

Usage:
    python scripts/csv_to_usd_anim.py assets/csv/0223_ani_test_01.csv
    python scripts/csv_to_usd_anim.py my_anim.csv -o assets/usd/uf850/
    python scripts/csv_to_usd_anim.py my_anim.csv --model-path ../uf850.usda
"""

import csv
import argparse
from pathlib import Path

# Which component of rotateXYZ each joint angle maps to: 0=X, 1=Y, 2=Z
# UF850 in Houdini Y-up, arm facing +X:
#   J1(base yaw)=Y, J2(shoulder pitch)=Z, J3(elbow pitch)=Z,
#   J4(wrist roll)=Y, J5(wrist bend)=Z, J6(tool roll)=Y
JOINT_AXIS = [1, 2, 2, 1, 2, 1]

PRIM_NAMES = ["uf850", "base", "joint_1", "joint_2", "joint_3",
              "joint_4", "joint_5", "joint_6"]


def read_csv(path: str) -> tuple[list[dict], float]:
    """Read CSV, return (frames, fps).

    Each frame dict has 'frame' (int), 'time_s' (float), 'angles' (list[float]).
    """
    frames = []
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            frames.append({
                "frame": int(row["frame"]),
                "time_s": float(row["time_s"]),
                "angles": [float(row[f"j{i}_deg"]) for i in range(1, 7)],
            })
    # Auto-detect FPS from time column
    if len(frames) >= 2:
        dt = frames[1]["time_s"] - frames[0]["time_s"]
        fps = round(1.0 / dt) if dt > 0 else 24
    else:
        fps = 24
    return frames, fps


def generate_anim_usda(frames: list[dict], fps: float) -> str:
    """Generate USDA text with time-sampled rotateXYZ per joint."""
    start = frames[0]["frame"]
    end = frames[-1]["frame"]

    # Build time samples per joint
    samples: dict[int, list[tuple[int, tuple]]] = {j: [] for j in range(6)}
    for f in frames:
        for j in range(6):
            rot = [0.0, 0.0, 0.0]
            rot[JOINT_AXIS[j]] = f["angles"][j]
            samples[j].append((f["frame"], tuple(rot)))

    lines: list[str] = []

    def w(indent: int, text: str = ""):
        lines.append("    " * indent + text)

    # Header
    lines.append("#usda 1.0")
    lines.append("(")
    w(1, f"timeCodesPerSecond = {fps}")
    w(1, f"startTimeCode = {start}")
    w(1, f"endTimeCode = {end}")
    lines.append(")")
    lines.append("")

    # Open uf850 > base
    w(0, 'over "uf850"')
    w(0, "{")
    w(1, 'over "base"')
    w(1, "{")

    # 6 nested joints
    for j in range(6):
        indent = 2 + j
        w(indent, f'over "joint_{j + 1}"')
        w(indent, "{")
        # Time samples
        w(indent + 1, "double3 xformOp:rotateXYZ.timeSamples = {")
        for frame_num, rot in samples[j]:
            w(indent + 2, f"{frame_num}: ({rot[0]}, {rot[1]}, {rot[2]}),")
        w(indent + 1, "}")

    # Close all braces: 6 joints + base + uf850 = 8
    for j in range(5, -1, -1):
        w(2 + j, "}")
    w(1, "}")
    w(0, "}")

    return "\n".join(lines) + "\n"


def generate_scene_usda(anim_filename: str, model_ref: str = "./uf850.usda") -> str:
    """Generate a composed scene that sublayers animation over model."""
    return (
        "#usda 1.0\n"
        "(\n"
        f'    subLayers = [\n'
        f'        @{anim_filename}@,\n'
        f'        @{model_ref}@\n'
        f'    ]\n'
        ")\n"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Convert joint-angle CSV to USD animation layer for UF850"
    )
    parser.add_argument("csv_path", help="Input CSV file")
    parser.add_argument(
        "-o", "--output-dir",
        help="Output directory (default: assets/usd/uf850/)",
    )
    parser.add_argument(
        "--model-path", default="./uf850.usda",
        help="Relative path from output dir to uf850.usda model (default: ./uf850.usda)",
    )
    parser.add_argument(
        "--no-scene", action="store_true",
        help="Skip generating the composed scene file",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv_path)
    stem = csv_path.stem

    # Default output dir: assets/usd/uf850/ (same dir as model)
    if args.output_dir:
        out_dir = Path(args.output_dir)
    else:
        out_dir = Path(__file__).resolve().parent.parent / "assets" / "usd" / "uf850"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Read CSV
    frames, fps = read_csv(str(csv_path))
    print(f"Read {len(frames)} frames from {csv_path.name} ({fps} fps)")

    # Write animation layer
    anim_file = out_dir / f"{stem}.usda"
    anim_usda = generate_anim_usda(frames, fps)
    anim_file.write_text(anim_usda, encoding="utf-8")
    print(f"Animation: {anim_file}")

    # Write composed scene
    if not args.no_scene:
        scene_file = out_dir / f"{stem}_scene.usda"
        scene_usda = generate_scene_usda(f"./{stem}.usda", args.model_path)
        scene_file.write_text(scene_usda, encoding="utf-8")
        print(f"Scene:     {scene_file}")

    print("Done.")


if __name__ == "__main__":
    main()
