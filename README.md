<img align="right" src="assets/branding/brodi-logo.png" alt="Brodi logo: two hands clasped together in a warm painterly illustration" width="240" />

<h1 align="left">Brodi</h1>
<p align="left"><strong>Your bro for getting things done.</strong><br />Voice-controlled desktop assistant for Windows 11 and macOS.</p>

A hands-free AI assistant that lets you control your computer and browser using natural voice
commands. Powered by OpenAI GPT-Live and Decisions API to understand your intent, choose the
right actions, and get things done.

<br clear="right" />

> **Experimental. Not for production.** This prototype lets a language model run scripts
> and click around a browser on your machine. An early version, asked to "clean up my
> desktop", moved every file on the Desktop into a folder. The safety policy in
> [`brodi/policy.py`](brodi/policy.py) exists because of that. Keep it on, read it
> first, and don't point voice control at a machine whose setup you can't afford to lose.

```
Local VAD / wake / STT ─► memory / direct parser / Decisions ─► policy ─► AppleScript / PowerShell / UIA ─► TTS
                 ├─ website task ─► CDP DOM loop, one Decisions request per step
                 ├─ unknown action ─► Responses API writes a new tool, checked, cached
                 └─ small talk ─► OpenAI LLM, one sentence
```

**One `OPENAI_API_KEY`.** Routing, judgments, code generation, conversational replies,
and TTS use OpenAI when needed. Default console listening and transcription run locally;
there is no cloud STT session waiting for the wake phrase.

**macOS and Windows 11.** The same assistant, tools and policy run on both platforms:
AppleScript (`osascript`/`osacompile`) on macOS and PowerShell (`powershell.exe`/`pwsh`, AST
validation) on Windows 11.

## Why it's fast: choose, don't generate

A typical computer-use agent runs a loop of *screenshot → LLM reasons → emits an action*.
Each turn is a generation call: seconds of latency, free-form output that has to be parsed,
and a model that can hallucinate a button that isn't there.

Brodi inverts this. Code owns the workflow and hands the Decisions API small, typed questions:

- **Routing is one Choice.** The options are the tools that apply to the apps open *right
  now*, plus "chat" and "new action". Every option gets a probability, not prose. The
  confidence is a number the code can act on: high → run, middling → ask, low → don't.
- **Arguments ride along speculatively.** In the same request, every enum argument of
  every candidate tool is asked as its own Choice ("which app?", "louder or quieter?").
  Only the winner's answers are read. Free-text arguments are filled by having the Decisions
  API *select* the right span of the utterance rather than write one.
- **The browser is the same idea per step.** Code observes the page as an indexed table of
  controls, then one Decisions request picks an operation (click, fill, scroll, wait, done,
  blocked) and a target index. Model output never becomes a selector or code; every target
  is an element the code observed.
- **Judgments, not chat.** Follow-ups, missing form details, confirmations and outcome
  verification are single predicates or choices on the same endpoint.

## Voice: local activation, brief replies

Default console mode uses WebRTC VAD plus Vosk offline keyword spotting and command
transcription. A custom openWakeWord ONNX model can be supplied. Press **Ctrl+Alt+B**
for the hotkey fallback. Brodi replies briefly: “Gotcha,” “Yo bro,” or “Got it.”
OpenAI TTS uses `gpt-4o-mini-tts` and voice `onyx`; `BRODI_TTS_ENABLED=false` disables it.

Routing, codegen, text helpers, chat, and the bounded planner default to **gpt-6-luna**;
generation uses `BRODI_REASONING_EFFORT=none` and stable prompt-cache keys. Keep those
defaults unless you explicitly intend to change them.
`BRODI_LOCAL_WAKE=false` selects the legacy LiveKit console path, which uses cloud STT
(`BRODI_VOICE_MODE=pipeline`) or Realtime (`BRODI_VOICE_MODE=realtime`).

## Setup

```bash
uv sync
cp .env.example .env.local   # OPENAI_API_KEY is the only required secret
uv run python -m brodi.wake --download-model  # one-time offline speech asset download
```

- macOS asks for Automation access per app the first time. Grant it in System Settings ▸
  Privacy & Security ▸ Automation.
- Windows 11 needs Python 3.12 and `uv`; PowerShell 5.1 or 7 is already present.
- For browser tasks, once: open `chrome://inspect/#remote-debugging` and tick **Allow remote
  debugging for this browser instance**. Click Allow on Chrome's sheet at first connection.
- `BRODI_CHROME_PROFILE_EMAIL` pins Chrome to one Google account; unset, the last-used
  profile is kept. On Windows the Local State path comes from `%LOCALAPPDATA%`.
- Set `BRODI_OS=win32` or `darwin` to exercise the other platform backend on one machine.

## Run

On Windows, double-click `brodi.cmd` or run `.\brodi.cmd start`. It uses the project's
installed Python environment when available. Say **"Hey bro"** or **"Yo bro"** to activate
voice control, then give a command within 20 seconds. You can also combine them:

- "Hey bro, open Chrome."
- "Yo bro, open Notepad."
- "Hey bro, go to YouTube."
- "Search YouTube for lofi beats."
- "Open my Google Keep and show the top note."

"Hey bro" by itself replies "Gotcha." "Search YouTube" asks what to
search for; answer with the topic, or say "cancel". During the active window, short
commands work without repeating the wake phrase. After a task finishes, the window starts
again. Set `BRODI_WAKE_WINDOW_S` to change its length. `BRODI_WAKE_MODE` accepts
`continuous`, `hotkey`, or `both` (default). Audio stays local while waiting; accepted
command text can reach cloud routing when the direct parser or memory cannot handle it.

“Open Chrome and go to github.com” runs a bounded deterministic plan. Successful app/site
commands become local SQLite shortcuts; repeating one skips model routing. Say “remember
that my preferred browser is Arc,” “what do you remember?”, or “forget preferred browser.”
Replacing facts and deleting memory require confirmation. “Type hello world into Notepad”
uses an isolated Windows UIA helper and verifies the entered text.

Opening apps and websites and simple searches use deterministic routing. Google Keep note
tasks use the browser agent and need an already signed-in Chrome profile and remote debugging
enabled at `chrome://inspect/#remote-debugging`. Brodi discovers Chrome's local debugging
port automatically, or uses `BRODI_CDP_URL` when explicitly set. If access is unavailable,
it opens the Keep homepage and explains the setup step instead of claiming the note was opened.
Run `brodi doctor` (or `python -m brodi.cli --doctor`) to check the API key, Chrome,
browser connection, and policy. The diagnostic never prints the key or reads notes.
Project settings load from `.env.local`, followed by `.env`, before runtime defaults
are imported; existing environment variables take precedence. `brodi.yaml` supplies
lower-priority settings; copy [config.example.yaml](config.example.yaml).

```bat
brodi start        :: start the assistant and stream readable activity
brodi tray         :: open Windows tray controls without starting voice
brodi status       :: stream current activity, accepted input, and output
brodi status --once :: print a snapshot and exit
brodi start --once  :: start in the background and print a snapshot
brodi log          :: recent readable activity
brodi debug        :: technical logs and tracebacks
brodi stop
brodi restart
brodi doctor       :: check setup without recording audio or calling a model
```

### Windows tray controls

`brodi start` automatically opens the branded Brodi icon in the Windows system tray.
Right-click it for **Start assistant**, **Stop assistant**, **Restart assistant**,
**Show activity**, **Open log**, and **Quit Brodi**. Double-click the icon to show
activity. If Windows hides it, look in the tray's overflow menu.

**Stop assistant** leaves the tray available so you can start listening again.
**Quit Brodi** stops both the assistant and tray; apps Brodi opened stay open.
Run `brodi tray` to open the controls without starting voice. Set `BRODI_TRAY=false`
in `.env.local` to disable automatic tray startup (`true` is the default).

The live activity view shows **YOU**, **DOING**, **GATHERED**, **BRODI**, **RESULT**, and
**STATE** rows: what Brodi heard, what it is doing, information returned by tools/pages,
its reply, and whether it is listening or waiting for an answer. For example:

```text
13:04:01  YOU       Hey bro, open Chrome
13:04:01  DOING     Opening Google Chrome
13:04:02  GATHERED  Tool output
                    Output: Google Chrome opened
13:04:02  BRODI     Google Chrome opened
13:04:02  RESULT    Task completed
13:04:02  STATE     Listening for a task
```

In an interactive terminal, type **Q + Enter** to close the view while keeping the
assistant running; **Ctrl+C** also closes the viewer. Use `brodi stop` to stop Brodi.
The activity journal defaults to `.freebuff/activity.jsonl` (ignored by Git), or set
`BRODI_ACTIVITY_LOG`. See [activity documentation](docs/ACTIVITY.md) for details.

`brodi.cmd` forwards to `console.ps1` on Windows. On macOS, `brody` runs the same
local console mode through `uv` and tracks its process for `status` and `stop`.
If audio goes to the wrong device, set `BRODI_OUTPUT_DEVICE` in `.env.local` to a
speaker name shown by `uv run python agent.py console --list-devices`. You can set
`BRODI_INPUT_DEVICE` the same way for the microphone, then run `brody restart`.
Install the macOS command once with:

```bash
chmod +x brody
mkdir -p ~/.local/bin
ln -sf "$(pwd)/brody" ~/.local/bin/brody
# Add ~/.local/bin to PATH in ~/.zshrc if it is not already there.
```

No LiveKit server or cloud account is involved —
console mode runs the whole pipeline on your machine and your one OpenAI key. Put this
folder on your `PATH` (done automatically by setup on Windows) or call `.\console.ps1 start`
directly. Foreground and macOS equivalents:

```bash
uv run python agent.py console                    # foreground, either platform
brody start                                      # macOS detached: start|stop|status|restart|log
uv run python -m brodi.cli --dry "open github dot com"   # route only, no audio
uv run python -m brodi.cli --policy               # every tool and whether policy allows it
```

See [implementation and smoke tests](docs/IMPLEMENTATION.md) for the new modules,
configuration, live Windows results, and latency limits. Dispatch can be fast without
making speech endpointing, application startup, or network navigation instantaneous.

## What it can and can't do

**Allowed:** open, switch and quit ordinary apps; open files, folders and URLs; browser tabs
and web tasks; Slack, Messages and Mail; Notes, Reminders, Calendar; media, volume,
notifications, screenshots, dark mode.

**Blocked (both platforms):** System Settings and preference writes; terminals and developer
tools; shell commands outside a short allow-list; package managers and language tooling;
system and config paths; deleting, moving or duplicating files; emptying the trash; power and
session control; admin privileges; keychain, passwords and credential managers. Windows also
blocks `cmd`/`regedit`/Task Manager, `Remove-Item`/`Clear-RecycleBin`/`Stop-Computer`/
`Invoke-Expression`/`Set-ExecutionPolicy`, elevation (`-Verb RunAs`), and `C:\Windows`,
`C:\Program Files`, AppData, HKLM: and the Windows Run key. In the browser: buying, checkout,
payments, sign-in and account changes.

The policy runs when a tool is loaded, when a new tool is generated, and on the rendered
script right before execution. Anything that changes state (a risky script, an add-to-cart,
a freshly written tool) is read back and needs a spoken yes. It does not protect against a
mis-heard word that happens to be a valid command; `BRODI_POLICY=off` exists for debugging
and the greeting says so out loud.

## Layout

For the full module and runtime guide, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
For Brodi's brief "gotcha / yo bro / got it / on it" voice and interaction rules, see
[docs/PERSONAS.md](docs/PERSONAS.md). The [documentation index](docs/README.md) links both.

| Path | Role |
|---|---|
| [`agent.py`](agent.py) | Local wake console by default; legacy LiveKit pipeline/Realtime and worker entrypoints |
| [`brodi/wake.py`](brodi/wake.py), [`wake_runtime.py`](brodi/wake_runtime.py) | Offline microphone/wake lifecycle, hotkey, command capture, short speech replies |
| [`brodi/memory.py`](brodi/memory.py), [`multi_step.py`](brodi/multi_step.py) | Local facts/shortcuts and bounded plans, observation, retry, approval continuation |
| [`brodi/windows_uia.py`](brodi/windows_uia.py), [`native_launch.py`](brodi/native_launch.py) | Isolated native input/control and fast trusted Windows app launches |
| [`brodi/decisions.py`](brodi/decisions.py) | OpenAI Decisions wrapper (choices/predicates, `responses.parse` fallback) |
| [`brodi/router.py`](brodi/router.py) | Batched routing, speculative arguments, follow-up and completeness judgments |
| [`brodi/agent.py`](brodi/agent.py) | State machine: act, ask, confirm, learn |
| [`brodi/os_backend.py`](brodi/os_backend.py) | Execution, syntax validation, escaping and desktop context on both OSes |
| [`brodi/registry.py`](brodi/registry.py) + [`tools/seed.json`](tools/seed.json) | Tools with typed argument slots and `script`/`script_win`; learned ones go to `tools/learned.json` |
| [`brodi/generator.py`](brodi/generator.py) | Responses API writes a new tool: compile, effect, policy and Decisions-review gates |
| [`brodi/browser_task.py`](brodi/browser_task.py) | Self-contained CDP loop in the user's Chrome: pinned profile, follow-ups, spoken results |
| [`brodi/policy.py`](brodi/policy.py) | The safety policy, macOS and Windows 11 |
| [`brodi/chrome.py`](brodi/chrome.py), [`applescript.py`](brodi/applescript.py), [`resolvers.py`](brodi/resolvers.py), [`cli.py`](brodi/cli.py) | Profile lookup, platform facade, computed args, osascript, text REPL |

Add a tool by appending to `tools/seed.json`: a name, description, optional app `scope`,
`args` (enum with `criteria`, or `text`), an AppleScript `script`, a PowerShell `script_win`
for Windows, and what to `speak` afterwards. See the existing entries.

## Development

```bash
uv run pytest -q                                  # offline tests, including short voice commands
uv run ruff check . && uv run ruff format --check .
```

Real Chrome input and navigation tests use a separate headless profile and local fixture
pages. They do not use your signed-in browser, microphone, or paid APIs. On PowerShell:

```powershell
$env:BRODI_CHROME_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest -q
Remove-Item Env:BRODI_CHROME_TESTS
```

These tests cover replacing field text, submitting a search, opening a note card, and
operating checkboxes. They complement live account checks; a fixture note is not a test
of access to your real Google Keep account.

MIT, see [LICENSE](LICENSE).
