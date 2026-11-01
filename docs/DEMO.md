# Ready-made demo commands

Start Brodi, wait for **Listening with OpenAI**, and speak. Try **“open Notepad,”
“maximize Notepad,” “restore Notepad,” “close Notepad.”** “Hey bro” is optional.

OpenAI recognizes your voice. The commands below then use the reviewed tool catalog
directly, with no routing-model request or generated script. Replies stay short:
“Got it.” or “On it.”

## Demo menu

| Category | Try saying | Result |
| --- | --- | --- |
| Apps | “Open Notepad”, “open Calculator”, “open Paint”, “open File Explorer”, “open Snipping Tool” | Launch the installed app. Chrome and Edge also have direct launch paths. |
| Close | “Close Notepad”, “quit Chrome”, “exit Paint”, “close Calculator” | Request a normal close of the selected app window. Save prompts stay in the app. |
| Windows | “Minimize Notepad”, “maximize Chrome”, “restore Notepad”, “switch to Paint” | Control a named window. |
| Folders | “Open my downloads”, “show the documents folder”, “open desktop”, “open my home folder” | Open the real Windows known folder, including redirected folders. |
| Sites | “Open YouTube”, “open GitHub”, “open Google Keep”, “open Gmail”, “open Google Calendar”, “open ChatGPT” | Open the site in Chrome. No CDP setup is needed for opening a page. |
| Searches | “Search YouTube for lofi beats”, “search GitHub for voice agents”, “search Wikipedia for artificial intelligence”, “search Google Maps for coffee” | Open the encoded search URL; no click or playback is implied. |
| Media | “Toggle playback”, “next song”, “previous song”, “stop music” | Send a Windows media key to the active media session. Have a player ready first. |
| Volume | “Volume up”, “volume down” | Send five standard Windows volume key presses. |
| Screenshot | “Take a screenshot” | Save a new PNG on the Desktop and show its path in activity. |
| Quick answers | “What time is it?”, “what is today's date?”, “check battery” | Read current system information. |
| Arithmetic | “Calculate 23 times 7”, “calculate (100 minus 5) divided by 5” | Return a bounded numeric calculation without a model. |
| Routine | “Run presentation workspace” | Open Chrome and GitHub through the bounded plan runner. |

The catalog contains **51 example phrases**, with more website/search aliases in
`brodi/commands.py`. “Please”, “could you”, and wake prefixes work with common commands.
Show the list with `python -m brodi.demo --catalog`.

For window controls, bring the intended window forward when several windows of the
same app are open. Brodi refuses an ambiguous target. “Close” sends
[Windows' normal close message](https://learn.microsoft.com/en-us/windows/win32/winmsg/wm-close);
it never force-kills the process or clicks a save/discard dialog. The activity output
says a close was **requested** because the application may still ask you to save.

Windows native controls and media keys are Windows-only. Existing macOS app,
folder, screenshot, and browser actions continue through AppleScript. Time, date,
battery, and arithmetic helpers are portable. Text entry uses the existing UIA
helper and can take seconds; it is not part of the sub-500 ms dispatch claim.

## A fast 60-second stage sequence

1. “Open Notepad.” Show the real window.
2. “Maximize Notepad.” Then “restore Notepad.”
3. “Open Calculator.” Then “calculate 23 times 7.”
4. “Search GitHub for voice agents.” Show the results page.
5. “Open my downloads.” Show Explorer.
6. Bring the empty Notepad window forward. “Close Notepad.”

Keep activity visible. Explain the latency split accurately: **OpenAI handles speech;
ready-made commands skip model routing and dispatch directly to the desktop.**

## Measurements and repeatable tests

Measured on this Windows checkout on November 1, 2026:

| Live check | Samples | Measured dispatch |
| --- | --- | --- |
| 15 presentation scenarios, two passes (including a clarification turn) | 32 turns | 2.5–105.8 ms |
| Notepad maximize / minimize / restore / focus, with real window-state checks | 15 actions | 6.1–40.0 ms |
| Close the separate, empty test Notepad window; verify the window disappeared | 1 action | 9.7 ms |

Every measured command passed the **500 ms post-transcription dispatch budget**.
The runs made zero routing-model calls. Window-state verification is separate from
dispatch timing. Cloud recognition, speech playback, first window paint, and page
loading are excluded. Arbitrary requests may still need Decisions or browser work;
these results are not a promise that every spoken task completes in 500 ms.

A separate recorded-audio check used the real `gpt-live-transcribe` adapter:
“Open calculator.” reached the command engine, dispatched in **23.9 ms**, and a new
Calculator window was verified. The final transcript arrived **416.6 ms after the
last audio chunk** in that one sample. This is an integration check, not an
accuracy benchmark across microphones or accents.

```powershell
# Rehearse all 15 scenarios without actions or API calls.
.\scripts\demo.ps1

# Launch real apps/pages, repeat twice, and fail above the 500 ms dispatch budget.
.\scripts\demo.ps1 -Run -Repeat 2 -Output .freebuff/profiles/ready-tools

# Verify real window states and closing using a separate empty test document.
.\.venv\Scripts\python.exe -m brodi.smoke --desktop

# Select a scenario without opening the entire demo menu.
.\scripts\demo.ps1 -Task calculator -Run

# Offline regression tests; no microphone, API request, or real app control.
.\.venv\Scripts\python.exe -m pytest tests/test_quick_tools.py tests/test_native_launch.py tests/test_performance.py
```

The desktop smoke refuses to proceed if Notepad reuses an existing window instead of
opening the separate test document. On tabbed Notepad, select its option to open files
in a new window before running this check. It never closes existing user documents.
Reports from presentation runs are written to the selected output directory as
`report.json` and `trace.json`; those local artifacts are ignored by Git.

`BRODI_FAST_LAUNCH=true` enables native optimizations for existing Windows tools.
`false` restores their script paths. The new Windows-only window/media tools always
use their reviewed native implementation. Policy checks still run before dispatch;
unknown or destructive requests do not acquire a shortcut around confirmation.
