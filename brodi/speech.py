"""Low-latency PCM speech and reusable audio for fixed acknowledgements."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import sys
import tempfile
from array import array
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, aclosing
from dataclasses import dataclass
from pathlib import Path

from .paths import user_data_dir
from .profiler import span

log = logging.getLogger(__name__)
SAMPLE_RATE = 24000
CHUNK_BYTES = 1024  # About 21 ms of PCM16 mono, with no codec or resampling delay.
MAX_CACHE_BYTES = 2_000_000
ACKNOWLEDGEMENTS = frozenset({"Got it.", "Gotcha.", "Hey bro, I'm listening.", "Done.", "Cancelled.", "On it."})
DEFAULT_INSTRUCTIONS = (
    "Speak in a natural, warm male voice with clear, conversational delivery. "
    "Sound like a relaxed, helpful person. Use light, natural intonation and brief pauses. "
    "Do not sound theatrical, robotic, or like an announcer."
)


def speech_speed() -> float:
    speed = float(os.environ.get("BRODI_TTS_SPEED", "1.25"))
    if not math.isfinite(speed) or not 0.25 <= speed <= 4:
        raise ValueError("BRODI_TTS_SPEED must be between 0.25 and 4")
    return speed


@dataclass(frozen=True)
class SpeechSettings:
    model: str
    voice: str
    speed: float
    instructions: str

    @classmethod
    def current(cls) -> SpeechSettings:
        model = os.environ.get("BRODI_TTS_MODEL", "gpt-4o-mini-tts")
        return cls(
            model,
            os.environ.get("BRODI_VOICE", "onyx"),
            speech_speed(),
            "" if model in {"tts-1", "tts-1-hd"} else os.environ.get("BRODI_TTS_INSTRUCTIONS", DEFAULT_INSTRUCTIONS),
        )

    def request(self, text: str) -> dict:
        options = {
            "model": self.model,
            "voice": self.voice,
            "speed": self.speed,
            "input": text,
            "response_format": "pcm",
        }
        if self.instructions:
            options["instructions"] = self.instructions
        return options


class SpeechAudio:
    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = cache_dir if cache_dir is not None else user_data_dir() / "speech"
        self._cache: dict[str, bytes] = {}

    def _read(self, key: str) -> bytes | None:
        try:
            path = self.cache_dir / (key + ".pcm")
            if not 0 < path.stat().st_size <= MAX_CACHE_BYTES:
                return None
            data = path.read_bytes()
            return data if len(data) % 2 == 0 else None
        except OSError:
            return None

    def _write(self, key: str, pcm: bytes) -> None:
        temporary = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.cache_dir, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(pcm)
            temporary.replace(self.cache_dir / (key + ".pcm"))
        except OSError:
            log.warning("Could not persist acknowledgement audio")
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    async def stream(self, client, text: str) -> AsyncIterator[bytes]:
        request = SpeechSettings.current().request(text)
        key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        reusable = text in ACKNOWLEDGEMENTS
        if reusable:
            cached = self._cache.get(key)
            if cached is None:
                with span("speech.cache_read"):
                    cached = await asyncio.to_thread(self._read, key)
            if cached:
                self._remember(key, cached)
                with span("speech.cache_hit"):
                    for offset in range(0, len(cached), CHUNK_BYTES):
                        yield cached[offset : offset + CHUNK_BYTES]
                return

        saved = bytearray()
        remainder = b""
        received = False
        with span("speech.download"):
            async with AsyncExitStack() as stack:
                with span("speech.first_audio"):
                    response = await stack.enter_async_context(
                        client.audio.speech.with_streaming_response.create(**request)
                    )
                    chunks = response.iter_bytes(chunk_size=CHUNK_BYTES)
                    chunk = await anext(chunks, None)
                    while chunk == b"":
                        chunk = await anext(chunks, None)
                # The generator is closed on interruption: partial speech is never cached.
                while chunk is not None:
                    data = remainder + chunk
                    end = len(data) - len(data) % 2
                    remainder = data[end:]
                    data = data[:end]
                    if data:
                        received = True
                        if reusable:
                            saved.extend(data)
                            if len(saved) > MAX_CACHE_BYTES:
                                reusable = False
                                saved.clear()
                        yield data
                    chunk = await anext(chunks, None)
        if remainder:
            raise ValueError("Speech stream ended with an incomplete PCM sample")
        if not received:
            raise ValueError("Speech service returned no audio")
        if reusable:
            pcm = bytes(saved)
            self._remember(key, pcm)
            await asyncio.to_thread(self._write, key, pcm)

    def _remember(self, key: str, pcm: bytes) -> None:
        if len(self._cache) >= 8 and key not in self._cache:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = pcm

    async def pcm(self, client, text: str) -> bytes:
        """Collect speech for warmup/probes; the voice runtime uses stream()."""
        return b"".join([chunk async for chunk in self.stream(client, text)])

    async def warm(self, client) -> None:
        async def prepare(text):
            try:
                await self.pcm(client, text)
            except Exception:
                log.warning("Could not warm acknowledgement audio", exc_info=True)

        await asyncio.gather(prepare("Gotcha."), prepare("Got it."), prepare("On it."))


async def play_pcm(
    chunks: AsyncIterator[bytes],
    sd,
    *,
    on_start: Callable[[], None],
    on_finish: Callable[[], None],
    prepared_output=None,
) -> None:
    """Start with the first chunk; keep blocking sound-device writes off the event loop."""
    output = prepared_output
    started = False
    complete = False

    async def device_call(fn, *args):
        # Do not close the device while a cancelled to_thread write is still using it.
        task = asyncio.create_task(asyncio.to_thread(fn, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    try:
        async with aclosing(chunks):
            async for chunk in chunks:
                if not chunk:
                    continue
                if not started:
                    if output is None:
                        output = sd.RawOutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", latency="low")
                    started = True
                    on_start()
                    await device_call(output.start)
                with span("speech.playback_chunk"):
                    await device_call(output.write, chunk)
        complete = True
    finally:
        try:
            if output is not None:
                try:
                    await device_call(output.stop if complete else output.abort)
                finally:
                    if prepared_output is None:
                        await device_call(output.close)
        finally:
            if started:
                on_finish()


async def trim_silence(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Discard service padding while retaining quiet consonants and pauses inside speech."""
    frame_bytes = SAMPLE_RATE // 100 * 2  # 10 ms PCM16 frames.
    partial = b""
    silence = b""
    speaking = False

    def audible(frame):
        samples = array("h")
        samples.frombytes(frame)
        if sys.byteorder != "little":
            samples.byteswap()
        return sum(s * s for s in samples) > len(samples) * 100**2

    async with aclosing(chunks):
        async for chunk in chunks:
            partial += chunk
            while len(partial) >= frame_bytes:
                frame, partial = partial[:frame_bytes], partial[frame_bytes:]
                if audible(frame):
                    for offset in range(0, len(silence), CHUNK_BYTES):
                        yield silence[offset : offset + CHUNK_BYTES]
                    silence = b""
                    speaking = True
                    yield frame
                else:
                    # Preserve up to one second of internal pause, but only
                    # 20 ms of pre-roll before the first audible frame.
                    silence = (silence + frame)[-(SAMPLE_RATE * 2 if speaking else frame_bytes * 2) :]
        if partial:
            if audible(partial):
                for offset in range(0, len(silence), CHUNK_BYTES):
                    yield silence[offset : offset + CHUNK_BYTES]
                silence = b""
                speaking = True
                yield partial
            else:
                silence += partial
        if speaking and silence:
            yield silence[: frame_bytes * 4]  # A natural 40 ms tail, rather than >1 s of padding.


class SpeechPlayer:
    """Open the sound driver once at startup, then reuse it for every reply."""

    def __init__(self, sd):
        self.sd = sd
        self._output = None
        self._opening = asyncio.Lock()

    async def prepare(self) -> None:
        async with self._opening:
            if self._output is not None:
                return
            with span("speech.device_open"):
                task = asyncio.create_task(
                    asyncio.to_thread(
                        self.sd.RawOutputStream,
                        samplerate=SAMPLE_RATE,
                        channels=1,
                        dtype="int16",
                        latency="low",
                    )
                )
                try:
                    self._output = await asyncio.shield(task)
                except asyncio.CancelledError:
                    self._output = await task
                    raise

    async def play(self, chunks, *, on_start, on_finish):
        # Close the input stream even if device preparation fails before playback.
        async with aclosing(chunks):
            await self.prepare()
            await play_pcm(
                trim_silence(chunks), self.sd, on_start=on_start, on_finish=on_finish, prepared_output=self._output
            )

    async def aclose(self) -> None:
        if self._output is not None:
            await asyncio.to_thread(self._output.close)
            self._output = None
