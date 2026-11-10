"""Build the morning briefing page with a Play button in Stephanie's voice.

    python -m briefing.build_briefing --input samples/briefing-2026-09-13.md --out out/briefing.html
    python -m briefing.build_briefing --input brief.md --out out/brief.html --allow-staged-voice   # layout test only

Voice source, in order:
  1. ElevenLabs (ELEVENLABS_API_KEY + canonical voice id)      → label LIVE
  2. --allow-staged-voice: the backend's local formant synth   → label STAGED, visibly "not Stephanie's voice"
  3. otherwise the page ships with Play disabled and the reason printed on it.

There is no silent fallback. The page always says which of the three it is.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import voice_standard as vs
from .elevenlabs_client import ENV_VOICE_ID, ElevenLabsClient, ElevenLabsError, VoiceSettings
from .mp3_info import mp3_info
from .verify_voice import load_latest_evidence

MAX_PAGE_BYTES = 15 * 1024 * 1024
FONT_CANDIDATES = [
    Path("/mnt/skills/examples/morning/assets/fonts/fraunces-latin-600-normal.woff2"),
    *Path.home().glob(".claude/skills/synced/*/morning/assets/fonts/fraunces-latin-600-normal.woff2"),
]


# ---------------------------------------------------------------------------
# Briefing text → structure → spoken script
# ---------------------------------------------------------------------------

def parse_briefing(markdown: str) -> Dict[str, Any]:
    """Light markdown: '# headline', '## section', '- item' or '1. item', paragraphs."""
    headline = ""
    sections: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("# ") and not headline:
            headline = line[2:].strip()
        elif line.startswith("## "):
            current = {"title": line[3:].strip(), "items": [], "paragraphs": []}
            sections.append(current)
        elif re.match(r"^\s*([-*]|\d+[.)])\s+", line):
            if current is None:
                current = {"title": "", "items": [], "paragraphs": []}
                sections.append(current)
            current["items"].append(re.sub(r"^\s*([-*]|\d+[.)])\s+", "", line).strip())
        else:
            if current is None:
                current = {"title": "", "items": [], "paragraphs": []}
                sections.append(current)
            current["paragraphs"].append(line.strip())
    return {"headline": headline, "sections": sections}


def _plain(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)      # links → text
    text = re.sub(r"[*_`]+", "", text)                          # emphasis marks
    return text.strip()


def compose_script(brief: Dict[str, Any], name: str = "Michael", when: Optional[date] = None) -> str:
    when = when or date.today()
    parts = [f"Good morning, {name}. This is Stephanie with your NoblePort briefing for "
             f"{when.strftime('%A, %B')} {when.day}."]
    if brief["headline"]:
        parts.append(_plain(brief["headline"]))
    for section in brief["sections"]:
        block = []
        if section["title"]:
            block.append(_plain(section["title"]) + ".")
        for para in section["paragraphs"]:
            block.append(_plain(para))
        for i, item in enumerate(section["items"], 1):
            block.append(f"{'First' if i == 1 else 'Next' if i < len(section['items']) else 'Last'}: {_plain(item)}")
        parts.append(" ".join(block))
    parts.append(vs.PRESET_SCRIPTS["sign_off"])
    return vs.prepare_script(vs.PAUSE_MAP["section"].join(parts))


# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------

def synthesize_voice(script: str, voice_id: Optional[str], voice_name: Optional[str], model_id: Optional[str],
                     speed: Optional[float], allow_staged: bool, client_factory=ElevenLabsClient) -> Dict[str, Any]:
    """Return {status, label, mime, audio(bytes)|None, detail...}."""
    try:
        client = client_factory()
        voice = client.resolve_voice(voice_id, voice_name)
        result = client.synthesize(script, voice["voice_id"], model_id=model_id,
                                   settings=VoiceSettings(speed=speed) if speed else VoiceSettings())
        info = mp3_info(result.audio)
        duration = info.duration_s if info else 0.0
        return {"status": "live", "label": "LIVE", "mime": "audio/mpeg", "audio": result.audio,
                "voice_id": voice["voice_id"], "voice_name": voice.get("name"), "model_id": result.model_id,
                "duration_s": duration, "delivery": vs.score_delivery(script, duration).as_dict() if duration else None,
                "request_id": result.request_id, "bytes": result.bytes, "source": "ElevenLabs"}
    except ElevenLabsError as exc:
        reason = str(exc)
    if allow_staged:
        backend = Path(__file__).resolve().parent.parent / "backend"
        if str(backend) not in sys.path:
            sys.path.insert(0, str(backend))
        from models import audio_processor as ap
        from models.voice_engine import StephanieVoiceEngine

        engine = StephanieVoiceEngine(sample_rate=22050)
        audio = engine.generate_speech(script, voice_id="stephanie_primary", emotion="calm")
        wav = ap.encode_wav(audio, 22050)
        duration = ap.duration_seconds(audio, 22050)
        return {"status": "staged", "label": "STAGED", "mime": "audio/wav", "audio": wav, "voice_id": "stephanie_primary",
                "voice_name": "local formant synth (NOT Stephanie's voice)", "model_id": "local_formant",
                "duration_s": duration, "delivery": vs.score_delivery(script, duration).as_dict(),
                "bytes": len(wav), "source": "local synthesizer", "reason": reason}
    return {"status": "none", "label": "NO VOICE", "mime": None, "audio": None, "reason": reason, "source": None}


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def _font_face() -> str:
    for path in FONT_CANDIDATES:
        if path.is_file():
            data = base64.b64encode(path.read_bytes()).decode()
            return ("@font-face{font-family:'Fraunces';font-weight:600;font-style:normal;"
                    f"src:url(data:font/woff2;base64,{data}) format('woff2');}}")
    return ""


def render_html(brief: Dict[str, Any], script: str, voice: Dict[str, Any], when: date, name: str,
                phone_test: Optional[Dict[str, Any]], generated_at: str, artifact: bool = False) -> str:
    esc = html.escape
    day_line = f"{when.strftime('%A')} · {when.strftime('%B')} {when.day} {when.year}"
    headline = brief["headline"] or "Your NoblePort morning briefing."
    font_css = _font_face()
    headline_font = "'Fraunces', Georgia, serif" if font_css else "Georgia, serif"

    if voice["audio"]:
        src = f"data:{voice['mime']};base64,{base64.b64encode(voice['audio']).decode()}"
        audio_tag = f'<audio id="voice" preload="auto" playsinline src="{src}"></audio>'
        play_disabled = ""
    else:
        audio_tag = ""
        play_disabled = " disabled"

    if voice["status"] == "live":
        status_line = (f"Stephanie's voice · ElevenLabs · {esc(str(voice.get('voice_name')))} "
                       f"({esc(str(voice.get('voice_id')))}) · {esc(str(voice.get('model_id')))}")
    elif voice["status"] == "staged":
        status_line = ("STAGED placeholder — the local synthesizer, NOT Stephanie's voice. "
                       f"ElevenLabs was unavailable: {esc(voice.get('reason', ''))}")
    else:
        status_line = f"No voice on this page. {esc(voice.get('reason', ''))}"

    phone_line = "Phone playback test: not recorded."
    if phone_test:
        phone_line = (f"Phone playback test: {esc(phone_test.get('status', '?').upper())} on "
                      f"{esc(phone_test.get('device', '?'))} ({esc(phone_test.get('at', '')[:10])}).")
    delivery = voice.get("delivery") or {}
    delivery_line = (f"{delivery.get('words')} words · {delivery.get('duration_s')} s · {delivery.get('wpm')} wpm "
                     f"(target {vs.TARGET_WPM})") if delivery else ""

    sections_html = []
    for section in brief["sections"]:
        block = []
        if section["title"]:
            block.append(f"<h2>{esc(section['title'])}</h2>")
        for para in section["paragraphs"]:
            block.append(f"<p>{esc(_plain(para))}</p>")
        if section["items"]:
            block.append("<ol>" + "".join(f"<li>{esc(_plain(i))}</li>" for i in section["items"]) + "</ol>")
        sections_html.append("".join(block))

    head_open = ("" if artifact else '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
                 '<meta name="viewport" content="width=device-width,initial-scale=1">\n')
    head_close = "" if artifact else "</head><body>"
    tail = "" if artifact else "</body></html>"
    title = "Stephanie's Morning Briefing" if artifact else f"NoblePort Briefing · {day_line}"
    return f"""{head_open}<title>{esc(title)}</title>
<style>
{font_css}
:root{{--bg:#FCFCFB;--wash:#F9F9F7;--ink:#2E2C27;--soft:#6B6A63;--grey:#B4B3A8;--hair:#E4E3DC;--clay:#C6613F;--clay-h:#AE5133;--live:#3B7A57;--staged:#B7791F}}
*{{box-sizing:border-box}}html,body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 -apple-system,"Segoe UI",sans-serif;-webkit-text-size-adjust:100%}}
.band{{padding:40px 24px}}.top{{background:var(--wash);border-bottom:1px solid #E1E1DF}}.wrap{{max-width:860px;margin:0 auto}}
.date{{color:var(--soft);font-size:13px;letter-spacing:.02em}}h1{{font-family:{headline_font};font-weight:600;font-size:40px;line-height:1.15;margin:8px 0 26px}}
@media(max-width:640px){{h1{{font-size:30px}}.band{{padding:28px 18px}}}}
.player{{display:flex;align-items:center;gap:16px;flex-wrap:wrap}}
button.play{{background:var(--clay);color:#FCFCFB;border:1px solid var(--clay);border-radius:8px;padding:12px 22px;font:500 15px -apple-system,"Segoe UI",sans-serif;cursor:pointer;min-width:190px}}
button.play:hover{{background:var(--clay-h)}}button.play[disabled]{{background:var(--grey);border-color:var(--grey);cursor:not-allowed}}
.bar{{flex:1;min-width:160px;height:4px;background:var(--hair);position:relative}}.bar i{{position:absolute;left:0;top:0;bottom:0;width:0;background:var(--ink)}}
.time{{font-variant-numeric:tabular-nums;color:var(--soft);font-size:13px;min-width:86px}}
.status{{margin-top:14px;font-size:13px;color:var(--soft)}}.status b{{font-weight:600}}.live{{color:var(--live)}}.staged{{color:var(--staged)}}.none{{color:var(--clay)}}
h2{{font:600 13px -apple-system,"Segoe UI",sans-serif;letter-spacing:.06em;text-transform:uppercase;color:var(--ink);margin:30px 0 10px}}
ol{{padding-left:0;margin:0;list-style:none;counter-reset:n}}li{{counter-increment:n;padding:10px 0 10px 34px;position:relative;border-top:1px solid var(--hair)}}li:before{{content:counter(n);position:absolute;left:0;top:10px;color:var(--grey);font-size:13px}}
p{{margin:8px 0;color:var(--soft)}}details{{margin-top:34px;color:var(--soft);font-size:14px}}summary{{cursor:pointer;color:var(--ink)}}pre{{white-space:pre-wrap;font:14px/1.5 -apple-system,"Segoe UI",sans-serif;color:var(--soft)}}
.meta{{margin-top:40px;padding-top:14px;border-top:1px solid var(--hair);color:var(--grey);font-size:12px}}
</style>{head_close}
<section class="band top"><div class="wrap">
<div class="date">{esc(day_line)}</div>
<h1>{esc(headline)}</h1>
<div class="player">
<button class="play" id="playBtn"{play_disabled} aria-label="Play the briefing in Stephanie's voice">▶&nbsp; Play in Stephanie's voice</button>
<div class="bar" aria-hidden="true"><i id="bar"></i></div><div class="time" id="time">0:00 / 0:00</div>
</div>
{audio_tag}
<div class="status"><b class="{voice['status']}">{esc(voice['label'])}</b> — {status_line}<br>{esc(phone_line)}{(' · ' + esc(delivery_line)) if delivery_line else ''}</div>
</div></section>
<section class="band"><div class="wrap">
{''.join(sections_html)}
<details><summary>Transcript (what the voice reads)</summary><pre>{esc(script)}</pre></details>
<div class="meta">Generated {esc(generated_at)} · voice source: {esc(str(voice.get('source') or 'none'))} · NoblePort Systems · Stephanie.ai is an automated assistant; every item is staged for human review.</div>
</div></section>
<script>
(function(){{
  var a=document.getElementById('voice'),b=document.getElementById('playBtn'),bar=document.getElementById('bar'),t=document.getElementById('time');
  if(!a||!b||b.disabled)return;
  function fmt(s){{s=Math.max(0,Math.floor(s||0));return Math.floor(s/60)+':'+('0'+(s%60)).slice(-2)}}
  function tick(){{var d=a.duration||0,c=a.currentTime||0;bar.style.width=(d?100*c/d:0)+'%';t.textContent=fmt(c)+' / '+fmt(d)}}
  a.addEventListener('loadedmetadata',tick);a.addEventListener('timeupdate',tick);
  a.addEventListener('play',function(){{b.innerHTML='❚❚&nbsp; Pause'}});
  a.addEventListener('pause',function(){{b.innerHTML='▶&nbsp; Play in Stephanie\\'s voice'}});
  a.addEventListener('ended',function(){{b.innerHTML='↻&nbsp; Play again';a.currentTime=0;tick()}});
  a.addEventListener('error',function(){{b.disabled=true;b.textContent='Audio failed to load on this device'}});
  b.addEventListener('click',function(){{ if(a.paused){{var p=a.play(); if(p&&p.catch){{p.catch(function(e){{b.textContent='Playback blocked: '+e.name}})}} }} else {{a.pause()}} }});
}})();
</script>
{tail}"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build(input_path: Path, out_path: Path, when: date, name: str, voice_id: Optional[str], voice_name: Optional[str],
          model_id: Optional[str], speed: Optional[float], allow_staged: bool, no_audio: bool,
          allow_flagged: bool, client_factory=ElevenLabsClient, artifact: bool = False) -> Dict[str, Any]:
    markdown = input_path.read_text()
    brief = parse_briefing(markdown)
    script = compose_script(brief, name, when)
    flagged = vs.compliance_issues(script)
    if flagged and not allow_flagged:
        raise SystemExit("briefing text contains prohibited public-material terms: " + ", ".join(flagged))

    if no_audio:
        voice: Dict[str, Any] = {"status": "none", "label": "NO VOICE", "mime": None, "audio": None,
                                 "reason": "built with --no-audio", "source": None}
    else:
        voice = synthesize_voice(script, voice_id, voice_name, model_id, speed, allow_staged, client_factory)

    latest = load_latest_evidence()
    phone_test = (latest or {}).get("steps", {}).get("phone_playback")
    if phone_test and phone_test.get("status") not in ("pass", "fail"):
        phone_test = None
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    page = render_html(brief, script, voice, when, name, phone_test, generated_at, artifact=artifact)
    if len(page.encode()) > MAX_PAGE_BYTES:
        raise SystemExit(f"page is {len(page.encode())} bytes; exceeds the {MAX_PAGE_BYTES} byte limit")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page)
    if voice["audio"]:
        ext = "mp3" if voice["mime"] == "audio/mpeg" else "wav"
        out_path.with_suffix(f".{ext}").write_bytes(voice["audio"])

    meta = {k: v for k, v in voice.items() if k != "audio"}
    meta.update({"generated_at": generated_at, "date": when.isoformat(), "input": str(input_path),
                 "output": str(out_path), "page_bytes": len(page.encode()), "script_words": vs.word_count(script),
                 "compliance_flagged": flagged, "phone_test": phone_test})
    out_path.with_suffix(".json").write_text(json.dumps(meta, indent=2))
    return meta


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", required=True, help="briefing markdown/text file")
    p.add_argument("--out", required=True, help="output .html path (a .json sidecar and the audio file sit next to it)")
    p.add_argument("--date", help="YYYY-MM-DD (default today)")
    p.add_argument("--name", default="Michael")
    p.add_argument("--voice-id", help=f"ElevenLabs voice id (or env {ENV_VOICE_ID})")
    p.add_argument("--voice-name")
    p.add_argument("--model")
    p.add_argument("--speed", type=float)
    p.add_argument("--allow-staged-voice", action="store_true", help="embed the local STAGED voice when ElevenLabs is unavailable")
    p.add_argument("--no-audio", action="store_true")
    p.add_argument("--allow-flagged", action="store_true", help="skip the danger-word block (not for public delivery)")
    p.add_argument("--artifact", action="store_true", help="emit a claude.ai Artifact fragment (no html/head/body wrapper)")
    args = p.parse_args(argv)
    when = date.fromisoformat(args.date) if args.date else date.today()
    meta = build(Path(args.input), Path(args.out), when, args.name, args.voice_id, args.voice_name, args.model,
                 args.speed, args.allow_staged_voice, args.no_audio, args.allow_flagged, artifact=args.artifact)
    print(f"{meta['label']}: {meta['output']} ({meta['page_bytes']} bytes) — voice source: {meta.get('source')}"
          + (f" — {meta.get('reason')}" if meta.get("reason") else ""))
    return 0 if meta["status"] == "live" else 2


if __name__ == "__main__":
    sys.exit(main())
