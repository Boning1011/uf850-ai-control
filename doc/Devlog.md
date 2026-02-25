# Devlog

Development timeline — newest first.

---

## 2026-02-25 — Project Setup & Initial Architecture

**What happened:**
- Created project repo `uf850-ai-control`
- Wrote Design Brief defining three-layer architecture (AI Perception → Procedural Motion → Safety)
- Set up CLAUDE.md with workflow rules (auto-push, doc maintenance, knowledge logging)
- Documented UFactory Studio Docker simulator setup (image, ports, 850 = `6 12`)
- Documented dev environment (Win11 + Ubuntu, both RTX 4090, fal.ai / Anthropic / OpenAI accounts)
- Established Devlog for tracking progress and pivots

**Current direction:**
- Three-layer architecture is the working plan but explicitly tentative
- Next likely step: get Docker simulator running and test basic SDK control
- Training-based approaches (imitation learning, etc.) may be added later

**Open questions:**
- Exact VLM provider choice (Claude Vision vs GPT-4V vs other)
- Whether local models (YOLO/MediaPipe) will be used as pre-filter
- TouchDesigner integration timing
