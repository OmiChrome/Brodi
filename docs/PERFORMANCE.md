# Profiling and presentation tasks

## Current cloud transcription

The current console uses `gpt-live-transcribe` through the persistent Realtime
transcription connection. Recognition is entirely cloud-based; `brodi/endpointing.py`
only measures audio amplitude and commits a turn after 250 ms of quiet. It loads no
local neural or speech model. Language and application-name hints are configurable.
`gpt-transcribe` is available as an explicit accuracy-oriented option using cloud
server VAD. There is no automatic model substitution.

On November 1, 2026, a synthetic 24 kHz recording of "Hey bro, open Chrome and go to
GitHub dot com" was replayed at microphone pace through the actual LiveKit adapter.
`gpt-transcribe` returned the correct command 6,538 ms after the last recorded audio
chunk; `gpt-live-transcribe` returned it in 113 ms. These are single recorded-audio
checks, including recording padding and connection startup, not a benchmark of
physical microphones, accents, noise, or a guaranteed latency.

Reproduce with `python -m brodi.smoke --cloud-stt path/to/24khz-mono-pcm16.wav`.
It sends only that file to the cloud; it does not open a microphone or execute commands.
See the [official realtime transcription guide](https://developers.openai.com/api/docs/guides/realtime-transcription)
for the model's client-commit requirement. The following earlier profiling sections
describe the standalone speech player and historical local console, whose playback
measurements do not measure the current LiveKit voice pipeline.

The profiler measures elapsed time across awaits and worker threads. It covers routing,
context, catalog lookup, Decisions requests/fallback, argument selection, planning,
learning, browser observation/actions, native execution, and voice download/playback.
It also produces CPU profiles for local bottlenecks. Traces contain stage names, timing,
thread/parent IDs and error types; they omit commands, arguments, page text and secrets.

## Measured bottlenecks and changes

Measured on this Windows checkout on November 1, 2026, with real paid Decisions and TTS
requests. Local catalog/routing benchmarks run under the same `cProfile` instrumentation.
These samples identify avoidable work; two network requests are not a latency SLA.

| Work | Before | After | Change |
| --- | --- | --- | --- |
| Route “Could you open Notepad” (two samples) | 256.5–544.6 ms, two model requests | 0.205–0.215 ms, no model requests | Recognize conservative polite command prefixes locally. |
| Tool catalog lookup (100 iterations, median) | 10.44 ms | 0.915 ms | Cache policy checks by rendered template, scope and policy mode. Actual execution still checks the final script. |
| Generate “Got it.” audio | 2,249 ms for the first download | 0.081 ms for session-local reuse | Reuse fixed acknowledgement audio, keyed by voice/model/text. No user-provided speech is cached. |
| Six live demo scenarios, two passes (14 turns) | Existing direct scenarios: 3.5–47.8 ms | 2.9–37.9 ms | Deterministic plans and shortcuts; zero model calls. This is dispatch only. |

The biggest observed conversational delay was **speech download**, rather than local
decision code. The current voice runtime starts playback with the first 1,024-byte PCM
chunk instead of waiting for the full download. Speech uses the natural male `onyx`
voice at 1.25x speed, requested from the service without changing the speaker sample rate.
"Gotcha.", "Got it." and "On it." warm while the local decoder starts, and fixed replies
are cached on disk under the Brodi user-data `speech` directory. Cache keys include
model, voice, speed, delivery instructions and text; arbitrary replies are never cached.
The hotkey cancels both downloading and playback, and tasks longer than 250 ms get
"On it." while execution continues. First-time and arbitrary speech still require
network synthesis; the new `speech.first_audio` span measures that startup delay separately
from the rest of the stream. Browser workflows also make real
network decisions per step, so several steps can accumulate latency; the profiler exposes
each observation, decision, field-text generation and action separately.

Follow-up speaker profiling exposed another 385-473 ms per reply opening the sound
driver, plus more than a second of silent padding in a generated acknowledgement.
The runtime now prepares and reuses one output stream and removes only leading/trailing
padding (retaining short tails and pauses inside speech). With a prepared device,
the cached acknowledgement reached playback setup in **0.28 ms** and finished in
**503 ms**, compared with **2,227 ms** before these two changes. This is a real speaker
API check, not a microphone measurement of acoustic latency. An uncached short reply
still took **1.36 seconds** to receive its first PCM chunk in this sample.
The follow-up report, timeline and sample WAV are in `.freebuff/profiles/voice/`.

The PCM streaming implementation and synthesis speed control follow the
[official OpenAI speech guide](https://developers.openai.com/api/docs/guides/text-to-speech)
and [Speech API reference](https://developers.openai.com/api/reference/python/resources/audio/subresources/speech/methods/create).

The context poller now scans installed apps on a worker thread, keeping filesystem work
off the audio event loop. Its background task has an independent context so it does not
retain finished command profiles.

The live demo opens apps and tabs through the real execution and policy paths. A successful
dispatch means launch was requested; it does **not** prove the window painted or a remote
page loaded. These measurements exclude microphone endpointing, transcription, page load,
first paint, and speaker playback. No claim of end-to-end voice latency is made.

Evidence produced during implementation (ignored local artifacts):

- `.freebuff/profiles/before-route/report.json` and `cpu.pstats`: baseline catalog and real routing.
- `.freebuff/profiles/final/report.json`, `trace.json`, `cpu.pstats`: final catalog, routing and real speech comparison.
- `.freebuff/profiles/demo-before/report.json`: original four live scenarios, twice.
- `.freebuff/profiles/demo-after/report.json`: all six live scenarios, twice.
- `.freebuff/profiles/rehearsal/report.json`: offline rehearsal.

## Stage runbook

From the project directory on Windows:

```powershell
# Rehearse without opening apps, recording audio, or using paid APIs.
.\scripts\demo.ps1

# Run all six scenarios, twice, and show shortcut memory on the second pass.
# Opens Notepad, Chrome, GitHub and YouTube search pages.
.\scripts\demo.ps1 -Run -Repeat 2

# Or present one scenario at a time.
.\scripts\demo.ps1 -Task automation -Run
```

Each run writes `report.json` and a timeline `trace.json` to `.freebuff/profiles/demo`.
Use `-Output` to preserve a separate run. A failed expectation stops the run and returns
a nonzero exit code. Presentation tasks are guarded against model routing and learning.
Demo runs use temporary memory and omit learned tools, leaving the user's saved memory
and tools alone. The offline rehearsal validates parsing, exact tool arguments and policy;
the live run exercises clarification and shortcut replay as well.

| Scenario | Stage command | Expected behavior |
| --- | --- | --- |
| launch | “Hey bro, open Notepad” | Request a Notepad launch. |
| workspace | “Open Chrome and go to github.com” | Run a bounded two-step plan. |
| research | “Search YouTube for ambient piano” | Open the exact search URL. |
| followup | “Search YouTube”, then “lofi beats” | Ask for the topic, then open its search. |
| automation | “Run presentation workspace” | Run the predefined workspace plan. |
| polite | “Could you open Notepad” | Route locally without a model. |

With the normal assistant running, say **“Hey bro, run presentation workspace”**,
**“Hey bro, run presentation research”**, or **“Hey bro, run presentation reference”**.
These named tasks are available before any training or saved shortcut, and use the
same tool validation, policy, timeouts and confirmation machinery as other plans.
`BRODI_ENABLE_MULTI_STEP` must be enabled (the default). Definitions live in
`brodi/automations.py`; the six scripted presentation scenarios live in `brodi/demo.py`.
Chrome and Notepad must be installed for the full Windows presentation. The workspace,
research and reference automations use cross-platform browser tools.

## Reproduce the profile

```powershell
# Offline local workload, including a real OS snapshot; no app launches.
.\scripts\profile.ps1

# Include real paid API calls and compare speech download with cached reuse.
# No microphone or speaker playback.
.\scripts\profile.ps1 -Decisions -Speech -Output .freebuff/profiles/my-run

# Capture real voice turns and speech stages in the running assistant.
$env:BRODI_PROFILE = "$PWD/.freebuff/profiles/session.jsonl"
.\brodi.cmd restart
```

Remove `BRODI_PROFILE` and restart to disable session recording. Set it in `.env.local`
if the assistant is started from another terminal or the tray. Each turn/reply appends
one JSON object. For programmatic workloads, use `Profile.capture()` with `span()`.
P50/P95 use nearest-rank percentiles. Stage durations are inclusive and must not be
summed across nested spans. `queue_ms` and `total_ms` also appear in every agent outcome.

Import `trace.json` into a trace viewer such as Perfetto for a timeline, or inspect CPU
hotspots with:

```powershell
.\.venv\Scripts\python.exe -m pstats .freebuff/profiles/my-run/cpu.pstats
# In pstats: sort cumulative, then stats 25
```

Equivalent portable commands are `python -m brodi.demo` and `python -m brodi.performance`.
Use `--help` or `python -m brodi.demo --list` for available flags/tasks.
