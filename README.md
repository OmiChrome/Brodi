<h1 align="center">Brodi</h1>
<p align="center"><strong>Your bro for getting things done.</strong><br />Voice-controlled desktop assistance. Short requests, deterministic actions, brief replies.</p>

<img align="right" src="assets/branding/brodi-logo.png" alt="Brodi logo: two hands clasped in a painterly handshake" width="220" />

You want Chrome open, a YouTube search, or a sentence typed into Notepad. You should be able to say:

> **“Hey bro, open Chrome.”**

Brodi turns short requests into actions on **Windows 11 and macOS**. It checks saved shortcuts and uses the **[OpenAI Decisions API](https://developers.openai.com/api/docs/guides/decisions)** when a request needs interpretation. Windows listens locally; macOS defaults to cloud voice. Existing tools do the work, with policy and confirmation gates in charge.

It answers like a bro: **“Gotcha.” “Yo bro.” “Got it.” “On it.”** Longer replies are reserved for missing details, requested results, and approval questions.

Licensed under [MIT](LICENSE).

<br clear="right" />

---

## Why it exists

Small computer tasks should take a small instruction.

| What you need | What Brodi does |
| --- | --- |
| Open an app or website | Matches common commands locally and dispatches an existing tool. |
| Repeat a familiar request | Checks local SQLite shortcuts before calling a model. |
| Give a less familiar instruction | Uses typed decisions to select a tool and extract arguments. |
| Run a short sequence | Executes a bounded plan with policy checks, observation, and limited retry. |
| Know what is happening | Streams input, actions, gathered results, and replies. |
| Stay in control | Asks before risky actions and refuses policy-blocked requests. |

AppleScript, PowerShell, Chrome CDP, and Windows UI Automation are the primary executors.

---

## What you can say

| Request | Behavior |
| --- | --- |
| “Hey bro” / “Yo bro” | Opens a short conversation window and acknowledges you. |
| “Hey bro, open Chrome” | Opens Chrome using the configured local profile. |
| “Go to YouTube” | Opens the homepage. |
| “Search YouTube for lofi beats” | Opens search results. A bare “search YouTube” asks for a topic. |
| “Open Chrome and go to github.com” | Runs a short deterministic plan. |
| “Type hello world into Notepad” | Uses Windows UIA and verifies the entered text. |
| “Open my Google Keep and show the top note” | Uses signed-in Chrome and CDP; requires browser access. |
| “Remember that my preferred browser is Arc” | Saves a local fact; replacing a preference requires confirmation. |
| “What do you remember?” | Lists facts and the number of shortcuts. |
| “Forget preferred browser” | Asks before deleting memory. |

Follow-ups work within the listening window. Say **“cancel”** to dismiss a pending question or action. **Ctrl+Alt+B** activates the hotkey fallback.

---

## How it works

```text
Microphone -> wake + transcription (local on Windows)
                       |
              facts + shortcut lookup
                       |
           direct parser or OpenAI Decisions
                       |
          policy + required confirmation
                       |
      deterministic action or bounded short plan
                       |
          observe result -> reply + activity
```

1. **Capture a short request.** Windows uses local WebRTC VAD and Vosk, with no cloud STT session waiting for the wake phrase. macOS defaults to LiveKit cloud transcription because the project's Vosk dependency is unavailable there. Custom openWakeWord weights can be supplied where local wake is supported.
2. **Check memory first.** Eligible successful commands become local tool-sequence shortcuts. High-confidence matches skip model routing and still pass current tool and policy checks.
3. **Choose rather than generate.** Common commands resolve in Python. Other requests go to Decisions as choices and predicates; missing arguments become questions.
4. **Execute through existing tools.** macOS uses AppleScript, Windows uses PowerShell or isolated pywinauto UIA, and websites use observed DOM controls over CDP.
5. **Keep plans bounded.** Desktop plans default to five action attempts, eight seconds per step, and a forty-second execution budget. Safe failed steps get one retry before bounded recovery. Completed steps are not repeated.
6. **Report what happened.** Tool output appears in the activity feed. Brodi speaks briefly and waits at confirmation gates.

Unknown actions can enter optional tool generation. Proposed scripts are syntax-checked, policy-checked, reviewed, and staged for confirmation before first execution. The CDP browser workflow has a separate bounded loop.

---

## OpenAI APIs and models

**One `OPENAI_API_KEY`** supplies the cloud features. The linked names open official OpenAI documentation.

| OpenAI capability | Where Brodi uses it |
| --- | --- |
| **[Decisions API](https://developers.openai.com/api/docs/guides/decisions)** | Tool selection, argument choices, completeness, confirmation judgments, browser actions, and result verification. |
| **[Responses API](https://developers.openai.com/api/reference/responses/overview)** | New-tool generation, short plans, browser text helpers, chat, and structured routing fallback. |
| **[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)** | Pydantic-backed schemas for proposals, plans, helper text, and routing fallback. |
| **[Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)** | Stable instruction/schema prefixes and cache keys. Hits depend on provider requirements and cache state. |
| **[Text-to-speech API](https://developers.openai.com/api/docs/guides/text-to-speech)** | On-demand acknowledgments and requested results. The spoken voice is AI-generated. |
| **[Speech-to-text API](https://developers.openai.com/api/docs/guides/speech-to-text)** | Cloud transcription in the macOS/default LiveKit pipeline and explicit legacy mode. Windows defaults to local Vosk. |
| **[Realtime API](https://developers.openai.com/api/docs/guides/realtime)** | Optional legacy voice mode when local wake is disabled and `BRODI_VOICE_MODE=realtime`. |

### Model defaults

| Role | Default | Reasoning effort |
| --- | --- | --- |
| Routing and judgments | [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna), Decisions endpoint | Not passed |
| Code generation | [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) | `none` |
| Planner, helpers, chat, routing fallback | [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) | `none` |
| Spoken output | `gpt-4o-mini-tts`, voice `onyx` | Not applicable |
| macOS / legacy cloud transcription | `gpt-4o-mini-transcribe-2025-12-15` | Not applicable |
| Optional legacy Realtime | `gpt-realtime-2.1-mini` | Separate voice configuration |

These describe Brodi's configured defaults; availability and lifecycle notices are maintained in the linked OpenAI documentation. If the native Decisions interface is unavailable, the wrapper can use a structured Responses fallback. Its confidence values are not calibrated native Decisions probabilities.

---

## Requirements

- **Windows 11 or macOS**, **Python 3.12+**, and [uv](https://docs.astral.sh/uv/).
- An [OpenAI API key](https://platform.openai.com/api-keys) for model-backed features and speech output.
- A microphone and speaker for voice mode; the text CLI works without audio.
- Chrome for Chrome-specific tools. Personal website interaction needs an already signed-in profile and debugging access.

macOS requires microphone and per-app Automation permissions; hotkeys/native input may need Accessibility access. Windows UIA targets must be accessible to the current user.

---

## Quick start

```bash
git clone https://github.com/OmiChrome/Brodi.git
cd Brodi
uv sync
```

Create your local configuration:

```powershell
# Windows PowerShell
Copy-Item .env.example .env.local
```

```bash
# macOS
cp .env.example .env.local
```

Edit `.env.local` and set `OPENAI_API_KEY`. On **Windows**, download the offline speech asset once:

```bash
uv run python -m brodi.wake --download-model
```

### Windows

```powershell
.\brodi.cmd start          # assistant + live activity + tray
.\brodi.cmd tray           # controls only; microphone stays stopped
.\brodi.cmd status         # live activity
.\brodi.cmd status --once  # snapshot
.\brodi.cmd stop
```

Double-clicking `brodi.cmd` also starts Brodi. Say **“Hey bro, open Chrome.”**

### macOS

```bash
uv run python agent.py console
```

For the detached launcher, use `chmod +x brody`, then `./brody start`, `./brody status`, or `./brody stop`.

List devices with `uv run python agent.py console --list-devices`. Set `BRODI_INPUT_DEVICE` or `BRODI_OUTPUT_DEVICE` in `.env.local` and restart to change them.

### Text and diagnostics

```bash
uv run python -m brodi.cli --no-learn "open Chrome"
uv run python -m brodi.cli --dry "open github dot com"
uv run python -m brodi.cli --doctor
uv run python -m brodi.cli --memory list
```

`--dry` skips execution and learning; unfamiliar requests can still use model routing. `--doctor` checks setup/browser access without recording audio or reading notes. Local console mode needs no LiveKit cloud account; deployed workers have separate configuration.

---

## Windows tray and live activity

Right-click the logo for **Start assistant**, **Stop assistant**, **Restart assistant**, **Show activity**, **Open log**, or **Quit Brodi**. Double-click opens activity. Windows may place the icon in its hidden-icons menu.

Stop leaves the tray available for restarting. Quit stops both; opened apps stay open. `BRODI_TRAY=false` disables automatic tray startup. An explicit `brodi tray` still opens it.

```text
YOU       open Chrome
DOING     Opening Google Chrome
GATHERED  Tool output
BRODI     Got it.
RESULT    Task completed
STATE     Listening for a task
```

**Q + Enter** or **Ctrl+C** leaves the viewer while Brodi keeps running. Activity defaults to `.freebuff/activity.jsonl`; `brodi debug` shows technical logs. See [tray controls](docs/TRAY.md) and [activity](docs/ACTIVITY.md).

---

## Browser setup

Opening a URL does not require CDP. Reading notes or interacting with a page does.

1. Use an already signed-in Chrome profile for personal sites.
2. Open `chrome://inspect/#remote-debugging` and enable **Allow remote debugging for this browser instance** when supported by your Chrome build.
3. Allow the debugging connection when Chrome prompts.
4. Run `uv run python -m brodi.cli --doctor` to check access.

Brodi discovers the endpoint or uses `BRODI_CDP_URL`. `BRODI_CHROME_PROFILE_EMAIL` and `BRODI_CHROME_PROFILE_DIR` guide profile selection. If Keep access is unavailable, Brodi can open its homepage and explain the requirement; it must not claim it read a note.

---

## Configuration

Precedence: **environment > `.env.local` > `.env` > `brodi.yaml` > defaults**. Copy [.env.example](.env.example) or [config.example.yaml](config.example.yaml). YAML accepts scalar `BRODI_*` settings.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENAI_API_KEY` | Unset | Key for configured OpenAI services. |
| `BRODI_DECISIONS_MODEL` | `gpt-6-luna` | Routing and judgments. |
| `BRODI_CODEGEN_MODEL` | `gpt-6-luna` | New-tool generation. |
| `BRODI_FAST_MODEL`, `BRODI_FAST_FALLBACK_MODEL` | `gpt-6-luna` | Text helpers and routing fallback. |
| `BRODI_PLANNER_MODEL`, `BRODI_CHAT_MODEL` | `gpt-6-luna` | Plans and brief replies. |
| `BRODI_REASONING_EFFORT` | `none` | Configured generation effort. |
| `BRODI_LOCAL_WAKE` | Windows: `true`; macOS: `false` | Local listener or LiveKit cloud voice; keep the platform default unless the required local dependencies are available. |
| `BRODI_WAKE_WORDS` | `hey bro,yo bro` | Activation phrases. |
| `BRODI_WAKE_MODE` | `both` | `continuous`, `hotkey`, or `both`. |
| `BRODI_WAKE_HOTKEY` | `<ctrl>+<alt>+b` | Capture hotkey. |
| `BRODI_WAKE_SENSITIVITY`, `BRODI_WAKE_WINDOW_S` | `0.65`, `20` | Threshold and follow-up window. |
| `BRODI_TTS_ENABLED`, `BRODI_TRAY` | `true`, `true` | Speech and automatic Windows tray. |
| `BRODI_ENABLE_MULTI_STEP`, `BRODI_MAX_STEPS` | `true`, `5` | Desktop plans and maximum attempts. |
| `BRODI_STEP_TIMEOUT_MS`, `BRODI_PLAN_TIMEOUT_S` | `8000`, `40` | Step timeout and execution budget. |
| `BRODI_MEMORY_ENABLED` | `true` | Local facts and eligible shortcuts. |
| `BRODI_MEMORY_RETENTION_DAYS`, `BRODI_MEMORY_MAX_ENTRIES` | `30`, `500` | Matching retention and capacity. |
| `BRODI_LEARN`, `BRODI_POLICY` | `1`, `strict` | Optional learning and safety policy. |
| `BRODI_CDP_URL` | Auto-discovered | Chrome endpoint override. |

Keep pinned models unless you intend to change them. Never commit `.env`, `.env.local`, personal memory, or runtime logs.

---

## Privacy, safety, and limits

**Local listening is not a fully offline assistant.** Windows keeps the default microphone/transcription path local. macOS defaults to cloud transcription, including while waiting for a wake phrase. Accepted commands and relevant app/page context can reach OpenAI for routing, planning, browser decisions, or generation. Replies use cloud synthesis when enabled.

Facts and shortcuts live in an unencrypted SQLite database under `%LOCALAPPDATA%/Brodi` on Windows or `~/Library/Application Support/Brodi` on macOS. Logs can contain commands, tool output, and page text. Redaction is best-effort; treat these files as personal data.

Policy applies to definitions, generated proposals, rendered actions, and cached steps. Risky actions need confirmation. Strict policy blocks sensitive system changes, credentials, destructive filesystem operations, terminal/developer-tool control, and prohibited browser transactions. Approval does not override a hard block.

Brodi is experimental and runs with your permissions. Validation and policy are not an OS sandbox. Input can fail on inaccessible or ambiguous controls; websites can change. Stopping a timed-out worker cannot undo an issued action. Brief TTS playback suppresses microphone capture to avoid echo; the hotkey can interrupt.

Windows launch/shortcut probes completed in tens of milliseconds **after transcription**, excluding window paint, page load, and playback. Complete Notepad input/readback took seconds. **Sub-500 ms speech-to-visible-result is not guaranteed.** macOS has not been physically validated on this Windows test machine.

---

## Repository layout

```text
.
├── agent.py                 Voice entrypoint; local or legacy LiveKit
├── brodi.cmd / console.ps1  Windows launcher, status, logs, tray
├── brody / console.sh      macOS launchers
├── assets/branding/        Official logo and Windows icon
├── tools/                  Seed catalog; ignored learned definitions
├── tests/                  Unit, workflow, opt-in Chrome tests
├── docs/                   Architecture, persona, runtime guides
└── brodi/
    ├── agent.py            State, confirmations, execution
    ├── wake.py             Local wake/VAD, hotkey, capture
    ├── wake_runtime.py     Voice loop and reply playback
    ├── decisions.py        Decisions API and structured fallback
    ├── router.py           Intent and argument selection
    ├── registry.py         Typed tools and availability
    ├── policy.py           Action restrictions and memory gates
    ├── os_backend.py       AppleScript / PowerShell and context
    ├── windows_uia.py      Isolated native input and controls
    ├── native_launch.py    Fast trusted Windows launches
    ├── browser_task.py     Chrome CDP / observed-DOM workflow
    ├── generator.py        Tool generation and validation
    ├── memory.py           SQLite facts and shortcuts
    ├── multi_step.py       Plans, deadlines, observation, retry
    ├── tray.py             Notification-area controls
    ├── activity.py         Journal and streaming viewer
    └── smoke.py            Live probes and offline WAV replay
```

Treat the repository root and checked-in tools as the source of truth.

---

## Tests

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

The regular suite uses mocked provider/desktop responses for routing, wake, policy, confirmations, memory, plans, UIA, tray, activity, and browser workflows. It needs no paid model calls.

Opt into real Chrome tests with an isolated headless profile and local fixtures:

```powershell
$env:BRODI_CHROME_TESTS = '1'
uv run pytest -q
Remove-Item Env:BRODI_CHROME_TESTS
```

Live smoke tests have intentional local effects:

```bash
# Opens Chrome/GitHub; types into a separate Notepad document.
uv run python -m brodi.smoke --latency --notepad

# Local PCM16 WAV replay; no model request.
uv run python -m brodi.smoke --wake-audio path/to/command.wav
```

Fixture tests do not establish access to your real Keep account. See [implementation evidence](docs/IMPLEMENTATION.md) for measurements and test boundaries.

---

## Docs

| Document | Start here if... |
| --- | --- |
| [Documentation index](docs/README.md) | You want the full map. |
| [Architecture](docs/ARCHITECTURE.md) | You want modules, state, execution, and trust boundaries. |
| [Personas](docs/PERSONAS.md) | You are changing the “gotcha / yo bro / got it / on it” interaction. |
| [Implementation](docs/IMPLEMENTATION.md) | You want wake, native input, memory, plans, and smoke tests. |
| [Activity](docs/ACTIVITY.md) | You want accepted input, actions, gathered results, and logs. |
| [Windows tray](docs/TRAY.md) | You want controls, icon behavior, and lifecycle details. |
| [Environment example](.env.example) | You want supported starter settings. |

---

## Contributing

Start with an existing deterministic command or tool. Add a focused behavior test, preserve platform availability and policy checks, and keep replies brief. Browser input changes should use real local fixtures; account-specific behavior needs separate verification.

Read [ARCHITECTURE.md](docs/ARCHITECTURE.md) and [PERSONAS.md](docs/PERSONAS.md) before changing execution or speech.

---

## License and credits

[MIT](LICENSE). Built with OpenAI Decisions and Responses, OpenAI audio APIs, LiveKit, Vosk, WebRTC VAD, pywinauto, pystray, SQLite, and native Windows/macOS automation.

*Say a little. Get it done.*
