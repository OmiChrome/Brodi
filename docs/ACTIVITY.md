# Live activity

Brodi's activity view shows what it is doing without mixing in provider debug logs.
It follows new events as they happen and retains recent inputs and results.

## Commands

| Command | Behavior |
| --- | --- |
| `brodi start` | Start the background assistant and attach the live activity view. |
| `brodi status` | Attach to the running assistant's live activity. |
| `brodi start --once` | Start without following; print an initial snapshot. |
| `brodi status --once` | Print recent activity for the running process and exit. |
| `brodi log` | Show the latest recorded activity session, including after stopping. |
| `brodi debug` | Show technical logs for troubleshooting. |
| `brodi stop` | Stop the assistant. |

In an interactive terminal, type **Q + Enter** to leave the live view. **Ctrl+C** also
works, although Windows batch shells can ask whether to terminate the batch job.
Neither action is the stop command for the background assistant.

The macOS wrapper supports the same start/status/log/debug behaviors with
`./console.sh`. It does not gain the Windows-only restart or doctor wrapper commands.

For direct Python access:

```powershell
python -m brodi.activity
python -m brodi.activity --once
python -m brodi.activity --tail 30
python -m brodi.activity --path .freebuff/activity.jsonl --once
```

`--pid` restricts the view to a process or its directly recorded child. This handles
the virtual-environment Python launcher as well as the actual assistant process.
`--since` accepts a Unix timestamp. A process-bound live view exits when that process
stops; an unbound direct Python view continues waiting for events.

## What appears

| Row | Meaning |
| --- | --- |
| `STATE` | Starting, connecting audio, listening, understanding, speaking, or waiting for an answer/confirmation. |
| `YOU` | A command accepted by the voice gate and passed to the core. |
| `DOING` | An action being attempted, such as opening Chrome, searching YouTube, or clicking a page control. |
| `GATHERED` | Tool output, browser title/URL/visible text, or text entered into a field. |
| `BRODI` | The reply produced by Brodi, including conversational replies and startup greeting. |
| `RESULT` | Successful task completion with elapsed time. |
| `PROGRESS` | Longer-task updates and changed-page recovery. |
| `ERROR` | A blocked/failed action or a voice/browser connection issue. |

An action row means work is being attempted. A reply row is the generated response;
it does not independently verify that the speaker played the audio. A completion row
is emitted only when the core reports `Outcome.executed=True`. Questions do not create
completion rows. Partial Keep fallback results still explain that the note was not shown.

Browser observations are previews of the visible text the agent collected, not a
complete page export. Long details wrap and are shortened in the terminal. The journal
holds up to 4,000 characters per string field. Unaccepted background transcripts are
excluded from this feed; technical provider logs can still contain them.

## Storage and implementation

The default journal is `.freebuff/activity.jsonl` inside this checkout. Set
`BRODI_ACTIVITY_LOG` to override it. It is separate from `BRODI_LOG`, which controls the
technical launcher log. The default activity directory is ignored by Git.

[`brodi/activity.py`](../brodi/activity.py) writes JSON Lines events with time, session,
process and parent process IDs, source, event kind, message, and details. A thread lock
keeps events written by voice and browser work complete. Journal write failures are
logged without failing the user's task.

The reader preserves incomplete trailing records until the next poll, skips malformed
records, detects truncation/replacement, and limits the initial history read. It polls
every 200 milliseconds. Consecutive identical status updates are coalesced. The viewer
uses labeled rows and terminal colors when available; `NO_COLOR=1` disables colors.
The journal currently appends without automatic rotation.

The voice entrypoint injects one journal into the core. Core events cover accepted
input, action execution, returned results, and pending questions. Browser callbacks add
page observations and individual actions. The default local wake runtime adds wake
detections/counters, listening, replying/speaking, conversational replies, and audio
errors. Legacy LiveKit events provide equivalent voice-session state. Library users can pass an `ActivityLog`
to `DynamicMacAgent(activity=...)`; without one, the core does not write an activity feed.

Known environment credentials, API-key-shaped strings, sensitive detail keys, and
terminal control sequences are filtered. This is best-effort redaction: accepted
commands and page content may still contain personal information. The feed stays local;
it is not automatically uploaded or published. An explicitly configured external
path is not added to Git's ignore rules automatically.

## Verification

`tests/test_activity.py` covers event ordering, filtering, thread writes, malformed and
partial records, readable rendering, session/PID selection, and a real cross-process
live stream. Core tests cover action/input/output events and missing-query states.
Browser tests cover observations and action callbacks. Chrome integration tests still
exercise the actual browser input path separately.
