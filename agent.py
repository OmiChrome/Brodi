"""LiveKit Agents entrypoint for brodi.

    uv run python agent.py console      # local mic/speaker, no LiveKit server needed
    uv run python agent.py dev          # connect to LIVEKIT_URL as a worker
    uv run python agent.py download-files

Pipeline: OpenAI STT -> (Decisions router -> OS script) | OpenAI LLM for brief replies -> OpenAI TTS.
Everything runs on one OPENAI_API_KEY. Set BRODI_VOICE_MODE=realtime for the Realtime API.
"""

from __future__ import annotations

import logging
import os
import time

from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, StopResponse, get_job_context, llm
from livekit.plugins import openai as lk_openai
from openai.types.realtime import AudioTranscription
from openai.types.realtime.realtime_audio_input_turn_detection import SemanticVad

from brodi.config import flag, load_environment

load_environment()

from brodi import policy  # noqa: E402
from brodi.activity import ActivityLog  # noqa: E402
from brodi.agent import PENDING_TTL_S, DynamicMacAgent  # noqa: E402
from brodi.speech import SpeechSettings, speech_speed  # noqa: E402
from brodi.transcription import DEFAULT_MODEL as DEFAULT_STT_MODEL  # noqa: E402
from brodi.transcription import create_stt  # noqa: E402
from brodi.voice import VoiceGate  # noqa: E402

log = logging.getLogger("brodi.voice")

INSTRUCTIONS = """You are brodi, a terse voice assistant that controls this computer.
Mac actions and Windows actions are handled by a fast tool router before you see the message, so
anything that reaches you is small talk or a quick question. Answer in one short spoken sentence.
No markdown, no lists, no emoji, no follow-up questions."""

VOICE_MODE = os.environ.get("BRODI_VOICE_MODE", "pipeline")  # "pipeline" | "realtime"
STT_MODEL = os.environ.get("BRODI_STT_MODEL", DEFAULT_STT_MODEL)
TTS_MODEL = os.environ.get("BRODI_TTS_MODEL", "gpt-4o-mini-tts")
CHAT_MODEL = os.environ.get("BRODI_CHAT_MODEL", "gpt-6-luna")
REALTIME_MODEL = os.environ.get("BRODI_REALTIME_MODEL", "gpt-realtime-2.1-mini")
REALTIME_STT_MODEL = os.environ.get("BRODI_REALTIME_STT_MODEL", "gpt-transcribe")
VOICE = os.environ.get("BRODI_VOICE", "onyx")  # deep male voice
LANGUAGE = os.environ.get("BRODI_LANG", "en")


class BrodiAgent(Agent):
    def __init__(self, mac: DynamicMacAgent) -> None:
        super().__init__(instructions=INSTRUCTIONS)
        self.mac = mac
        self.awaiting_chat_reply = False
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

        self.awaiting_chat_reply = False
        outcome = await self.mac.handle(text)
        self.activation.activate()  # start the reply window after a long task finishes, too
        route = outcome.route
        log.info(
            "turn %r -> %s timings=%s",
            text,
            "llm" if outcome.handoff_to_llm else (route.summary if route else "?"),
            {k: round(v) for k, v in outcome.timings.items()},
        )
        if outcome.handoff_to_llm:
            self.awaiting_chat_reply = True
            return  # normal LLM reply

        if outcome.stop:
            try:
                await self.session.say(outcome.speak or "Goodbye.", add_to_chat_ctx=False, allow_interruptions=False)
            except RuntimeError:
                pass
            if flag("BRODI_BACKGROUND", default=False):
                from brodi.background import request_stop

                request_stop()  # Explicit stop must not be undone by the supervisor.
            get_job_context().shutdown(reason="user asked brodi to stop")
            raise StopResponse()

        if outcome.speak:
            # Speak the deterministic result and keep it in history so the LLM has context later.
            try:
                await self.session.say(outcome.speak, add_to_chat_ctx=True, allow_interruptions=False)
            except RuntimeError as exc:  # session closing mid-turn
                log.warning("could not speak result: %s", exc)
        raise StopResponse()


def build_session() -> AgentSession:
    """One OpenAI key: either the classic STT/LLM/TTS pipeline or the Realtime API."""
    if VOICE_MODE == "realtime":
        return AgentSession(
            llm=lk_openai.realtime.RealtimeModel(
                model=REALTIME_MODEL,
                voice=VOICE,
                speed=speech_speed(),
                turn_detection=SemanticVad(type="semantic_vad", create_response=False, interrupt_response=False),
                input_audio_transcription=AudioTranscription(model=REALTIME_STT_MODEL),
            ),
            turn_handling={"turn_detection": "realtime_llm", "preemptive_generation": {"enabled": False}},
            vad=None,
        )
    return AgentSession(
        turn_handling={
            "turn_detection": "stt",
            "interruption": {"enabled": False},
            "preemptive_generation": {"enabled": False},
        },
        stt=create_stt(model=STT_MODEL, language=LANGUAGE),
        vad=None,
        llm=lk_openai.LLM(model=CHAT_MODEL, reasoning_effort="none"),
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

    def _filler(text: str) -> None:
        activity.emit("progress", text)
        if session is None:
            return
        try:
            session.say(text, add_to_chat_ctx=False)
        except RuntimeError:
            pass

    mac = DynamicMacAgent(
        enable_learning=flag("BRODI_LEARN"),
        on_learning=_filler,
        activity=activity,
    )
    await mac.start()

    session = build_session()
    voice_agent = BrodiAgent(mac)

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

    @session.on("conversation_item_added")
    def _conversation(event) -> None:
        item = event.item
        if voice_agent.awaiting_chat_reply and getattr(item, "role", None) == "assistant":
            text = getattr(item, "text_content", None)
            if text:
                activity.emit("output", text)
                voice_agent.awaiting_chat_reply = False

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
        if flag("BRODI_BACKGROUND", default=False):
            # A dead voice session can leave its console worker alive. Exit it so
            # the supervisor can reconnect; an explicit stop already set its stop flag.
            ctx.shutdown(reason="background voice session closed")

    async def _close() -> None:
        await mac.aclose()
        activity.emit("status", "Brodi stopped")

    ctx.add_shutdown_callback(_close)

    log.info("pipeline: mode=%s stt=%s tts=%s voice=%s llm=%s", VOICE_MODE, STT_MODEL, TTS_MODEL, VOICE, CHAT_MODEL)
    activity.emit("status", "Connecting microphone and speaker")
    try:
        await session.start(agent=voice_agent, room=ctx.room)
    except Exception as exc:
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
    greeting = "Brodi ready. Say hey bro, then tell me what to do."
    if not flag("BRODI_WAKE_REQUIRED", default=False):
        greeting = "Brodi ready. Tell me what to do."
    if not policy.ENABLED:
        greeting += " Warning: the safety policy is off."
    activity.emit("output", greeting)
    session.say(greeting, add_to_chat_ctx=False, allow_interruptions=False)


if __name__ == "__main__":
    agents.cli.run_app(server)
