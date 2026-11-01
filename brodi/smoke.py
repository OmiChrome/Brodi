"""Opt-in live probes. They use temporary memory and a dedicated Notepad test document."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import time
import uuid
import wave
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


def wake_audio(path: Path) -> dict:
    import numpy as np
    import vosk

    from .wake import WakeController, model_path

    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError("Wake smoke requires PCM16 WAV")
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
        if wav.getnchannels() > 1:
            samples = samples.reshape(-1, wav.getnchannels()).mean(axis=1)
        if wav.getframerate() != 16000:
            samples = np.interp(
                np.arange(0, len(samples), wav.getframerate() / 16000), np.arange(len(samples)), samples
            ).astype(np.int16)
    vosk.SetLogLevel(-1)
    decoder = vosk.KaldiRecognizer(vosk.Model(str(model_path())), 16000)
    commands = []
    controller = WakeController(commands.append)
    audio = samples.astype(np.int16).tobytes() + bytes(16000 * 2)
    transcripts = []
    for offset in range(0, len(audio), 2560):
        final = decoder.AcceptWaveform(audio[offset : offset + 2560])
        data = json.loads(decoder.Result() if final else decoder.PartialResult())
        text = data.get("text" if final else "partial", "")
        if text:
            controller.feed_text(text, final=bool(final))
            if final:
                transcripts.append(text)
    final_text = json.loads(decoder.FinalResult()).get("text", "")
    if final_text:
        controller.feed_text(final_text)
        transcripts.append(final_text)
    return {"transcripts": transcripts, "commands": commands, "wake_detections": controller.stats.detections}


def main() -> None:
    load_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--latency", action="store_true", help="actually open Chrome/GitHub and measure dispatch/cache paths"
    )
    parser.add_argument("--notepad", action="store_true", help="type and read back hello world in a new test document")
    parser.add_argument("--wake-audio", type=Path, help="decode a prerecorded PCM16 WAV completely locally")
    args = parser.parse_args()
    output = {}
    if args.latency:
        output.update(asyncio.run(latency()))
    if args.notepad:
        output.update(notepad())
    if args.wake_audio:
        output.update(wake_audio(args.wake_audio))
    if not output:
        parser.error("Choose a smoke test")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
