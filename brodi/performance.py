"""Repeatable profiler workloads: real local catalog/routing and opt-in paid decisions."""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import json
from pathlib import Path

from .config import load_environment
from .profiler import Profile, span


async def benchmark(
    iterations: int, decisions: bool, speech: bool = False, *, profile: Profile | None = None
) -> tuple[dict, Profile]:
    from .applescript import snapshot_context
    from .decisions import ChoiceQuestion, DecisionsClient
    from .registry import ToolRegistry
    from .router import JevRouter

    profile = profile or Profile("performance")
    with profile.capture():
        with span("context.snapshot"):
            ctx = await snapshot_context()
        with span("registry.load"):
            registry = ToolRegistry()
        router = JevRouter(registry)
        try:
            for _ in range(iterations):
                registry.available(ctx)
                await router.route("open notepad", ctx)
            if decisions:
                for _ in range(2):
                    with span("route.polite_request"):
                        routed = await router.route("Could you open Notepad", ctx)
                        if not routed.tool or routed.tool.name != "open_app":
                            raise RuntimeError("Routing probe selected the wrong action")
                # Identical bounded calls expose endpoint fallback/retry overhead.
                client = DecisionsClient()
                try:
                    for _ in range(2):
                        response = await client.evaluate(
                            state="The user asks to mute audio.",
                            questions={
                                "intent": ChoiceQuestion(
                                    "Select the requested action.", {"mute": "Mute audio", "open": "Open an app"}
                                )
                            },
                        )
                        if response.choices["intent"].choice != "mute":
                            raise RuntimeError("Decision probe selected the wrong action")
                finally:
                    await client.aclose()
            if speech:
                import openai

                from .speech import SpeechAudio

                audio = SpeechAudio()
                async with openai.AsyncOpenAI(timeout=15, max_retries=0) as client:
                    for _ in range(2):
                        with span("speech.synthesize"):
                            pcm = await audio.pcm(client, "Got it.")
                            if not pcm:
                                raise RuntimeError("Speech probe returned no audio")
        finally:
            await router.aclose()
    return {
        "ok": True,
        "iterations": iterations,
        "live_decisions": decisions,
        "live_speech": speech,
        "measurement": "Real OS context/catalog and route-only local dispatch. Optional decisions measure polite routing twice and two small decisions; optional speech downloads one acknowledgement then reuses it. Paid APIs, no UI actions or playback.",
    }, profile


def main() -> None:
    load_environment()
    from .demo import save_report

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--decisions", action="store_true", help="also measure real paid routing and model decisions")
    parser.add_argument(
        "--speech", action="store_true", help="measure paid acknowledgement synthesis and local reuse, without playback"
    )
    parser.add_argument("--output", type=Path, default=Path(".freebuff/profiles/benchmark"))
    ns = parser.parse_args()
    if not 1 <= ns.iterations <= 10000:
        parser.error("--iterations must be between 1 and 10000")
    ns.output.mkdir(parents=True, exist_ok=True)
    cpu = cProfile.Profile()
    profile = Profile("performance")
    with cpu:
        try:
            result, profile = asyncio.run(benchmark(ns.iterations, ns.decisions, ns.speech, profile=profile))
        except Exception as exc:
            # A failing endpoint is useful profiling evidence too. Keep errors
            # to their type: SDK exception strings can include request contents.
            result = {"ok": False, "error": type(exc).__name__}
    cpu.dump_stats(str(ns.output / "cpu.pstats"))
    save_report(ns.output, result, profile)
    print(json.dumps(profile.report()["stages"], indent=2))
    print(f"Report: {ns.output / 'report.json'}")
    if not result["ok"]:
        print(f"Benchmark failed: {result['error']}; see the saved trace.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
