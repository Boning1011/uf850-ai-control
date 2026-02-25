# Development Environment

## Hardware

| Location | OS           | GPU        |
|----------|--------------|------------|
| Work     | Windows 11   | RTX 4090   |
| Home     | Ubuntu       | RTX 4090   |

Both machines are capable of local model inference (24 GB VRAM) — relevant if the project pivots toward on-device training or local model serving.

## Accounts & Services

- **fal.ai** — API access for hosted model inference (image gen, vision models, etc.)
- **Anthropic / OpenAI** — Cloud VLM for perception layer

## Software & Tooling

- **Docker** — Used for UFactory Studio simulator and potentially other services
- **Python venv** — Project isolation; prefer venv for reproducibility
- **UFactory Studio** — Installed locally on Windows; also available via Docker for simulation
- **Git + GitHub** — Version control; auto-push on every change (see CLAUDE.md)

## Notes

- Cross-platform: code must work on both Windows and Ubuntu. Use forward slashes and `pathlib` where possible; avoid OS-specific assumptions.
- When writing Docker commands, ensure they work on both platforms (avoid Linux-only flags like `--network=host` without noting the Windows alternative).
