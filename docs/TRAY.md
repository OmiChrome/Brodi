# Windows tray controls and branding

The official Brodi artwork is [brodi-logo.png](../assets/branding/brodi-logo.png).
The README uses the original image; [brodi.ico](../assets/branding/brodi.ico) contains
16, 24, 32, 48, 64, 128, and 256 pixel sizes for Windows.

After `uv sync`, starting Brodi through `brodi start` also launches its tray icon.
`brodi tray` opens the controls without starting the microphone. Windows may place the
icon in the notification area's hidden-icons menu; drag it onto the taskbar if desired.
Right-click the logo for:

| Control | Behavior |
| --- | --- |
| Start assistant | Start the existing voice console hidden, after checking the key and local voice asset. |
| Stop assistant | Stop this checkout's voice processes and outstanding UIA workers; leave user apps open. |
| Restart assistant | Stop, then launch a fresh voice session. Pending approvals do not survive restart. |
| Show activity | Open the existing readable activity feed in a console. Also available by double-clicking the icon. |
| Open log | Open the technical voice log in Notepad. |
| Quit Brodi | Stop the assistant and remove the tray icon. |

The tooltip and first menu row show current activity, including listening, execution,
confirmation waits, and stopped state. Stopping voice leaves the icon available for
starting it again. Closing an activity window leaves the assistant running.
Stop/Quit interrupt current assistant work; issued OS actions cannot be rolled back.
The tray does not change policy, approve actions, clear memory, or send model requests.

`BRODI_TRAY=true` is the default Windows launcher behavior. Set it to `false` in the
environment, `.env.local`, or `brodi.yaml` to disable automatic tray startup.
An explicit `brodi tray` still opens it. Foreground `python agent.py console` and macOS
launchers retain their existing behavior and do not automatically start this Windows UI.

`brodi/tray.py` owns the pystray UI and an `AgentController` for the same checkout.
Process matching checks the exact entrypoint and repository path. The controller never
uses a shell to launch voice commands. A Windows session-local mutex allows one tray
instance per checkout. Menu actions run in a worker thread with a lock, so start/stop
cannot overlap and slow process shutdown does not freeze the menu. Voice execution
remains in the separate console process.

The icon uses Windows-only `pystray` and Pillow dependencies. No registry changes,
startup-at-login registration, or system service installation are performed.
Voice logs use `BRODI_LOG` or `%TEMP%/brodi-console.log`; tray errors use
`%LOCALAPPDATA%/Brodi/tray.log` (or `BRODI_DATA_DIR`).

Run `uv run pytest tests/test_tray.py -q` for lifecycle ownership, safe stop, launch,
status, menu-state, duplicate-action, and error-reporting checks. The general suite
continues to exercise routing, confirmations, memory, wake, OS and browser paths.
The tray implementation follows the [pystray menu/lifecycle API](https://pystray.readthedocs.io/en/latest/usage.html).

A live Windows smoke check verified icon registration using `Shell_NotifyIconGetRect`,
then started the voice listener, restarted it to a new PID, stopped it, and quit the
tray. The named mutex rejected a second instance. These checks do not establish physical
macOS tray support; this feature is Windows-only.
