# Microphone mute and listening cues

The local voice console registers **Ctrl+Space** on Windows and **Control+Space** on macOS. It works while another app has focus. One press toggles; holding the keys does not repeatedly toggle. The shortcut is released on shutdown. Remote LiveKit workers retain their existing input behavior.

The 180 ms rising chime means listening; the falling chime means muted. Both are quiet PCM tones generated locally, with no TTS or network request. The input stays blocked during a cue and a short echo tail. The tray and activity view provide the corresponding visual hint. A speaker failure reports an error without disabling microphone controls.

## Audio boundary

`brodi.microphone.MicrophoneControl` owns the state for both the keyboard shortcut and tray. `GatedAudioInput` drains and drops input while muted, before the common LiveKit input reaches streaming transcription or the realtime backend. This explicit filter is necessary because the installed TCP console input does not implement a detach hook.

Every transition clears the current user turn. The voice agent rejects turns while muted and rejects a late turn whose speech-start timestamp precedes the most recent resume. This also blocks wake words and confirmation answers. Muting does not stop an action that was accepted earlier, mute speakers, change system microphone settings, or affect another app's microphone.

The native helper in `brodi.hotkey` registers exactly one shortcut using Windows `RegisterHotKey` with `MOD_NOREPEAT`, or macOS Carbon `RegisterEventHotKey`. Its separate process keeps the native event loop on its main thread. Closing its stdin lifetime pipe terminates it and releases registration; helper failure mutes the voice session. A conflict or unavailable desktop session is reported explicitly, with the tray as a fallback.

The tray uses `brodi.mic_control.request_mute(pid, muted)`. Its mailbox is scoped to the activity directory, PID and fresh session nonce. Requests specify a boolean state; a stale request from a previous process cannot toggle the new session. `read_state(pid)` and activity details expose `mic_muted`, `mic_available` and `hotkey_available`. A request is acknowledged by published state, not by merely writing the request file.

## Validation

```bash
uv run pytest tests/test_microphone.py tests/test_mic_hotkey.py tests/test_voice_session.py tests/test_tray.py -q
```

The offline end-to-end tests run a real LiveKit session and Brodi's real routing/execution path against scripted transcription and an intercepted OS action. They cover both macOS and Windows command paths, input dropped before the transcription provider, resume, tray IPC, stale transcripts, pending confirmations, rapid toggles, failed cues, helper failure, and cleanup. These tests do not contact a model or operate user apps.

An opt-in host-desktop test registers the real native shortcut, sends synthetic Control+Space, plays all three cues (startup, mute, resume), verifies held-key behavior, then checks re-registration after cleanup. It does not record microphone audio. Run it in a logged-in desktop session; macOS synthetic keyboard testing needs Accessibility access:

```bash
BRODI_NATIVE_MIC_TESTS=1 uv run pytest tests/test_mic_hotkey.py::test_native_shortcut_chimes_and_cleanup -q
```

To press the keys yourself instead of granting synthetic keyboard access, also set `BRODI_NATIVE_MIC_EXTERNAL_KEYS=1` and add `-s`. The test prints when to mute and resume, allowing 45 seconds for each press.

Windows PowerShell:

```powershell
$env:BRODI_NATIVE_MIC_TESTS = '1'
uv run pytest tests/test_mic_hotkey.py::test_native_shortcut_chimes_and_cleanup -q
Remove-Item Env:BRODI_NATIVE_MIC_TESTS
```

For a physical demo check, start Brodi, issue a harmless command, mute, speak another command or play a demo recording, and confirm that there is no new transcript/action. Unmute and speak the command again. Check the tray hint, cues and other apps' microphone use throughout. Physical speaker/microphone acoustics and Windows-native behavior require testing on those devices.

### Validation recorded on November 1, 2026

- Full offline suite: **322 passed, 7 optional tests skipped**. Lint passed; all microphone changes passed formatting checks.
- End-to-end cases used the installed LiveKit session and actual Brodi routing for both platform paths. Muted frames never reached the scripted transcription provider; fresh commands executed after resume.
- Native macOS Control+Space registration and release succeeded. A running local Brodi voice session acknowledged mute and resume through the shared tray API, kept its shortcut registered, and played both cues without audio errors. Its original listening state was restored.
- Automated physical key events were blocked by macOS Accessibility; the alternate desktop-control attempt timed out. The physical keypress test therefore did **not** pass. A subsequent separate native test could not register because the running Brodi session already owned the shortcut, so that session was tested through its existing controls instead.
- No Windows host was available. Windows native registration/message handling is covered by offline contract tests; native Windows keyboard and speaker testing remains outstanding. No live cloud transcription accuracy or physical acoustic feedback claim is made by these checks.

Native API references: [Windows RegisterHotKey](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey); macOS declarations and lifecycle contracts are in the installed SDK's `CarbonEvents.h` and `CarbonEventsCore.h`.
