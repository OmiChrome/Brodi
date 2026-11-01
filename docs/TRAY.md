# macOS menu-bar and Windows tray controls

Brodi's desktop controls work on **macOS and Windows**. After `uv sync`, a background
start also opens the controls. You can open them separately without starting the microphone:

```sh
# macOS
./brody tray
./brody start
```

```powershell
# Windows
.\brodi.cmd tray
.\brodi.cmd start
```

On macOS, click the microphone in the menu bar. Its native template icon adapts to
light and dark appearance; a slash indicates muted or stopped. On Windows,
right-click the Brodi logo in the notification area. Windows may initially place it
under hidden icons. A badge shows green for listening, amber for muted, blue while
starting, gray when stopped, and red when attention is needed. Muted and stopped
badges also carry a dash. The menu always names the state in text.

| Control | Behavior |
| --- | --- |
| Mute / Unmute microphone | Changes only Brodi's microphone input, using the same control and chimes as Control+Space. |
| Start assistant | Starts a background voice session after checking configuration. |
| Stop assistant | Stops this checkout's voice processes and helper workers; keeps tray controls available. |
| Restart assistant | Starts a fresh voice session. Pending approvals do not survive restart. |
| Show activity | Opens the readable activity feed in Terminal on macOS or a console on Windows. Shows recent history when stopped. Also available by activating the Windows icon. |
| Open log | Opens the technical voice log in TextEdit on macOS or Notepad on Windows. |
| Quit Brodi | Stops the assistant and removes its desktop icon. |

The first menu row and tooltip show **Listening**, **Mic muted**, **Starting**, or
**Stopped**. Assistant activity appears separately, so opening an app or completing
a task cannot hide the microphone state. Microphone controls remain disabled until
the voice session is ready. A mute request is reflected only after the voice process
acknowledges it. If Control+Space is unavailable, the menu explains that and keeps the
microphone control available as a fallback. The shortcut is **Control**, not Command,
on macOS; Input Sources or another app may already use it.

Actions show progress and cannot overlap. Errors remain in the menu even if the OS
cannot display notifications. Closing the activity window leaves Brodi running.
Stop and Quit interrupt current assistant work; already issued OS actions cannot be
rolled back. The tray does not approve actions or send model requests.

## Configuration and lifecycle

`BRODI_TRAY=true` is the default for the macOS and Windows background launchers.
Set it to `false` in the environment, `.env.local`, or `brodi.yaml` to disable automatic
tray startup. Explicit `./brody tray` and `brodi tray` still open the controls.
Foreground `python agent.py console` does not automatically open a tray.
`console.sh` shares the macOS background controller and keeps its terminal activity view.

Each checkout gets one desktop icon. macOS uses a file lock and Windows uses a
session-local mutex; both release automatically when the tray exits. Process matching
requires the checkout and exact entrypoint. Microphone requests use the voice session's
local mailbox. macOS launches the detached voice console through a pseudo-terminal;
Windows launches it without a console window. Cocoa UI updates run on the main thread.

The original [Brodi artwork](../assets/branding/brodi-logo.png) and multi-size
[Windows icon](../assets/branding/brodi.ico) are retained. `pystray` and Pillow are
installed on both supported platforms. The tray itself does not register login startup.
If Hey bro login startup is enabled separately, its background supervisor opens the
tray on either platform and tray Stop/Quit also stops that supervisor.

Voice logs use `BRODI_LOG`, or `brody-<user-id>.log` on macOS and `brodi-console.log`
on Windows in the system temporary directory.
Tray errors use `tray.log` inside `~/Library/Application Support/Brodi` on macOS or
`%LOCALAPPDATA%/Brodi` on Windows, overridable with `BRODI_DATA_DIR`.

## Verification

Run `uv run pytest tests/test_tray.py tests/test_console_service.py -q` for platform
launch commands, process ownership, helper cleanup, state and menu behavior, microphone
mailbox integration, duplicate actions, notification failure, and macOS instance locks.
The tray follows the [pystray menu/lifecycle API](https://pystray.readthedocs.io/en/latest/usage.html).
Native Windows UI smoke testing requires a Windows desktop.
