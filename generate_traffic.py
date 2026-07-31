#!/usr/bin/env python3
"""
generate_traffic.py — drive realistic LLM + GenAI traffic at the AI-Sim server
so an API sensor (Akamai / Noname) learns the endpoints and applies its
"LLM" and "GenAI" insight tags.

Run it FROM A DIFFERENT HOST than the server (e.g. your Mac) so the traffic
crosses the LAN interface the sensor watches — not loopback.

Usage:
    python3 generate_traffic.py                         # defaults below, runs forever
    BASE_URL=http://192.168.1.102:8011 python3 generate_traffic.py
    python3 generate_traffic.py --rounds 50 --delay 1.0
    python3 generate_traffic.py --only llm             # llm | genai | all
    AI_SIM_AUTH_TOKEN=xxxx python3 generate_traffic.py  # if the server gates on a token

Each round sends a varied mix so the schema the sensor learns is rich:
prompts/messages + model params + token-usage for LLM, and image/audio/video
generation bodies with vendor/model naming for GenAI.
"""

import argparse
import os
import random
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

BASE_URL = os.environ.get("BASE_URL", "http://192.168.1.102:8011").rstrip("/")
AUTH_TOKEN = os.environ.get("AI_SIM_AUTH_TOKEN", "").strip()

SESSION = requests.Session()
if AUTH_TOKEN:
    SESSION.headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
SESSION.headers["Content-Type"] = "application/json"

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

STATS = {"ok": 0, "err": 0}


# ── vendor request headers ──────────────────────────────────────────────────────
# One of the LLM/GenAI classifier signals is "known vendor header patterns", so we
# send the same recognizable auth/version headers real provider SDKs send. These
# are FAKE, correctly-shaped credentials for a lab — not real keys.
#
# NOTE: if the app's own AI_SIM_AUTH_TOKEN gate is enabled, we must not clobber the
# Authorization header the app expects, so vendor Authorization is only added when
# the gate is OFF (the recommended demo setup).
def _rand(n):
    import string
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


def _vendor_headers(vendor):
    h = {}
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
    elif vendor == "stability":
        if not AUTH_TOKEN:
            h["Authorization"] = "Bearer sk-" + _rand(48)
        h["Accept"] = "application/json"
        h["User-Agent"] = "stability-sdk/0.8.5"
    elif vendor == "replicate":
        if not AUTH_TOKEN:
            h["Authorization"] = "Token r8_" + _rand(37)
        h["User-Agent"] = "replicate-python/0.34.0"
    elif vendor == "elevenlabs":
        h["xi-api-key"] = _rand(32)
        h["User-Agent"] = "elevenlabs-python/1.0.0"
    return h


def _hit(method, path, vendor="openai", **kw):
    url = BASE_URL + path
    headers = _vendor_headers(vendor)
    try:
        r = SESSION.request(method, url, timeout=10, headers=headers, **kw)
        ok = r.status_code < 400
        STATS["ok" if ok else "err"] += 1
        tag = "LLM " if _is_llm(path) else "GenAI"
        print(f"  [{tag}] {method:4} {path:48} -> {r.status_code}")
        return r
    except Exception as e:
        STATS["err"] += 1
        print(f"  [ERR ] {method:4} {path:48} -> {e}")
        return None


def _is_llm(path):
    return any(
        s in path
        for s in ("chat/completions", "/completions", "/embeddings", "/models", "/messages", "/deployments/")
    )


# ── LLM traffic ─────────────────────────────────────────────────────────────────
def llm_round():
    prompt = random.choice(PROMPTS)

    _hit("POST", "/v1/chat/completions", json={
        "model": random.choice(CHAT_MODELS),
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt},
        ],
        "temperature": round(random.uniform(0, 1), 2),
        "max_tokens": random.choice([128, 256, 512]),
    })

    _hit("POST", "/v1/completions", json={
        "model": "gpt-3.5-turbo-instruct",
        "prompt": prompt,
        "max_tokens": 64,
        "temperature": 0.7,
    })

    _hit("POST", "/v1/embeddings", json={
        "model": random.choice(EMBED_MODELS),
        "input": random.sample(PROMPTS, k=random.randint(1, 3)),
    })

    _hit("POST", "/v1/messages", vendor="anthropic", json={
        "model": random.choice(ANTHROPIC_MODELS),
        "max_tokens": 512,
        "messages": [{"role": "user", "content": prompt}],
    })

    dep = random.choice(["gpt-4o-prod", "gpt35-eastus"])
    _hit("POST", f"/openai/deployments/{dep}/chat/completions", vendor="azure", json={
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.5,
    })

    _hit("GET", "/v1/models")


# ── GenAI traffic ───────────────────────────────────────────────────────────────
def genai_round():
    _hit("POST", "/v1/images/generations", json={
        "model": "dall-e-3",
        "prompt": random.choice(IMAGE_PROMPTS),
        "n": 1,
        "size": random.choice(["1024x1024", "1792x1024"]),
        "quality": "hd",
    })

    _hit("POST", "/v1/images/edits", json={
        "model": "dall-e-2",
        "prompt": "add a rainbow in the background",
    })

    _hit("POST", "/v1/audio/speech", json={
        "model": "tts-1",
        "voice": random.choice(["alloy", "nova", "shimmer"]),
        "input": random.choice(PROMPTS),
    })

    _hit("POST", "/v1/audio/transcriptions", json={
        "model": "whisper-1",
        "language": "en",
    })

    engine = random.choice(["stable-diffusion-xl-1024-v1-0", "stable-diffusion-v1-6"])
    _hit("POST", f"/v1/generation/{engine}/text-to-image", vendor="stability", json={
        "text_prompts": [{"text": random.choice(IMAGE_PROMPTS)}],
        "cfg_scale": 7,
        "steps": 30,
    })

    _hit("POST", "/v1/predictions", vendor="replicate", json={
        "version": "a9758cb...sdxl",
        "input": {"prompt": random.choice(IMAGE_PROMPTS), "width": 1024, "height": 1024},
    })

    voice_id = random.choice(["21m00Tcm4TlvDq8ikWAM", "AZnzlk1XvdvUeBnXmlld"])
    _hit("POST", f"/v1/text-to-speech/{voice_id}", vendor="elevenlabs", json={
        "text": random.choice(PROMPTS),
        "model_id": "eleven_multilingual_v2",
    })

    _hit("POST", "/v1/video/generations", json={
        "model": "sora-1.0",
        "prompt": random.choice(VIDEO_PROMPTS),
        "duration": random.choice([5, 10]),
        "resolution": "1280x720",
    })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=0, help="0 = run forever")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between rounds")
    ap.add_argument("--only", choices=["llm", "genai", "all"], default="all")
    args = ap.parse_args()

    print(f"AI-Sim traffic generator -> {BASE_URL}")
    print(f"mode={args.only}  rounds={args.rounds or 'infinite'}  delay={args.delay}s")
    if AUTH_TOKEN:
        print("auth: Bearer token set")
    print("-" * 60)

    # quick reachability check
    try:
        h = SESSION.get(BASE_URL + "/healthz", timeout=5)
        print(f"healthz -> {h.status_code} {h.text!r}\n")
    except Exception as e:
        sys.exit(f"Cannot reach {BASE_URL} (/healthz): {e}\n"
                 f"Is the service up and the firewall open on its port?")

    n = 0
    try:
        while True:
            n += 1
            print(f"round {n}:")
            if args.only in ("all", "llm"):
                llm_round()
            if args.only in ("all", "genai"):
                genai_round()
            print(f"  totals: ok={STATS['ok']} err={STATS['err']}\n")
            if args.rounds and n >= args.rounds:
                break
            time.sleep(args.delay)
    except KeyboardInterrupt:
        print("\nstopped.")
    print(f"done. ok={STATS['ok']} err={STATS['err']}")


if __name__ == "__main__":
    main()
