"""LiveKit Agents entrypoint for brodi.

    uv run python agent.py console      # local mic/speaker, no LiveKit server needed
    uv run python agent.py dev          # connect to LIVEKIT_URL as a worker
    uv run python agent.py download-files

Pipeline: English OpenAI STT -> Decisions router -> OS action -> brief English TTS.
Everything runs on one OPENAI_API_KEY. Brodi executes commands; it does not chat.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable

from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, StopResponse, get_job_context, llm
from livekit.plugins import openai as lk_openai

from brodi.config import flag, load_environment

load_environment()

from brodi import policy  # noqa: E402
from brodi.activity import ActivityLog  # noqa: E402
from brodi.agent import PENDING_TTL_S, DynamicMacAgent  # noqa: E402
from brodi.console_audio import close_console_audio  # noqa: E402
from brodi.speech import SpeechSettings, speech_speed  # noqa: E402
from brodi.transcription import DEFAULT_MODEL as DEFAULT_STT_MODEL  # noqa: E402
from brodi.transcription import create_stt  # noqa: E402
from brodi.voice import VoiceGate, english_script, spoken_reply  # noqa: E402

log = logging.getLogger("brodi.voice")

INSTRUCTIONS = """Brodi executes English desktop commands. It is not a conversational assistant.
Spoken output is English only, with at most two words. All actions go through the tool router."""

VOICE_MODE = os.environ.get("BRODI_VOICE_MODE", "pipeline")
STT_MODEL = os.environ.get("BRODI_STT_MODEL", DEFAULT_STT_MODEL)
TTS_MODEL = os.environ.get("BRODI_TTS_MODEL", "gpt-4o-mini-tts")
VOICE = os.environ.get("BRODI_VOICE", "onyx")  # deep male voice


class BrodiAgent(Agent):
    def __init__(self, mac: DynamicMacAgent, *, on_command: Callable[[], None] | None = None) -> None:
        super().__init__(instructions=INSTRUCTIONS)
        self.mac = mac
        self.on_command = on_command
        self.activation = VoiceGate(
            required=flag("BRODI_WAKE_REQUIRED", default=False),
            window_s=float(os.environ.get("BRODI_WAKE_WINDOW_S", "20")),
        )

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        text = new_message.text_content or ""
        if not text.strip():
            raise StopResponse()

        pending = bool(self.mac.pending and time.time() - self.mac.pending.staged_at <= PENDING_TTL_S)
        pending = pending or bool(
            self.mac.pending_question and time.time() - self.mac.pending_question[1] <= PENDING_TTL_S
        )
        if not self.activation.accepts(text, pending=pending):
            log.debug("ignoring speech while waiting for a wake phrase")
            raise StopResponse()

        if not english_script(text):
            await self.session.say("English only.", add_to_chat_ctx=False, allow_interruptions=False)
            raise StopResponse()

        if self.on_command:
            self.on_command()
        outcome = await self.mac.handle(text)
        self.activation.activate()  # start the reply window after a long task finishes, too
        route = outcome.route
        log.info(
            "turn %r -> %s timings=%s",
            text,
            route.summary if route else "command",
            {k: round(v) for k, v in outcome.timings.items()},
        )
        if outcome.stop:
            try:
                await self.session.say("Goodbye.", add_to_chat_ctx=False, allow_interruptions=False)
            except RuntimeError:
                pass
            if flag("BRODI_BACKGROUND", default=False):
                from brodi.background import request_stop

                request_stop()  # Explicit stop must not be undone by the supervisor.
            get_job_context().shutdown(reason="user asked brodi to stop")
            raise StopResponse()

        missing = ""
        if self.mac.pending_question:
            missing = self.mac.pending_question[0].missing_arg or "details"
        elif (getattr(self.mac, "last_browser", None) or {}).get("status") == "needs_info":
            missing = route.web_goal_missing if route and route.web_goal_missing != "nothing" else "details"
        reply = spoken_reply(outcome, confirmation=bool(self.mac.pending), missing=missing)
        if reply:
            try:
                await self.session.say(reply, add_to_chat_ctx=False, allow_interruptions=False)
            except RuntimeError as exc:  # session closing mid-turn
                log.warning("could not speak result: %s", exc)
        raise StopResponse()


def build_session() -> AgentSession:
    """English command pipeline without a conversational generation model."""
    if VOICE_MODE != "pipeline":
        raise ValueError("Brodi is command-only. Set BRODI_VOICE_MODE=pipeline.")
    return AgentSession(
        turn_handling={
            "turn_detection": "stt",
            "interruption": {"enabled": False},
            "preemptive_generation": {"enabled": False},
        },
        stt=create_stt(model=STT_MODEL),
        vad=None,
        tts=lk_openai.TTS(
            model=TTS_MODEL,
            voice=VOICE,
            speed=speech_speed(),
            instructions=SpeechSettings.current().instructions,
        ),
    )


server = AgentServer()


@server.rtc_session(agent_name=os.environ.get("BRODI_AGENT_NAME", "brodi"))
async def entrypoint(ctx: agents.JobContext) -> None:
    session: AgentSession | None = None
    activity = ActivityLog()
    activity.emit("status", "Starting Brodi")
    progress_spoken = False

    def _filler(text: str) -> None:
        nonlocal progress_spoken
        activity.emit("progress", text)
        if session is None or progress_spoken:
            return
        try:
            session.say("On it.", add_to_chat_ctx=False, allow_interruptions=False)
            progress_spoken = True
        except RuntimeError:
            pass

    mac = DynamicMacAgent(
        enable_learning=flag("BRODI_LEARN"),
        on_learning=_filler,
        activity=activity,
    )
    await mac.start()

    session = build_session()

    def _command_started() -> None:
        nonlocal progress_spoken
        progress_spoken = False

    voice_agent = BrodiAgent(mac, on_command=_command_started)

    @session.on("agent_state_changed")
    def _state_changed(event) -> None:
        state = event.new_state
        if state == "listening":
            status = mac.waiting_status()
            if voice_agent.activation.required and not mac.pending and not mac.pending_question:
                status = "Listening for Hey bro with OpenAI"
            activity.emit("status", status)
        elif state == "speaking":
            activity.emit("status", "Speaking")

    @session.on("error")
    def _session_error(event) -> None:
        error = getattr(event, "error", None)
        activity.emit(
            "error",
            "Voice connection needs attention",
            reason=str(getattr(error, "message", "Check brodi debug for connection details.")),
        )

    @session.on("close")
    def _session_closed(event) -> None:
        close_console_audio(session)
        if flag("BRODI_BACKGROUND", default=False):
            # A dead voice session can leave its console worker alive. Exit it so
            # the supervisor can reconnect; an explicit stop already set its stop flag.
            ctx.shutdown(reason="background voice session closed")

    async def _close() -> None:
        close_console_audio(session)
        await mac.aclose()
        activity.emit("status", "Brodi stopped")

    ctx.add_shutdown_callback(_close)

    log.info(
        "pipeline: command-only language=en max_spoken_words=2 stt=%s tts=%s voice=%s", STT_MODEL, TTS_MODEL, VOICE
    )
    activity.emit("status", "Connecting microphone and speaker")
    try:
        await session.start(agent=voice_agent, room=ctx.room)
    except Exception as exc:
        close_console_audio(session)
        activity.emit("error", "Could not start the voice session", reason=str(exc))
        raise
    required = voice_agent.activation.required
    activity.emit(
        "status",
        "Listening for Hey bro with OpenAI" if required else "Listening with OpenAI",
        hint="Say Hey bro, open GitHub" if required else "Tell me what to do; Hey bro is optional",
    )
    if not flag("BRODI_STARTUP_GREETING", default=True):
        return
    greeting = "Ready."
    if not policy.ENABLED:
        activity.emit("error", "The safety policy is off")
        greeting = "Policy disabled."
    activity.emit("output", greeting)
    session.say(greeting, add_to_chat_ctx=False, allow_interruptions=False)


if __name__ == "__main__":
    agents.cli.run_app(server)
