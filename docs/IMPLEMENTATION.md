# Local wake, native input, memory, and bounded plans

> Voice update: Brodi uses `gpt-live-transcribe` cloud streaming transcription on both platforms. A lightweight amplitude-based silence detector commits audio after 250 ms of quiet; no Vosk or local speech model is loaded. Start the app to listen; wake phrases are optional. The former offline runtime and its model dependencies were removed. Earlier local-listener setup and measurements below are historical; use the [current README](../README.md) and [hackathon guide](HACKATHON.md) for the active voice flow.


## What changed

The default `agent.py console` path uses OpenAI streaming transcription, server-side
turn detection, and on-demand speech output. Commands enter `DynamicMacAgent` for
routing, memory, deterministic execution, and bounded plans. No local speech or VAD
model is loaded, and no local model download is required.

All Brodi instances were stopped for this implementation. The live probes create
short-lived helpers and test documents; they do not install a background service.

```text
brodi/
  models.py          model defaults and stable prompt cache keys
  config.py          env + safe YAML configuration
  paths.py           platform-specific private user-data location
  wake.py            text-only wake-prefix utilities; no audio or models
  windows_uia.py     bounded subprocess owning pywinauto backend="uia"
  native_launch.py   fast trusted Windows app/site launch; no shell generation
  memory.py          versioned SQLite facts and tool-sequence shortcuts
  multi_step.py      typed plans, strict API proposal schema, observe/retry/recovery
  smoke.py           opt-in real Notepad, wake-audio, and latency probes
  agent.py           shared state, per-action gates, memory lookup, plan continuation
tests/
  test_wake_local.py
  test_windows_uia.py
  test_memory_plans.py
  test_native_launch.py
  ...existing routing, policy, OS, activity, and Chrome tests
config.example.yaml
.env.example
```

The original AppleScript, PowerShell, CDP, generator, activity feed, and text CLI remain.
This adds a bounded desktop workflow, not a general screenshot/vision agent.

## Model defaults and caching

Routing, codegen, helpers, planner, chat, and the structured routing fallback default to
`gpt-6-luna`. Text-generation calls explicitly use `reasoning.effort=none` by default.
Decisions keeps its existing endpoint and does not receive a reasoning parameter.
Speech model defaults are separate and were not changed to a text-only model.

Stable system instructions precede changing user input. Codegen, browser helpers,
planner, chat, and the Responses fallback use stable `prompt_cache_key` values.
Dynamic fallback schema construction has a bounded in-process LRU cache. Actual
server prompt-cache hits remain dependent on prefix length, compatibility, and server
cache state; setting a cache key does not guarantee a hit.

The model/effort defaults and structured planner/codegen calls were checked against
the live API. The implementation uses the model the user requested rather than
substituting a different tier.

## OpenAI voice

Start the app and give a command. `gpt-live-transcribe` recognizes speech through a
streaming OpenAI transcription connection. A lightweight amplitude detector commits
turns after 250 ms of quiet; it does not transcribe speech or load a neural model.
The STT plugin uses this explicit detector and AgentSession uses `vad=None`, preventing
the SDK's local Silero fallback. Automatic conversational generation waits for task routing.
?Hey bro? is optional (`BRODI_WAKE_REQUIRED=false`). Stop/Quit closes the voice session.
The former Vosk runtime, installer, and local inference dependencies have been removed.
`wake.py` now contains text-only prefix utilities used by tests and smoke probes.

A prerecorded ?Hey bro. Open Chrome.? passed through the real OpenAI transcription
stream and the core to an actual Chrome launch. Repeating it hit local shortcut memory
without a routing-model call. The hidden Windows voice process reached its listening
state. These integration checks are not a broad acoustic accuracy benchmark.

## Native Windows input

`windows_uia.py` is the isolated helper. It imports pywinauto only in the Windows worker,
always with `backend="uia"`. Public operations include focus, read state/text, append/set
text, and click a uniquely identified button. Targets support title, PID, or handle.
Ambiguous windows or text fields produce an error instead of choosing arbitrarily.

The helper prefers `set_edit_text`, preserves existing content for normal typing,
and uses escaped literal `type_keys` only after checking that a failed COM call did
not already change the field. Read-back verifies the result. A timeout never triggers
an automatic repeat of text input. Replacing existing text requires explicit confirmation
through the helper API. Password controls and sensitive applications are blocked.

IPC is one JSON request/result per subprocess. Timeouts kill the owned helper tree,
including the Windows virtualenv redirector's child, so a hung UIA call does not leave
a typing worker running indefinitely. Already-issued native actions cannot be undone
by killing the helper, so timeouts tell the user to check the app before retrying.

The registry includes `type_text`, `focus_window`, `window_state`, and
`click_native_button`. Button clicks use the existing confirmation state; generic
credential/destructive buttons are also refused by the helper. On macOS, text input
retains its AppleScript implementation; the Windows-only control tools are unavailable.

## Facts and shortcuts

Memory defaults to `%LOCALAPPDATA%\Brodi\memory.sqlite3` on Windows or
`~/Library/Application Support/Brodi/memory.sqlite3` on macOS. It uses SQLite WAL,
parameterized queries, and `PRAGMA user_version=1`. Future schema versions are refused.
POSIX database permissions are restricted to the owner.

The two collections are facts and shortcuts. Shortcuts store tool names, string
arguments, and success criteria, never executable scripts. Exact normalized lookup
occurs before routing; conservative fuzzy lookup only tolerates punctuation/spacing
changes for app-opening shortcuts. Changed commands or arguments are not guessed.
Every cached step is resolved against today's registry and passes today's policy and
confirmation gates. Previously stored approval is never reused.

Successful common app/homepage commands and plans can be cached automatically.
Payload-bearing text, arbitrary searches, browser-agent tasks, and risky actions are
not automatically cached. The cap rejects new entries rather than silently evicting
old ones. Retention excludes expired entries from matching; inspection still exposes
expired records. Replacing active facts or different active shortcut definitions and
deleting memory require confirmation. Credentials are rejected by the memory policy.

Examples:

```text
remember that my preferred browser is Arc
remember that I work in ~/code
what do you remember?
forget preferred browser
yes
```

The preferred-browser fact is used for `open browser`; explicit app names still win.
Other facts are stored and inspectable, not permission to execute instructions or
automatically access the remembered filesystem path.

```powershell
python -m brodi.cli --memory list
python -m brodi.cli --memory forget --memory-key preferred_browser
python -m brodi.cli --memory clear --yes
```

Noninteractive deletion without `--yes` exits without deleting anything. Memory is
local application data and is not synced or automatically sent to an LLM.

## Bounded desktop plans

An exact compound such as `open Chrome and go to github.com` is split into known tools
without a planner request. Otherwise a batched Decisions predicate can mark a request
multi-step, and the cheap Luna planner proposes a short tool sequence. Its strict API
schema represents arguments as name/value arrays, then converts them into internal
typed `Step` objects. Unknown tools/arguments and duplicate arguments are rejected.
Missing details still lead to a question rather than invented values.

`PlanRunner` defaults to five total action attempts, eight seconds per attempt, and
40 seconds overall. Configuration cannot raise the action cap beyond six. Retries and
recovery attempts consume that same budget. A failed safe app/site-opening step gets
one retry after bounded backoff, followed by at most one recovery proposal. Successful
earlier steps are not replayed. Stateful typing is not automatically retried.

Observation is lightweight: tool result, active app, running apps, and a foreground
window title on Windows. Explicit success criteria trigger observation and a Decisions
judgment; unknown/unverified results do not get a success reply. Recovered actions retain
the original success criterion. No screenshot recognition or clipboard harvesting was added.

Every step revalidates arguments, availability, and policy. A risky step pauses the
plan at the existing approval gate; continuation retains its remaining steps, attempt
budget, and deadline. An expired plan cannot execute after a late confirmation.
The optional planner and observation calls are bounded too. Existing CDP website
tasks keep their separate browser loop/budget and are not nested as an unbounded desktop
plan step. Successful eligible plans are persisted as memory shortcuts.

## Configuration

Precedence: existing environment, `.env.local`, `.env`, then `brodi.yaml` (or the file
named by `BRODI_CONFIG`). YAML is parsed with `safe_load`, allows scalar `BRODI_*`
settings, and is ignored by Git. Copy [config.example.yaml](../config.example.yaml).

| Key | Default |
| --- | --- |
| `BRODI_DECISIONS_MODEL`, `BRODI_CODEGEN_MODEL`, `BRODI_FAST_MODEL`, `BRODI_PLANNER_MODEL`, `BRODI_CHAT_MODEL`, `BRODI_FAST_FALLBACK_MODEL` | `gpt-6-luna` |
| `BRODI_REASONING_EFFORT` | `none` |
| `BRODI_WAKE_WINDOW_S` | `20`; pending answers can extend capture to 60 seconds |
| `BRODI_TTS_ENABLED` | `true` |
| `BRODI_ENABLE_MULTI_STEP` | `true` |
| `BRODI_MAX_STEPS` | `5`, capped at `6` |
| `BRODI_STEP_TIMEOUT_MS` | `8000` |
| `BRODI_PLAN_TIMEOUT_S` | `40` |
| `BRODI_MEMORY_ENABLED` | `true` |
| `BRODI_MEMORY_RETENTION_DAYS`, `BRODI_MEMORY_MAX_ENTRIES` | `30`, `500` per collection |
| `BRODI_MEMORY_PATH`, `BRODI_DATA_DIR` | Optional application data overrides |
| `BRODI_FAST_LAUNCH` | `true`; `false` keeps the PowerShell launcher path |

## Running the probes

```powershell
uv run pytest -q
$env:BRODI_CHROME_TESTS = '1'
uv run pytest -q
uv run ruff check .
uv run ruff format --check .

# Actual local side effects: opens Chrome/GitHub and a separate Notepad test document.
uv run python -m brodi.smoke --latency --notepad

```

Latency probes use temporary memory rather than changing the user's facts/shortcuts.
Notepad probes create a uniquely named test document and verify `hello world` through
the wake-controller -> core -> UIA path. They leave the test document open and never
overwrite an existing user document.

Measured on this Windows checkout with Chrome already installed/running: first Chrome
dispatch **19.23 ms**, repeated memory hit **13.32 ms**, two-step Chrome/GitHub dispatch
**30.62 ms**, repeated plan **25.39 ms**, with **zero model-routing calls** for those
direct examples. This is post-transcription dispatch completion, not window paint,
network navigation completion, wake endpointing, or TTS playback latency. The complete
Notepad input/read-back probe took **3.3–4.4 seconds** across live runs. Sub-500 ms speech-to-visible-result
is not guaranteed.

Live probes also decoded generated `hey bro` and `yo bro` WAVs locally, including
the two-step command, and ran the idle microphone runtime with network connects blocked
and **zero connection attempts**. These are not accent/noise accuracy benchmarks or
a test of another person's physical voice. macOS paths are unit/simulation tested,
not physically exercised on this Windows machine. Custom openWakeWord weights,
long-running false-positive measurement, and acoustic echo cancellation remain follow-up
work. The OpenAI pipeline disables speech interruptions during short spoken replies. Chrome remote-debugging permission
is still required for real website interaction.

References: [Luna model/effort support](https://developers.openai.com/api/docs/models/gpt-6-luna),
[prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching),
[openWakeWord](https://github.com/dscripka/openWakeWord),
[Vosk models](https://alphacephei.com/vosk/models),
[pywinauto UIA controls](https://pywinauto.readthedocs.io/en/latest/code/pywinauto.controls.uia_controls.html).
