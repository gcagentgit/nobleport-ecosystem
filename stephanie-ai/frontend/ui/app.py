"""Stephanie.ai Voice — CustomTkinter desktop application."""

from __future__ import annotations

import base64
import json
import threading
from tkinter import filedialog, messagebox
from typing import Any, Dict, List, Optional

import customtkinter as ctk

from . import styles
from .components import (ApiClient, ApiError, AudioPlayer, LabeledCombo, LabeledEntry, ListPanel, SectionHeader,
                         StatusBar, load_settings, run_in_thread, save_settings, wav_from_pcm_chunks)

DEFAULT_API = "http://localhost:8000"
DEFAULT_TEXT = ("Hello, this is Stephanie from NoblePort Systems. Your estimate for 12 Main Street is ready "
                "for human review. Reply to this message or press 1 to speak with the project team.")


class StephanieAIApp:
    def __init__(self, api_base_url: Optional[str] = None):
        styles.apply_theme(ctk)
        self.settings = load_settings()
        self.api = ApiClient(
            api_base_url or self.settings.get("api_base_url", DEFAULT_API),
            human_approval=self.settings.get("human_approval", ""),
            admin_token=self.settings.get("admin_token", ""),
        )
        self.player = AudioPlayer()
        self.current_audio: Optional[bytes] = None
        self.current_meta: Dict[str, Any] = {}
        self.voices: List[Dict[str, Any]] = []
        self.emotions: List[str] = ["neutral", "warm", "happy", "sad", "angry", "excited", "calm", "surprised"]
        self.languages: List[Dict[str, str]] = [{"code": "en", "name": "English"}]
        self.selected_audio_path: Optional[str] = None
        self.nav_buttons: Dict[str, ctk.CTkButton] = {}

        self.window = ctk.CTk()
        self.window.title("Stephanie.ai — NoblePort Voice")
        self.window.geometry("1240x820")
        self.window.minsize(980, 680)
        self.window.configure(fg_color=styles.NAVY)
        self.setup_ui()
        self.window.after(200, self.refresh_health)

    # ------------------------------------------------------------------ layout
    def setup_ui(self) -> None:
        self.status_bar = StatusBar(self.window)
        self.status_bar.pack(side="bottom", fill="x")
        self.main_container = ctk.CTkFrame(self.window, fg_color="transparent")
        self.main_container.pack(fill="both", expand=True)
        self.create_sidebar()
        self.content_frame = ctk.CTkFrame(self.main_container, fg_color=styles.NAVY_LIGHT, corner_radius=0)
        self.content_frame.pack(side="right", fill="both", expand=True)
        self.show_voice_generator()

    def create_sidebar(self) -> None:
        sidebar = ctk.CTkFrame(self.main_container, width=230, **styles.SIDEBAR)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        ctk.CTkLabel(sidebar, text="Stephanie.ai", font=styles.font(ctk, styles.TITLE_SIZE, "bold"),
                     text_color=styles.BRASS).pack(pady=(28, 0))
        ctk.CTkLabel(sidebar, text="NoblePort Voice Layer", font=styles.font(ctk, styles.SMALL_SIZE),
                     text_color=styles.SLATE_DIM).pack(pady=(0, 24))
        for text, command in [
            ("🎙  Voice Generator", self.show_voice_generator),
            ("🧬  Voice Cloning", self.show_voice_cloning),
            ("📞  Call Management", self.show_call_management),
            ("💬  Messages", self.show_messages),
            ("⚙  Settings", self.show_settings),
        ]:
            btn = ctk.CTkButton(sidebar, text=text, command=command, height=42, anchor="w",
                                font=styles.font(ctk), **styles.BUTTON_SECONDARY)
            btn.pack(fill="x", padx=16, pady=4)
            self.nav_buttons[text] = btn
        ctk.CTkLabel(sidebar, text="Human-gated. Audit-first.\nSTAGED until a provider is wired.",
                     font=styles.font(ctk, styles.SMALL_SIZE), text_color=styles.SLATE_DIM, justify="left").pack(
            side="bottom", pady=18, padx=16, anchor="w")

    def clear_content(self) -> None:
        for widget in self.content_frame.winfo_children():
            widget.destroy()

    def _title(self, text: str, subtitle: str = "") -> None:
        ctk.CTkLabel(self.content_frame, text=text, font=styles.font(ctk, styles.TITLE_SIZE, "bold"),
                     text_color=styles.WHITE, anchor="w").pack(fill="x", padx=24, pady=(22, 0))
        if subtitle:
            ctk.CTkLabel(self.content_frame, text=subtitle, font=styles.font(ctk, styles.SMALL_SIZE),
                         text_color=styles.SLATE_DIM, anchor="w", justify="left").pack(fill="x", padx=24, pady=(2, 10))

    def _error(self, exc: Exception) -> None:
        self.status_bar.say(str(exc), error=True)
        messagebox.showerror("Stephanie.ai", str(exc))

    # ------------------------------------------------------------------ health
    def refresh_health(self) -> None:
        def done(health: Dict[str, Any]) -> None:
            self.status_bar.set_connected(True, f"({self.api.base_url})")
            self.status_bar.set_truth_labels(health.get("truth_labels", {}))
            self.status_bar.say(f"synthesizer: {health.get('synthesizer')} · transport: {health.get('telephony_transport')}")

        def failed(exc: Exception) -> None:
            self.status_bar.set_connected(False)
            self.status_bar.say(str(exc), error=True)

        run_in_thread(lambda: self.api.get("/health"), done, failed, self.window)

    def load_voices(self, on_loaded=None) -> None:
        def done(data: Dict[str, Any]) -> None:
            self.voices = data.get("voices", [])
            self.emotions = data.get("emotions", self.emotions)
            self.languages = data.get("languages", self.languages)
            if on_loaded:
                on_loaded()

        run_in_thread(lambda: self.api.get("/api/v1/voices"), done, self._error, self.window)

    def _voice_names(self) -> List[str]:
        return [v["name"] for v in self.voices] or ["Stephanie"]

    def _voice_id_for(self, name: str) -> str:
        for v in self.voices:
            if v["name"] == name:
                return v["voice_id"]
        return "stephanie_primary"

    # ------------------------------------------------------------------ voice generator
    def show_voice_generator(self) -> None:
        self.clear_content()
        self._title("Voice Generator", "Text → Stephanie's voice. Prohibited public claims are blocked before synthesis.")

        self.text_input = ctk.CTkTextbox(self.content_frame, height=140, font=styles.font(ctk))
        self.text_input.pack(fill="x", padx=24, pady=(4, 10))
        self.text_input.insert("1.0", DEFAULT_TEXT)

        settings = ctk.CTkFrame(self.content_frame, **styles.PANEL)
        settings.pack(fill="x", padx=24, pady=6)
        row1 = ctk.CTkFrame(settings, fg_color="transparent")
        row1.pack(fill="x", padx=12, pady=(12, 4))
        self.voice_combo = LabeledCombo(row1, "Voice", self._voice_names())
        self.voice_combo.pack(side="left", padx=(0, 16))
        self.emotion_combo = LabeledCombo(row1, "Emotion", self.emotions, default="neutral", width=150)
        self.emotion_combo.pack(side="left", padx=(0, 16))
        self.language_combo = LabeledCombo(row1, "Language", [l["name"] for l in self.languages], width=170)
        self.language_combo.pack(side="left")

        row2 = ctk.CTkFrame(settings, fg_color="transparent")
        row2.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(row2, text="Speed", width=110, anchor="w", font=styles.font(ctk)).pack(side="left", padx=(0, 8))
        self.speed_slider = ctk.CTkSlider(row2, from_=0.5, to=2.0, number_of_steps=30, width=220,
                                          command=lambda v: self.speed_label.configure(text=f"{v:.2f}×"))
        self.speed_slider.set(1.0)
        self.speed_slider.pack(side="left")
        self.speed_label = ctk.CTkLabel(row2, text="1.00×", width=50, font=styles.font(ctk))
        self.speed_label.pack(side="left", padx=(6, 24))
        ctk.CTkLabel(row2, text="Intensity", width=70, anchor="w", font=styles.font(ctk)).pack(side="left")
        self.intensity_slider = ctk.CTkSlider(row2, from_=0.0, to=1.5, number_of_steps=15, width=160)
        self.intensity_slider.set(1.0)
        self.intensity_slider.pack(side="left", padx=(6, 0))

        row3 = ctk.CTkFrame(settings, fg_color="transparent")
        row3.pack(fill="x", padx=12, pady=(4, 12))
        self.background_combo = LabeledCombo(row3, "Background", ["none", "office", "jobsite", "hold_music"], width=150)
        self.background_combo.pack(side="left", padx=(0, 16))
        self.enhance_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(row3, text="Enhance audio", variable=self.enhance_var, font=styles.font(ctk)).pack(side="left", padx=8)
        self.disclaimer_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(row3, text="Append disclaimer", variable=self.disclaimer_var, font=styles.font(ctk)).pack(side="left", padx=8)
        self.stream_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(row3, text="Stream (WebSocket)", variable=self.stream_var, font=styles.font(ctk)).pack(side="left", padx=8)

        actions = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        actions.pack(fill="x", padx=24, pady=(10, 4))
        self.generate_btn = ctk.CTkButton(actions, text="Generate Voice", command=self.generate_voice, height=42, width=180,
                                          font=styles.font(ctk, styles.HEADING_SIZE, "bold"), **styles.BUTTON)
        self.generate_btn.pack(side="left")
        ctk.CTkButton(actions, text="Check compliance", command=self.check_compliance, height=42, width=150,
                      **styles.BUTTON_SECONDARY).pack(side="left", padx=10)
        self.play_btn = ctk.CTkButton(actions, text="▶ Play", command=self.play_audio, width=100, state="disabled",
                                      **styles.BUTTON_SECONDARY)
        self.play_btn.pack(side="left", padx=(30, 6))
        self.stop_btn = ctk.CTkButton(actions, text="■ Stop", command=self.stop_audio, width=100, state="disabled",
                                      **styles.BUTTON_SECONDARY)
        self.stop_btn.pack(side="left", padx=6)
        self.save_btn = ctk.CTkButton(actions, text="💾 Save WAV", command=self.save_audio, width=120, state="disabled",
                                      **styles.BUTTON_SECONDARY)
        self.save_btn.pack(side="left", padx=6)

        self.progress_bar = ctk.CTkProgressBar(self.content_frame, progress_color=styles.BRASS)
        self.progress_bar.pack(fill="x", padx=24, pady=(8, 4))
        self.progress_bar.set(0)
        self.meta_label = ctk.CTkLabel(self.content_frame, text="", font=styles.font(ctk, styles.SMALL_SIZE),
                                       text_color=styles.SLATE_DIM, anchor="w", justify="left")
        self.meta_label.pack(fill="x", padx=24)

        self.load_voices(on_loaded=self._refresh_generator_combos)

    def _refresh_generator_combos(self) -> None:
        if hasattr(self, "voice_combo") and self.voice_combo.winfo_exists():
            self.voice_combo.set_values(self._voice_names())
            self.emotion_combo.set_values(self.emotions, default="neutral")
            self.language_combo.set_values([l["name"] for l in self.languages])

    def _generation_payload(self) -> Dict[str, Any]:
        text = self.text_input.get("1.0", "end-1c").strip()
        if not text:
            raise ApiError("Please enter text to speak.")
        lang_code = next((l["code"] for l in self.languages if l["name"] == self.language_combo.get()), "en")
        background = self.background_combo.get()
        return {
            "text": text,
            "voice_id": self._voice_id_for(self.voice_combo.get()),
            "emotion": self.emotion_combo.get(),
            "speed": round(float(self.speed_slider.get()), 2),
            "emotion_intensity": round(float(self.intensity_slider.get()), 2),
            "language": lang_code,
            "enhance_audio": bool(self.enhance_var.get()),
            "append_disclaimer": bool(self.disclaimer_var.get()),
            "background_audio": None if background == "none" else background,
        }

    def check_compliance(self) -> None:
        text = self.text_input.get("1.0", "end-1c").strip()

        def done(result: Dict[str, Any]) -> None:
            if result.get("ok"):
                messagebox.showinfo("Compliance", "No prohibited public-material terms found.")
            else:
                alt = result.get("approved_alternatives", {}).get("stephanie_ai", "")
                messagebox.showwarning("Compliance", "Flagged: " + ", ".join(result["flagged_terms"]) +
                                       (f"\n\nApproved wording:\n{alt}" if alt else ""))

        run_in_thread(lambda: self.api.post("/api/v1/voice/compliance-check", {"text": text}), done, self._error, self.window)

    def generate_voice(self) -> None:
        try:
            payload = self._generation_payload()
        except ApiError as exc:
            self._error(exc)
            return
        self.generate_btn.configure(state="disabled")
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start()
        self.status_bar.say("generating…")
        if self.stream_var.get():
            threading.Thread(target=self._stream_voice_thread, args=(payload,), daemon=True).start()
        else:
            run_in_thread(lambda: self.api.post("/api/v1/voice/generate", payload), self._on_voice_generated,
                          self._on_voice_failed, self.window)

    def _stream_voice_thread(self, payload: Dict[str, Any]) -> None:
        """Consume /ws/stream and assemble a WAV as chunks arrive."""
        try:
            from websockets.sync.client import connect  # type: ignore
        except ImportError:
            self.window.after(0, lambda: self._on_voice_failed(ApiError("pip install websockets to enable streaming")))
            return
        ws_url = self.api.base_url.replace("https://", "wss://").replace("http://", "ws://") + "/ws/stream"
        chunks: List[bytes] = []
        sample_rate = 44100
        try:
            with connect(ws_url) as ws:
                ws.send(json.dumps({"type": "generate", **payload}))
                while True:
                    msg = json.loads(ws.recv())
                    if msg["type"] == "audio_chunk":
                        chunks.append(base64.b64decode(msg["data"]))
                        sample_rate = msg.get("sample_rate", sample_rate)
                        seg, total = msg.get("segment", 0) + 1, msg.get("segments_total", 1)
                        self.window.after(0, lambda s=seg, t=total: self.status_bar.say(f"streaming segment {s}/{t}"))
                    elif msg["type"] == "done":
                        break
                    elif msg["type"] == "error":
                        raise ApiError(msg.get("message", "stream error"))
                ws.send(json.dumps({"type": "stop"}))
        except Exception as exc:  # noqa: BLE001
            self.window.after(0, lambda: self._on_voice_failed(exc))
            return
        wav = wav_from_pcm_chunks(chunks, sample_rate)
        result = {"audio": base64.b64encode(wav).decode(), "format": "wav",
                  "metadata": {"sample_rate": sample_rate, "duration": round(len(b"".join(chunks)) / 2 / sample_rate, 2),
                               "voice_id": payload["voice_id"], "emotion": payload["emotion"], "truth_label": "STAGED",
                               "synthesizer": "stream"}}
        self.window.after(0, lambda: self._on_voice_generated(result))

    def _on_voice_generated(self, result: Dict[str, Any]) -> None:
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.progress_bar.set(1.0)
        self.generate_btn.configure(state="normal")
        self.current_audio = base64.b64decode(result["audio"])
        self.current_meta = result.get("metadata", {})
        meta = self.current_meta
        self.meta_label.configure(text=(f"{meta.get('voice_name', meta.get('voice_id'))} · {meta.get('emotion')} · "
                                        f"{meta.get('duration')}s @ {meta.get('sample_rate')} Hz · "
                                        f"{meta.get('synthesizer')} [{meta.get('truth_label')}] · {result.get('format', 'wav')}"))
        for btn in (self.play_btn, self.stop_btn, self.save_btn):
            btn.configure(state="normal")
        self.status_bar.say("voice generated")

    def _on_voice_failed(self, exc: Exception) -> None:
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.progress_bar.set(0)
        self.generate_btn.configure(state="normal")
        self._error(exc)

    def play_audio(self) -> None:
        if not self.current_audio or self.player.playing:
            return
        if self.current_meta.get("format") == "mp3":
            self._error(ApiError("MP3 playback is not supported in the desktop client; save the file instead."))
            return
        self.play_btn.configure(state="disabled")
        self.player.play(self.current_audio, on_done=lambda: self.window.after(0, lambda: self.play_btn.configure(state="normal")))

    def stop_audio(self) -> None:
        self.player.stop()

    def save_audio(self) -> None:
        if not self.current_audio:
            return
        path = filedialog.asksaveasfilename(defaultextension=".wav", initialfile="stephanie_voice.wav",
                                            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")])
        if path:
            with open(path, "wb") as fh:
                fh.write(self.current_audio)
            self.status_bar.say(f"saved {path}")

    # ------------------------------------------------------------------ voice cloning
    def show_voice_cloning(self) -> None:
        self.clear_content()
        self._title("Voice Cloning", "Upload a WAV sample (≥ 1 s). The clone is STAGED: it captures pitch and pacing, not a neural voice.")
        panel = ctk.CTkFrame(self.content_frame, **styles.PANEL)
        panel.pack(fill="x", padx=24, pady=8)
        row = ctk.CTkFrame(panel, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=12)
        ctk.CTkButton(row, text="Select WAV file", command=self.select_audio_file, **styles.BUTTON_SECONDARY).pack(side="left")
        self.selected_file_label = ctk.CTkLabel(row, text="No file selected", text_color=styles.SLATE_DIM, font=styles.font(ctk))
        self.selected_file_label.pack(side="left", padx=12)
        self.voice_name_entry = LabeledEntry(panel, "Voice name", "e.g. Site Supervisor")
        self.voice_name_entry.pack(fill="x", padx=12, pady=6)
        self.voice_desc_entry = LabeledEntry(panel, "Description", "optional")
        self.voice_desc_entry.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkButton(self.content_frame, text="Clone Voice", command=self.clone_voice, height=42, width=180,
                      font=styles.font(ctk, styles.HEADING_SIZE, "bold"), **styles.BUTTON).pack(padx=24, pady=10, anchor="w")

        self.voices_panel = ListPanel(self.content_frame, "Voice Library", on_refresh=self.refresh_voice_library, height=200)
        self.voices_panel.pack(fill="both", expand=True, padx=24, pady=8)
        row2 = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        row2.pack(fill="x", padx=24, pady=(0, 16))
        self.delete_voice_entry = LabeledEntry(row2, "Cloned voice id", "cloned_…", width=240)
        self.delete_voice_entry.pack(side="left")
        ctk.CTkButton(row2, text="Delete", width=90, command=self.delete_voice, **styles.BUTTON_SECONDARY).pack(side="left", padx=8)
        ctk.CTkButton(row2, text="Share link", width=100, command=self.share_voice, **styles.BUTTON_SECONDARY).pack(side="left")
        self.refresh_voice_library()

    def refresh_voice_library(self) -> None:
        def done() -> None:
            if hasattr(self, "voices_panel") and self.voices_panel.winfo_exists():
                self.voices_panel.set_lines([
                    f"{v['voice_id']:<28} {v['name']:<24} {v['gender']:<7} {v['language']:<6} "
                    f"[{v.get('truth_label', 'STAGED')}]{'  (cloned)' if v.get('cloned') else ''}" for v in self.voices
                ])

        self.load_voices(on_loaded=done)

    def select_audio_file(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("WAV files", "*.wav"), ("All files", "*.*")])
        if path:
            self.selected_audio_path = path
            self.selected_file_label.configure(text=path, text_color=styles.SLATE)

    def clone_voice(self) -> None:
        if not self.selected_audio_path:
            self._error(ApiError("Select a WAV file first."))
            return
        name = self.voice_name_entry.get()
        if not name:
            self._error(ApiError("Enter a voice name."))
            return
        try:
            with open(self.selected_audio_path, "rb") as fh:
                audio_b64 = base64.b64encode(fh.read()).decode()
        except OSError as exc:
            self._error(exc)
            return
        payload = {"audio_data": audio_b64, "name": name, "description": self.voice_desc_entry.get()}

        def done(result: Dict[str, Any]) -> None:
            messagebox.showinfo("Voice cloned", f"Voice id: {result['voice_id']}\nTruth label: {result.get('truth_label')}")
            self.refresh_voice_library()

        run_in_thread(lambda: self.api.post("/api/v1/voice/clone", payload), done, self._error, self.window)

    def delete_voice(self) -> None:
        voice_id = self.delete_voice_entry.get()
        if not voice_id:
            return
        run_in_thread(lambda: self.api.delete(f"/api/v1/voices/{voice_id}"),
                      lambda r: self.refresh_voice_library(), self._error, self.window)

    def share_voice(self) -> None:
        voice_id = self.delete_voice_entry.get()
        if not voice_id:
            return
        run_in_thread(lambda: self.api.get(f"/api/v1/voices/{voice_id}/share"),
                      lambda r: messagebox.showinfo("Share link", r["share_link"]), self._error, self.window)

    # ------------------------------------------------------------------ calls
    def show_call_management(self) -> None:
        self.clear_content()
        self._title("Call Management", "Outbound calls to real numbers require the human-approval token (Settings). "
                                       "Without Twilio credentials everything here is STAGED.")
        panel = ctk.CTkFrame(self.content_frame, **styles.PANEL)
        panel.pack(fill="x", padx=24, pady=8)
        self.phone_entry = LabeledEntry(panel, "Phone number", "+16175551234", width=220)
        self.phone_entry.pack(fill="x", padx=12, pady=(12, 6))
        ctk.CTkLabel(panel, text="Spoken prompt (optional)", anchor="w", font=styles.font(ctk)).pack(fill="x", padx=12)
        self.call_text = ctk.CTkTextbox(panel, height=80, font=styles.font(ctk))
        self.call_text.pack(fill="x", padx=12, pady=(2, 6))
        self.call_text.insert("1.0", "This is Stephanie from NoblePort. Your permit checklist is ready for review.")
        row = ctk.CTkFrame(panel, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 12))
        self.call_voice_combo = LabeledCombo(row, "Voice", self._voice_names())
        self.call_voice_combo.pack(side="left", padx=(0, 16))
        self.record_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(row, text="Record", variable=self.record_var, font=styles.font(ctk)).pack(side="left", padx=8)
        self.transcribe_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(row, text="Transcribe", variable=self.transcribe_var, font=styles.font(ctk)).pack(side="left", padx=8)
        ctk.CTkButton(row, text="📞 Make Call", command=self.make_call, height=38, width=150, **styles.BUTTON).pack(side="right")

        self.calls_panel = ListPanel(self.content_frame, "Calls", on_refresh=self.refresh_calls, height=170)
        self.calls_panel.pack(fill="both", expand=True, padx=24, pady=8)

        row2 = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        row2.pack(fill="x", padx=24, pady=(0, 6))
        self.session_entry = LabeledEntry(row2, "Session id", "call_…", width=220)
        self.session_entry.pack(side="left")
        for text, cmd in [("End", self.end_call), ("Transcript", self.show_transcript), ("Rec start", lambda: self._call_action("recording/start")),
                          ("Rec stop", lambda: self._call_action("recording/stop"))]:
            ctk.CTkButton(row2, text=text, width=90, command=cmd, **styles.BUTTON_SECONDARY).pack(side="left", padx=4)
        row3 = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        row3.pack(fill="x", padx=24, pady=(0, 16))
        self.forward_entry = LabeledEntry(row3, "Forward to", "+1…", width=220)
        self.forward_entry.pack(side="left")
        ctk.CTkButton(row3, text="Forward", width=90, command=self.forward_call, **styles.BUTTON_SECONDARY).pack(side="left", padx=4)
        self.load_voices(on_loaded=lambda: self.call_voice_combo.set_values(self._voice_names())
                         if self.call_voice_combo.winfo_exists() else None)
        self.refresh_calls()

    def refresh_calls(self) -> None:
        def done(data: Dict[str, Any]) -> None:
            if hasattr(self, "calls_panel") and self.calls_panel.winfo_exists():
                self.calls_panel.set_lines([
                    f"{c['session_id']:<22} {c['direction']:<9} {c['to_number']:<16} {c['status']:<11} "
                    f"rec={'on' if c['recording_enabled'] else 'off'} [{c['truth_label']}]" for c in data.get("calls", [])
                ])

        run_in_thread(lambda: self.api.get("/api/v1/calls"), done, self._error, self.window)

    def make_call(self) -> None:
        number = self.phone_entry.get()
        if not number:
            self._error(ApiError("Enter a phone number."))
            return
        text = self.call_text.get("1.0", "end-1c").strip() or None
        payload = {"to_number": number, "text": text, "voice_id": self._voice_id_for(self.call_voice_combo.get()),
                   "record": bool(self.record_var.get()), "transcribe": bool(self.transcribe_var.get())}

        def done(result: Dict[str, Any]) -> None:
            self.session_entry.set(result["session_id"])
            self.status_bar.say(f"call {result['session_id']} {result['status']} [{result['truth_label']}]")
            self.refresh_calls()

        run_in_thread(lambda: self.api.post("/api/v1/calls", payload), done, self._error, self.window)

    def _call_action(self, action: str) -> None:
        sid = self.session_entry.get()
        if not sid:
            self._error(ApiError("Enter a session id."))
            return
        run_in_thread(lambda: self.api.post(f"/api/v1/calls/{sid}/{action}"), lambda r: self.refresh_calls(), self._error, self.window)

    def end_call(self) -> None:
        self._call_action("end")

    def forward_call(self) -> None:
        sid, target = self.session_entry.get(), self.forward_entry.get()
        if not sid or not target:
            self._error(ApiError("Enter a session id and a target number."))
            return
        run_in_thread(lambda: self.api.post(f"/api/v1/calls/{sid}/forward", {"target_number": target}),
                      lambda r: self.refresh_calls(), self._error, self.window)

    def show_transcript(self) -> None:
        sid = self.session_entry.get()
        if not sid:
            return

        def done(data: Dict[str, Any]) -> None:
            lines = [f"[{l['at'][11:19]}] {l['speaker']}: {l['text']}" for l in data.get("transcript", [])]
            messagebox.showinfo("Transcript", "\n".join(lines) or "(no transcript lines yet)")

        run_in_thread(lambda: self.api.get(f"/api/v1/calls/{sid}/transcription"), done, self._error, self.window)

    # ------------------------------------------------------------------ messages
    def show_messages(self) -> None:
        self.clear_content()
        self._title("SMS Messages", "Outbound SMS is screened against the NoblePort danger-word list and human-gated when LIVE.")
        panel = ctk.CTkFrame(self.content_frame, **styles.PANEL)
        panel.pack(fill="x", padx=24, pady=8)
        self.recipient_entry = LabeledEntry(panel, "To", "+16175551234", width=220)
        self.recipient_entry.pack(fill="x", padx=12, pady=(12, 6))
        self.message_text = ctk.CTkTextbox(panel, height=100, font=styles.font(ctk))
        self.message_text.pack(fill="x", padx=12, pady=6)
        row = ctk.CTkFrame(panel, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 12))
        self.sms_disclaimer_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(row, text="Append disclaimer", variable=self.sms_disclaimer_var, font=styles.font(ctk)).pack(side="left")
        ctk.CTkButton(row, text="Send Message", command=self.send_message, height=38, width=150, **styles.BUTTON).pack(side="right")
        self.messages_panel = ListPanel(self.content_frame, "Message History", on_refresh=self.refresh_messages, height=240)
        self.messages_panel.pack(fill="both", expand=True, padx=24, pady=8)
        self.refresh_messages()

    def refresh_messages(self) -> None:
        def done(data: Dict[str, Any]) -> None:
            if hasattr(self, "messages_panel") and self.messages_panel.winfo_exists():
                self.messages_panel.set_lines([
                    f"{m['timestamp'][11:19]} {m['direction']:<8} {m['to'] if m['direction'] == 'outbound' else m['from']:<16} "
                    f"{m['status']:<9} [{m['truth_label']}] {m['body'][:60]}" for m in data.get("messages", [])
                ])

        run_in_thread(lambda: self.api.get("/api/v1/messages"), done, self._error, self.window)

    def send_message(self) -> None:
        recipient = self.recipient_entry.get()
        message = self.message_text.get("1.0", "end-1c").strip()
        if not recipient or not message:
            self._error(ApiError("Enter a recipient and a message."))
            return
        payload = {"to_number": recipient, "message": message, "append_disclaimer": bool(self.sms_disclaimer_var.get())}

        def done(result: Dict[str, Any]) -> None:
            self.message_text.delete("1.0", "end")
            self.status_bar.say(f"message {result['message_id']} {result['status']} [{result['truth_label']}]")
            self.refresh_messages()

        run_in_thread(lambda: self.api.post("/api/v1/messages", payload), done, self._error, self.window)

    # ------------------------------------------------------------------ settings
    def show_settings(self) -> None:
        self.clear_content()
        self._title("Settings", "Connection and governance tokens are saved to ~/.stephanie_voice.json")
        panel = ctk.CTkFrame(self.content_frame, **styles.PANEL)
        panel.pack(fill="x", padx=24, pady=8)
        SectionHeader(panel, "Backend").pack(fill="x", padx=12, pady=(12, 4))
        self.api_url_entry = LabeledEntry(panel, "API URL", DEFAULT_API, width=360)
        self.api_url_entry.set(self.api.base_url)
        self.api_url_entry.pack(fill="x", padx=12, pady=4)
        SectionHeader(panel, "Governance").pack(fill="x", padx=12, pady=(14, 4))
        self.approval_entry = LabeledEntry(panel, "Human approval", "X-Human-Approval token", width=360, show="•")
        self.approval_entry.set(self.api.human_approval)
        self.approval_entry.pack(fill="x", padx=12, pady=4)
        self.admin_entry = LabeledEntry(panel, "Admin token", "X-Admin-Token", width=360, show="•")
        self.admin_entry.set(self.api.admin_token)
        self.admin_entry.pack(fill="x", padx=12, pady=(4, 12))

        vs = ctk.CTkFrame(self.content_frame, **styles.PANEL)
        vs.pack(fill="x", padx=24, pady=8)
        SectionHeader(vs, "Voice settings (ElevenLabs-style)").pack(fill="x", padx=12, pady=(12, 4))
        self.setting_sliders: Dict[str, ctk.CTkSlider] = {}
        for key, label in [("stability", "Stability"), ("similarity_boost", "Similarity"), ("style", "Style")]:
            row = ctk.CTkFrame(vs, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=3)
            ctk.CTkLabel(row, text=label, width=110, anchor="w", font=styles.font(ctk)).pack(side="left")
            slider = ctk.CTkSlider(row, from_=0.0, to=1.0, number_of_steps=20, width=260)
            slider.pack(side="left")
            self.setting_sliders[key] = slider
        self.speaker_boost_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(vs, text="Speaker boost", variable=self.speaker_boost_var, font=styles.font(ctk)).pack(anchor="w", padx=12, pady=(4, 12))

        ctk.CTkButton(self.content_frame, text="Save Settings", command=self.save_settings, height=42, width=180,
                      font=styles.font(ctk, styles.HEADING_SIZE, "bold"), **styles.BUTTON).pack(padx=24, pady=12, anchor="w")

        def loaded(data: Dict[str, Any]) -> None:
            for key, slider in self.setting_sliders.items():
                if slider.winfo_exists():
                    slider.set(float(data.get(key, 0.5)))
            self.speaker_boost_var.set(bool(data.get("use_speaker_boost", True)))

        run_in_thread(lambda: self.api.get("/api/v1/voice/settings"), loaded, lambda e: None, self.window)

    def save_settings(self) -> None:
        self.api = ApiClient(self.api_url_entry.get() or DEFAULT_API, human_approval=self.approval_entry.get(),
                             admin_token=self.admin_entry.get())
        save_settings({"api_base_url": self.api.base_url, "human_approval": self.api.human_approval,
                       "admin_token": self.api.admin_token})
        payload = {key: round(float(slider.get()), 2) for key, slider in self.setting_sliders.items()}
        payload["use_speaker_boost"] = bool(self.speaker_boost_var.get())
        run_in_thread(lambda: self.api.put("/api/v1/voice/settings", payload),
                      lambda r: self.status_bar.say("settings saved"), self._error, self.window)
        self.refresh_health()

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        self.window.mainloop()
