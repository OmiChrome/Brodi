"""Opt-in live probes. They use temporary memory and a dedicated Notepad test document."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import time
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from .config import load_environment


async def latency() -> dict:
    from .agent import DynamicMacAgent
    from .memory import MemoryStore
    from .wake import WakeController

    rows = []
    with TemporaryDirectory(prefix="brodi-latency-") as folder:
        agent = DynamicMacAgent(enable_learning=False, memory=MemoryStore(Path(folder) / "memory.db"))
        await agent.start()
        try:
            for phrase in [
                "hey bro open chrome",
                "hey bro open chrome",
                "yo bro open chrome and go to github.com",
                "yo bro open chrome and go to github.com",
            ]:
                commands = []
                WakeController(commands.append).feed_text(phrase)
                started = time.perf_counter()
                outcome = await agent.handle(commands[0])
                rows.append(
                    {
                        "phrase": phrase,
                        "command": commands[0],
                        "dispatch_ms": round((time.perf_counter() - started) * 1000, 2),
                        "executed": outcome.executed,
                        "memory_hit": outcome.memory_hit,
                        "model_calls": agent.router.client.usage["calls"],
                        "reply": outcome.speak,
                    }
                )
            assert all(row["executed"] for row in rows), rows
            assert rows[1]["memory_hit"] and rows[3]["memory_hit"]
            assert rows[-1]["model_calls"] == 0
        finally:
            await agent.aclose()
    return {
        "latency": rows,
        "measurement": "After transcription; dispatch completion, not first window paint or spoken reply latency.",
    }


def notepad() -> dict:
    from .agent import DynamicMacAgent
    from .memory import MemoryStore
    from .wake import WakeController
    from .windows_uia import WindowTarget, request

    path = Path(".freebuff") / f"brodi-uia-{uuid.uuid4().hex[:8]}.txt"
    path = path.resolve()
    path.parent.mkdir(exist_ok=True)
    path.write_text("")
    subprocess.Popen(["notepad.exe", str(path)])
    time.sleep(0.5)
    started = time.perf_counter()

    async def command():
        commands = []
        WakeController(commands.append).feed_text("hey bro type hello world into " + path.name)
        with TemporaryDirectory(prefix="brodi-uia-memory-") as folder:
            agent = DynamicMacAgent(enable_learning=False, memory=MemoryStore(Path(folder) / "memory.db"))
            try:
                await agent.start()
                result = await agent.handle(commands[0])
                assert result.executed, result.speak
            finally:
                await agent.aclose()

    asyncio.run(command())
    result = request("get_text", WindowTarget(title=path.name), timeout=6)
    assert result.ok, result.error
    actual = json.loads(result.output)
    assert "hello world" in actual["text"]
    return {
        "notepad": actual,
        "milliseconds": round((time.perf_counter() - started) * 1000),
        "test_document": str(path),
    }


def main() -> None:
    load_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--latency", action="store_true", help="actually open Chrome/GitHub and measure dispatch/cache paths"
    )
    parser.add_argument("--notepad", action="store_true", help="type and read back hello world in a new test document")
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="verify minimize/maximize/restore/focus/close on a separate empty Notepad window; 500 ms dispatch budget",
    )
    parser.add_argument(
        "--cloud-stt",
        type=Path,
        help="send a PCM16 mono 24 kHz WAV through the real cloud STT adapter; no microphone or actions",
    )
    args = parser.parse_args()
    output = {}
    if args.latency:
        output.update(asyncio.run(latency()))
    if args.notepad:
        output.update(notepad())
    if args.desktop:
        from .desktop_smoke import run

        output["desktop"] = asyncio.run(run())
    if args.cloud_stt:
        output.update(asyncio.run(cloud_stt(args.cloud_stt)))
    if not output:
        parser.error("Choose a smoke test")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if args.desktop and not output["desktop"]["ok"]:
        raise SystemExit(1)


async def cloud_stt(path: Path) -> dict:
    """Replay a fixture at microphone pace through the same adapter as the app."""
    import wave

    from livekit import rtc
    from livekit.agents import stt, utils

    from .transcription import create_stt

    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 24000):
            raise ValueError("Cloud probe requires mono PCM16 WAV at 24000 Hz")
        pcm = wav.readframes(wav.getnframes())
    first_text = None
    last_audio = None
    started = time.perf_counter()
    async with utils.http_context.open():
        recognizer = create_stt()
        stream = recognizer.stream()

        async def send():
            nonlocal last_audio
            # End with a second of silence for the cloud speech detector.
            data = pcm + bytes(48000)
            for offset in range(0, len(data), 960):
                chunk = data[offset : offset + 960]
                stream.push_frame(rtc.AudioFrame(chunk, 24000, 1, len(chunk) // 2))
                if offset < len(pcm):
                    last_audio = time.perf_counter()
                await asyncio.sleep(len(chunk) / 48000)

        sender = asyncio.create_task(send())
        try:
            async with asyncio.timeout(30):
                async for event in stream:
                    if event.type == stt.SpeechEventType.INTERIM_TRANSCRIPT and first_text is None:
                        first_text = (time.perf_counter() - started) * 1000
                    if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT and event.alternatives:
                        return {
                            "cloud_stt": {
                                "model": recognizer._opts.model,
                                "transcript": event.alternatives[0].text,
                                "first_text_ms": round(first_text, 2) if first_text is not None else None,
                                "final_after_last_audio_ms": round(
                                    (time.perf_counter() - (last_audio or started)) * 1000, 2
                                ),
                                "silence_ms": round(recognizer._vad.silence_s * 1000)
                                if recognizer._vad
                                else recognizer._opts.turn_detection.silence_duration_ms,
                                "measurement": "Synthetic recorded speech sent at microphone pace; no actions executed.",
                            }
                        }
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            await stream.aclose()
            await recognizer.aclose()
    raise RuntimeError("Cloud transcription ended without a final transcript")


if __name__ == "__main__":
    main()
