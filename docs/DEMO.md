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
| Calculator | “What is 500 x 450?”, “calculate (100 minus 5) divided by 5”, “close calculator” | Press actual Calculator controls, verify the displayed answer, then close its window when asked. |
| Spotify Web | “Play Mockingbird by Eminem”, “play God's Plan by Drake on Spotify” | Select the matching track and verify playback in your signed-in Chrome profile. |
| GitHub Trending | “Open trending page in GitHub”, “open GitHub trending” | Open `https://github.com/trending` directly. |
| Routine | “Run presentation workspace” | Open Chrome and GitHub through the bounded plan runner. |

The catalog contains **55 example phrases**, with more website/search aliases in
`brodi/commands.py`. “Please”, “could you”, and wake prefixes work with common commands.
Show the list with `python -m brodi.demo --catalog`.

For window controls, bring the intended window forward when several windows of the
same app are open. Brodi refuses an ambiguous target. “Close” sends
[Windows' normal close message](https://learn.microsoft.com/en-us/windows/win32/winmsg/wm-close);
it never force-kills the process or clicks a save/discard dialog. The activity output
says a close was **requested** because the application may still ask you to save.

Windows native controls and media keys are Windows-only. Existing macOS app,
folder, screenshot, and browser actions continue through AppleScript. Time, date,
battery helpers are portable. Calculator uses actual UIA button invokes on Windows
and expression keystrokes through AppleScript on macOS. The Windows result is read
back and checked; the macOS path still needs a live Mac check. Text entry uses the existing UIA
helper and can take seconds; it is not part of the sub-500 ms dispatch claim.

## A fast 60-second stage sequence

1. “Open Notepad.” Show the real window.
2. “Maximize Notepad.” Then “restore Notepad.”
3. “What is 500 x 450?” Show **225,000** in Calculator. Then “close calculator.”
4. “Open trending page in GitHub.” Show the actual page.
5. “Open my downloads.” Show Explorer.
6. Bring the empty Notepad window forward. “Close Notepad.”

Keep activity visible. Explain the latency split accurately: **OpenAI handles speech;
ready-made commands skip model routing and dispatch directly to the desktop.**

## Measurements and repeatable tests

Measured on this Windows checkout on November 1, 2026:

| Live check | Samples | Measured dispatch |
| --- | --- | --- |
| Earlier 15-scenario run, before Calculator used visible button entry | 32 turns | 2.5–105.8 ms |
| Notepad maximize / minimize / restore / focus, with real window-state checks | 15 actions | 6.1–40.0 ms |
| Close the separate, empty test Notepad window; verify the window disappeared | 1 action | 9.7 ms |
| GitHub Trending, native Chrome launch | 1 command | 24 ms |
| First 500 × 450, including Calculator launch and verified display | 1 command | 1,340.9 ms |
| Repeat 500 × 450 in the same Calculator window | 1 command | 101.3 ms |
| Parentheses, precedence, percentages, negatives, square root, power | 6 commands | 152.2–352.2 ms |

The runs made zero routing-model calls. Desktop launch timings exclude first paint;
Calculator timings include button entry and display verification. Cold Calculator
startup exceeds 500 ms. Cloud recognition and speech playback are excluded, and
Spotify/page loading adds network latency. These are measured examples, not a promise
that every spoken task completes in 500 ms.

Calculator supports short expressions with addition, subtraction, multiplication,
division, parentheses, powers, square roots, and “20 percent of 500”. It checks numeric
input before launch, preserves operator precedence, and never types a precomputed
answer into the app. It owns a separate Calculator window and leaves other windows
alone. Brodi says **“Got it”**, leaving the answer on screen.

### Spotify stage setup

Sign in to Spotify Web in Brodi's configured Chrome profile. Enable browser access
using the [README instructions](../README.md#browser-access), then test your chosen
song before going on stage. Say “play Mockingbird by Eminem” or “play God's Plan by Drake”.
Other requests use Spotify search and require a matching title and artist. Brodi
clicks Play once and checks the track identity and playing state; it reports a missing
login, blocked player, or timeout instead of claiming success. Spotify's account and
regional playback restrictions still apply. This is a live-web demo, not a 500 ms demo.

The real Chrome DOM fixture test passed search, selection, clicking, and playback
state checks. Live Spotify audio remains unverified on this workstation because
Chrome remote debugging was unavailable during the check.

A separate recorded-audio check used the real `gpt-live-transcribe` adapter:
“Open calculator.” reached the command engine, dispatched in **23.9 ms**, and a new
Calculator window was verified. The final transcript arrived **416.6 ms after the
last audio chunk** in that one sample. This is an integration check, not an
accuracy benchmark across microphones or accents.

```powershell
# Rehearse all 15 scenarios without actions or API calls.
.\scripts\demo.ps1

# Check a fast launch path against the 500 ms dispatch budget.
.\scripts\demo.ps1 -Task launch -Run -Repeat 2 -Output .freebuff/profiles/ready-tools

# The complete menu includes cold Calculator startup; give it the UI budget.
.\scripts\demo.ps1 -Run -Repeat 2 -MaxDispatchMs 8000

# Verify real window states and closing using a separate empty test document.
.\.venv\Scripts\python.exe -m brodi.smoke --desktop

# Real Calculator buttons, eight verified calculations, and owned-window close.
.\.venv\Scripts\python.exe -m brodi.smoke --calculator

# Actually play a song and verify the signed-in web player.
.\.venv\Scripts\python.exe -m brodi.smoke --spotify "Mockingbird by Eminem"

# Isolated real Chrome DOM tests; no account or real music playback involved.
$env:BRODI_CHROME_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest tests/test_spotify_integration.py tests/test_chrome_integration.py -q

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
