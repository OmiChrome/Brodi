"""App-local microphone gate, shortcut lifecycle and offline listening cues."""

from __future__ import annotations

import asyncio
import math
import os
import sys
import time
from array import array

from livekit import rtc
from livekit.agents.voice import io

from .hotkey import GlobalMicHotkey
from .mic_control import MicrophoneMailbox
from .speech import SAMPLE_RATE, play_pcm

SHORTCUT = "Ctrl+Space" if sys.platform == "win32" else "Control+Space"


def chime_pcm(listening: bool) -> bytes:
    """Quiet 180 ms rising/falling cue, synthesized locally with smooth edges."""
    samples = array("h")
    for frequency in (660, 880) if listening else (660, 440):
        count = int(SAMPLE_RATE * 0.09)
        for index in range(count):
            envelope = min(1, index / 240, (count - 1 - index) / 480)
            samples.append(round(4500 * envelope * math.sin(2 * math.pi * frequency * index / SAMPLE_RATE)))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


async def ready_chime_frames():
    """Play the local ready cue through the session's speaker on either OS."""
    pcm = chime_pcm(True)
    yield rtc.AudioFrame(data=pcm, sample_rate=SAMPLE_RATE, num_channels=1, samples_per_channel=len(pcm) // 2)


async def play_chime(listening: bool):
    import sounddevice as sd

    async def chunks():
        yield chime_pcm(listening)

    # Use the configured speaker. Playback never changes system input/output mute.
    output = sd.RawOutputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        latency="low",
        device=os.environ.get("BRODI_OUTPUT_DEVICE") or None,
    )
    try:
        await play_pcm(chunks(), sd, on_start=lambda: None, on_finish=lambda: None, prepared_output=output)
    finally:
        await asyncio.to_thread(output.close)


class GatedAudioInput(io.AudioInput):
    """Drain muted frames instead of buffering them, including TCP console input.

    Some LiveKit inputs do not implement on_detached, so set_audio_enabled(False)
    alone is insufficient. This gate sits before both STT and realtime providers.
    """

    def __init__(self, source, control):
        super().__init__(label="Brodi microphone", source=source)
        self.control = control

    async def __anext__(self):
        while True:
            epoch = self.control.epoch
            frame = await self.source.__anext__()
            if self.control.listening and epoch == self.control.epoch:
                return frame


class MicrophoneControl:
    def __init__(
        self, session, activity, *, cue=play_chime, hotkey_factory=GlobalMicHotkey, mailbox_factory=MicrophoneMailbox
    ):
        self.session, self.activity = session, activity
        self.cue, self.hotkey_factory = cue, hotkey_factory
        self.muted = True
        self.listening = False
        self.listening_label = "Listening"
        self.epoch = 0
        self.accept_after = float("inf")
        self.closed = False
        self.attached = False
        self.failed = False
        self.hotkey = None
        self.hotkey_available = False
        self.mailbox = mailbox_factory()
        self.mailbox_task = None
        self.transition = None
        self._transitions = set()
        self._cue_lock = asyncio.Lock()

    def attach(self):
        if self.session.input.audio is None:
            raise RuntimeError("Brodi has no microphone input")
        self.session.input.audio = GatedAudioInput(self.session.input.audio, self)
        self.session.input.set_audio_enabled(False)
        self.session.clear_user_turn()
        self.attached = True

    async def start(self):
        self.mailbox_task = asyncio.create_task(self._requests(), name="brodi-mic-controls")
        self.hotkey = self.hotkey_factory(self.toggle, self.fail)
        try:
            await self.hotkey.start()
        except Exception as exc:
            self.fail(str(exc))
            return
        self.hotkey_available = True
        self.set_muted(False)
        # Pressing mute during the startup chime cancels that cue, not the session.
        await asyncio.gather(self.transition, return_exceptions=True)

    def accepts(self, message) -> bool:
        metrics = getattr(message, "metrics", {}) or {}
        started = metrics.get("started_speaking_at", getattr(message, "created_at", time.time()))
        return self.listening and started >= self.accept_after

    def status(self):
        if self.failed and self.muted:
            return "Mic muted — use tray to listen; shortcut unavailable"
        if self.failed and self.listening:
            return f"{self.listening_label} — use tray to mute; shortcut unavailable"
        return (
            f"Mic muted — {SHORTCUT} to listen"
            if self.muted
            else (f"{self.listening_label} — {SHORTCUT} to mute" if self.listening else "Resuming microphone…")
        )

    def publish(self):
        if self.closed:
            return
        message = self.status()
        # A read-only/full activity directory must not disable the keyboard mute.
        try:
            self.mailbox.publish(
                muted=not self.listening,
                available=self.attached,
                hotkey_available=self.hotkey_available,
            )
        except OSError as exc:
            self.activity.emit("error", "Tray microphone control unavailable", reason=str(exc))
        self.activity.emit(
            "status",
            message,
            mic_muted=not self.listening,
            mic_available=self.attached and not self.closed,
            hotkey_available=self.hotkey_available,
            hint=f"{SHORTCUT} toggles Brodi's mic" if self.hotkey_available else "Use the tray microphone control",
        )

    async def _requests(self):
        while not self.closed:
            try:
                muted = await asyncio.to_thread(self.mailbox.poll)
                if muted is not None and muted != self.muted:
                    self.set_muted(muted)
            except OSError as exc:
                self.activity.emit("error", "Tray microphone control unavailable", reason=str(exc))
            await asyncio.sleep(0.05)

    def toggle(self):
        if not self.closed and not self.failed:
            self.set_muted(not self.muted)

    def set_muted(self, muted: bool):
        if self.closed:
            return
        self.muted = muted
        self.listening = False  # block immediately, before yielding or playing a cue
        self.epoch += 1
        epoch = self.epoch
        self.session.input.set_audio_enabled(False)
        self.session.clear_user_turn()
        self.publish()
        if self.transition:
            self.transition.cancel()
        self.transition = asyncio.create_task(self._feedback(epoch), name="brodi-mic-cue")
        self._transitions.add(self.transition)
        self.transition.add_done_callback(self._transitions.discard)

    async def _feedback(self, epoch):
        # Cancellation waits for any device write; serialize rapid toggles.
        async with self._cue_lock:
            try:
                await self.cue(not self.muted)
            except Exception as exc:
                self.activity.emit("error", "Microphone sound cue unavailable", reason=str(exc))
            # Drop speaker echo as well as the cue itself before accepting audio.
            await asyncio.sleep(0.12)
            if self.closed or epoch != self.epoch or self.muted:
                return
            self.session.clear_user_turn()
            self.accept_after = time.time()
            self.epoch += 1  # discard a source read begun during mute/cue playback
            self.session.input.set_audio_enabled(True)
            self.listening = True
            self.publish()

    def fail(self, reason: str):
        if self.closed:
            return
        self.failed = True
        self.hotkey_available = False
        self.set_muted(True)
        self.activity.emit("error", "Mic muted — microphone shortcut unavailable", reason=reason)

    async def aclose(self):
        self.closed = True
        self.listening = False
        self.muted = True
        self.epoch += 1
        self.session.input.set_audio_enabled(False)
        for transition in self._transitions:
            transition.cancel()
        await asyncio.gather(*self._transitions, return_exceptions=True)
        if self.hotkey:
            await self.hotkey.aclose()
        if self.mailbox_task:
            self.mailbox_task.cancel()
            await asyncio.gather(self.mailbox_task, return_exceptions=True)
        try:
            self.mailbox.close()
        except OSError:
            pass  # the process/session nonce still prevents a stale request
