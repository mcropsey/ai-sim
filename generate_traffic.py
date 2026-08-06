#!/usr/bin/env python3
"""
generate_traffic.py — drive realistic LLM + GenAI traffic at the AI-Sim server
so an API sensor (Akamai / Noname) learns the endpoints and applies its
"LLM" and "GenAI" insight tags.

Run it FROM A DIFFERENT HOST than the server (e.g. your Mac) so the traffic
crosses the LAN interface the sensor watches — not loopback.

Usage:
    python3 generate_traffic.py                          # defaults below, runs forever
    BASE_URL=http://192.168.1.102:8011 python3 generate_traffic.py
    python3 generate_traffic.py --rounds 50 --delay 1.0
    python3 generate_traffic.py --only llm               # llm | genai | all
    AI_SIM_AUTH_TOKEN=xxxx python3 generate_traffic.py   # if the server gates on a token

Two listeners, on purpose:
    BASE_URL        LLM endpoints        default http://192.168.1.102:8011
    GENAI_URL       GenAI endpoints      default same host, port 8012

WHY THE SPLIT
-------------
The classifier accumulates score per API and emits ONE final classification.
When LLM and GenAI endpoints shared a host:port they were scored as a single
API, the stronger text-generation signal won, and the GenAI tag never appeared.
Separate ports = separate API entities = independent scoring.

The second half of that fix is here: GenAI requests no longer carry OpenAI *LLM*
vendor headers. Previously every media call went out with
`OpenAI-Beta: assistants=v2`, `OpenAI-Organization`, and `User-Agent:
OpenAI/Python`, which is textbook LLM vendor-pattern signal riding on an image
endpoint. Each call now sends only what its real vendor SDK sends.

Also corrected: /v1/images/edits and /v1/audio/transcriptions are
multipart/form-data with file uploads in the real OpenAI API — they were being
sent as JSON, so they matched nothing. They now send real multipart bodies.

Each round sends a varied mix so the schema the sensor learns is rich:
prompts/messages + model params + token-usage for LLM, and image/audio/video
generation bodies with vendor/model naming for GenAI.
"""

import argparse
import base64
import os
import random
import string
import sys
import time
from urllib.parse import urlsplit, urlunsplit

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

BASE_URL = os.environ.get("BASE_URL", "http://192.168.1.102:8011").rstrip("/")


def _sibling_url(url, port):
    """Same scheme+host as BASE_URL, different port — used to derive GENAI_URL."""
    parts = urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    if ":" in host:  # IPv6
        host = f"[{host}]"
    return urlunsplit((parts.scheme, f"{host}:{port}", "", "", ""))


GENAI_PORT = os.environ.get("GENAI_PORT", "8012")
GENAI_URL = os.environ.get("GENAI_URL", _sibling_url(BASE_URL, GENAI_PORT)).rstrip("/")
AUTH_TOKEN = os.environ.get("AI_SIM_AUTH_TOKEN", "").strip()

SESSION = requests.Session()
if AUTH_TOKEN:
    SESSION.headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
# NOTE: Content-Type is set PER REQUEST, never on the session. A session-wide
# application/json would corrupt the multipart uploads below (requests needs to
# generate its own Content-Type with a boundary).

# ── sample content pools (variety helps the sensor build a real schema) ─────────
PROMPTS = [
    "Summarize the key risks in our Q3 security posture.",
    "Write a haiku about API gateways.",
    "Explain OAuth2 device flow to a new engineer.",
    "What are the trade-offs between REST and gRPC?",
    "Draft a friendly reminder email about the maintenance window.",
    "Classify this ticket: 'login page returns 502 intermittently'.",
]
CHAT_MODELS = ["gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo"]
ANTHROPIC_MODELS = ["claude-3-5-sonnet-20241022", "claude-3-opus-20240229"]
EMBED_MODELS = ["text-embedding-3-small", "text-embedding-3-large"]
IMAGE_PROMPTS = [
    "a photorealistic red fox in a snowy forest, golden hour",
    "isometric illustration of a data center, flat colors",
    "watercolor painting of the Cincinnati skyline at dusk",
]
VIDEO_PROMPTS = [
    "drone shot flying over a coastal highway at sunrise",
    "a paper airplane looping through a sunlit office",
]

# tiny real files for the multipart uploads
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_WAV_SILENT = (
    b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    b"\x44\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
)

STATS = {"ok": 0, "err": 0, "llm": 0, "genai": 0}


# ── vendor request headers ──────────────────────────────────────────────────────
# "Known vendor header patterns" is one of the classifier signals, so we send the
# same recognizable auth/version headers the real provider SDKs send. These are
# FAKE, correctly-shaped credentials for a lab — not real keys.
#
# CRITICAL: the media vendors below deliberately do NOT send OpenAI-Beta,
# OpenAI-Organization, or an LLM-flavored User-Agent. Those are text-generation
# vendor signals and sending them on an image/audio/video call is what pushed the
# GenAI endpoints into the LLM bucket.
#
# NOTE: if the app's own AI_SIM_AUTH_TOKEN gate is enabled, we must not clobber
# the Authorization header the app expects, so vendor Authorization is only added
# when the gate is OFF (the recommended demo setup).
def _rand(n):
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


def _vendor_headers(vendor):
    h = {}
    # ── LLM vendors ───────────────────────────────────────────────────────────
    if vendor == "openai":
        if not AUTH_TOKEN:
            h["Authorization"] = "Bearer sk-proj-" + _rand(40)
        h["OpenAI-Organization"] = "org-" + _rand(20)
        h["OpenAI-Beta"] = "assistants=v2"
        h["User-Agent"] = "OpenAI/Python 1.51.0"
    elif vendor == "anthropic":
        h["x-api-key"] = "sk-ant-api03-" + _rand(48)
        h["anthropic-version"] = "2023-06-01"
        h["User-Agent"] = "Anthropic/Python 0.39.0"
    elif vendor == "azure":
        h["api-key"] = _rand(32)
        h["User-Agent"] = "openai-python-azure/1.51.0"
    # ── GenAI / media vendors — no LLM markers ────────────────────────────────
    elif vendor == "openai_image":
        if not AUTH_TOKEN:
            h["Authorization"] = "Bearer sk-proj-" + _rand(40)
        h["User-Agent"] = "openai-images/1.51.0"
        h["X-Media-Type"] = "image"
    elif vendor == "openai_audio":
        if not AUTH_TOKEN:
            h["Authorization"] = "Bearer sk-proj-" + _rand(40)
        h["User-Agent"] = "openai-audio/1.51.0"
        h["X-Media-Type"] = "audio"
    elif vendor == "openai_video":
        if not AUTH_TOKEN:
            h["Authorization"] = "Bearer sk-proj-" + _rand(40)
        h["User-Agent"] = "openai-video/1.51.0"
        h["X-Media-Type"] = "video"
    elif vendor == "stability":
        if not AUTH_TOKEN:
            h["Authorization"] = "Bearer sk-" + _rand(48)
        h["Accept"] = "application/json"
        h["User-Agent"] = "stability-sdk/0.8.5"
        h["X-Media-Type"] = "image"
    elif vendor == "replicate":
        # Real Replicate accepts both "Bearer r8_…" and "Token r8_…".
        if not AUTH_TOKEN:
            h["Authorization"] = "Bearer r8_" + _rand(37)
        h["User-Agent"] = "replicate-python/0.34.0"
        h["Prefer"] = "wait"
        h["X-Media-Type"] = "image"
    elif vendor == "elevenlabs":
        h["xi-api-key"] = _rand(32)
        h["Accept"] = "audio/mpeg"
        h["Content-Type"] = "application/json"
        h["User-Agent"] = "elevenlabs-python/1.0.0"
        h["X-Media-Type"] = "audio"
    return h


def _hit(method, path, tag, vendor="openai", base=None, **kw):
    """tag is 'LLM' or 'GenAI' — declared explicitly, not guessed from the path."""
    root = base if base is not None else (BASE_URL if tag == "LLM" else GENAI_URL)
    url = root + path
    headers = _vendor_headers(vendor)
    try:
        r = SESSION.request(method, url, timeout=15, headers=headers, **kw)
        ok = r.status_code < 400
        STATS["ok" if ok else "err"] += 1
        STATS["llm" if tag == "LLM" else "genai"] += 1
        print(f"  [{tag:5}] {method:4} {path:48} -> {r.status_code}")
        return r
    except Exception as e:
        STATS["err"] += 1
        print(f"  [ERR  ] {method:4} {path:48} -> {e}")
        return None


# ── LLM traffic (port 8011) ─────────────────────────────────────────────────────
def llm_round():
    prompt = random.choice(PROMPTS)

    _hit("POST", "/v1/chat/completions", "LLM", json={
        "model": random.choice(CHAT_MODELS),
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt},
        ],
        "temperature": round(random.uniform(0, 1), 2),
        "max_tokens": random.choice([128, 256, 512]),
    })

    _hit("POST", "/v1/completions", "LLM", json={
        "model": "gpt-3.5-turbo-instruct",
        "prompt": prompt,
        "max_tokens": 64,
        "temperature": 0.7,
    })

    _hit("POST", "/v1/embeddings", "LLM", json={
        "model": random.choice(EMBED_MODELS),
        "input": random.sample(PROMPTS, k=random.randint(1, 3)),
    })

    _hit("POST", "/v1/messages", "LLM", vendor="anthropic", json={
        "model": random.choice(ANTHROPIC_MODELS),
        "max_tokens": 512,
        "messages": [{"role": "user", "content": prompt}],
    })

    dep = random.choice(["gpt-4o-prod", "gpt35-eastus"])
    _hit("POST", f"/openai/deployments/{dep}/chat/completions", "LLM", vendor="azure", json={
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.5,
        "max_tokens": 256,
    })

    _hit("GET", "/v1/models", "LLM")


# ── GenAI traffic (port 8012) ───────────────────────────────────────────────────
def genai_round():
    _hit("POST", "/v1/images/generations", "GenAI", vendor="openai_image", json={
        "model": "dall-e-3",
        "prompt": random.choice(IMAGE_PROMPTS),
        "n": 1,
        "size": random.choice(["1024x1024", "1792x1024"]),
        "quality": "hd",
        "response_format": "b64_json",
    })

    # multipart/form-data with a real image part — this is how the OpenAI edit
    # endpoint is actually called. Do NOT set Content-Type; requests builds the
    # boundary itself.
    _hit("POST", "/v1/images/edits", "GenAI", vendor="openai_image",
         data={"model": "dall-e-2", "prompt": "add a rainbow in the background",
               "n": "1", "size": "1024x1024"},
         files={"image": ("source.png", _PNG_1x1, "image/png"),
                "mask": ("mask.png", _PNG_1x1, "image/png")})

    _hit("POST", "/v1/audio/speech", "GenAI", vendor="openai_audio", json={
        "model": random.choice(["tts-1", "tts-1-hd"]),
        "voice": random.choice(["alloy", "nova", "shimmer"]),
        "input": random.choice(PROMPTS),
        "response_format": "mp3",
        "speed": 1.0,
    })

    # /v1/audio/transcriptions omitted: Whisper converts audio→text (LLM signal, not GenAI).

    engine = random.choice(["stable-diffusion-xl-1024-v1-0", "stable-diffusion-v1-6"])
    _hit("POST", f"/v1/generation/{engine}/text-to-image", "GenAI", vendor="stability", json={
        "text_prompts": [{"text": random.choice(IMAGE_PROMPTS), "weight": 1}],
        "cfg_scale": 7,
        "steps": 30,
        "width": 1024,
        "height": 1024,
        "samples": 1,
    })

    _hit("POST", "/v1/predictions", "GenAI", vendor="replicate", json={
        "version": "a9758cb...sdxl",
        "input": {"prompt": random.choice(IMAGE_PROMPTS), "width": 1024,
                  "height": 1024, "num_outputs": 1, "output_format": "png"},
    })

    voice_id = random.choice(["21m00Tcm4TlvDq8ikWAM", "AZnzlk1XvdvUeBnXmlld"])
    _hit("POST", f"/v1/text-to-speech/{voice_id}", "GenAI", vendor="elevenlabs", json={
        "text": random.choice(PROMPTS),
        "model_id": "eleven_multilingual_v2",
        "output_format": "mp3_44100_128",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    })

    _hit("POST", "/v1/video/generations", "GenAI", vendor="openai_video", json={
        "model": "sora-1.0",
        "prompt": random.choice(VIDEO_PROMPTS),
        "duration": random.choice([5, 10]),
        "resolution": "1280x720",
        "fps": 24,
    })

    # /v1/models omitted: strongest LLM URL pattern in the OpenAI API — contaminates GenAI score.


def _healthz(url, label):
    try:
        h = SESSION.get(url + "/healthz", timeout=5)
        print(f"healthz {label:5} {url} -> {h.status_code} {h.text!r}")
        return True
    except Exception as e:
        print(f"healthz {label:5} {url} -> UNREACHABLE ({e})")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=0, help="0 = run forever")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between rounds")
    ap.add_argument("--only", choices=["llm", "genai", "all"], default="all")
    args = ap.parse_args()

    print(f"AI-Sim traffic generator")
    print(f"  LLM   -> {BASE_URL}")
    print(f"  GenAI -> {GENAI_URL}")
    print(f"mode={args.only}  rounds={args.rounds or 'infinite'}  delay={args.delay}s")
    if AUTH_TOKEN:
        print("auth: Bearer token set")
    print("-" * 60)

    need_llm = args.only in ("all", "llm")
    need_genai = args.only in ("all", "genai")
    ok_llm = _healthz(BASE_URL, "llm") if need_llm else True
    ok_genai = _healthz(GENAI_URL, "genai") if need_genai else True
    print()
    if not (ok_llm and ok_genai):
        sys.exit("One or more listeners unreachable. Is the service up (AI_SIM_ROLE=both) "
                 "and are BOTH ports open in the firewall?")

    n = 0
    try:
        while True:
            n += 1
            print(f"round {n}:")
            if need_llm:
                llm_round()
            if need_genai:
                genai_round()
            print(f"  totals: ok={STATS['ok']} err={STATS['err']} "
                  f"(llm={STATS['llm']} genai={STATS['genai']})\n")
            if args.rounds and n >= args.rounds:
                break
            time.sleep(args.delay)
    except KeyboardInterrupt:
        print("\nstopped.")
    print(f"done. ok={STATS['ok']} err={STATS['err']} "
          f"llm={STATS['llm']} genai={STATS['genai']}")


if __name__ == "__main__":
    main()
