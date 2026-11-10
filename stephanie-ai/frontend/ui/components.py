"""Reusable widgets and helpers for the Stephanie.ai desktop client."""

from __future__ import annotations

import base64
import json
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import customtkinter as ctk
import requests

from . import styles

SETTINGS_PATH = Path.home() / ".stephanie_voice.json"


# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------

def load_settings() -> Dict[str, Any]:
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(data: Dict[str, Any]) -> None:
    try:
        SETTINGS_PATH.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class ApiClient:
    """Thin requests wrapper; raises ApiError with the server's detail text."""

    def __init__(self, base_url: str, human_approval: str = "", admin_token: str = "", timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.human_approval = human_approval
        self.admin_token = admin_token
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        headers = {}
        if self.human_approval:
            headers["X-Human-Approval"] = self.human_approval
        if self.admin_token:
            headers["X-Admin-Token"] = self.admin_token
        return headers

    def request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.request(method, url, headers=self._headers(), timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise ApiError(f"Cannot reach backend at {self.base_url}: {exc}") from exc
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            if isinstance(detail, dict):
                msg = detail.get("message", json.dumps(detail))
                if detail.get("flagged_terms"):
                    msg += "\nFlagged: " + ", ".join(detail["flagged_terms"])
                    alt = detail.get("approved_alternatives", {}).get("stephanie_ai")
                    if alt:
                        msg += f"\nApproved wording: {alt}"
                detail = msg
            raise ApiError(f"{resp.status_code}: {detail}")
        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return resp.content

    def get(self, path: str, **kwargs) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, payload: Optional[Dict[str, Any]] = None, **kwargs) -> Any:
        return self.request("POST", path, json=payload, **kwargs)

    def put(self, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("PUT", path, json=payload)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)


class ApiError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Audio playback
# ---------------------------------------------------------------------------

class AudioPlayer:
    """Plays WAV bytes. Uses PyAudio when installed, else a system player."""

    def __init__(self) -> None:
        self._playing = False
        self._proc: Optional[subprocess.Popen] = None
        try:
            import pyaudio  # type: ignore

            self._pyaudio = pyaudio.PyAudio()
        except Exception:  # noqa: BLE001 — optional dependency
            self._pyaudio = None

    @property
    def playing(self) -> bool:
        return self._playing

    def play(self, wav_bytes: bytes, on_done: Optional[Callable[[], None]] = None) -> None:
        if self._playing:
            return
        self._playing = True
        threading.Thread(target=self._play_thread, args=(wav_bytes, on_done), daemon=True).start()

    def stop(self) -> None:
        self._playing = False
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def _play_thread(self, wav_bytes: bytes, on_done: Optional[Callable[[], None]]) -> None:
        try:
            if self._pyaudio:
                self._play_pyaudio(wav_bytes)
            else:
                self._play_system(wav_bytes)
        finally:
            self._playing = False
            if on_done:
                on_done()

    def _play_pyaudio(self, wav_bytes: bytes) -> None:
        import io
        import pyaudio  # type: ignore

        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            stream = self._pyaudio.open(format=self._pyaudio.get_format_from_width(wf.getsampwidth()),
                                        channels=wf.getnchannels(), rate=wf.getframerate(), output=True)
            try:
                chunk = wf.readframes(1024)
                while chunk and self._playing:
                    stream.write(chunk)
                    chunk = wf.readframes(1024)
            finally:
                stream.stop_stream()
                stream.close()

    def _play_system(self, wav_bytes: bytes) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(wav_bytes)
            path = tmp.name
        system = platform.system()
        if system == "Darwin":
            cmd = ["afplay", path]
        elif system == "Windows":
            cmd = ["powershell", "-c", f"(New-Object Media.SoundPlayer '{path}').PlaySync()"]
        else:
            player = shutil.which("aplay") or shutil.which("paplay") or shutil.which("ffplay")
            if not player:
                raise RuntimeError("no audio player found (install pyaudio, or aplay/paplay)")
            cmd = [player, path] if "ffplay" not in player else [player, "-nodisp", "-autoexit", path]
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._proc.wait()
        finally:
            self._proc = None
            try:
                os.unlink(path)
            except OSError:
                pass


def wav_from_pcm_chunks(chunks: List[bytes], sample_rate: int) -> bytes:
    """Assemble streamed pcm_s16le chunks into a WAV file."""
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"".join(chunks))
    return buf.getvalue()


def decode_b64(data: str) -> bytes:
    return base64.b64decode(data)


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class SectionHeader(ctk.CTkLabel):
    def __init__(self, master, text: str, **kwargs):
        super().__init__(master, text=text, font=styles.font(ctk, styles.HEADING_SIZE, "bold"),
                         text_color=styles.BRASS, anchor="w", **kwargs)


class LabeledEntry(ctk.CTkFrame):
    """Label + entry on one row."""

    def __init__(self, master, label: str, placeholder: str = "", width: int = 320, show: Optional[str] = None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        ctk.CTkLabel(self, text=label, width=140, anchor="w", font=styles.font(ctk)).pack(side="left", padx=(0, 8))
        self.entry = ctk.CTkEntry(self, placeholder_text=placeholder, width=width, show=show)
        self.entry.pack(side="left", fill="x", expand=True)

    def get(self) -> str:
        return self.entry.get().strip()

    def set(self, value: str) -> None:
        self.entry.delete(0, "end")
        self.entry.insert(0, value)


class LabeledCombo(ctk.CTkFrame):
    def __init__(self, master, label: str, values: List[str], default: Optional[str] = None, width: int = 220, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        ctk.CTkLabel(self, text=label, width=110, anchor="w", font=styles.font(ctk)).pack(side="left", padx=(0, 8))
        self.combo = ctk.CTkComboBox(self, values=values or [""], width=width, state="readonly")
        self.combo.pack(side="left")
        if default:
            self.combo.set(default)
        elif values:
            self.combo.set(values[0])

    def get(self) -> str:
        return self.combo.get()

    def set_values(self, values: List[str], default: Optional[str] = None) -> None:
        self.combo.configure(values=values or [""])
        if default and default in values:
            self.combo.set(default)
        elif values:
            self.combo.set(values[0])


class ListPanel(ctk.CTkFrame):
    """Read-only scrolling list with a title and optional refresh button."""

    def __init__(self, master, title: str, on_refresh: Optional[Callable[[], None]] = None, height: int = 180, **kwargs):
        super().__init__(master, **styles.PANEL, **kwargs)
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=(10, 4))
        SectionHeader(header, title).pack(side="left")
        if on_refresh:
            ctk.CTkButton(header, text="↻ Refresh", width=90, command=on_refresh, **styles.BUTTON_SECONDARY).pack(side="right")
        self.box = ctk.CTkTextbox(self, height=height, font=styles.font(ctk, styles.SMALL_SIZE), wrap="none")
        self.box.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.box.configure(state="disabled")

    def set_lines(self, lines: List[str]) -> None:
        self.box.configure(state="normal")
        self.box.delete("1.0", "end")
        self.box.insert("1.0", "\n".join(lines) if lines else "(nothing yet)")
        self.box.configure(state="disabled")

    def append(self, line: str) -> None:
        self.box.configure(state="normal")
        if self.box.get("1.0", "end-1c") == "(nothing yet)":
            self.box.delete("1.0", "end")
        self.box.insert("end", line + "\n")
        self.box.configure(state="disabled")
        self.box.see("end")


class StatusBar(ctk.CTkFrame):
    """Bottom bar: connection state, truth labels, last message."""

    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color=styles.NAVY, height=32, corner_radius=0, **kwargs)
        self.conn = ctk.CTkLabel(self, text="● disconnected", text_color=styles.RED, font=styles.font(ctk, styles.SMALL_SIZE))
        self.conn.pack(side="left", padx=12)
        self.labels_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.labels_frame.pack(side="left")
        self.message = ctk.CTkLabel(self, text="", text_color=styles.SLATE_DIM, font=styles.font(ctk, styles.SMALL_SIZE))
        self.message.pack(side="right", padx=12)

    def set_connected(self, ok: bool, detail: str = "") -> None:
        self.conn.configure(text=f"● {'connected' if ok else 'disconnected'} {detail}".strip(),
                            text_color=styles.GREEN if ok else styles.RED)

    def set_truth_labels(self, labels: Dict[str, str]) -> None:
        for child in self.labels_frame.winfo_children():
            child.destroy()
        for name, value in labels.items():
            color = styles.TRUTH_COLORS.get(value, styles.SLATE_DIM)
            ctk.CTkLabel(self.labels_frame, text=f"{name}: {value}", text_color=color,
                         font=styles.font(ctk, styles.SMALL_SIZE)).pack(side="left", padx=6)

    def say(self, text: str, error: bool = False) -> None:
        self.message.configure(text=text, text_color=styles.RED if error else styles.SLATE_DIM)


def run_in_thread(fn: Callable[[], Any], on_success: Callable[[Any], None], on_error: Callable[[Exception], None],
                  widget) -> None:
    """Run ``fn`` off the UI thread and marshal the result back with ``after``."""

    def worker() -> None:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 — surfaced to the UI
            widget.after(0, lambda: on_error(exc))
            return
        widget.after(0, lambda: on_success(result))

    threading.Thread(target=worker, daemon=True).start()
