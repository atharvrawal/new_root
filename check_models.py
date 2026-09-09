"""Probe which Gemini models this key can actually generate with.

models.list() shows what's *visible*; it does not tell you what your tier
is allowed to *call*. Only a real request does. Each probe below is a
handful of tokens.
"""
import json, os, sys, time
from pathlib import Path
from google import genai
from google.genai import types

cfg = json.loads((Path(os.environ["APPDATA"]) / "ThumbnailAssistant" / "config.json").read_text(encoding="utf-8"))
client = genai.Client(api_key=cfg["gemini_api_key"])

# Text/vision models only - skip tts, image-gen, live and native-audio.
skip = ("tts", "image", "live", "native-audio", "omni", "embedding", "lyria", "veo", "imagen")
listed = [m.name.replace("models/", "") for m in client.models.list()]
candidates = [n for n in listed if not any(s in n for s in skip)]

print(f"{len(candidates)} text/vision models visible to this key\n")
print(f"{'model':<40} {'result'}")
print("-" * 78)

ok, denied = [], []
for name in candidates:
    try:
        r = client.models.generate_content(
            model=name,
            contents="hi",
            config=types.GenerateContentConfig(max_output_tokens=1),
        )
        ok.append(name)
        print(f"{name:<40} OK")
    except Exception as e:
        msg = str(e).replace("\n", " ")
        code = "429 quota" if "429" in msg or "RESOURCE_EXHAUSTED" in msg else \
               "403 denied" if "403" in msg or "PERMISSION" in msg else \
               "404 missing" if "404" in msg else "error"
        denied.append((name, code))
        print(f"{name:<40} {code}: {msg[:90]}")
    time.sleep(1.2)

print(f"\nUSABLE ({len(ok)}): " + ", ".join(ok))
print(f"BLOCKED ({len(denied)}): " + ", ".join(f"{n} [{c}]" for n, c in denied))
