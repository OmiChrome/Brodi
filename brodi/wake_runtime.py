"""Local microphone runtime: no network/audio provider connection while waiting for wake."""

from __future__ import annotations

import asyncio
import os
import signal

from .activity import ActivityLog
from .agent import DynamicMacAgent
from .config import flag
from .models import cache_key, model
from .wake import LocalListener, WakeController


async def run() -> None:
    import numpy as np
    import openai
    import sounddevice as sd

    devices = [os.environ.get(f"BRODI_{role}_DEVICE") for role in ("INPUT", "OUTPUT")]
    if any(devices):
        sd.default.device = [int(value) if value and value.isdecimal() else value for value in devices]

    activity = ActivityLog()
    activity.emit("status", "Starting local wake listener")
    loop = asyncio.get_running_loop()
    pending: asyncio.Queue[str] = asyncio.Queue(maxsize=4)
    closing = asyncio.Event()
    speech_epoch = 0

    def accept(command: str) -> None:
        def enqueue():
            if not pending.full():
                pending.put_nowait(command)
            else:
                activity.emit("error", "Task queue is full; please wait for the current task")

        loop.call_soon_threadsafe(enqueue)

    def detected() -> None:
        nonlocal speech_epoch
        speech_epoch += 1
        sd.stop()  # Interrupt TTS playback; the independent RawInputStream stays open.

    controller = WakeController(
        accept, on_wake=detected, on_event=activity.emit, window_s=float(os.environ.get("BRODI_WAKE_WINDOW_S", "20"))
    )
    listener = LocalListener(controller)
    agent = DynamicMacAgent(enable_learning=flag("BRODI_LEARN"), activity=activity)
    client = openai.AsyncOpenAI(timeout=15, max_retries=0)

    async def say(text: str):
        epoch = speech_epoch
        if not flag("BRODI_TTS_ENABLED") or not pending.empty():
            return
        try:
            activity.emit("status", "Replying")
            async with client.audio.speech.with_streaming_response.create(
                model=os.environ.get("BRODI_TTS_MODEL", "gpt-4o-mini-tts"),
                voice=os.environ.get("BRODI_VOICE", "onyx"),
                input=text,
                response_format="pcm",
            ) as response:
                pcm = await response.read()
            if not closing.is_set() and epoch == speech_epoch and pending.empty():
                activity.emit("status", "Speaking")
                listener.suspend_for_playback()
                try:
                    await asyncio.to_thread(sd.play, np.frombuffer(pcm, dtype=np.int16), 24000, blocking=True)
                finally:
                    listener.resume_after_playback()
        except Exception as exc:
            activity.emit("error", "Could not play the reply", reason=str(exc))

    def stop(*_):
        loop.call_soon_threadsafe(closing.set)

    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, stop)
        except ValueError:
            pass
    try:
        await agent.start()
        await asyncio.to_thread(listener.start)
        activity.emit(
            "status",
            "Listening locally for Hey bro or Yo bro",
            hotkey=os.environ.get("BRODI_WAKE_HOTKEY", "Ctrl+Alt+B"),
            engine=listener.engine,
        )
        while not closing.is_set():
            try:
                command = await asyncio.wait_for(pending.get(), 0.3)
            except TimeoutError:
                if not listener.running:
                    raise RuntimeError(listener.error or "Local wake listener stopped") from None
                continue
            if not command:
                activity.emit("output", "Gotcha.")
                await say("Gotcha.")
                activity.emit("status", "Listening for a task")
                continue
            if command.lower().rstrip(".!?") in {"false alarm", "that was a false alarm"}:
                activity.emit("input", command)
                controller.mark_false_positive()
                controller.active_until = 0
                activity.emit("status", "Listening locally for Hey bro or Yo bro")
                continue
            result = await agent.handle(command)
            if result.handoff_to_llm:
                response = await client.responses.create(
                    model=model("chat"),
                    reasoning={"effort": "none"},
                    prompt_cache_key=cache_key("chat"),
                    input=[
                        {
                            "role": "system",
                            "content": "You are Brodi, a bro who helps. Reply in one very short spoken sentence. Never claim an action you did not execute.",
                        },
                        {"role": "user", "content": command},
                    ],
                )
                reply = response.output_text
                activity.emit("output", reply)
            else:
                reply = result.speak or ""
            if reply:
                await say(reply)
            window = (
                60 if agent.pending or agent.pending_question else float(os.environ.get("BRODI_WAKE_WINDOW_S", "20"))
            )
            controller.active_until = loop.time() + window
            if result.stop:
                break
            activity.emit("status", agent.waiting_status())
    except Exception as exc:
        activity.emit("error", "The local voice session stopped", reason=str(exc))
        raise
    finally:
        await asyncio.to_thread(listener.stop)
        sd.stop()
        await agent.aclose()
        await client.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        activity.emit("status", "Brodi stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    from .config import load_environment

    load_environment()
    main()
