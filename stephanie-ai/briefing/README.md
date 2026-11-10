# Stephanie Morning Briefing — voice, Play button, verification

Turns the written morning briefing into a page with a **Play** button that reads
it in Stephanie's ElevenLabs voice, and produces the evidence that the voice
actually works before anyone says it does.

```
briefing/
├── elevenlabs_client.py   GET /user · GET /voices · POST /text-to-speech (httpx, no SDK, no fallback)
├── voice_standard.py      145 wpm target, trade-term pronunciations, numeric rules, preset scripts
├── mp3_info.py            MP3 duration/bitrate from frame headers (for the WPM calibration)
├── verify_voice.py        the 5-step verification; writes evidence/voice-verification-*.json
├── build_briefing.py      briefing markdown → spoken script → MP3 → HTML page with Play button
├── samples/               example briefing input
├── evidence/              verification runs (JSON committed, MP3s ignored)
└── tests/                 19 pytest cases, all offline (mocked ElevenLabs)
```

## Status (what is and is not verified)

| Step | Status | Evidence |
|------|--------|----------|
| Written briefing routine | running | separate routine; not touched here |
| ElevenLabs integration code | written and unit-tested against a mocked API | `tests/test_elevenlabs_client.py` |
| Authenticated ElevenLabs access | **not verified** | no `ELEVENLABS_API_KEY` in the build environment; `api.elevenlabs.io` is also blocked by that environment's egress proxy, so verification must run on a machine with outbound access |
| Stephanie's canonical voice ID | **not chosen** | `verify_voice.py` fails at step 3 and lists the account's voices until `STEPHANIE_ELEVENLABS_VOICE_ID` is set |
| Phone playback | **not tested** | recorded only by a human via `--record-phone-test` |
| Page mechanics (Play button, iPhone layout) | rendered at 390×844 and 960×900 with a STAGED placeholder voice, no console errors | `build_briefing.py --allow-staged-voice` |

`verify_voice.py` prints `VERIFIED` only when all five steps pass, including the
human phone test. Nothing in this directory produces a "ready" without that file.

## Unblock in three commands (on a machine with internet access)

```bash
cd stephanie-ai && pip install -r backend/requirements.txt
export ELEVENLABS_API_KEY=sk_...                      # 1. authenticated access

python -m briefing.verify_voice                        # 2. lists the account's voices, fails at step 3
export STEPHANIE_ELEVENLABS_VOICE_ID=<chosen id>       #    pick Stephanie's voice from that list
python -m briefing.verify_voice                        #    → SYNTHESIS OK, MP3 in briefing/evidence/

# 3. AirDrop/open the MP3 on the phone, listen, then record the result:
python -m briefing.verify_voice --record-phone-test pass --device "iPhone 15 Pro" --note "clear, correct voice"
python -m briefing.verify_voice --status               # → VERIFIED
```

If the measured pace is off the 145 wpm target the summary suggests a `--speed`
value (ElevenLabs accepts 0.7–1.2); re-run with it and the evidence records both.

## Build the briefing page

```bash
python -m briefing.build_briefing --input briefing/samples/briefing-2026-09-13.md \
    --out briefing/out/briefing-2026-09-13.html --date 2026-09-13
```

Input is light markdown: `# headline`, `## section`, `- items`. The spoken script
adds Stephanie's greeting and sign-off, applies the pronunciation guide and
number rules, and is screened against `core/config/launch-gates.json` (a
prohibited term stops the build). The page embeds the MP3 as a data URI so it
works offline and inside a claude.ai Artifact (`--artifact` emits the fragment
form), shows the voice source at the top (LIVE / STAGED / NO VOICE), the
measured pace, and the latest phone-test result, and keeps the transcript under
a disclosure.

Exit code 0 means the page carries Stephanie's ElevenLabs voice; 2 means it
does not (no key, or `--allow-staged-voice` / `--no-audio`).

## Hooking it into the 8 a.m. routine

After the routine writes the briefing text, add one step:

```
Save the briefing as stephanie-ai/briefing/out/briefing-<date>.md, run
`python -m briefing.build_briefing --input <that file> --out <same stem>.html --artifact`
from stephanie-ai/, and publish the HTML as the artifact. If the command exits 2,
publish anyway — the page states that the voice is missing — and say so in the
run summary. Never substitute another voice.
```

The routine's environment needs `ELEVENLABS_API_KEY` and
`STEPHANIE_ELEVENLABS_VOICE_ID` set, and outbound access to `api.elevenlabs.io`.

## Voice standard, as code

`voice_standard.py` carries the measurable parts of *Stephanie Voice Profile &
Delivery Parameters v1.0*: the 145 wpm target (±10), the trade-term
pronunciation table (AHJ, AWO, GC, HVAC, NoblePort, Ipswich, USDC, NBPT,
ERC-3643…), numeric handling via the backend text processor, the pause map
(paragraph breaks), and three preset scripts (`calibration`, `greeting`,
`sign_off`). The written standard itself is not in this repository; if it lives
elsewhere, drift between the two is what the monthly voice QA routine should
compare.
