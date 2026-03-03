"""Quick latency benchmark for VLM pipeline — testing different configs."""
import os, sys, time, cv2, json
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

class VLMOutput(BaseModel):
    energy: float = Field(ge=0, le=1)
    mood: float = Field(ge=0, le=1)
    presence: float = Field(ge=0, le=1)
    urgency: float = Field(ge=0, le=1)

SYSTEM = (
    "You are the perception system for an interactive robotic arm. "
    "Analyze camera frames. Output behavioral parameters as JSON. "
    "energy: 0=dormant 1=excited. mood: 0=tense 1=playful. "
    "presence: 0=empty 1=crowded. urgency: 0=stable 1=sudden_change."
)

N_FRAMES = 3
N_CALLS = 5

cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
time.sleep(0.3)


def grab_frames(n):
    frames = []
    for _ in range(n):
        ret, frame = cap.read()
        if ret:
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            frames.append(buf.tobytes())
        time.sleep(0.02)
    return frames


def run_test(label, model, extra_config=None):
    print(f"\n=== {label} ({N_CALLS} calls, {N_FRAMES} frames) ===\n")
    latencies = []
    for i in range(N_CALLS):
        frames = grab_frames(N_FRAMES)
        parts = [types.Part.from_bytes(data=f, mime_type="image/jpeg") for f in frames]
        parts.append(
            f"{N_FRAMES} sequential camera frames. Analyze scene."
        )

        cfg = dict(
            system_instruction=SYSTEM,
            response_mime_type="application/json",
            response_schema=VLMOutput,
        )
        if extra_config:
            cfg.update(extra_config)

        t0 = time.time()
        try:
            resp = client.models.generate_content(
                model=model,
                contents=parts,
                config=types.GenerateContentConfig(**cfg),
            )
            dt = time.time() - t0
            latencies.append(dt)

            s = resp.parsed
            if s:
                print(f"  #{i+1}: {dt:.2f}s  e={s.energy:.2f} m={s.mood:.2f} "
                      f"p={s.presence:.2f} u={s.urgency:.2f}")
            else:
                txt = resp.text or ""
                if "```" in txt:
                    txt = txt.split("```")[1]
                    if txt.startswith("json"):
                        txt = txt[4:]
                try:
                    data = json.loads(txt.strip())
                    print(f"  #{i+1}: {dt:.2f}s  e={data['energy']:.2f} m={data['mood']:.2f} "
                          f"p={data['presence']:.2f} u={data['urgency']:.2f} (fallback)")
                except Exception:
                    print(f"  #{i+1}: {dt:.2f}s  PARSE FAIL: {txt[:80]}")
        except Exception as e:
            dt = time.time() - t0
            print(f"  #{i+1}: {dt:.2f}s  ERROR: {str(e)[:100]}")

    if latencies:
        avg = sum(latencies) / len(latencies)
        print(f"\n  => avg={avg:.2f}s  min={min(latencies):.2f}s  max={max(latencies):.2f}s  rate={1/avg:.2f}Hz")
    return latencies


# Test 1: gemini-2.5-flash with thinking budget = 0 (disable thinking)
lat1 = run_test(
    "gemini-2.5-flash (thinking=OFF)",
    "gemini-2.5-flash",
    extra_config={"thinking_config": types.ThinkingConfig(thinking_budget=0)},
)

# Test 2: gemini-2.0-flash (no thinking, pure speed)
lat2 = run_test(
    "gemini-2.0-flash (no thinking model)",
    "gemini-2.0-flash",
)

# Test 3: gemini-2.5-flash with low thinking budget
lat3 = run_test(
    "gemini-2.5-flash (thinking=1024)",
    "gemini-2.5-flash",
    extra_config={"thinking_config": types.ThinkingConfig(thinking_budget=1024)},
)

cap.release()

print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
if lat1:
    print(f"  2.5-flash (no think): avg={sum(lat1)/len(lat1):.2f}s")
if lat2:
    print(f"  2.0-flash:            avg={sum(lat2)/len(lat2):.2f}s")
if lat3:
    print(f"  2.5-flash (think=1k): avg={sum(lat3)/len(lat3):.2f}s")
