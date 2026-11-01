"""Lightweight audio boundaries for cloud STT; no local speech model or transcription."""

import math
from array import array

from livekit.agents import vad


class SilenceEndpoint(vad.VAD):
    def __init__(self, *, silence_s: float = 0.25, rms_threshold: float = 160):
        super().__init__(capabilities=vad.VADCapabilities(update_interval=0.02))
        if not math.isfinite(rms_threshold) or not 1 <= rms_threshold <= 10000:
            raise ValueError("BRODI_STT_RMS_THRESHOLD must be between 1 and 10000")
        self.silence_s = silence_s
        self.rms_threshold = rms_threshold

    def stream(self):
        return EndpointStream(self)


class EndpointStream(vad.VADStream):
    async def _main_task(self):
        elapsed = speech = silence = candidate = 0.0
        samples = 0
        speaking = False

        def emit(kind):
            self._event_ch.send_nowait(
                vad.VADEvent(
                    type=kind,
                    samples_index=samples,
                    timestamp=elapsed,
                    speech_duration=max(0.0, speech - silence),
                    silence_duration=silence,
                )
            )

        async for frame in self._input_ch:
            if isinstance(frame, self._FlushSentinel):
                if speaking:
                    emit(vad.VADEventType.END_OF_SPEECH)
                speech = silence = candidate = 0.0
                speaking = False
                continue
            values = array("h", frame.data.tobytes())
            audible = sum(v * v for v in values) >= len(values) * self._vad.rms_threshold**2
            duration = frame.duration
            elapsed += duration
            samples += frame.samples_per_channel
            if not speaking:
                candidate = candidate + duration if audible else 0.0
                if candidate >= 0.06:
                    speaking = True
                    speech = candidate
                    emit(vad.VADEventType.START_OF_SPEECH)
            else:
                speech += duration
                silence = 0.0 if audible else silence + duration
                if silence + 1e-6 >= self._vad.silence_s or speech >= 30:
                    emit(vad.VADEventType.END_OF_SPEECH)
                    speaking = False
                    speech = silence = candidate = 0.0
