"""Stephanie voice verification — produces evidence, never a claim.

    python -m briefing.verify_voice                       # run the automated steps
    python -m briefing.verify_voice --voice-name Stephanie
    python -m briefing.verify_voice --record-phone-test pass --device "iPhone 15 Pro"
    python -m briefing.verify_voice --status              # show the latest evidence

Steps (each recorded in evidence/voice-verification-<timestamp>.json):
  1. key_present      ELEVENLABS_API_KEY is set
  2. auth             GET /v1/user succeeds (tier + remaining characters)
  3. voice_resolved   canonical voice id resolves in the account
  4. synthesis        calibration script → MP3 on disk, sha256, duration, WPM vs 145 target
  5. phone_playback   a human played the MP3 on the phone and recorded the result

Exit codes: 0 = all five passed · 2 = automated steps passed, phone playback pending · 1 = a step failed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import voice_standard as vs
from .elevenlabs_client import (ENV_API_KEY, ENV_VOICE_ID, ENV_VOICE_NAME, ElevenLabsClient, ElevenLabsError,
                                VoiceSettings)
from .mp3_info import mp3_info

EVIDENCE_DIR = Path(__file__).resolve().parent / "evidence"
STEP_ORDER = ["key_present", "auth", "voice_resolved", "synthesis", "phone_playback"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _step(status: str, **detail: Any) -> Dict[str, Any]:
    return {"status": status, "at": _now(), **detail}


def load_latest_evidence(evidence_dir: Path = EVIDENCE_DIR) -> Optional[Dict[str, Any]]:
    files = sorted(evidence_dir.glob("voice-verification-*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text())
    except (OSError, ValueError):
        return None


def verdict(evidence: Dict[str, Any]) -> str:
    steps = evidence.get("steps", {})
    automated = [steps.get(s, {}).get("status") for s in STEP_ORDER[:4]]
    phone = steps.get("phone_playback", {}).get("status")
    if all(s == "pass" for s in automated) and phone == "pass":
        return "VERIFIED — Stephanie's ElevenLabs voice played on the phone"
    if all(s == "pass" for s in automated):
        return "SYNTHESIS OK — phone playback not yet recorded"
    failed = [s for s in STEP_ORDER[:4] if steps.get(s, {}).get("status") not in ("pass", None)]
    return "NOT VERIFIED — failed: " + ", ".join(failed or ["nothing run"])


def exit_code(evidence: Dict[str, Any]) -> int:
    v = verdict(evidence)
    return 0 if v.startswith("VERIFIED") else 2 if v.startswith("SYNTHESIS OK") else 1


def run_automated(voice_id: Optional[str], voice_name: Optional[str], model_id: Optional[str], speed: Optional[float],
                  script: str, evidence_dir: Path = EVIDENCE_DIR, client_factory=ElevenLabsClient) -> Dict[str, Any]:
    evidence: Dict[str, Any] = {
        "kind": "stephanie_voice_verification", "started_at": _now(),
        "host": {"machine": platform.node(), "python": platform.python_version()},
        "inputs": {"voice_id_arg": voice_id, "voice_name_arg": voice_name, "model_id": model_id, "speed": speed,
                   "env": {ENV_VOICE_ID: bool(os.getenv(ENV_VOICE_ID)), ENV_VOICE_NAME: bool(os.getenv(ENV_VOICE_NAME))}},
        "steps": {},
    }
    steps = evidence["steps"]

    # 1. key present
    key = os.getenv(ENV_API_KEY, "")
    if not key:
        steps["key_present"] = _step("fail", reason=f"{ENV_API_KEY} not set in this environment")
        for s in STEP_ORDER[1:4]:
            steps[s] = _step("skipped", reason="no API key")
        return _finish(evidence, evidence_dir)
    steps["key_present"] = _step("pass", key_preview=f"{key[:4]}…{key[-4:]}", key_length=len(key))

    # 2. auth
    try:
        client = client_factory()
        auth = client.check_auth()
        steps["auth"] = _step("pass", **auth)
    except ElevenLabsError as exc:
        steps["auth"] = _step("fail", reason=str(exc), http_status=exc.status, body=exc.body)
        for s in STEP_ORDER[2:4]:
            steps[s] = _step("skipped", reason="auth failed")
        return _finish(evidence, evidence_dir)

    # 3. voice
    try:
        voice = client.resolve_voice(voice_id, voice_name)
        steps["voice_resolved"] = _step("pass", **voice)
    except ElevenLabsError as exc:
        steps["voice_resolved"] = _step("fail", reason=str(exc))
        steps["synthesis"] = _step("skipped", reason="no canonical voice id")
        return _finish(evidence, evidence_dir)

    # 4. synthesis + calibration
    text = vs.prepare_script(script)
    try:
        result = client.synthesize(text, voice["voice_id"], model_id=model_id,
                                   settings=VoiceSettings(speed=speed) if speed else VoiceSettings())
    except ElevenLabsError as exc:
        steps["synthesis"] = _step("fail", reason=str(exc), http_status=exc.status, body=exc.body)
        return _finish(evidence, evidence_dir)
    stamp = _stamp()
    mp3_path = evidence_dir / f"stephanie-calibration-{stamp}.mp3"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    mp3_path.write_bytes(result.audio)
    info = mp3_info(result.audio)
    score = vs.score_delivery(text, info.duration_s) if info else None
    steps["synthesis"] = _step(
        "pass" if info else "fail",
        reason=None if info else "response was not a decodable MP3",
        mp3_path=str(mp3_path), sha256=hashlib.sha256(result.audio).hexdigest(), bytes=result.bytes,
        model_id=result.model_id, output_format=result.output_format, request_id=result.request_id,
        history_item_id=result.history_item_id, latency_ms=result.latency_ms, characters=result.characters,
        audio=info.as_dict() if info else None, delivery=score.as_dict() if score else None,
        script_preview=text[:160],
    )
    evidence["_stamp"] = stamp
    client.close()
    return _finish(evidence, evidence_dir)


def _finish(evidence: Dict[str, Any], evidence_dir: Path) -> Dict[str, Any]:
    evidence["steps"].setdefault("phone_playback", _step("pending", reason="record with --record-phone-test"))
    evidence["finished_at"] = _now()
    evidence["verdict"] = verdict(evidence)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / f"voice-verification-{evidence.pop('_stamp', _stamp())}.json"
    path.write_text(json.dumps(evidence, indent=2))
    evidence["evidence_path"] = str(path)
    return evidence


def record_phone_test(result: str, device: str, note: str, evidence_dir: Path = EVIDENCE_DIR) -> Dict[str, Any]:
    latest = load_latest_evidence(evidence_dir)
    if not latest:
        raise SystemExit("no verification run to attach a phone test to — run the automated steps first")
    synth = latest["steps"].get("synthesis", {})
    if synth.get("status") != "pass":
        raise SystemExit("the latest run has no successful synthesis; a phone test needs a real MP3")
    latest["steps"]["phone_playback"] = _step(result, device=device, note=note, mp3_sha256=synth.get("sha256"),
                                              recorded_by=os.getenv("USER") or os.getenv("USERNAME") or "unknown")
    latest["verdict"] = verdict(latest)
    files = sorted(evidence_dir.glob("voice-verification-*.json"))
    files[-1].write_text(json.dumps(latest, indent=2))
    latest["evidence_path"] = str(files[-1])
    return latest


def print_summary(evidence: Dict[str, Any]) -> None:
    print("Stephanie voice verification")
    print(f"  evidence: {evidence.get('evidence_path', '-')}")
    for name in STEP_ORDER:
        step = evidence["steps"].get(name, {"status": "not run"})
        status = step.get("status", "?").upper()
        detail = step.get("reason") or ""
        if name == "auth" and status == "PASS":
            detail = f"tier={step.get('tier')} remaining_chars={step.get('characters_remaining')}"
        if name == "voice_resolved" and status == "PASS":
            detail = f"{step.get('name')} ({step.get('voice_id')}) via {step.get('resolved_by')}"
        if name == "synthesis" and status == "PASS":
            d = step.get("delivery") or {}
            detail = (f"{step.get('bytes')} bytes, {d.get('duration_s')}s, {d.get('wpm')} wpm "
                      f"(target {d.get('target_wpm')}, {'ok' if d.get('within_target') else 'adjust speed→' + str(d.get('suggested_speed'))}) "
                      f"→ {step.get('mp3_path')}")
        if name == "phone_playback" and status == "PASS":
            detail = f"{step.get('device')} — {step.get('note') or 'played'}"
        print(f"  {status:<8} {name:<16} {detail}")
    print(f"  VERDICT: {evidence.get('verdict')}")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--voice-id", help=f"canonical voice id (or env {ENV_VOICE_ID})")
    p.add_argument("--voice-name", help=f"resolve by exact name (or env {ENV_VOICE_NAME})")
    p.add_argument("--model", help="ElevenLabs model id (default eleven_multilingual_v2)")
    p.add_argument("--speed", type=float, help="ElevenLabs speed 0.7–1.2 (calibrate toward 145 wpm)")
    p.add_argument("--script", default="calibration", help="preset name or path to a text file")
    p.add_argument("--evidence-dir", default=str(EVIDENCE_DIR))
    p.add_argument("--record-phone-test", choices=["pass", "fail"], help="attach a manual phone playback result")
    p.add_argument("--device", default="", help="phone model used for the playback test")
    p.add_argument("--note", default="", help="what was heard / what went wrong")
    p.add_argument("--status", action="store_true", help="print the latest evidence and exit")
    args = p.parse_args(argv)
    evidence_dir = Path(args.evidence_dir)

    if args.status:
        latest = load_latest_evidence(evidence_dir)
        if not latest:
            print("no verification evidence yet")
            return 1
        latest["evidence_path"] = str(sorted(evidence_dir.glob("voice-verification-*.json"))[-1])
        print_summary(latest)
        return exit_code(latest)

    if args.record_phone_test:
        if not args.device:
            p.error("--device is required with --record-phone-test")
        evidence = record_phone_test(args.record_phone_test, args.device, args.note, evidence_dir)
        print_summary(evidence)
        return exit_code(evidence)

    script = vs.PRESET_SCRIPTS.get(args.script) or Path(args.script).read_text()
    evidence = run_automated(args.voice_id, args.voice_name, args.model, args.speed, script, evidence_dir)
    print_summary(evidence)
    return exit_code(evidence)


if __name__ == "__main__":
    sys.exit(main())
