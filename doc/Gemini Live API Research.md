# Gemini Live API Research (2026-02-26)

Research for real-time audience perception in the UFactory 850 art installation.
Use case: camera feed -> structured scene understanding at ~0.5-1 Hz.

---

## 1. What It Does

The **Gemini Live API** (also called Multimodal Live API) is a stateful, bidirectional streaming API that enables real-time multimodal interaction with Gemini models. Unlike standard request/response APIs, it maintains a persistent WebSocket connection where you can continuously stream audio, video, and text **into** the model and receive audio or text responses **out** in real time.

**Key capabilities:**
- Continuous video stream input (processed at 1 FPS internally)
- Continuous audio stream input (16kHz PCM)
- Continuous text input
- Real-time text OR audio output (one modality per session, not both)
- Function calling / tool use within the live session
- Voice Activity Detection (VAD) for natural conversation
- User interruption handling
- Session memory within a single connection

**Current model:** `gemini-2.5-flash-native-audio-preview` (GA on Vertex AI)

---

## 2. Technical Architecture

### Protocol
- **WebSocket** connection to `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent`
- Stateful: the model retains context of everything seen/heard within the session
- Bidirectional: client sends frames/audio/text, server sends responses concurrently

### Message Types (Client -> Server)
```
{
  "setup": BidiGenerateContentSetup,        // Initial config (model, tools, system prompt)
  "clientContent": ...,                       // Text turns
  "realtimeInput": ...,                       // Audio/video frames
  "toolResponse": ...                         // Function call results
}
```

### Input Modalities
| Modality | Format | Details |
|----------|--------|---------|
| Video    | JPEG/PNG frames, or video MIME types | Processed at **1 FPS** internally; recommended **768x768** native resolution |
| Audio    | Raw 16-bit PCM, 16kHz, mono, little-endian | Also supports AAC, FLAC, MP3, WAV, OGG, WebM |
| Text     | UTF-8 strings | Sent as `clientContent` messages |

### Output Modalities
| Modality | Format | Details |
|----------|--------|---------|
| Text     | UTF-8 strings | Structured JSON possible via function calling |
| Audio    | Raw 16-bit PCM, 24kHz, little-endian | Natural speech output |

**Critical constraint: only ONE output modality (TEXT or AUDIO) per session.** For our use case (structured JSON output), we would use TEXT mode.

### Token Consumption Rates
| Input type | Tokens/second |
|------------|---------------|
| Video      | ~258-300 tokens/sec (at default resolution) |
| Video (low res) | ~100 tokens/sec |
| Audio      | ~25-32 tokens/sec |

### Python SDK
```python
from google import genai
from google.genai.types import LiveConnectConfig, Modality, Content, Part

client = genai.Client(api_key='GEMINI_API_KEY')

async with client.aio.live.connect(
    model="gemini-2.5-flash-native-audio-preview",
    config=LiveConnectConfig(
        response_modalities=[Modality.TEXT],
        # system_instruction=...,
        # tools=[...],
    )
) as session:
    # Send video frame
    await session.send_realtime_input(media=frame_bytes, mime_type="image/jpeg")

    # Or send text
    await session.send_client_content(
        turns=Content(role="user", parts=[Part(text="Describe the scene")])
    )

    # Receive responses
    async for msg in session.receive():
        if msg.text:
            print(msg.text)
```

**SDK:** `pip install google-genai` -- the official `google-genai` package is actively maintained and supports Live API natively with async WebSocket management.

---

## 3. Session Limits

| Constraint | Value |
|------------|-------|
| WebSocket connection lifetime | ~10 minutes |
| Audio-only session (no compression) | 15 minutes |
| Audio+video session (no compression) | **2 minutes** |
| Context window | 128k tokens |
| Concurrent sessions per API key | 3 (Developer API) |
| Concurrent sessions (Vertex AI) | 1,000 per project |

### Extending Sessions
- **Context window compression**: sliding-window that truncates/summarizes oldest turns. Enables "unlimited" duration.
- **Session resumption**: server stores session state for up to 24 hours; reconnect with a resumption token (valid 2 hours after disconnect). Server sends `GoAway` message before disconnecting.

**Important:** The 2-minute video session limit is the raw limit without compression. With compression enabled, video sessions can be extended, but this is a relatively new feature and may have rough edges.

---

## 4. Pricing

### Gemini Live API (Native Audio model) -- Paid Tier
| Token type | Per 1M tokens |
|------------|---------------|
| Text input | $0.50 |
| Audio/video input | $3.00 |
| Text output | $2.00 |
| Audio output | $12.00 |

### Cost Estimate for Our Use Case
At 1 FPS video input, ~258 tokens/sec:
- 1 minute of video = ~15,480 tokens
- 1 hour of video = ~928,800 tokens (~0.93M tokens)
- **Cost for 1 hour of continuous video input: ~$2.79** (at $3.00/M tokens)
- Plus text output costs for structured responses

For comparison, standard Gemini 2.5 Flash (non-live):
| Token type | Per 1M tokens |
|------------|---------------|
| Text/image/video input | $0.30 |
| Audio input | $1.00 |
| Text output | $2.50 |

### Free Tier
Live API models have free tier access but with very restrictive limits (5-10 RPM, low daily quotas). Sufficient for development/testing but not production.

### Rate Limits (Paid Tier 1)
- 150-300 RPM depending on model
- 1M TPM
- 1,500 RPD

---

## 5. Key Limitations and Gotchas

### Critical for Our Use Case

1. **2-minute video session limit** (without compression) -- This is the biggest issue. With compression enabled it can be extended, but the feature maturity is uncertain.

2. **1 FPS video processing** -- The model internally samples at 1 FPS regardless of input rate. Fine for our 0.5-1 Hz requirement, but no benefit to sending faster.

3. **TEXT or AUDIO output, not both** -- Must choose one per session. For structured JSON, use TEXT mode.

4. **No native structured output / response schema in Live API** -- Unlike the standard `generateContent` API which supports `response_mime_type: "application/json"` with a schema, the Live API's structured output support is through **function calling** only. You define tool schemas and the model calls them with structured arguments.

5. **3 concurrent sessions per API key** (Developer API) -- Low limit for production.

6. **128k token context window** -- With video at ~258 tokens/sec, that's ~8 minutes of video context before the window fills. Context compression is required for longer sessions.

7. **Server-to-server auth only** -- Not designed for direct client connections in production. Needs an intermediate server.

### General Gotchas

8. **Connection drops** -- WebSocket connections have a ~10 minute lifetime. Must implement reconnection logic with session resumption.

9. **Audio echo** -- If using audio output, the model can hear its own output and interrupt itself. Need echo cancellation or headphones.

10. **Tool responses are manual** -- Unlike `generateContent`, the Live API doesn't auto-execute tools. Client code must handle tool calls and send responses back.

11. **Model version deprecation** -- The preview model `gemini-live-2.5-flash-preview-native-audio-09-2025` is deprecated March 19, 2026. Must track model migrations.

---

## 6. Comparison for Our Use Case

**Use case reminder:** Robotic arm art installation, camera at ~0.5-1 Hz, output structured scene understanding (audience count, position, gesture, behavioral state).

### Option A: Gemini Live API (Streaming)

| Aspect | Assessment |
|--------|------------|
| **Latency** | Very low (~sub-second). Persistent connection, no per-request overhead |
| **Video input** | Native streaming at 1 FPS. Perfect match for our 0.5-1 Hz |
| **Structured output** | Via function calling only (not native JSON schema). Workable but less clean |
| **Session management** | Complex. 2-min video limit without compression, reconnection logic needed |
| **Cost** | ~$2.79/hr for video input + output tokens. Cheap |
| **SDK maturity** | `google-genai` is official, actively maintained, good async support |
| **Complexity** | High. WebSocket state management, session resumption, compression config |
| **Best for** | Continuous real-time perception with minimal latency |

### Option B: Gemini Standard Vision API (Non-Live, Periodic Frames)

| Aspect | Assessment |
|--------|------------|
| **Latency** | Per-request: ~1-3 seconds for Gemini 2.5 Flash |
| **Video input** | Send individual frames as images in `generateContent` requests |
| **Structured output** | Full native support: `response_mime_type="application/json"` + JSON schema |
| **Session management** | Stateless. No session to manage. Much simpler |
| **Cost** | $0.30/M input tokens (2.5 Flash). Even cheaper than Live API |
| **SDK maturity** | Very mature, well-documented |
| **Complexity** | Low. Standard request/response pattern |
| **Best for** | Simple periodic perception. Our 0.5-1 Hz rate is naturally periodic |

### Option C: Claude Vision (Anthropic) with Periodic Frames

| Aspect | Assessment |
|--------|------------|
| **Latency** | Per-request: ~2-5 seconds depending on model |
| **Video input** | Send frames as base64 images. No streaming API |
| **Structured output** | Excellent via tool_use or structured prompting |
| **Session management** | Stateless requests. Simple |
| **Cost** | Haiku 4.5: $1/$5 per M tokens. Sonnet 4.6: $3/$15. Images ~1,600 tokens each |
| **SDK maturity** | Very mature (`anthropic` package) |
| **Complexity** | Low. Well-understood pattern |
| **Best for** | High-quality reasoning about scenes. Best structured output reliability |
| **Drawback** | No streaming. Higher per-image cost than Gemini Flash. No video-native model |

### Option D: GPT-4o / GPT-4.1 with Periodic Frames (OpenAI)

| Aspect | Assessment |
|--------|------------|
| **Latency** | Per-request: ~1-3 seconds |
| **Video input** | Send frames as images in chat completions. No continuous video stream |
| **Structured output** | Native JSON mode + function calling. Very reliable |
| **Session management** | Stateless requests |
| **Cost** | GPT-4o: $2.50/$10 per M tokens. GPT-4.1-mini: $0.40/$1.60 |
| **SDK maturity** | Very mature (`openai` package) |
| **Complexity** | Low |
| **Best for** | Balanced quality/cost for periodic analysis |

### Option E: OpenAI Realtime API

| Aspect | Assessment |
|--------|------------|
| **Latency** | Very low. WebSocket/WebRTC based |
| **Video input** | **NOT real video streaming.** Takes snapshots on demand, charges as image input. Not true continuous video understanding |
| **Structured output** | JSON tool schemas, deterministic |
| **Session management** | WebSocket-based, similar complexity to Gemini Live |
| **Cost** | Higher than Gemini for audio. No native video pricing |
| **Best for** | Voice-first applications. NOT for continuous video perception |
| **Verdict** | **Not suitable** for our video perception use case |

---

## 7. Recommendation for the Art Installation

### Primary recommendation: **Option B -- Gemini Standard Vision API (2.5 Flash)**

**Rationale:**
1. Our perception rate is 0.5-1 Hz -- this is inherently periodic, not truly continuous. We don't need a persistent streaming connection.
2. Standard API has **native structured output** with JSON schema enforcement -- much more reliable for producing consistent `{count, positions, gestures, state}` objects.
3. **Dramatically simpler** to implement and maintain. No WebSocket state, no session resumption, no compression config, no reconnection logic.
4. **Cheapest option** at $0.30/M input tokens.
5. 1-3 second latency per request is acceptable when the arm's motion is continuous (Perlin noise / procedural) and the VLM just modulates parameters.
6. Gemini Flash is fast enough that at 1 Hz polling, the response arrives well before the next request.

### When to consider Gemini Live API instead:
- If we need sub-second latency between scene change and arm response
- If we want the model to maintain continuous temporal context (seeing the "flow" of people, not isolated snapshots)
- If we add real-time audio interaction (audience speaks to the arm)

### Fallback: **Option C -- Claude Vision (Haiku 4.5 or Sonnet 4.6)**
- Better reasoning quality if Gemini's structured output proves unreliable
- Prompt caching (90% discount) could make repeated similar-scene analysis very cheap
- Already have Anthropic API key in the project

### Implementation sketch (Option B):
```python
import asyncio
from google import genai

client = genai.Client(api_key=API_KEY)

async def perceive(frame_jpeg: bytes) -> dict:
    """Send a single frame, get structured scene understanding."""
    response = await client.aio.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            {"role": "user", "parts": [
                {"text": SYSTEM_PROMPT},
                {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(frame_jpeg).decode()}}
            ]}
        ],
        config={
            "response_mime_type": "application/json",
            "response_schema": SCENE_SCHEMA,  # enforced JSON schema
        }
    )
    return json.loads(response.text)

# Main loop: ~1 Hz
while True:
    frame = capture_frame()
    scene = await perceive(frame)
    update_arm_parameters(scene)
    await asyncio.sleep(1.0)
```
