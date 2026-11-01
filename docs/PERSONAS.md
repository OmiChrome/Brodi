# Brodi: English commands, brief feedback

Brodi listens for a desktop task, performs it, and acknowledges the result. It is
an action tool, not a conversational assistant. English is the supported language.

## Speech rule

Every spoken reply has **one or two words**. This includes startup, wake replies,
progress, clarification, success, failure, confirmation cues, and shutdown.
`brodi.voice.spoken_reply` selects fixed English phrases at the audio boundary.
It does not truncate tool results or ask a model to summarize them.

| Situation | Spoken reply |
| --- | --- |
| Startup | "Ready." |
| Wake phrase | "Listening." |
| Work in progress | "On it." (once per command) |
| Successful action | "Got it." |
| Missing search topic | "Search terms?" |
| Missing app | "Which app?" |
| Other missing information | "More details?" |
| Action awaiting approval | "Review action." |
| User cancels | "Cancelled." |
| Failed action | "Didn't work." |
| Timed-out action | "Timed out." |
| Policy blocks an action | "Action blocked." |
| Detailed result | "Check activity." |
| Casual conversation | "Commands only." |
| Unsupported script in a transcript | "English only." |
| Stop | "Goodbye." |

Full results, diagnostics, and approval details stay in the activity feed. A short
acknowledgment never replaces the policy checks or authorizes an action. When
Brodi says "Review action," inspect the specific action and target there before
answering yes. A failed or partially completed action must not get a success reply.

The voice remains natural, warm, and male: `onyx`, at `1.25x` synthesis speed.
Speak only the supplied words, without narration or added commentary. Pitch is
preserved by keeping playback at the original sample rate. Speech is AI-generated.

## Listening and routing

The live cloud recognizer is pinned to English. Old language overrides cannot
enable multilingual recognition. A script guard rejects non-Latin transcripts
before action routing; this is not a complete language classifier. The intent
router is instructed to accept English commands, and all spoken output comes
from the fixed English vocabulary regardless of tool or page content.

The LiveKit session has no conversational LLM or speech-to-speech chat mode.
OpenAI Decisions still routes commands and supports the existing action planning.
Background mode requires "Hey bro" and allows a short window for follow-ups.
See [wake setup](WAKE.md) for startup, microphone use, and Stop controls.

## Implementation

- `agent.py`: command-only session, bounded progress speech, short startup/stop.
- `brodi/voice.py`: wake gate, script guard, and spoken response policy.
- `brodi/transcription.py`: fixed English cloud recognition.
- `brodi/speech.py`: English delivery instructions, male voice, and speech speed.
- `brodi/agent.py`: full action outcomes and unchanged confirmation gates.
- `tests/test_voice_replies.py`: speech length, language, failure, and routing regressions.
