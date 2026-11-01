# Brodi: hackathon pitch and judge demo

## The one-line pitch

**Your computer has menus. Brodi gives it a conversation: say the task, see it happen.**

## The 30-second pitch

“We built Brodi for the small computer tasks that interrupt your day. Open the app,
say ‘open Chrome,’ ‘search YouTube,’ or ‘type this into Notepad,’ and Brodi handles it.
OpenAI transcribes your voice; its Decisions API helps choose the right action.
Common tasks take a direct path, repeated tasks become shortcuts, and longer requests
get a bounded workflow. The result is a desktop companion that says less and does more,
with a visible activity feed and confirmation before sensitive changes.”

## The strongest points to present

| Point | Why it matters | What to show |
| --- | --- | --- |
| **Voice becomes action** | A request produces a desktop result you can inspect. | Speak a command and show the actual app or entered text. |
| **Purposeful OpenAI integration** | Speech recognition, action decisions, and structured generation have separate jobs. | Explain the STT → Decisions → execution → TTS flow. |
| **Small tasks stay small** | Familiar commands avoid unnecessary generation and planning. | Show the direct path in activity/timing evidence. |
| **Repetition gets easier** | A successful eligible command can become a local shortcut. | Repeat it and show a memory hit with no model-routing call. Audio still uses OpenAI STT. |
| **Automation is visible** | People can see input, current work, observations, and output. | Keep the activity view next to the app during the demo. |
| **People stay in control** | Sensitive actions require approval; saved plans never reuse an old approval. | Demonstrate a harmless confirmation and cancellation, or show the relevant passing test. |
| **A finished interaction** | Tray controls and short replies make the prototype easy to operate. | Start from the tray, perform a task, then stop the assistant. |

This is the technical distinction to explain clearly: **Brodi separates recognizing
speech, choosing an action, and executing it.** The
[Decisions API](https://developers.openai.com/api/docs/guides/decisions) answers bounded
questions; [Responses with Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
handles proposals and plans. Typed results are checked before reaching local executors.

## A two-minute live demo

For a faster demonstration, use the [60-second ready-made command sequence](DEMO.md).
Its 51-phrase menu includes “close Notepad,” window controls, Calculator, folders, and
searches. The latest 32-turn live run measured 2.5–105.8 ms **after transcription**, with
zero routing-model calls; separate native state checks verified minimize/maximize/close.
Show these numbers as dispatch measurements, alongside the real app result.

1. **Start from the tray.** Show “Listening with OpenAI.” Say “open Chrome” directly,
   establishing that the user can speak immediately after starting the app.
2. **Show a useful task.** Say “search YouTube for ambient piano.” Show the actual results.
3. **Show native control.** Open one clean Notepad window. Say “type hello world into
   Notepad.” Show the text and verification. Close other Notepad test windows beforehand
   so the target is unambiguous.
4. **Show a short workflow.** Say “open Chrome and go to github.com.” Keep the activity
   view visible so the two steps are understandable.
5. **Show learning through reuse.** Repeat that request. Point out the saved shortcut;
   do not imply this eliminates cloud transcription or all task latency.
6. **Finish with control.** Stop the assistant from the tray. Explain that Start/Stop
   controls the active voice session.

Use a signed-in Keep task only after confirming Chrome CDP access and the correct
account before the presentation. Browser permissions are an environmental prerequisite,
not something to discover on stage. Keep demo content free of private notes and credentials.

## Real-world impact to discuss

- **Less interruption:** everyday app switching and navigation can begin with one spoken request.
- **Hands-free workflows:** useful when typing is inconvenient or hands are occupied.
  This is an opportunity to evaluate with users, not a claim of certified accessibility.
- **Lower interaction burden:** simple phrasing, short replies, and visible progress reduce
  the amount of instruction a person must give.
- **Reusable routines:** local shortcuts can reduce repeated model interpretation for common tasks.

Describe these as intended benefits. User studies and measured time savings are follow-up
work unless you have actually run and documented them.

## Evidence that makes the pitch credible

| Claim | Evidence to bring |
| --- | --- |
| Commands work | Live results in Chrome/Notepad, plus the relevant workflow tests. |
| Repeated tasks bypass model routing | The memory-hit flag and model-call count before/after repetition. |
| Plans are bounded | Tests for retry limits, timeouts, and confirmation continuation. |
| The design is responsive | Timing by stage: transcription, routing, execution, and speech playback. |
| Security is part of the product | Approval/cancellation examples and policy tests, not just a slide. |

Existing Windows smoke runs recorded launch/shortcut dispatch in tens of milliseconds
after transcription; Notepad typing/readback took seconds. These are component timings,
not end-to-end voice benchmarks. The earlier offline-listener probes do not measure the
new OpenAI voice default. See [implementation evidence](IMPLEMENTATION.md).

The OpenAI-only update was also checked end to end with a prerecorded command:
`gpt-4o-transcribe` produced “Hey bro. Open Chrome.”, the core executed the Chrome
launch, and the repeat used a memory shortcut with zero routing-model calls. A real
hidden Windows console startup reached “Listening with OpenAI.” This validates the
provider-to-action integration, not an accent/noise accuracy benchmark.

Do not claim “100% accurate,” “works in every app,” “fully offline,” or “all voice tasks
finish under 500 ms.” Native macOS execution remains a separate validation requirement
from Windows live results. Report the actual current test result, not a hard-coded count.

## Likely judge questions

**Why Decisions instead of a long prompt for every command?**
Most desktop requests are small choices with known arguments. Brodi resolves familiar
ones directly and uses bounded Decisions questions when interpretation is needed.
Generation is reserved for tasks that benefit from it.

**Why not use screenshots for everything?**
App launches, scripts, browser DOM controls, and UIA provide direct execution paths.
Brodi's scope is everyday desktop tasks with bounded workflows.

**What happens when it fails?**
It reports missing details, requests approval where needed, retries safe failed steps
within a budget, and stops when it cannot verify or complete the task.

**What is next?**
Evaluate recognition and task completion across accents and noisy rooms; measure complete
voice latency; test with people who prefer hands-free interaction; expand native app
coverage; and validate macOS on physical hardware.

## Close with this

**“Brodi turns ‘I need to do this’ into ‘got it.’ We made the everyday computer task
feel like asking a capable friend.”**

The current streaming adapter was checked with `gpt-live-transcribe`: it recognized
?Hey Bro, open Chrome and go to github dot com.? A simple RMS/silence calculation
marks audio boundaries; it is not a local model and performs no speech recognition.
