# Brodi documentation

Brodi is a voice-controlled desktop assistant for short, everyday requests. Its product
personality is a capable bro who gets things done and speaks very little.

| Document | Read it for |
| --- | --- |
| [PERSONAS.md](PERSONAS.md) | Personality, speech rules, response examples, and voice acceptance criteria. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Current modules, execution flows, tools, state, configuration, and testing. |
| [ACTIVITY.md](ACTIVITY.md) | Live startup/status view, accepted inputs, gathered outputs, storage, and debugging. |
| [TRAY.md](TRAY.md) | Official artwork, Windows tray controls, process lifecycle, configuration, and tests. |
| [IMPLEMENTATION.md](IMPLEMENTATION.md) | Offline wake, UIA, SQLite memory, bounded plans, configuration, smoke tests, and measured limits. |
| [Project README](../README.md) | Installation, startup commands, and a quick introduction. |
| [Configuration example](../.env.example) | Environment settings to copy into a local configuration file. |
| [YAML configuration](../config.example.yaml) | Optional lower-priority local `brodi.yaml` settings. |

The architecture describes the checked-in implementation. The persona document defines
the intended product voice and identifies the runtime locations that own spoken copy.
Markdown documents are not loaded as runtime prompts.

For changes to Brodi, start with the relevant module in the architecture map and the
matching test group. Use the persona guide whenever changing something the user hears.
