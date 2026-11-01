# Brodi — Voice-Controlled AI Computer Assistant

A hands-free AI assistant that lets you control your computer and browser using natural voice
commands. Powered by OpenAI GPT-Live and Decisions API to understand your intent, choose the
right actions, and get things done.

> **Experimental. Not for production.** This prototype lets a language model run scripts
> and click around a browser on your machine. An early version, asked to "clean up my
> desktop", moved every file on the Desktop into a folder. The safety policy in
> [`brodi/policy.py`](brodi/policy.py) exists because of that. Keep it on, read it
> first, and don't point voice control at a machine whose setup you can't afford to lose.

```
OpenAI STT ─► Decisions picks a tool + its arguments (one request) ─► AppleScript / PowerShell ─► OpenAI TTS
                 ├─ website task ─► CDP DOM loop, one Decisions request per step
                 ├─ unknown action ─► Responses API writes a new tool, checked, cached
                 └─ small talk ─► OpenAI LLM, one sentence
```

**One `OPENAI_API_KEY`.** Voice, routing, judgments, code generation and the browser agent
all run on OpenAI. No Gradium, TypeSafe, jev-ultrafast, Silero or local-model server.

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

## Voice: OpenAI

The pipeline mode uses OpenAI STT (`gpt-4o-mini-transcribe`), a small chat model for brief
replies (`gpt-5.4-nano`) and OpenAI TTS (`gpt-4o-mini-tts`, voice `onyx` — a deep male voice). Set
`BRODI_VOICE_MODE=realtime` to run the OpenAI Realtime API instead (`gpt-realtime-2.1-mini`)
with semantic VAD; the turn is still intercepted before a response is generated, so the
router stays in charge.

Every model is an optional override: `BRODI_STT_MODEL`, `BRODI_TTS_MODEL`, `BRODI_CHAT_MODEL`,
`BRODI_REALTIME_MODEL`, `BRODI_VOICE`, `BRODI_DECISIONS_MODEL` (default `gpt-6-luna`),
`BRODI_CODEGEN_MODEL` (default `gpt-6.1-sol`), `BRODI_FAST_MODEL` (default `gpt-5.4-nano`).

## Setup

```bash
uv sync
cp .env.example .env.local   # OPENAI_API_KEY is the only required secret
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

"Hey bro" by itself replies "Hey bro, I'm listening." "Search YouTube" asks what to
search for; answer with the topic, or say "cancel". During the active window, short
commands work without repeating the wake phrase. After a task finishes, the window starts
again. Set `BRODI_WAKE_WINDOW_S` to change its length, or `BRODI_WAKE_REQUIRED=0` for
always-active voice control. Wake activation happens after transcription; the microphone
and OpenAI transcription stay active while Brodi runs. It is not an offline wake-word detector.

Opening apps and websites and simple searches use deterministic routing. Google Keep note
tasks use the browser agent and need an already signed-in Chrome profile and remote debugging
enabled at `chrome://inspect/#remote-debugging`. Brodi discovers Chrome's local debugging
port automatically, or uses `BRODI_CDP_URL` when explicitly set. If access is unavailable,
it opens the Keep homepage and explains the setup step instead of claiming the note was opened.
Run `brodi doctor` (or `python -m brodi.cli --doctor`) to check the API key, Chrome,
browser connection, and policy. The diagnostic never prints the key or reads notes.
Project settings load from `.env.local`, followed by `.env`, before runtime defaults
are imported; existing environment variables take precedence.

```bat
brodi start        :: Windows 11: detached voice assistant, local mic and speaker
brodi status       :: is it still running, and for how long
brodi log          :: transcripts, routes, errors
brodi stop
brodi restart
brodi doctor       :: check setup without recording audio or calling a model
```

`brodi.cmd` forwards to `console.ps1` on Windows. On macOS, `brody` runs the same
local console mode through `uv` and tracks its process for `status` and `stop`.
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

**Testing on a budget.** `BRODI_VOICE_MODE=realtime` and the default `gpt-6.1-sol` codegen
model cost more. For cheap runs use the no-thinking pair below; routing is always the
non-generative Decisions API (`gpt-6-luna`, ~300 input tokens per turn, no output tokens):

```bash
BRODI_CODEGEN_MODEL=gpt-5-nano BRODI_REASONING_EFFORT=minimal
```

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

| Path | Role |
|---|---|
| [`agent.py`](agent.py) | LiveKit entrypoint: OpenAI STT/LLM/TTS or Realtime, router before the LLM |
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
