# AI-Sim Server — LLM + GenAI discovery demo for Noname

A standalone service that mimics real LLM and GenAI provider APIs so Akamai /
Noname will auto-apply its **LLM** and **GenAI** insight tags after it observes
traffic. It is completely separate from the crAPI MCP server — different app,
different port — so your MCP demo keeps working untouched and this adds the
AI tags alongside it.

```
Traffic generator (your Mac) ──HTTP over LAN──> AI-Sim box :8011 ──> (no backend; realistic stubs)
                                                192.168.1.102
        Noname sensor watches the LAN interface and learns /v1/* endpoints
```

Run it on the **same box** as your MCP server (different port, 8011) if you want
both API groups to appear under one host in Inventory, or on any box the sensor
watches.

---

## Why this produces the tags (how Noname decides)

Noname's AI insight tags are generated automatically **after API learning**, from
the API URL, query params, and request/response **body**. The endpoints here are
shaped to carry exactly the signals its docs describe:

- **LLM** — requests carry `messages`/`prompt` + model params (`model`,
  `temperature`, `max_tokens`); responses carry completion/output text +
  **token-usage** fields (`usage.prompt_tokens` / `completion_tokens` /
  `total_tokens`, and Anthropic's `input_tokens` / `output_tokens`), on
  recognizable vendor endpoints:
  - `POST /v1/chat/completions` (OpenAI)
  - `POST /v1/completions` (OpenAI legacy)
  - `POST /v1/embeddings` (OpenAI)
  - `GET  /v1/models`
  - `POST /v1/messages` (Anthropic)
  - `POST /openai/deployments/{deployment}/chat/completions` (Azure OpenAI)
- **GenAI** — image / audio / video **generation** endpoints with recognizable
  vendor + model naming:
  - `POST /v1/images/generations`, `POST /v1/images/edits` (DALL·E)
  - `POST /v1/audio/speech` (TTS), `POST /v1/audio/transcriptions` (Whisper)
  - `POST /v1/generation/{engine}/text-to-image` (Stability)
  - `POST /v1/predictions` (Replicate)
  - `POST /v1/text-to-speech/{voice_id}` (ElevenLabs)
  - `POST /v1/video/generations` (Sora / Runway style)

Response headers also advertise vendor-ish markers (`openai-version`,
`openai-processing-ms`, `anthropic-version`, `x-request-id`) to reinforce the
vendor pattern.

> Noname does not publish the exact pattern list, scoring thresholds, or minimum
> traffic volume. Tags appear **after** learning and may take a few minutes and
> repeated hits, and can shift as the schema evolves. That's why the generator
> sends a varied, sustained mix rather than one request each.

---

## 1. Put the files on the box

Copy `ai_sim_server.py`, `requirements.txt`, `ai-sim.env`, `ai-sim.service` to
the box, then:

```bash
sudo useradd --system --home /opt/ai-sim --shell /sbin/nologin aisim
sudo mkdir -p /opt/ai-sim
sudo cp ai_sim_server.py requirements.txt ai-sim.env /opt/ai-sim/
```

## 2. Python venv + dependencies

```bash
cd /opt/ai-sim
sudo python3.11 -m venv .venv         # any 3.9+ works
sudo .venv/bin/pip install --upgrade pip
sudo .venv/bin/pip install -r requirements.txt
sudo chown -R aisim:aisim /opt/ai-sim
```

## 3. Open the firewall (so the sensor and generator see it on the LAN)

```bash
sudo firewall-cmd --add-port=8011/tcp --permanent
sudo firewall-cmd --reload
```

## 4. Install + start the service

```bash
sudo cp ai-sim.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-sim
systemctl status ai-sim               # expect: active (running)

# smoke test on the box
curl -s http://127.0.0.1:8011/healthz                     # -> ok
curl -s -X POST http://127.0.0.1:8011/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}]}'
```

A JSON body with `choices[].message` and a `usage` block = working.

---

## 5. Generate traffic (this is what makes the tags appear)

Run the generator **from your Mac** (or any host other than the box) so the
traffic crosses the LAN interface the sensor watches — not loopback.

```bash
# point it at the box and let it run through the learning window
BASE_URL=http://192.168.1.102:8011 python3 generate_traffic.py

# other options
python3 generate_traffic.py --rounds 100 --delay 1.0   # bounded run
python3 generate_traffic.py --only llm                 # only LLM endpoints
python3 generate_traffic.py --only genai               # only GenAI endpoints
```

Each round sends 6 LLM + 8 GenAI requests with varied prompts, models, and
parameters. Leave it running ~10–15 minutes to cover the learning window; a
richer, repeated schema tags more reliably than a single burst.

Confirm the traffic is actually on the wire the sensor sees (on the box):

```bash
sudo tcpdump -i eth0 -nn 'tcp port 8011'
```

You should see `POST /v1/...` from your Mac's IP paired with `200`s. Silence =
loopback/wrong interface, and the sensor won't learn it.

---

## 6. Check Noname

In the Discovery Console, filter for the AI insight tags. After learning you
should see the `192.168.1.102:8011` endpoints populate:

- `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/messages`,
  `/openai/deployments/.../chat/completions` → **LLM**
- `/v1/images/*`, `/v1/audio/*`, `/v1/generation/.../text-to-image`,
  `/v1/predictions`, `/v1/text-to-speech/*`, `/v1/video/generations` → **GenAI**

If tags don't appear after sustained traffic:
- confirm `tcpdump` shows the `/v1/*` POSTs on the monitored interface,
- give the learning window more time and more rounds (schema/volume driven),
- check the API records exist untagged first (learning precedes tagging), then
  the AI insight tags follow.

## Troubleshooting: endpoints are in Inventory but not tagged LLM/GenAI

Observed state: all 17 `/v1/*` endpoints learned into Inventory under
`192.168.1.102:8011`, every row `In Progress` / `Not Detected`, no LLM or GenAI
insight tag. Per Noname SME guidance, discovery and tagging are two separate
layers: **an API appearing in Inventory does not guarantee either tag** — the
tags require learned traffic **plus matching classifier signals** in the
request/response body.

Unlike MCP (which is identified from explicit protocol headers like
`mcp-session-id`), the LLM/GenAI classifier reads the **body**. So the two things
that block it are:

1. **Body capture / "source limits."** Confirm the sensor is actually capturing
   request and response BODIES, not just URL + method + status. In the UI, open a
   sample transaction for `/v1/chat/completions` and verify you can see the
   request body (`messages`, `model`, `temperature`) and the response body
   (`choices`, `usage.total_tokens`). If those are blank or truncated, the
   classifier has nothing to match — enable full payload/body capture on the
   traffic source. This is the single most common blocker and it is a sensor
   config, not the app.

2. **Vendor signal strength + volume.** The generator now also sends recognizable
   vendor REQUEST headers (fake but correctly-shaped): OpenAI
   `Authorization: Bearer sk-proj-…` + `OpenAI-Organization`, Anthropic
   `x-api-key: sk-ant-api03-…` + `anthropic-version`, Azure `api-key`, Stability
   `Authorization: Bearer sk-…`, Replicate `Authorization: Token r8_…`,
   ElevenLabs `xi-api-key`. This directly targets the "known vendor header
   patterns" signal. Then drive real volume — the minimum sample count/threshold
   is not published, so run `--delay 0` and/or several parallel generators for
   10–15+ minutes.

Note on GenAI specifically: the audio/image endpoints return binary media
(`audio/mpeg`, image bytes). Noname may treat binary/media responses as *ignored*
traffic, which can suppress GenAI tagging even while the JSON-bodied LLM endpoints
tag fine. Check the **Processed vs Ignored** traffic view. The
chat/completions/embeddings/messages endpoints (clean JSON + token usage) are the
most reliable first tag.

If bodies are confirmed captured, vendor headers are present, and volume is high
but tags still never appear: per SME, collect a sanitized request/response pair,
the API URL/ID, tenant version, and timestamp and hand them to Support for
classifier pattern/threshold validation — the exact signatures and thresholds are
not publicly documented and may be tenant-specific.

---

## Files

- `ai_sim_server.py` — the service (LLM + GenAI endpoints; realistic stub bodies).
- `ai-sim.env` — host/port/optional auth token.
- `ai-sim.service` — systemd unit (mirrors the crapi-mcp unit).
- `requirements.txt` — starlette, uvicorn, requests.
- `generate_traffic.py` — traffic driver (run from a different host than the box).

## Endpoint reference

| Tag | Method | Path | Vendor shape |
|---|---|---|---|
| LLM | POST | `/v1/chat/completions` | OpenAI chat |
| LLM | POST | `/v1/completions` | OpenAI legacy |
| LLM | POST | `/v1/embeddings` | OpenAI embeddings |
| LLM | GET | `/v1/models` | OpenAI models |
| LLM | POST | `/v1/messages` | Anthropic messages |
| LLM | POST | `/openai/deployments/{deployment}/chat/completions` | Azure OpenAI |
| GenAI | POST | `/v1/images/generations` | OpenAI DALL·E |
| GenAI | POST | `/v1/images/edits` | OpenAI DALL·E edit |
| GenAI | POST | `/v1/audio/speech` | OpenAI TTS |
| GenAI | POST | `/v1/audio/transcriptions` | OpenAI Whisper |
| GenAI | POST | `/v1/generation/{engine}/text-to-image` | Stability AI |
| GenAI | POST | `/v1/predictions` | Replicate |
| GenAI | POST | `/v1/text-to-speech/{voice_id}` | ElevenLabs |
| GenAI | POST | `/v1/video/generations` | Sora / Runway |
