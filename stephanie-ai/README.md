# Stephanie.ai Voice — NoblePort Voice Generation & Telephony

Stephanie.ai's voice layer: a Python/FastAPI backend for Linux and a CustomTkinter
desktop client. It combines the ten most useful **ElevenLabs**-style synthesis
features with the ten most useful **Twilio**-style telephony features, wrapped in
NoblePort's governance model (compliance screening, human-gated outbound,
truth labels on every response).

```
stephanie-ai/
├── backend/                    FastAPI service (Linux)
│   ├── main.py                 REST + WebSocket API
│   ├── config.py               env-driven settings, truth labels
│   ├── models/
│   │   ├── voice_engine.py     voice profiles, local formant synth, ElevenLabs client
│   │   ├── text_processor.py   normalisation, numbers/money, compliance screen, chunking
│   │   └── audio_processor.py  numpy DSP: WAV/mu-law codecs, EQ, compressor, mixing
│   ├── services/
│   │   ├── elevenlabs_features.py   10 synthesis features
│   │   ├── twilio_features.py       10 telephony features + TwiML
│   │   └── stephanie_ai.py          orchestrator
│   ├── tests/                  70 pytest cases (unit + API + WebSocket)
│   ├── requirements.txt
│   └── .env.example
├── frontend/                   CustomTkinter desktop client
│   ├── main.py
│   └── ui/  app.py · components.py · styles.py
└── README.md
```

## Morning briefing with Play button

`briefing/` builds the written morning briefing into a page that reads it in
Stephanie's ElevenLabs voice, and ships the verification tool that proves the
voice works (authenticated access → canonical voice ID → synthesis → phone
playback). See [`briefing/README.md`](briefing/README.md).

## Truth labels

| Component | Label | What it means |
|-----------|-------|---------------|
| Local synthesis | **STAGED** | Deterministic formant synthesizer. Speech-shaped, brand-consistent, not a neural voice. |
| ElevenLabs | STAGED → **LIVE** when `ELEVENLABS_API_KEY` is set | Neural TTS via the ElevenLabs API (PCM output feeds the same pipeline). |
| Twilio | STAGED → **LIVE** when SID, token and number are set | Calls and SMS through the Twilio REST API. |
| Human gate | SIMULATED_ONLY → **ENFORCED** when `STEPHANIE_HUMAN_APPROVAL_TOKEN` is set | LIVE outbound calls/SMS require the `X-Human-Approval` header. |

`GET /health` reports all four. Nothing claims LIVE without a real provider behind it.

## Governance

* **Compliance screen.** Every text that would be spoken or sent is checked against
  `core/config/launch-gates.json → danger_words.prohibited_in_public_materials`
  ("guaranteed returns", "passive income", "token launch", …). A hit returns
  HTTP 422 with the flagged terms and the approved alternative wording.
* **Human gate.** When telephony is LIVE, `POST /api/v1/calls` and
  `POST /api/v1/messages` refuse to dispatch (HTTP 403) unless
  `X-Human-Approval` matches the configured token. If the transport is LIVE and
  no token is configured, the gate fails closed.
* **Disclaimer.** `append_disclaimer: true` appends the approved Stephanie.ai
  description (the `proper_disclaimers` requirement for `stephanie_ai_routing`).
  Outbound call prompts always include it.
* **Admin token.** Deleting cloned voices requires `X-Admin-Token` when
  `STEPHANIE_ADMIN_TOKEN` is set.

## Features

### ElevenLabs-inspired (`services/elevenlabs_features.py`)
1. **Instant voice cloning** — WAV sample ≥ 1 s → pitch/pacing profile, persisted to the voice library
2. **Multilingual synthesis** — 10 language prosody presets (en, es, fr, de, it, pt, pl, hi, ar, zh)
3. **Voice settings** — stability, similarity boost, style, speaker boost (`PUT /api/v1/voice/settings`)
4. **Real-time streaming** — `/ws/stream` yields PCM16 chunks per sentence segment
5. **Voice library** — list / get / delete / share links (HMAC-signed)
6. **Emotion control** — neutral, warm, happy, sad, angry, excited, calm, surprised, with intensity
7. **Audio enhancement** — noise gate + roll-off, compressor, 3-band EQ, normalisation
8. **Custom pronunciation** — per-request or registered dictionaries
9. **Voice mixing** — blend two profiles (`POST /api/v1/voice/mix`)
10. **Background audio** — generated office / jobsite / hold-music beds

### Twilio-inspired (`services/twilio_features.py`)
1. **Voice calls** — outbound with synthesized prompt served to `<Play>`
2. **SMS** — outbound (screened, gated) and inbound webhook
3. **Call recording** — start/stop, captured media served as WAV
4. **Real-time transcription** — per-call transcript lines with webhooks
5. **Conference calls** — create, add, mute, remove participants
6. **Call queuing** — positions, dequeue
7. **IVR menus** — `<Gather>` TwiML + spoken prompt in Stephanie's voice
8. **Call forwarding** — `<Dial>` redirect
9. **Webhooks** — register handlers for `call.*`, `sms.*`, `ivr.*`, `transcript.*`, `*`
10. **Media streams** — `/ws/media-stream` speaks Twilio Media Streams (8 kHz mu-law in/out)

## Quick start

### Backend (Linux)
```bash
cd stephanie-ai/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # optional: add ElevenLabs / Twilio keys
python main.py                  # http://localhost:8000/docs
```

Generate speech from the shell:
```bash
curl -s -X POST localhost:8000/api/v1/voice/generate.wav \
  -H 'Content-Type: application/json' \
  -d '{"text":"Your permit checklist is ready for review.","voice_id":"stephanie_warm","emotion":"warm"}' \
  -o stephanie.wav
```

Run the tests:
```bash
cd stephanie-ai/backend && python -m pytest -q
```

### Frontend (desktop)
```bash
cd stephanie-ai/frontend
pip install -r requirements.txt   # needs a Python with tkinter (python3-tk on Debian/Ubuntu)
python main.py --api http://localhost:8000
```
Views: Voice Generator (REST or WebSocket streaming, play/save WAV, compliance check),
Voice Cloning (library management), Call Management (calls, recording,
transcript, forward), Messages, Settings (API URL, approval/admin tokens,
ElevenLabs-style voice settings). Playback uses PyAudio if installed, otherwise
`aplay`/`paplay`/`afplay`.

## API

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/v1/voice/generate` | Text → base64 WAV (or MP3 when ffmpeg is present) + metadata |
| POST | `/api/v1/voice/generate.wav` | Same, raw `audio/wav` body |
| POST | `/api/v1/voice/clone` | Clone a voice from a base64 WAV sample |
| POST | `/api/v1/voice/mix` | Blend two voices |
| POST | `/api/v1/voice/compliance-check` | Screen text without synthesizing |
| GET/PUT | `/api/v1/voice/settings` | ElevenLabs-style voice settings |
| GET/POST | `/api/v1/voice/pronunciations` | Custom pronunciation dictionary |
| GET | `/api/v1/voice/brand-intro.wav` | The recorded Stephanie Boston intro (`ai-voices/`) |
| GET | `/api/v1/voices` · `/{id}` · `/{id}/share` · DELETE `/{id}` | Voice library |
| POST/GET | `/api/v1/calls` · `/{id}` · `/{id}/end` | Calls |
| POST | `/api/v1/calls/{id}/recording/start` and `/stop` · GET `/{id}/recording.wav` | Recording |
| POST/GET | `/api/v1/calls/{id}/transcription` | Transcription |
| POST | `/api/v1/calls/{id}/forward` | Forwarding |
| POST/GET | `/api/v1/messages` | SMS |
| POST/GET | `/api/v1/conferences` · `/{id}/participants` · `/{pid}/mute` | Conferences |
| POST/GET | `/api/v1/queues` · `/{id}/enqueue` · `/{id}/dequeue` · `/{id}/position/{sid}` | Queues |
| POST/GET | `/api/v1/ivr` · `/{id}/input` · `/{id}/prompt.wav` | IVR |
| GET/POST | `/webhooks/twilio/voice` · `/ivr/{id}` · `/sms` · `/recording` | TwiML for Twilio |
| WS | `/ws/stream` | Streaming synthesis for clients |
| WS | `/ws/media-stream` | Twilio Media Streams |
| GET | `/health` | Status + truth labels |

Interactive docs: `http://localhost:8000/docs`.

## Configuration

| Variable | Default | Notes |
|----------|---------|-------|
| `ELEVENLABS_API_KEY` | — | Enables neural synthesis |
| `ELEVENLABS_MODEL_ID` | `eleven_multilingual_v2` | |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_PHONE_NUMBER` | — | All three enable LIVE telephony |
| `STEPHANIE_PUBLIC_BASE_URL` | `http://localhost:8000` | URL Twilio uses for webhooks / `<Play>` / `<Stream>` |
| `STEPHANIE_HUMAN_APPROVAL_TOKEN` | — | Required header value for LIVE outbound |
| `STEPHANIE_ADMIN_TOKEN` | — | Guards voice deletion |
| `STEPHANIE_HOST` / `STEPHANIE_PORT` | `0.0.0.0` / `8000` | |
| `STEPHANIE_SAMPLE_RATE` | `44100` | |
| `STEPHANIE_MAX_TEXT_LENGTH` | `5000` | |
| `STEPHANIE_MAX_AUDIO_DURATION` | `600` | seconds |
| `STEPHANIE_DATA_DIR` | `backend/data` | cloned-voice library |
| `STEPHANIE_LAUNCH_GATES` | `core/config/launch-gates.json` | danger-word source |

## Production notes

* Run behind a reverse proxy with TLS; set `STEPHANIE_CORS_ORIGINS` to the desktop/web origins you use.
* `uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1` — call/queue state is in-process, so keep one worker (or move state to Redis before scaling out).
* Twilio needs a public URL: `STEPHANIE_PUBLIC_BASE_URL=https://voice.example.com` and point your number's voice/SMS webhooks at `/webhooks/twilio/voice` and `/webhooks/twilio/sms`.
* Dependencies are numpy + FastAPI only; no torch/librosa, so the backend runs on the standard NoblePort dev containers.
