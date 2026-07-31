"""
AI-Sim Server — LLM + GenAI API simulator
==========================================
A standalone HTTP service that mimics real LLM and GenAI provider APIs so an
API sensor (e.g. Akamai / Noname) will learn the endpoints and auto-apply its
"LLM" and "GenAI" insight tags after observing traffic.

It runs the same way as the crAPI MCP box: env file + systemd unit + uvicorn,
single file, no external app behind it. Nothing here does real inference — every
response is a realistic, schema-accurate stub.

Why the responses look the way they do (Noname tagging criteria)
----------------------------------------------------------------
Noname derives the tags from the API URL, query params, and request/response
BODY, so the bodies below are shaped to carry the exact signals it looks for:

  LLM  -> prompts/messages + model parameters in the request, and
          completion/output text + token-usage fields in the response, on
          recognizable vendor endpoints (OpenAI /v1/chat/completions,
          /v1/completions, /v1/embeddings; Anthropic /v1/messages;
          Azure OpenAI /openai/deployments/.../chat/completions).

  GenAI -> image / audio / video GENERATION endpoints with recognizable vendor
          and model naming (OpenAI images/audio, Stability text-to-image,
          Replicate predictions, ElevenLabs TTS, Runway/Sora-style video).

Response headers also advertise vendor-ish markers (openai-*, x-request-id) to
reinforce the vendor pattern signal. Tags appear AFTER learning and may take a
few minutes and repeated hits — use generate_traffic.py to drive volume.
"""

import base64
import json
import os
import random
import time
import uuid

import uvicorn
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import Receive, Scope, Send


# ── Config (env file, same pattern as the MCP box) ──────────────────────────────
def _load_env_file():
    path = os.environ.get("AI_SIM_ENV") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "ai-sim.env"
    )
    if not os.path.isfile(path):
        return
    with open(path) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, sep, value = line.partition("=")
            if not sep:
                continue
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()

HOST = os.environ.get("AI_SIM_HOST", "0.0.0.0")
PORT = int(os.environ.get("AI_SIM_PORT", "8011"))
# Optional shared-secret gate. If set, clients must send Authorization: Bearer <token>.
AUTH_TOKEN = os.environ.get("AI_SIM_AUTH_TOKEN", "").strip()


# ── Helpers ─────────────────────────────────────────────────────────────────────
async def _read_body(receive: Receive) -> dict:
    """Collect the full request body and parse JSON (best-effort)."""
    chunks = b""
    more = True
    while more:
        msg = await receive()
        if msg["type"] == "http.request":
            chunks += msg.get("body", b"")
            more = msg.get("more_body", False)
        else:
            more = False
    if not chunks:
        return {}
    try:
        return json.loads(chunks)
    except Exception:
        return {}


def _tokens(*parts) -> int:
    text = " ".join(str(p) for p in parts)
    return max(1, len(text) // 4)


def _vendor_headers(extra=None):
    """Vendor-ish response headers that reinforce the LLM/GenAI vendor signal."""
    h = {
        "x-request-id": "req_" + uuid.uuid4().hex[:24],
        "openai-processing-ms": str(random.randint(80, 900)),
        "openai-version": "2020-10-01",
        "x-ratelimit-remaining-requests": str(random.randint(100, 5000)),
    }
    if extra:
        h.update(extra)
    return h


def _json(data, headers=None):
    return JSONResponse(data, headers=_vendor_headers(headers))


# tiny valid 1x1 PNG (so image responses can carry real b64 image data)
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


# ══════════════════════════════════════════════════════════════════════════════
# LLM ENDPOINTS  (prompts/messages + model params in, completion + token usage out)
# ══════════════════════════════════════════════════════════════════════════════
CHAT_MODELS = ["gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo"]
ANTHROPIC_MODELS = ["claude-3-5-sonnet-20241022", "claude-3-opus-20240229"]

_SAMPLE_COMPLETIONS = [
    "Here is a concise summary of the key points you asked about.",
    "Sure — the main trade-offs are latency, cost, and accuracy.",
    "To reset the widget, call reset() then re-initialize the client.",
    "The three leading causes are configuration drift, quota limits, and DNS.",
]


async def openai_chat_completions(scope, receive, send):
    """POST /v1/chat/completions — OpenAI-style chat (primary LLM signal)."""
    body = await _read_body(receive)
    model = body.get("model", "gpt-4o")
    messages = body.get("messages", [])
    prompt_text = " ".join(m.get("content", "") for m in messages if isinstance(m, dict))
    completion = random.choice(_SAMPLE_COMPLETIONS)
    pt = _tokens(prompt_text)
    ct = _tokens(completion)
    resp = {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": completion},
                "logprobs": None,
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
        },
        "system_fingerprint": "fp_" + uuid.uuid4().hex[:10],
    }
    await _json(resp)(scope, receive, send)


async def openai_completions(scope, receive, send):
    """POST /v1/completions — OpenAI-style legacy text completion."""
    body = await _read_body(receive)
    model = body.get("model", "gpt-3.5-turbo-instruct")
    prompt = body.get("prompt", "")
    completion = " " + random.choice(_SAMPLE_COMPLETIONS)
    pt = _tokens(prompt)
    ct = _tokens(completion)
    resp = {
        "id": "cmpl-" + uuid.uuid4().hex[:24],
        "object": "text_completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {"text": completion, "index": 0, "logprobs": None, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
    }
    await _json(resp)(scope, receive, send)


async def openai_embeddings(scope, receive, send):
    """POST /v1/embeddings — OpenAI-style embeddings."""
    body = await _read_body(receive)
    model = body.get("model", "text-embedding-3-small")
    inp = body.get("input", "")
    items = inp if isinstance(inp, list) else [inp]
    data = []
    for i, _ in enumerate(items):
        vec = [round(random.uniform(-1, 1), 6) for _ in range(1536)]
        data.append({"object": "embedding", "index": i, "embedding": vec})
    pt = _tokens(*items)
    resp = {
        "object": "list",
        "data": data,
        "model": model,
        "usage": {"prompt_tokens": pt, "total_tokens": pt},
    }
    await _json(resp)(scope, receive, send)


async def openai_models(scope, receive, send):
    """GET /v1/models — model catalog."""
    now = int(time.time())
    models = CHAT_MODELS + ["text-embedding-3-small", "dall-e-3", "tts-1", "whisper-1"]
    resp = {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": now, "owned_by": "openai"}
            for m in models
        ],
    }
    await _json(resp)(scope, receive, send)


async def anthropic_messages(scope, receive, send):
    """POST /v1/messages — Anthropic-style Messages API (LLM vendor variety)."""
    body = await _read_body(receive)
    model = body.get("model", "claude-3-5-sonnet-20241022")
    messages = body.get("messages", [])
    prompt_text = " ".join(
        (m.get("content") if isinstance(m.get("content"), str) else "")
        for m in messages
        if isinstance(m, dict)
    )
    completion = random.choice(_SAMPLE_COMPLETIONS)
    it = _tokens(prompt_text)
    ot = _tokens(completion)
    resp = {
        "id": "msg_" + uuid.uuid4().hex[:24],
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": completion}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": it, "output_tokens": ot},
    }
    await _json(resp, headers={"anthropic-version": "2023-06-01"})(scope, receive, send)


async def azure_chat_completions(scope, receive, send):
    """POST /openai/deployments/{deployment}/chat/completions — Azure OpenAI style."""
    body = await _read_body(receive)
    messages = body.get("messages", [])
    prompt_text = " ".join(m.get("content", "") for m in messages if isinstance(m, dict))
    completion = random.choice(_SAMPLE_COMPLETIONS)
    pt = _tokens(prompt_text)
    ct = _tokens(completion)
    resp = {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "gpt-4o",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": completion},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
    }
    await _json(resp)(scope, receive, send)


# ══════════════════════════════════════════════════════════════════════════════
# GenAI ENDPOINTS  (image / audio / video generation, vendor + model naming)
# ══════════════════════════════════════════════════════════════════════════════
async def openai_images_generations(scope, receive, send):
    """POST /v1/images/generations — OpenAI DALL·E image generation."""
    body = await _read_body(receive)
    model = body.get("model", "dall-e-3")
    n = int(body.get("n", 1))
    size = body.get("size", "1024x1024")
    resp = {
        "created": int(time.time()),
        "model": model,
        "data": [
            {
                "revised_prompt": body.get("prompt", ""),
                "url": f"https://cdn.example-genai.com/img/{uuid.uuid4().hex}.png",
            }
            for _ in range(max(1, n))
        ],
        "size": size,
    }
    await _json(resp)(scope, receive, send)


async def openai_images_edits(scope, receive, send):
    """POST /v1/images/edits — image-to-image editing."""
    resp = {
        "created": int(time.time()),
        "model": "dall-e-2",
        "data": [{"url": f"https://cdn.example-genai.com/edit/{uuid.uuid4().hex}.png"}],
    }
    await _json(resp)(scope, receive, send)


async def openai_audio_speech(scope, receive, send):
    """POST /v1/audio/speech — text-to-speech (returns audio bytes)."""
    await Response(
        content=b"ID3\x03\x00\x00\x00" + os.urandom(512),
        media_type="audio/mpeg",
        headers=_vendor_headers({"content-disposition": "attachment; filename=speech.mp3"}),
    )(scope, receive, send)


async def openai_audio_transcriptions(scope, receive, send):
    """POST /v1/audio/transcriptions — Whisper speech-to-text."""
    resp = {
        "task": "transcribe",
        "language": "english",
        "duration": round(random.uniform(2, 30), 2),
        "text": "This is a mock transcription produced by the whisper-1 model.",
        "model": "whisper-1",
    }
    await _json(resp)(scope, receive, send)


async def stability_text_to_image(scope, receive, send):
    """POST /v1/generation/{engine}/text-to-image — Stability AI style."""
    resp = {
        "artifacts": [
            {
                "base64": base64.b64encode(_PNG_1x1).decode(),
                "seed": random.randint(1, 2**32),
                "finishReason": "SUCCESS",
            }
        ],
        "engine": "stable-diffusion-xl-1024-v1-0",
    }
    await _json(resp)(scope, receive, send)


async def replicate_predictions(scope, receive, send):
    """POST /v1/predictions — Replicate-style model run (image/video)."""
    pid = uuid.uuid4().hex
    resp = {
        "id": pid,
        "version": "a9758cb...sdxl",
        "model": "stability-ai/sdxl",
        "status": "succeeded",
        "input": (await _read_body(receive)).get("input", {}),
        "output": [f"https://replicate.delivery/pbxt/{pid}/out-0.png"],
        "metrics": {"predict_time": round(random.uniform(1, 8), 2)},
    }
    await _json(resp)(scope, receive, send)


async def elevenlabs_tts(scope, receive, send):
    """POST /v1/text-to-speech/{voice_id} — ElevenLabs style TTS (audio bytes)."""
    await Response(
        content=b"ID3\x03\x00\x00\x00" + os.urandom(512),
        media_type="audio/mpeg",
        headers=_vendor_headers(),
    )(scope, receive, send)


async def video_generations(scope, receive, send):
    """POST /v1/video/generations — Sora / Runway style text-to-video."""
    body = await _read_body(receive)
    resp = {
        "id": "video_" + uuid.uuid4().hex[:20],
        "object": "video.generation",
        "created": int(time.time()),
        "model": body.get("model", "sora-1.0"),
        "status": "completed",
        "prompt": body.get("prompt", ""),
        "data": [
            {
                "url": f"https://cdn.example-genai.com/video/{uuid.uuid4().hex}.mp4",
                "duration_seconds": body.get("duration", 5),
                "resolution": body.get("resolution", "1280x720"),
            }
        ],
    }
    await _json(resp)(scope, receive, send)


# ── Routing table ───────────────────────────────────────────────────────────────
# (method, exact_path) -> handler.  Prefix routes handled separately below.
EXACT_ROUTES = {
    # LLM
    ("POST", "/v1/chat/completions"): openai_chat_completions,
    ("POST", "/v1/completions"): openai_completions,
    ("POST", "/v1/embeddings"): openai_embeddings,
    ("GET", "/v1/models"): openai_models,
    ("POST", "/v1/messages"): anthropic_messages,
    # GenAI
    ("POST", "/v1/images/generations"): openai_images_generations,
    ("POST", "/v1/images/edits"): openai_images_edits,
    ("POST", "/v1/audio/speech"): openai_audio_speech,
    ("POST", "/v1/audio/transcriptions"): openai_audio_transcriptions,
    ("POST", "/v1/predictions"): replicate_predictions,
    ("POST", "/v1/video/generations"): video_generations,
}


async def asgi_app(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return

    method = scope.get("method", "GET")
    path = scope.get("path", "")

    if path == "/healthz":
        await PlainTextResponse("ok")(scope, receive, send)
        return

    # Optional bearer gate (kept off by default; these are meant to be scanned).
    if AUTH_TOKEN:
        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"").decode()
        if provided != f"Bearer {AUTH_TOKEN}":
            await _json({"error": {"message": "unauthorized", "type": "invalid_request_error"}})(
                scope, receive, send
            )
            return

    handler = EXACT_ROUTES.get((method, path))
    if handler:
        await handler(scope, receive, send)
        return

    # Prefix routes (path params)
    if method == "POST" and path.startswith("/openai/deployments/") and path.endswith("/chat/completions"):
        await azure_chat_completions(scope, receive, send)
        return
    if method == "POST" and path.startswith("/v1/generation/") and path.endswith("/text-to-image"):
        await stability_text_to_image(scope, receive, send)
        return
    if method == "POST" and path.startswith("/v1/text-to-speech/"):
        await elevenlabs_tts(scope, receive, send)
        return

    await _json({"error": {"message": "Unknown endpoint", "type": "invalid_request_error"}})(
        scope, receive, send
    )


if __name__ == "__main__":
    uvicorn.run(asgi_app, host=HOST, port=PORT, workers=1)
