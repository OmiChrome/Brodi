"""Local wake detection and utterance capture. This module never opens a cloud session."""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import threading
import time
import urllib.request
import zipfile
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .paths import user_data_dir

MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"


def model_path() -> Path:
    return Path(os.environ.get("BRODI_WAKE_MODEL_PATH", user_data_dir() / "models" / MODEL_NAME))


def install_model() -> Path:
    target = model_path()
    if (target / "am/final.mdl").exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    archive = target.parent / (MODEL_NAME + ".zip")
    try:
        urllib.request.urlretrieve(MODEL_URL, archive)
        with zipfile.ZipFile(archive) as zipped:
            for member in zipped.infolist():
                resolved = (target.parent / member.filename).resolve()
                if not resolved.is_relative_to(target.parent.resolve()):
                    raise ValueError("Unsafe path in wake model archive")
            zipped.extractall(target.parent)
        extracted = target.parent / MODEL_NAME
        if extracted != target:
            extracted.rename(target)
    finally:
        archive.unlink(missing_ok=True)
    if not (target / "am/final.mdl").exists():
        raise RuntimeError("Wake model download did not contain the expected model.")
    return target


@dataclass
class WakeStats:
    detections: int = 0
    commands: int = 0
    expired_windows: int = 0
    false_positives: int = 0

    @property
    def false_positive_rate(self) -> float:
        return self.false_positives / max(1, self.detections)


class WakeController:
    """Testable text/engine boundary: wake phrase -> remainder, or a brief answer window."""

    def __init__(
        self,
        on_command: Callable[[str], None],
        *,
        on_wake: Callable[[], None] | None = None,
        on_event: Callable | None = None,
        words: list[str] | None = None,
        mode: str | None = None,
        window_s: float = 20,
    ):
        words = words or os.environ.get("BRODI_WAKE_WORDS", "hey bro,yo bro").split(",")
        self.pattern = re.compile(
            r"^\s*(?:"
            + "|".join(re.escape(w.strip()).replace(r"\ ", r"[\s,]+") for w in words if w.strip())
            + r")\b[\s,!.:;?-]*(?:please\s+)?",
            re.I,
        )
        self.mode = mode or os.environ.get("BRODI_WAKE_MODE", "both")
        if self.mode not in {"continuous", "hotkey", "both"}:
            raise ValueError("BRODI_WAKE_MODE must be continuous, hotkey, or both")
        self.on_command, self.on_wake, self.on_event = on_command, on_wake, on_event
        self.window_s = window_s
        self.active_until = 0.0
        self.in_utterance = False
        self.stats = WakeStats()

    def _event(self, message: str):
        if self.on_event:
            self.on_event(
                "progress",
                message,
                counters={**asdict(self.stats), "false_positive_rate": self.stats.false_positive_rate},
            )

    def activate(self) -> None:
        self.active_until = time.monotonic() + self.window_s
        self.in_utterance = True
        self.stats.detections += 1
        if self.on_wake:
            self.on_wake()
        self._event("Wake detected; listening for your task")

    def mark_false_positive(self) -> None:
        self.stats.false_positives = min(self.stats.detections, self.stats.false_positives + 1)
        self._event("Wake marked as a false positive")

    def tick(self) -> None:
        if self.active_until and time.monotonic() > self.active_until:
            self.stats.expired_windows += 1
            self.active_until = 0
            self.in_utterance = False
            self._event("Wake listening window expired; waiting locally")

    def feed_text(self, text: str, *, final: bool = True) -> None:
        now = time.monotonic()
        match = self.pattern.match(text)
        if match and self.mode != "hotkey" and not self.in_utterance:
            self.activate()
        active = now <= self.active_until
        if not active:
            if self.active_until:
                self.stats.expired_windows += 1
                self.active_until = 0
            self.in_utterance = False
            return
        if final:
            remainder = text[match.end() :].strip() if match else text.strip()
            self.in_utterance = False
            self.active_until = time.monotonic() + self.window_s
            if remainder:
                self.stats.commands += 1
            self.on_command(remainder)


class LocalListener:
    """Mic -> local Vosk endpointing/spotting; optional custom openWakeWord first stage."""

    def __init__(self, controller: WakeController, *, sensitivity: float | None = None):
        self.controller = controller
        self.sensitivity = (
            sensitivity if sensitivity is not None else float(os.environ.get("BRODI_WAKE_SENSITIVITY", "0.65"))
        )
        if not 0.5 <= self.sensitivity <= 0.8:
            raise ValueError("Wake sensitivity must be between 0.5 and 0.8")
        self._stop = threading.Event()
        self._thread = None
        self._stream = None
        self._keys = None
        self._audio = queue.Queue(maxsize=20)
        self.error: str | None = None
        self._recognizer = None
        self._wake_model = None
        self._reset_requested = threading.Event()
        self._overrun = threading.Event()
        self._playback = threading.Event()

    def suspend_for_playback(self) -> None:
        """Discard speaker echo during reply playback; the hotkey remains available."""
        self._playback.set()
        self._reset_requested.set()

    def resume_after_playback(self) -> None:
        self._reset_requested.set()
        self._playback.clear()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        import sounddevice as sd
        import vosk
        import webrtcvad

        if not model_path().exists():
            raise RuntimeError("Local wake model is missing. Run python -m brodi.wake --download-model first.")
        vosk.SetLogLevel(-1)
        self._model = vosk.Model(str(model_path()))
        self._recognizer = vosk.KaldiRecognizer(self._model, 16000)
        self._recognizer.SetWords(True)
        self._recognizer.SetPartialWords(True)
        self._vad = webrtcvad.Vad(2)
        self._wake_model = None
        custom = os.environ.get("BRODI_OPENWAKEWORD_MODEL")
        if custom:
            try:
                from openwakeword.model import Model

                self._wake_model = Model(wakeword_models=[custom], inference_framework="onnx")
            except Exception as exc:
                self.controller._event(f"Custom wake engine unavailable; using local VAD/keyword fallback: {exc}")
        self._stop.clear()

        def capture(data, frames, timing, status):
            if self._playback.is_set():
                return
            try:
                self._audio.put_nowait(bytes(data))
            except queue.Full:
                # Bound memory use and discard stale input rather than queueing old commands.
                self.error = "Microphone processing fell behind; some audio was dropped."
                self._overrun.set()

        self._stream = sd.RawInputStream(samplerate=16000, blocksize=1280, channels=1, dtype="int16", callback=capture)
        self._stream.start()
        if self.controller.mode in {"hotkey", "both"}:
            try:
                from pynput.keyboard import GlobalHotKeys

                def hotkey():
                    self._reset_requested.set()
                    self.controller.activate()

                self._keys = GlobalHotKeys({os.environ.get("BRODI_WAKE_HOTKEY", "<ctrl>+<alt>+b"): hotkey})
                self._keys.start()
            except Exception as exc:
                self.stop()
                raise RuntimeError(
                    "Could not register the wake hotkey. Set continuous mode or check accessibility permissions."
                ) from exc
        self._thread = threading.Thread(target=self._run, daemon=True, name="brodi-local-wake")
        self._thread.start()

    def _run(self) -> None:
        import numpy as np

        rolling = deque(maxlen=40)
        last_voice = time.monotonic()
        try:
            while not self._stop.is_set():
                self.controller.tick()
                if self._overrun.is_set():
                    self._recognizer.Reset()
                    while not self._audio.empty():
                        try:
                            self._audio.get_nowait()
                        except queue.Empty:
                            break
                    self.controller.active_until = 0
                    self.controller.in_utterance = False
                    self._overrun.clear()
                    self.controller._event("Audio was dropped. Say the wake phrase again.")
                if self._reset_requested.is_set():
                    self._recognizer.Reset()
                    while not self._audio.empty():
                        try:
                            self._audio.get_nowait()
                        except queue.Empty:
                            break
                    self._reset_requested.clear()
                try:
                    audio = self._audio.get(timeout=0.2)
                except queue.Empty:
                    continue
                if self._playback.is_set():
                    self._recognizer.Reset()
                    continue
                rolling.append(audio)
                speech = any(self._vad.is_speech(audio[i : i + 640], 16000) for i in range(0, len(audio) - 639, 640))
                if speech:
                    last_voice = time.monotonic()
                if not speech and time.monotonic() - last_voice > 2 and time.monotonic() > self.controller.active_until:
                    self._recognizer.Reset()
                    continue
                if (
                    self._wake_model is not None
                    and self.controller.mode != "hotkey"
                    and time.monotonic() > self.controller.active_until
                ):
                    scores = self._wake_model.predict(np.frombuffer(audio, dtype=np.int16))
                    if max(scores.values(), default=0) >= self.sensitivity:
                        self.controller.activate()
                        self._recognizer.Reset()
                        for previous in list(rolling)[:-1]:
                            self._recognizer.AcceptWaveform(previous)
                final = self._recognizer.AcceptWaveform(audio)
                data = json.loads(self._recognizer.Result() if final else self._recognizer.PartialResult())
                text = data.get("text" if final else "partial", "")
                words = data.get("result" if final else "partial_result", [])
                confidence = min((word.get("conf", 1) for word in words[:2]), default=1)
                active = time.monotonic() <= self.controller.active_until
                if text and (active or confidence >= self.sensitivity) and (self._wake_model is None or active):
                    self.controller.feed_text(text, final=bool(final))
        except Exception as exc:
            self.error = str(exc)
            self._stop.set()

    def stop(self) -> None:
        self._stop.set()
        if self._stream:
            try:
                self._stream.stop()
            finally:
                self._stream.close()
            self._stream = None
        if self._keys:
            self._keys.stop()
            self._keys = None
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    @property
    def engine(self) -> str:
        return (
            "openWakeWord + local Vosk decoder"
            if self._wake_model is not None
            else "WebRTC VAD + Vosk offline keyword spotting"
        )


if __name__ == "__main__":
    from .config import load_environment

    load_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-model", action="store_true")
    args = parser.parse_args()
    if args.download_model:
        print(install_model())
