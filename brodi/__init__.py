"""brodi: voice-controlled macOS and Windows 11 agent.

OpenAI STT/TTS (or the Realtime API) -> OpenAI Decisions API routes each utterance to a
tool and its arguments in one request -> AppleScript on macOS or PowerShell on Windows 11
executes it. Unknown requests fall back to the Responses API, which writes a new tool that
is compiled, policy-checked and cached so the next request is fast. Multi-step website work
runs through a self-contained CDP loop in the user's Chrome, one Decisions request per step.
"""

__all__ = ["DynamicMacAgent"]


def __getattr__(name: str):
    # Let command-line entrypoints load configuration before model/profile modules.
    if name == "DynamicMacAgent":
        from .agent import DynamicMacAgent

        return DynamicMacAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
