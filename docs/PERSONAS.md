# Brodi: English commands, brief feedback

Brodi listens for a desktop task, performs it, and acknowledges the result. It is
an action tool, not a conversational assistant. English is the supported language.

## Speech rule

Startup, wake replies, progress, success, confirmation cues, and shutdown use
brief acknowledgements. Calculation questions and failures use a short, useful
English sentence. `brodi.voice.spoken_reply` speaks the actual error, redacts
credentials, and limits error speech to 240 characters. Successful tool results
stay in activity rather than being read aloud.

| Situation | Spoken reply |
| --- | --- |
| Startup | "Ready." |
| Wake phrase | "Yes sir, I'm listening." followed by the ready beep. |
| Work in progress | "On it." (once per command) |
| Successful action | "Got it." |
| Missing search topic | "Search terms?" |
| Missing app | "Which app?" |
| Missing or unfinished calculation | "What should I calculate? Say the full expression." |
| Other missing information | "More details?" |
| Action awaiting approval | "Review action." |
| User cancels | "Cancelled." |
| Failed action | The actual error, such as "Cannot divide by zero." |
| Timed-out action | The timeout reason and suggested next step. |
| Policy blocks an action | The reason the action was blocked. |
| Detailed result | "Check activity." |
| Unrecognized task | "I couldn't understand the task. Ask me to open an app, search, or calculate." |
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
router is instructed to accept English commands. Successful page content stays
in activity; failures are spoken as bounded English feedback.

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
