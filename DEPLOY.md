# AI-Sim Server — LLM + GenAI discovery demo for Noname

A standalone service that mimics real LLM and GenAI provider APIs so Akamai /
Noname will auto-apply its **LLM** and **GenAI** insight tags after it observes
traffic. It is completely separate from the crAPI MCP server — different app,
different ports — so your MCP demo keeps working untouched and this adds the
AI tags alongside it.

```
                             ┌─> :8011  LLM endpoints    ─┐
Traffic generator (your Mac) ┤                            ├─> (no backend; realistic stubs)
                             └─> :8012  GenAI endpoints  ─┘
                                  192.168.1.102

        Noname sensor watches the LAN interface and learns /v1/* on both ports
```

---

## Read this first: why GenAI wasn't tagging

If you ran the earlier single-port build and saw everything land under **LLM**
with no **GenAI** tag, that was not a traffic problem. `generate_traffic.py`
defaults to `--only all` and was already sending 8 GenAI requests per round. The
GenAI traffic was on the wire; it was being *scored* as LLM. Three causes, all
fixed in this revision:

**1. One API entity, one classification.** Everything served from a single
`host:8011` under `/v1/`. The classifier accumulates score per API and emits a
**single final classification** — it does not tag one API both LLM and GenAI. The
text-generation signal outscored the media signal and GenAI was suppressed.
→ *Fixed:* LLM now serves on **8011**, GenAI on **8012**. Two distinct host:port
API entities, scored independently.

**2. GenAI calls were wearing OpenAI LLM vendor headers.** `_hit()` defaulted to
`vendor="openai"`, so `/v1/images/generations`, `/v1/audio/*` and
`/v1/video/generations` all went out with `OpenAI-Beta: assistants=v2`,
`OpenAI-Organization`, and `User-Agent: OpenAI/Python`. Worse, the server's
`_vendor_headers()` stamped `openai-version` and `openai-processing-ms` on
**every** response — including Stability, Replicate, ElevenLabs and Sora. That is
textbook LLM vendor-pattern signal riding on image and video endpoints.
→ *Fixed:* headers are now per-vendor and path-aware on both sides. Media
responses carry `x-media-type`, `x-image-model` / `x-audio-model` /
`x-video-model`, `replicate-*`, `xi-*`, `x-stability-engine` — and zero `openai-*`
markers.

**3. Two GenAI endpoints had no parseable body, two had the wrong shape.**
`/v1/audio/speech` and `/v1/text-to-speech/{voice_id}` returned raw `audio/mpeg`
bytes, which sensors routinely file as ignored/unparseable traffic — those
transactions contributed nothing to scoring. And `/v1/images/edits` and
`/v1/audio/transcriptions` are `multipart/form-data` with file uploads in the
real OpenAI API, but were being sent as JSON, so they matched no known pattern.
→ *Fixed:* TTS returns a JSON envelope with `b64_audio` plus explicit
`media_type` / `mime_type` / `output_format` (set `AI_SIM_AUDIO_JSON=0` to
restore raw bytes), and the two multipart endpoints now send real multipart
bodies with actual file parts.

Also corrected along the way: request bodies are always drained (an unread body
can reset a keep-alive connection, costing you captured transactions), the auth
gate returns a real **401** instead of a 200 carrying an error object, and
unknown paths return **404** instead of 200 — 2xx-on-error muddies both the
"successful samples" the classifier wants and your own traffic stats.

> Note: `_is_llm()` in the old generator only labelled console output. It never
> influenced what the sensor saw — don't read anything into it.

---

## How the tags are decided

Noname's AI insight tags are generated automatically **after API learning**, from
the API URL, query params, and request/response **body**. The endpoints here are
shaped to carry exactly the signals its docs describe:

- **LLM** (port 8011) — requests carry `messages`/`prompt` + model params
  (`model`, `temperature`, `max_tokens`); responses carry completion/output text
  + **token-usage** fields (`usage.prompt_tokens` / `completion_tokens` /
  `total_tokens`, and Anthropic's `input_tokens` / `output_tokens`), on
  recognizable vendor endpoints:
  - `POST /v1/chat/completions` (OpenAI)
  - `POST /v1/completions` (OpenAI legacy)
  - `POST /v1/embeddings` (OpenAI)
  - `GET  /v1/models` (text models only)
  - `POST /v1/messages` (Anthropic)
  - `POST /openai/deployments/{deployment}/chat/completions` (Azure OpenAI)
- **GenAI** (port 8012) — image / audio / video **generation** endpoints with
  recognizable vendor + model naming, and responses that name the medium
  explicitly (`media_type`, `mime_type`, `output_format`, width/height,
  duration_seconds):
  - `POST /v1/images/generations`, `POST /v1/images/edits` (DALL·E)
  - `POST /v1/audio/speech` (TTS), `POST /v1/audio/transcriptions` (Whisper)
  - `POST /v1/generation/{engine}/text-to-image` (Stability)
  - `POST /v1/predictions` (Replicate)
  - `POST /v1/text-to-speech/{voice_id}` (ElevenLabs)
  - `POST /v1/video/generations` (Sora / Runway style)
  - `GET  /v1/models` (media models only)

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

## 3. Open the firewall — BOTH ports

```bash
sudo firewall-cmd --add-port=8011/tcp --permanent
sudo firewall-cmd --add-port=8012/tcp --permanent
sudo firewall-cmd --reload
```

Forgetting 8012 is the most likely way to reproduce the original symptom, since
the LLM half will keep working perfectly and look fine.

## 4. Install + start the service

```bash
sudo cp ai-sim.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-sim
systemctl status ai-sim               # expect: active (running)

# smoke test on the box — both listeners
curl -s http://127.0.0.1:8011/healthz                     # -> ok llm
curl -s http://127.0.0.1:8012/healthz                     # -> ok genai

curl -s -X POST http://127.0.0.1:8011/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}]}'

curl -s -X POST http://127.0.0.1:8012/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"dall-e-3","prompt":"a red fox","size":"1024x1024"}'
```

A JSON body with `choices[].message` and a `usage` block = LLM side working.
A JSON body with `media_type: "image"` and `mime_type: "image/png"` = GenAI side
working.

**Header hygiene check** — confirm the LLM markers are not leaking onto the
GenAI listener. This should print nothing:

```bash
curl -s -D - -o /dev/null -X POST http://127.0.0.1:8012/v1/images/generations \
  -H 'Content-Type: application/json' -d '{}' | grep -i 'openai-'
```

> One process serves both ports (`AI_SIM_ROLE=both`). To put them on two
> separate hosts — the strongest possible separation — set `AI_SIM_ROLE=llm` on
> one box and `AI_SIM_ROLE=genai` on the other.

---

## 5. Generate traffic (this is what makes the tags appear)

Run the generator **from your Mac** (or any host other than the box) so the
traffic crosses the LAN interface the sensor watches — not loopback.

```bash
# point it at the box; GenAI URL is derived as same-host:8012 automatically
BASE_URL=http://192.168.1.102:8011 python3 generate_traffic.py

# override the GenAI target explicitly (e.g. a second host)
BASE_URL=http://192.168.1.102:8011 GENAI_URL=http://192.168.1.103:8012 \
  python3 generate_traffic.py

# other options
python3 generate_traffic.py --rounds 100 --delay 1.0   # bounded run
python3 generate_traffic.py --only llm                 # only LLM endpoints
python3 generate_traffic.py --only genai               # only GenAI endpoints
```

Each round sends 6 LLM + 9 GenAI requests with varied prompts, models, and
parameters. The generator health-checks **both** listeners at startup and exits
if either is unreachable, so a missing firewall rule fails loudly instead of
silently halving your traffic.

Leave it running ~10–15 minutes to cover the learning window; a richer, repeated
schema tags more reliably than a single burst.

Confirm the traffic is actually on the wire the sensor sees (on the box):

```bash
sudo tcpdump -i eth0 -nn 'tcp port 8011 or tcp port 8012'
```

You should see `POST /v1/...` from your Mac's IP paired with `200`s on **both**
ports. Silence on 8012 = firewall or wrong interface, and GenAI won't learn.

---

## 6. Check Noname

In the Discovery Console, filter for the AI insight tags. After learning you
should see two separate API entities:

- `192.168.1.102:8011` → `/v1/chat/completions`, `/v1/completions`,
  `/v1/embeddings`, `/v1/messages`,
  `/openai/deployments/.../chat/completions` → **LLM**
- `192.168.1.102:8012` → `/v1/images/*`, `/v1/audio/*`,
  `/v1/generation/.../text-to-image`, `/v1/predictions`,
  `/v1/text-to-speech/*`, `/v1/video/generations` → **GenAI**

---

## Troubleshooting: endpoints are in Inventory but not tagged

Per Noname SME guidance, discovery and tagging are two separate layers: **an API
appearing in Inventory does not guarantee either tag.** Unlike MCP (identified
from explicit protocol headers like `mcp-session-id`), the LLM/GenAI classifier
reads the **body**. Work the list in order:

**1. Confirm body capture is on.** This is the single most common blocker and it
is a sensor config, not the app. Open a sample transaction for
`/v1/chat/completions` in the UI and verify you can see the request body
(`messages`, `model`, `temperature`) and the response body (`choices`,
`usage.total_tokens`). If those are blank or truncated, the classifier has
nothing to match — enable full payload/body capture on the traffic source and
check the source's capture limits.

**2. Check Processed vs Ignored traffic.** If GenAI transactions are landing in
*Ignored*, you are probably running with `AI_SIM_AUDIO_JSON=0` (raw `audio/mpeg`
responses). Set it back to `1` and restart.

**3. Confirm both ports are actually being learned.** If only `:8011` appears in
Inventory, the GenAI listener isn't reaching the sensor — firewall, `AI_SIM_ROLE`,
or the generator exiting at its health check. `tcpdump` on 8012 settles it.

**4. Verify header hygiene.** Run the `grep -i 'openai-'` check from step 4
above. If OpenAI LLM markers reappear on the GenAI listener, something has
reintroduced the shared header helper and GenAI will get pulled back toward LLM.

**5. Drive real volume.** The minimum sample count and score threshold are not
published. Run `--delay 0` and/or several parallel generators for 10–15+ minutes.
The `chat/completions` / `embeddings` / `messages` endpoints (clean JSON + token
usage) are the most reliable first tag; GenAI typically follows.

If bodies are confirmed captured, both ports are learned, headers are clean, and
volume is high but tags still never appear: per SME, collect a sanitized
request/response pair, the API URL/ID, tenant version, and timestamp and hand
them to Support for classifier pattern/threshold validation — the exact
signatures and thresholds are not publicly documented and may be tenant-specific.

---

## Configuration reference (`ai-sim.env`)

| Variable | Default | Purpose |
|---|---|---|
| `AI_SIM_HOST` | `0.0.0.0` | Listen address |
| `AI_SIM_PORT` | `8011` | LLM listener port |
| `AI_SIM_GENAI_PORT` | `8012` | GenAI listener port |
| `AI_SIM_ROLE` | `both` | `both` \| `llm` \| `genai` — which listeners to start |
| `AI_SIM_AUDIO_JSON` | `1` | `1` = JSON-wrapped audio (classifier-friendly); `0` = raw `audio/mpeg` |
| `AI_SIM_AUTH_TOKEN` | *(unset)* | Optional Bearer gate. Leave off for discovery demos |

> Keep comments on their own lines in `ai-sim.env`. systemd's `EnvironmentFile`
> does not strip trailing comments, so `AI_SIM_PORT=8011  # LLM` is read as the
> literal string `8011  # LLM`.

## Files

- `ai_sim_server.py` — the service (LLM + GenAI listeners; realistic stub bodies).
- `ai-sim.env` — hosts/ports/role/audio mode/optional auth token.
- `ai-sim.service` — systemd unit (mirrors the crapi-mcp unit).
- `requirements.txt` — starlette, uvicorn, requests.
- `generate_traffic.py` — traffic driver (run from a different host than the box).

## Endpoint reference

| Tag | Port | Method | Path | Vendor shape |
|---|---|---|---|---|
| LLM | 8011 | POST | `/v1/chat/completions` | OpenAI chat |
| LLM | 8011 | POST | `/v1/completions` | OpenAI legacy |
| LLM | 8011 | POST | `/v1/embeddings` | OpenAI embeddings |
| LLM | 8011 | GET | `/v1/models` | OpenAI models (text) |
| LLM | 8011 | POST | `/v1/messages` | Anthropic messages |
| LLM | 8011 | POST | `/openai/deployments/{deployment}/chat/completions` | Azure OpenAI |
| GenAI | 8012 | POST | `/v1/images/generations` | OpenAI DALL·E |
| GenAI | 8012 | POST | `/v1/images/edits` | OpenAI DALL·E edit (multipart) |
| GenAI | 8012 | POST | `/v1/audio/speech` | OpenAI TTS |
| GenAI | 8012 | POST | `/v1/audio/transcriptions` | OpenAI Whisper (multipart) |
| GenAI | 8012 | POST | `/v1/generation/{engine}/text-to-image` | Stability AI |
| GenAI | 8012 | POST | `/v1/predictions` | Replicate |
| GenAI | 8012 | POST | `/v1/text-to-speech/{voice_id}` | ElevenLabs |
| GenAI | 8012 | POST | `/v1/video/generations` | Sora / Runway |
| GenAI | 8012 | GET | `/v1/models` | Media model catalog |
