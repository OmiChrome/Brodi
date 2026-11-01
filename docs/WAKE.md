# Hey bro in the background

Enable once after setting up the API key and microphone:

```powershell
# Windows
.\brodi.cmd wake enable
.\brodi.cmd wake status
```

```sh
# macOS
chmod +x brody
./brody wake enable
./brody wake status
```

Close the terminal and say **"Hey bro, open GitHub."** The full request is handled
by the normal tool router. Say **"Hey bro"** or **"Hey bro, wake up"** to hear
**"Yes sir, I'm listening."**, followed by a short ready beep on your selected
speaker or headphones. Then speak your command. This sequence works on macOS and
Windows; the 20-second command window starts after the beep finishes. Follow-up
commands and answers work in that window; after it expires, say the wake phrase
again. Pending confirmations keep their existing timeout and policy checks.

No app window needs to be open. A background supervisor and cloud voice process
do need to stay running. The service starts when you sign in and restarts the
voice process after an unexpected exit, with a bounded retry delay. It does not
wake a sleeping or powered-off computer, and cannot listen before sign-in.

## Microphone and controls

**Microphone audio is streamed to OpenAI while the listener is enabled and
running, including before the wake phrase.** This uses the configured cloud
transcription service, needs internet access, and incurs API usage. Wake gating
controls which transcribed requests reach the tool router; it is not a local
wake-word model. Background mode uses the cloud pipeline and retains the 1.25x
Onyx voice setting. Recognition and speech are English-only; acknowledgments,
clarifications, failures, and startup speech use at most two words, except for the
wake acknowledgement above. The beep is generated locally and uses no speech API.
Full results and the specific details of actions awaiting confirmation remain in the activity
feed. "Review action" means inspect those details before answering yes.

- `brodi stop` on Windows or `./brody stop` on macOS stops the supervisor and
  microphone process. Windows tray **Stop assistant** / **Quit Brodi** does the same.
  Spoken requests that stop the assistant also stop its supervisor.
- `brodi start` / `./brody start` resumes it. If login startup is enabled, the
  listener also resumes at your next sign-in.
- `brodi wake disable` / `./brody wake disable` stops listening and removes login
  startup. Enable it again to restore background wake.

On macOS, grant **Microphone** permission to the Python/Brodi process when
requested. Check **System Settings > Privacy & Security > Microphone** if the
background process cannot hear you. Desktop actions also need the app's
**Accessibility** and **Automation** permissions. Run `uv run python agent.py console`
once in the foreground to diagnose audio setup. Background and foreground
processes may have separate permission attribution; foreground success alone
does not verify background microphone access.

### Commands are heard but do not respond

On a MacBook, check the selected microphone as well as transcription. An external
headset input can capture sound without reliably recognizing the wake phrase.
If Brodi's activity never shows your command, try the built-in microphone while
keeping replies on your system's selected headphones.

The working configuration from the MacBook live test is saved in
[`config.macbook.example.yaml`](../config.macbook.example.yaml). To apply the same
settings to an existing installation, update these keys in `.env.local`:

```dotenv
BRODI_STT_MODEL=gpt-transcribe
BRODI_STT_SILENCE_MS=400
BRODI_INPUT_DEVICE=MacBook Air Microphone
```

This uses cloud speech boundaries instead of the local volume threshold that can
keep a turn open in background noise. The microphone name is specific to a
MacBook Air; choose an available input on other computers. Audio output continues
to use the system default unless `BRODI_OUTPUT_DEVICE` is configured separately.
Keep your API key in the ignored `.env.local` file. YAML presets do not override
existing environment or `.env` settings.

Restart with `./brody restart`, then say **"Hey Brodi, open Chrome"** and pause.
The live check recognized the request, opened Chrome, and returned **"Got it."**
After the 20-second follow-up window expires, include the wake phrase again;
restarting Brodi also resets that window. Login startup remains enabled across
restarts.

## Implementation and verification

Windows installs a checkout-specific, current-user `Run` registry value pointing
to the virtual environment's `pythonw.exe` and an absolute script path. macOS
installs a user LaunchAgent under `~/Library/LaunchAgents`, with an absolute
virtualenv interpreter, working directory, and a pseudo-terminal for LiveKit.
No administrator access, API keys in startup entries, shell profiles, or `uv` on
the login process's PATH are required. Keep this checkout and its `.venv` in
place; disable and re-enable startup after moving them.

An OS file lock prevents duplicate supervisors. PID records are checked against
process creation time and the exact entrypoint before stopping anything. The
supervisor keeps one cloud voice process warm so a combined wake phrase and task
cannot be lost while launching another transcription session. Explicit Stop is
respected; it is not treated as a crash.

Automated tests cover both platform launch definitions, duplicate-instance locks,
stale PIDs, crash recovery, retry backoff, explicit stop, wake gating, full-command
delivery, and command-window expiry. macOS microphone permissions and real
device behavior must also be checked on a Mac.

Platform references: [Windows login startup](https://learn.microsoft.com/en-us/windows/win32/setupapi/run-and-runonce-registry-keys)
and [Apple user LaunchAgents](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html).
