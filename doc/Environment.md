# Development Environment

## Hardware

| Location | OS           | GPU        |
|----------|--------------|------------|
| Work     | Windows 11   | RTX 4090   |
| Home     | macOS        | —          |

Work machine (Windows) has 24 GB VRAM — relevant if the project pivots toward on-device training or local model serving.

## Accounts & Services

- **Google Gemini** — Primary cloud VLM for perception layer (Gemini 2.5 Flash, via `google-genai`). API key: `GEMINI_API_KEY` in `.env`.
- **fal.ai** — API access for hosted model inference (available, not currently used in main pipeline)
- **Anthropic / OpenAI** — Available; not currently used in main pipeline

## Software & Tooling

- **uv** — Python dependency management (`pyproject.toml` + `uv.lock`). On new machine: `uv sync`. See CLAUDE.md for workflow.
- **Docker** — Used for UFactory Studio simulator. Image: `danielwang123321/uf-ubuntu-docker`, 850 = `6 12`, connect to `127.0.0.1`.
- **UFactory Studio** — Available via Docker for simulation; also installable locally on Windows
- **Git + GitHub** — Version control; auto-push on every change (see CLAUDE.md)
- **MediaPipe** — Local hand landmark detection. Model file `models/hand_landmarker.task` (~50 MB) not in Git; download separately (see README).

## Notes

- Cross-platform: code must work on both Windows and macOS. Use forward slashes and `pathlib` where possible; avoid OS-specific assumptions.
- Windows: `.venv/Scripts/python.exe`; macOS: `.venv/bin/python`
- When writing Docker commands, ensure they work on both platforms (avoid Linux-only flags like `--network=host` without noting the Windows alternative).
- Only env var required at runtime: `GEMINI_API_KEY` (for VLM mode). Hand tracking mode works without any API key.
