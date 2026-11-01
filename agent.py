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

from brodi.config import load_environment

load_environment()

from brodi import policy  # noqa: E402
from brodi.agent import PENDING_TTL_S, DynamicMacAgent  # noqa: E402
from brodi.voice import VoiceGate  # noqa: E402

log = logging.getLogger("brodi.voice")

INSTRUCTIONS = """You are brodi, a terse voice assistant that controls this computer.
Mac actions and Windows actions are handled by a fast tool router before you see the message, so
anything that reaches you is small talk or a quick question. Answer in one short spoken sentence.
No markdown, no lists, no emoji, no follow-up questions."""

VOICE_MODE = os.environ.get("BRODI_VOICE_MODE", "pipeline")  # "pipeline" | "realtime"
STT_MODEL = os.environ.get("BRODI_STT_MODEL", "gpt-4o-mini-transcribe-2025-12-15")
TTS_MODEL = os.environ.get("BRODI_TTS_MODEL", "gpt-4o-mini-tts")
CHAT_MODEL = os.environ.get("BRODI_CHAT_MODEL", "gpt-5.4-nano")
REALTIME_MODEL = os.environ.get("BRODI_REALTIME_MODEL", "gpt-realtime-2.1-mini")
VOICE = os.environ.get("BRODI_VOICE", "onyx")  # deep male voice
LANGUAGE = os.environ.get("BRODI_LANG", "en")


class BrodiAgent(Agent):
    def __init__(self, mac: DynamicMacAgent) -> None:
        super().__init__(instructions=INSTRUCTIONS)
        self.mac = mac
        self.activation = VoiceGate(
            required=os.environ.get("BRODI_WAKE_REQUIRED", "1") != "0",
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
            return  # normal LLM reply

        if outcome.stop:
            try:
                await self.session.say(outcome.speak or "Goodbye.", add_to_chat_ctx=False)
            except RuntimeError:
                pass
            get_job_context().shutdown(reason="user asked brodi to stop")
            raise StopResponse()

        if outcome.speak:
            # Speak the deterministic result and keep it in history so the LLM has context later.
            try:
                self.session.say(outcome.speak, add_to_chat_ctx=True)
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
                turn_detection=SemanticVad(type="semantic_vad", create_response=False, interrupt_response=False),
                input_audio_transcription=AudioTranscription(model=STT_MODEL),
            ),
            turn_handling={"turn_detection": "realtime_llm", "preemptive_generation": {"enabled": False}},
            vad=None,
        )
    return AgentSession(
        # Short desktop commands need local endpointing, not a cloud turn-detector dependency.
        turn_handling={
            "turn_detection": "vad",
            "interruption": {"mode": "vad"},
            "preemptive_generation": {"enabled": False},
        },
        stt=lk_openai.STT(model=STT_MODEL, language=LANGUAGE),
        llm=lk_openai.LLM(model=CHAT_MODEL, temperature=0.3),
        tts=lk_openai.TTS(model=TTS_MODEL, voice=VOICE),
    )


server = AgentServer()


@server.rtc_session(agent_name=os.environ.get("BRODI_AGENT_NAME", "brodi"))
async def entrypoint(ctx: agents.JobContext) -> None:
    session: AgentSession | None = None

    def _filler(text: str) -> None:
        if session is None:
            return
        try:
            session.say(text, add_to_chat_ctx=False)
        except RuntimeError:
            pass

    mac = DynamicMacAgent(
        enable_learning=os.environ.get("BRODI_LEARN", "1") != "0",
        on_learning=_filler,
    )
    await mac.start()

    session = build_session()

    async def _close() -> None:
        await mac.aclose()

    ctx.add_shutdown_callback(_close)

    log.info("pipeline: mode=%s stt=%s tts=%s voice=%s llm=%s", VOICE_MODE, STT_MODEL, TTS_MODEL, VOICE, CHAT_MODEL)
    await session.start(agent=BrodiAgent(mac), room=ctx.room)
    greeting = "Brodi ready. Say hey bro, then tell me what to do."
    if os.environ.get("BRODI_WAKE_REQUIRED", "1") == "0":
        greeting = "Brodi ready. Tell me what to do."
    if not policy.ENABLED:
        greeting += " Warning: the safety policy is off."
    session.say(greeting, add_to_chat_ctx=False)


if __name__ == "__main__":
    agents.cli.run_app(server)
