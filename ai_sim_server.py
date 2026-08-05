"""
AI-Sim Server — LLM + GenAI API simulator
==========================================
A standalone HTTP service that mimics real LLM and GenAI provider APIs so an
API sensor (e.g. Akamai / Noname) will learn the endpoints and auto-apply its
"LLM" and "GenAI" insight tags after observing traffic.

It runs the same way as the crAPI MCP box: env file + systemd unit + uvicorn,
single file, no external app behind it. Nothing here does real inference — every
response is a realistic, schema-accurate stub.


WHAT CHANGED IN THIS REVISION (and why GenAI wasn't tagging)
------------------------------------------------------------
The previous build served every endpoint on ONE listener and stamped OpenAI LLM
vendor headers (openai-version, openai-processing-ms) on EVERY response —
including Stability, Replicate, ElevenLabs and Sora. The classifier accumulates
score per API and emits a single final classification, so the LLM signal won
everywhere and GenAI never surfaced. Three fixes:

  1. SPLIT LISTENERS. LLM endpoints serve on AI_SIM_PORT (8011); GenAI endpoints
     serve on AI_SIM_GENAI_PORT (8012). Two distinct host:port API entities means
     they are scored independently and cannot suppress each other.

  2. PATH-AWARE VENDOR HEADERS. Each handler emits only headers its real vendor
     would send. No openai-* markers ride on media responses anymore.

  3. PARSEABLE MEDIA BODIES. TTS endpoints previously returned raw audio/mpeg
     bytes, which sensors commonly drop as unparseable ("ignored traffic") — so
     those transactions contributed nothing. They now return a JSON envelope
     carrying b64 audio plus explicit media_type / mime_type fields. Set
     AI_SIM_AUDIO_JSON=0 to restore raw-bytes realism.

Also fixed: request bodies are now always drained (unread bodies can reset
keep-alive connections and cost you captured transactions), the auth gate
returns a real 401 instead of a 200 with an error body, and unknown paths
return 404 instead of 200.


Tagging criteria these bodies target
------------------------------------
Noname derives the tags from the API URL, query params, and request/response
BODY, so the bodies below are shaped to carry the exact signals it looks for:

  LLM  -> prompts/messages + model parameters in the request, and
          completion/output text + token-usage fields in the response, on
          recognizable vendor endpoints (OpenAI /v1/chat/completions,
          /v1/completions, /v1/embeddings; Anthropic /v1/messages;
          Azure OpenAI /openai/deployments/.../chat/completions).

  GenAI -> image / audio / video GENERATION endpoints with recognizable vendor
          and model naming, and responses that name the medium explicitly
          (media_type, mime_type, output_format, width/height, duration).

Tags appear AFTER learning and may take a few minutes and repeated hits — use
generate_traffic.py to drive volume.
"""

import asyncio
import base64
import json
import os
import random
import re
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
            value = value.strip()
            # Strip a trailing inline comment on unquoted values. systemd's own
            # EnvironmentFile parser does NOT do this, so keep comments on their
            # own lines in ai-sim.env — this is belt-and-braces for hand launches.
            if value[:1] not in ('"', "'"):
                value = value.split(" #", 1)[0].split("\t#", 1)[0].strip()
            os.environ.setdefault(key.strip(), value.strip('"').strip("'"))


_load_env_file()


def _int_env(name, default):
    raw = str(os.environ.get(name, default)).strip().split()[0]
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name}={os.environ.get(name)!r} is not a port number "
                         f"(check for an inline comment in ai-sim.env)")


HOST = os.environ.get("AI_SIM_HOST", "0.0.0.0").split()[0]
PORT = _int_env("AI_SIM_PORT", 8011)              # LLM listener
GENAI_PORT = _int_env("AI_SIM_GENAI_PORT", 8012)  # GenAI listener
# both | llm | genai — which listeners this process should bring up.
ROLE = os.environ.get("AI_SIM_ROLE", "both").strip().lower()
# 1 = TTS returns a JSON envelope with b64 audio (classifier-friendly, default)
# 0 = TTS returns raw audio/mpeg bytes (more realistic, often dropped by sensors)
AUDIO_JSON = os.environ.get("AI_SIM_AUDIO_JSON", "1").strip() not in ("0", "false", "no")
# Optional shared-secret gate. If set, clients must send Authorization: Bearer <token>.
AUTH_TOKEN = os.environ.get("AI_SIM_AUTH_TOKEN", "").strip()


# ── Helpers ─────────────────────────────────────────────────────────────────────
_CD_NAME = re.compile(rb'name="([^"]*)"')
_CD_FILENAME = re.compile(rb'filename="([^"]*)"')
_CT_PART = re.compile(rb"Content-Type:\s*([^\r\n]+)", re.IGNORECASE)


def _parse_multipart(raw: bytes) -> dict:
    """
    Minimal multipart/form-data reader.

    Splits on the boundary taken from the body's own opening delimiter, so it
    needs no access to the Content-Type header. Text fields are returned as
    values; file parts contribute <name>_filename and <name>_content_type
    instead of their bytes, which keeps the echoed response body small while
    still naming the medium that was uploaded.
    """
    nl = raw.find(b"\r\n")
    if nl <= 0:
        return {}
    delim = raw[:nl]
    if not delim.startswith(b"--"):
        return {}

    out = {}
    for part in raw.split(delim):
        hdr_end = part.find(b"\r\n\r\n")
        if hdr_end == -1:
            continue
        head, body = part[:hdr_end], part[hdr_end + 4:]
        if body.endswith(b"\r\n"):
            body = body[:-2]
        m = _CD_NAME.search(head)
        if not m:
            continue
        name = m.group(1).decode("utf-8", "replace")

        fn = _CD_FILENAME.search(head)
        if fn:
            out[name + "_filename"] = fn.group(1).decode("utf-8", "replace")
            ct = _CT_PART.search(head)
            if ct:
                out[name + "_content_type"] = ct.group(1).decode("utf-8", "replace").strip()
            continue

        try:
            out[name] = body[:1024].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return out


async def _read_body(receive: Receive) -> dict:
    """
    Drain the full request body and parse it (best-effort).

    Always call this, even when the handler ignores the content: leaving a body
    unread on a keep-alive connection can make the server reset the socket, and a
    reset transaction is a transaction the sensor never gets to classify.

    Handles JSON and simple multipart/form-data (images/edits and
    audio/transcriptions are multipart in the real OpenAI API).
    """
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
        pass
    # multipart fallback — images/edits and audio/transcriptions are multipart
    try:
        return _parse_multipart(chunks)
    except Exception:
        return {}


def _tokens(*parts) -> int:
    text = " ".join(str(p) for p in parts)
    return max(1, len(text) // 4)


# ══════════════════════════════════════════════════════════════════════════════
# VENDOR RESPONSE HEADERS — scoped per vendor, NEVER shared across LLM/GenAI.
#
# This is the fix for the "everything tags LLM" problem: previously one helper
# stamped openai-version / openai-processing-ms onto every response, so image,
# audio and video endpoints were broadcasting an OpenAI *LLM* vendor signal.
# Each handler now emits only what its real vendor sends.
# ══════════════════════════════════════════════════════════════════════════════
def _rid():
    return "req_" + uuid.uuid4().hex[:24]


def _hdr(kind, extra=None):
    h = {"x-request-id": _rid()}

    if kind == "openai_llm":
        h.update({
            "openai-processing-ms": str(random.randint(80, 900)),
            "openai-version": "2020-10-01",
            "x-ratelimit-limit-tokens": "150000",
            "x-ratelimit-remaining-tokens": str(random.randint(1000, 149000)),
            "x-ratelimit-remaining-requests": str(random.randint(100, 5000)),
        })
    elif kind == "anthropic":
        h.update({
            "anthropic-version": "2023-06-01",
            "anthropic-organization-id": "org_" + uuid.uuid4().hex[:12],
            "anthropic-ratelimit-input-tokens-remaining": str(random.randint(1000, 40000)),
            "anthropic-ratelimit-output-tokens-remaining": str(random.randint(1000, 8000)),
            "request-id": _rid(),
        })
    elif kind == "azure":
        h.update({
            "apim-request-id": uuid.uuid4().hex,
            "x-ms-region": random.choice(["East US", "West Europe"]),
            "azureml-model-session": "d" + uuid.uuid4().hex[:12],
        })
    # ── GenAI side: media vendors only, no openai-* LLM markers ────────────────
    # Real OpenAI still stamps openai-version / openai-processing-ms on image
    # endpoints, but we deliberately omit them so the classifier cannot score
    # this host:port as another OpenAI LLM surface.
    elif kind == "openai_media":
        h.update({
            "x-generation-time-ms": str(random.randint(1200, 9000)),
            "x-media-type": "image",
            "x-image-model": random.choice(["dall-e-3", "dall-e-2"]),
        })
    elif kind == "openai_audio":
        h.update({
            "x-generation-time-ms": str(random.randint(300, 2500)),
            "x-media-type": "audio",
            "x-audio-model": random.choice(["tts-1", "tts-1-hd", "whisper-1"]),
        })
    elif kind == "stability":
        # Real Stability responses expose Finish-Reason + Seed headers.
        seed = str(random.randint(1, 2**31 - 1))
        h.update({
            "x-generation-time-ms": str(random.randint(1500, 9000)),
            "x-media-type": "image",
            "Finish-Reason": "SUCCESS",
            "finish-reason": "SUCCESS",
            "Seed": seed,
            "seed": seed,
            "x-stability-engine": "stable-diffusion-xl-1024-v1-0",
        })
    elif kind == "replicate":
        h.update({
            "x-media-type": "image",
            "replicate-prediction-id": uuid.uuid4().hex,
            "replicate-model": "stability-ai/sdxl",
            "Prefer": "wait",
        })
    elif kind == "elevenlabs":
        # Real ElevenLabs SDKs expose x-character-count / character-cost.
        cost = str(random.randint(20, 400))
        h.update({
            "x-media-type": "audio",
            "x-character-count": cost,
            "character-cost": cost,
            "xi-character-cost": cost,
            "history-item-id": uuid.uuid4().hex[:20],
            "request-id": _rid(),
        })
    elif kind == "video":
        h.update({
            "x-generation-time-ms": str(random.randint(8000, 45000)),
            "x-media-type": "video",
            "x-video-model": "sora-1.0",
        })

    if extra:
        h.update(extra)
    return h


def _json(data, kind="openai_llm", headers=None, status=200):
    return JSONResponse(data, status_code=status, headers=_hdr(kind, headers))


# tiny valid 1x1 PNG (so image responses can carry real b64 image data)
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _fake_mp3(n=512):
    return b"ID3\x03\x00\x00\x00" + os.urandom(n)


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
    await _json(resp, "openai_llm")(scope, receive, send)


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
    await _json(resp, "openai_llm")(scope, receive, send)


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
    await _json(resp, "openai_llm")(scope, receive, send)


async def openai_models(scope, receive, send):
    """GET /v1/models — model catalog (text models only; media models live on the GenAI port)."""
    await _read_body(receive)
    now = int(time.time())
    models = CHAT_MODELS + ["gpt-3.5-turbo-instruct", "text-embedding-3-small", "text-embedding-3-large"]
    resp = {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": now, "owned_by": "openai"}
            for m in models
        ],
    }
    await _json(resp, "openai_llm")(scope, receive, send)


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
    await _json(resp, "anthropic")(scope, receive, send)


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
    await _json(resp, "azure")(scope, receive, send)


# ══════════════════════════════════════════════════════════════════════════════
# GenAI ENDPOINTS  (image / audio / video generation, vendor + model naming)
#
# Every response names the medium explicitly — media_type, mime_type,
# output_format, width/height, duration_seconds — so a body-reading classifier
# has unambiguous non-text-generation signal to score.
# ══════════════════════════════════════════════════════════════════════════════
async def openai_images_generations(scope, receive, send):
    """POST /v1/images/generations — OpenAI DALL·E image generation."""
    body = await _read_body(receive)
    model = body.get("model", "dall-e-3")
    n = int(body.get("n", 1) or 1)
    size = body.get("size", "1024x1024")
    try:
        w, h = (int(x) for x in str(size).lower().split("x"))
    except Exception:
        w, h = 1024, 1024
    resp = {
        "created": int(time.time()),
        "model": model,
        "media_type": "image",
        "mime_type": "image/png",
        "output_format": "png",
        "size": size,
        "width": w,
        "height": h,
        "data": [
            {
                "revised_prompt": body.get("prompt", ""),
                "url": f"https://cdn.example-genai.com/img/{uuid.uuid4().hex}.png",
                "b64_json": base64.b64encode(_PNG_1x1).decode(),
                "content_type": "image/png",
            }
            for _ in range(max(1, n))
        ],
    }
    await _json(resp, "openai_media")(scope, receive, send)


async def openai_images_edits(scope, receive, send):
    """POST /v1/images/edits — image-to-image editing (multipart in the real API)."""
    body = await _read_body(receive)
    resp = {
        "created": int(time.time()),
        "model": body.get("model", "dall-e-2"),
        "media_type": "image",
        "mime_type": "image/png",
        "output_format": "png",
        "operation": "image_edit",
        "prompt": body.get("prompt", ""),
        "source_image": body.get("image_filename", ""),
        "source_content_type": body.get("image_content_type", "image/png"),
        "data": [
            {
                "url": f"https://cdn.example-genai.com/edit/{uuid.uuid4().hex}.png",
                "b64_json": base64.b64encode(_PNG_1x1).decode(),
                "content_type": "image/png",
            }
        ],
    }
    await _json(resp, "openai_media")(scope, receive, send)


async def openai_audio_speech(scope, receive, send):
    """
    POST /v1/audio/speech — text-to-speech.

    Real OpenAI returns raw audio/mpeg. Sensors frequently classify binary media
    responses as ignored/unparseable traffic, so by default we wrap the audio in
    a JSON envelope that still says "this is generated audio" in the body.
    Set AI_SIM_AUDIO_JSON=0 for byte-accurate realism.
    """
    body = await _read_body(receive)
    audio = _fake_mp3()
    if not AUDIO_JSON:
        await Response(
            content=audio,
            media_type="audio/mpeg",
            headers=_hdr("openai_audio", {"content-disposition": "attachment; filename=speech.mp3"}),
        )(scope, receive, send)
        return
    resp = {
        "object": "audio.speech",
        "created": int(time.time()),
        "model": body.get("model", "tts-1"),
        "voice": body.get("voice", "alloy"),
        "media_type": "audio",
        "mime_type": "audio/mpeg",
        "output_format": "mp3",
        "input_text": body.get("input", ""),
        "duration_seconds": round(random.uniform(1.5, 12.0), 2),
        "b64_audio": base64.b64encode(audio).decode(),
    }
    await _json(resp, "openai_audio")(scope, receive, send)


async def openai_audio_transcriptions(scope, receive, send):
    """POST /v1/audio/transcriptions — Whisper speech-to-text (multipart in the real API)."""
    body = await _read_body(receive)
    resp = {
        "task": "transcribe",
        "language": body.get("language", "english"),
        "duration": round(random.uniform(2, 30), 2),
        "text": "This is a mock transcription produced by the whisper-1 model.",
        "model": body.get("model", "whisper-1"),
        "media_type": "audio",
        "mime_type": body.get("file_content_type", "audio/mpeg"),
        "source_file": body.get("file_filename", ""),
    }
    await _json(resp, "openai_audio")(scope, receive, send)


async def stability_text_to_image(scope, receive, send):
    """POST /v1/generation/{engine}/text-to-image — Stability AI style."""
    body = await _read_body(receive)
    prompts = body.get("text_prompts") or []
    text = prompts[0].get("text", "") if prompts and isinstance(prompts[0], dict) else ""
    resp = {
        "artifacts": [
            {
                "base64": base64.b64encode(_PNG_1x1).decode(),
                "seed": random.randint(1, 2**32),
                "finishReason": "SUCCESS",
                "mime_type": "image/png",
            }
        ],
        "engine": "stable-diffusion-xl-1024-v1-0",
        "media_type": "image",
        "mime_type": "image/png",
        "output_format": "png",
        "prompt": text,
        "width": 1024,
        "height": 1024,
    }
    await _json(resp, "stability")(scope, receive, send)


async def replicate_predictions(scope, receive, send):
    """POST /v1/predictions — Replicate-style model run (image/video)."""
    body = await _read_body(receive)
    pid = uuid.uuid4().hex
    resp = {
        "id": pid,
        "version": "a9758cb...sdxl",
        "model": "stability-ai/sdxl",
        "status": "succeeded",
        "input": body.get("input", {}),
        "output": [f"https://replicate.delivery/pbxt/{pid}/out-0.png"],
        "media_type": "image",
        "mime_type": "image/png",
        "output_format": "png",
        "metrics": {"predict_time": round(random.uniform(1, 8), 2)},
    }
    await _json(resp, "replicate")(scope, receive, send)


async def elevenlabs_tts(scope, receive, send):
    """POST /v1/text-to-speech/{voice_id} — ElevenLabs style TTS."""
    body = await _read_body(receive)
    audio = _fake_mp3()
    if not AUDIO_JSON:
        await Response(
            content=audio,
            media_type="audio/mpeg",
            headers=_hdr("elevenlabs"),
        )(scope, receive, send)
        return
    resp = {
        "object": "text_to_speech",
        "model_id": body.get("model_id", "eleven_multilingual_v2"),
        "voice_id": scope.get("path", "").rsplit("/", 1)[-1],
        "media_type": "audio",
        "mime_type": "audio/mpeg",
        "output_format": "mp3_44100_128",
        "text": body.get("text", ""),
        "duration_seconds": round(random.uniform(1.5, 15.0), 2),
        "b64_audio": base64.b64encode(audio).decode(),
    }
    await _json(resp, "elevenlabs")(scope, receive, send)


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
        "media_type": "video",
        "mime_type": "video/mp4",
        "output_format": "mp4",
        "data": [
            {
                "url": f"https://cdn.example-genai.com/video/{uuid.uuid4().hex}.mp4",
                "duration_seconds": body.get("duration", 5),
                "resolution": body.get("resolution", "1280x720"),
                "fps": 24,
                "content_type": "video/mp4",
            }
        ],
    }
    await _json(resp, "video")(scope, receive, send)


async def genai_models(scope, receive, send):
    """GET /v1/models — media model catalog, served on the GenAI listener only."""
    await _read_body(receive)
    now = int(time.time())
    models = ["dall-e-3", "dall-e-2", "tts-1", "tts-1-hd", "whisper-1",
              "sora-1.0", "stable-diffusion-xl-1024-v1-0", "eleven_multilingual_v2"]
    resp = {
        "object": "list",
        "media_type": "image,audio,video",
        "data": [
            {"id": m, "object": "model", "created": now, "owned_by": "genai", "modality": "media"}
            for m in models
        ],
    }
    await _json(resp, "openai_media")(scope, receive, send)


# ══════════════════════════════════════════════════════════════════════════════
# Routing — two separate tables so the two listeners are genuinely distinct APIs
# ══════════════════════════════════════════════════════════════════════════════
LLM_EXACT = {
    ("POST", "/v1/chat/completions"): openai_chat_completions,
    ("POST", "/v1/completions"): openai_completions,
    ("POST", "/v1/embeddings"): openai_embeddings,
    ("GET", "/v1/models"): openai_models,
    ("POST", "/v1/messages"): anthropic_messages,
}

GENAI_EXACT = {
    ("POST", "/v1/images/generations"): openai_images_generations,
    ("POST", "/v1/images/edits"): openai_images_edits,
    ("POST", "/v1/audio/speech"): openai_audio_speech,
    ("POST", "/v1/audio/transcriptions"): openai_audio_transcriptions,
    ("POST", "/v1/predictions"): replicate_predictions,
    ("POST", "/v1/video/generations"): video_generations,
    ("GET", "/v1/models"): genai_models,
}


def _llm_prefix(method, path):
    if method == "POST" and path.startswith("/openai/deployments/") and path.endswith("/chat/completions"):
        return azure_chat_completions
    return None


def _genai_prefix(method, path):
    if method == "POST" and path.startswith("/v1/generation/") and path.endswith("/text-to-image"):
        return stability_text_to_image
    if method == "POST" and path.startswith("/v1/text-to-speech/"):
        return elevenlabs_tts
    return None


def make_app(exact_routes, prefix_resolver, label):
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
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
            await PlainTextResponse(f"ok {label}")(scope, receive, send)
            return

        # Optional bearer gate (kept off by default; these are meant to be scanned).
        if AUTH_TOKEN:
            headers = dict(scope.get("headers") or [])
            provided = headers.get(b"authorization", b"").decode()
            if provided != f"Bearer {AUTH_TOKEN}":
                await _read_body(receive)
                await JSONResponse(
                    {"error": {"message": "unauthorized", "type": "invalid_request_error",
                               "code": "invalid_api_key"}},
                    status_code=401,
                    headers={"x-request-id": _rid(), "www-authenticate": "Bearer"},
                )(scope, receive, send)
                return

        handler = exact_routes.get((method, path)) or prefix_resolver(method, path)
        if handler:
            await handler(scope, receive, send)
            return

        # Drain before replying so the connection stays clean for the next request.
        await _read_body(receive)
        await JSONResponse(
            {"error": {"message": f"Unknown endpoint on {label} listener",
                       "type": "invalid_request_error", "code": "unknown_url"}},
            status_code=404,
            headers={"x-request-id": _rid()},
        )(scope, receive, send)

    return app


llm_app = make_app(LLM_EXACT, _llm_prefix, "llm")
genai_app = make_app(GENAI_EXACT, _genai_prefix, "genai")

# Backwards compatibility: anything importing asgi_app gets the LLM listener.
asgi_app = llm_app


async def _serve():
    servers = []
    if ROLE in ("both", "llm"):
        servers.append(uvicorn.Server(uvicorn.Config(
            llm_app, host=HOST, port=PORT, workers=1, log_level="info")))
    if ROLE in ("both", "genai"):
        servers.append(uvicorn.Server(uvicorn.Config(
            genai_app, host=HOST, port=GENAI_PORT, workers=1, log_level="info")))
    if not servers:
        raise SystemExit(f"AI_SIM_ROLE={ROLE!r} is not one of: both, llm, genai")
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    print(f"AI-Sim starting  role={ROLE}  llm=:{PORT}  genai=:{GENAI_PORT}  "
          f"audio_json={AUDIO_JSON}  auth={'on' if AUTH_TOKEN else 'off'}")
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
