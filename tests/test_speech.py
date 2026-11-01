import asyncio
from types import SimpleNamespace

import pytest

from brodi.profiler import Profile
from brodi.speech import SpeechAudio, SpeechPlayer, SpeechSettings, play_pcm, speech_speed, trim_silence


@pytest.fixture(autouse=True)
def speech_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("BRODI_DATA_DIR", str(tmp_path))
    for key in ("BRODI_TTS_SPEED", "BRODI_TTS_MODEL", "BRODI_VOICE", "BRODI_TTS_INSTRUCTIONS"):
        monkeypatch.delenv(key, raising=False)


def client_for(response):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return response()

    return SimpleNamespace(
        audio=SimpleNamespace(speech=SimpleNamespace(with_streaming_response=SimpleNamespace(create=create)))
    ), calls


class Response:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_bytes(self, chunk_size):
        yield bytes(1024)
        yield bytes(512)


def test_speech_defaults_use_natural_male_delivery_and_server_side_speed():
    client, calls = client_for(Response)
    assert len(asyncio.run(SpeechAudio().pcm(client, "A new reply"))) == 1536
    assert calls[0]["voice"] == "onyx"
    assert calls[0]["speed"] == 1.25
    assert calls[0]["response_format"] == "pcm"
    assert "natural, warm male voice" in calls[0]["instructions"]


@pytest.mark.parametrize("speed", ["nan", "inf", "0", "4.1", "invalid"])
def test_invalid_speed_is_rejected(speed, monkeypatch):
    monkeypatch.setenv("BRODI_TTS_SPEED", speed)
    with pytest.raises(ValueError):
        speech_speed()


def test_legacy_tts_model_omits_unsupported_instructions(monkeypatch):
    monkeypatch.setenv("BRODI_TTS_MODEL", "tts-1")
    assert "instructions" not in SpeechSettings.current().request("Hi")


def test_acknowledgement_cache_survives_restart_and_never_saves_arbitrary_text(tmp_path):
    client, calls = client_for(Response)

    async def scenario():
        audio = SpeechAudio()
        await audio.warm(client)
        assert len(calls) == 3
        restarted = SpeechAudio()
        assert await restarted.pcm(client, "Got it.") == bytes(1536)
        assert await restarted.pcm(client, "Gotcha.") == bytes(1536)
        assert len(calls) == 3
        await restarted.pcm(client, "my private note")
        await restarted.pcm(client, "my private note")

    asyncio.run(scenario())
    assert len(calls) == 5
    assert len(list((tmp_path / "speech").glob("*.pcm"))) == 3


@pytest.mark.parametrize(
    "setting,value",
    [
        ("BRODI_TTS_SPEED", "1.5"),
        ("BRODI_VOICE", "echo"),
        ("BRODI_TTS_MODEL", "tts-1"),
        ("BRODI_TTS_INSTRUCTIONS", "Speak gently."),
    ],
)
def test_cached_audio_separates_model_voice_speed_and_delivery(monkeypatch, setting, value):
    client, calls = client_for(Response)

    async def scenario():
        audio = SpeechAudio()
        await audio.pcm(client, "Got it.")
        monkeypatch.setenv(setting, value)
        await audio.pcm(client, "Got it.")

    asyncio.run(scenario())
    assert len(calls) == 2


def test_stream_reassembles_odd_network_chunks_into_complete_pcm_samples():
    class OddResponse(Response):
        async def iter_bytes(self, chunk_size):
            yield b"a"
            yield b"bc"
            yield b"def"

    client, _ = client_for(OddResponse)

    async def scenario():
        chunks = [c async for c in SpeechAudio().stream(client, "A new reply")]
        assert all(len(c) % 2 == 0 for c in chunks)
        assert b"".join(chunks) == b"abcdef"

    asyncio.run(scenario())


def test_truncated_speech_is_not_cached(tmp_path):
    class Truncated(Response):
        async def iter_bytes(self, chunk_size):
            yield b"abc"

    client, calls = client_for(Truncated)

    async def scenario():
        audio = SpeechAudio()
        for _ in range(2):
            with pytest.raises(ValueError, match="incomplete PCM"):
                await audio.pcm(client, "Got it.")

    asyncio.run(scenario())
    assert len(calls) == 2
    assert not list(tmp_path.rglob("*.pcm"))


def test_streaming_playback_starts_before_the_rest_of_the_audio_arrives():
    async def scenario():
        first_played = asyncio.Event()
        release = asyncio.Event()
        loop = asyncio.get_running_loop()
        events = []

        async def chunks():
            yield bytes(1024)
            await release.wait()
            yield bytes(512)

        class Output:
            def __init__(self, **kwargs):
                assert kwargs == {"samplerate": 24000, "channels": 1, "dtype": "int16", "latency": "low"}

            def start(self):
                events.append("start")

            def write(self, chunk):
                events.append(len(chunk))
                loop.call_soon_threadsafe(first_played.set)

            def stop(self):
                events.append("drain")

            def abort(self):
                events.append("abort")

            def close(self):
                events.append("close")

        task = asyncio.create_task(
            play_pcm(
                chunks(),
                SimpleNamespace(RawOutputStream=Output),
                on_start=lambda: events.append("suspend"),
                on_finish=lambda: events.append("resume"),
            )
        )
        await asyncio.wait_for(first_played.wait(), 2)
        assert not task.done()
        assert events[:3] == ["suspend", "start", 1024]
        release.set()
        await task
        assert events == ["suspend", "start", 1024, 512, "drain", "close", "resume"]

    asyncio.run(scenario())


def test_interrupt_aborts_playback_closes_download_and_never_caches_partial_audio(tmp_path):
    async def scenario():
        playing = asyncio.Event()
        loop = asyncio.get_running_loop()
        events = []

        class SlowResponse(Response):
            async def __aexit__(self, *args):
                events.append("response_closed")

            async def iter_bytes(self, chunk_size):
                yield bytes(1024)
                await asyncio.Event().wait()

        class Output:
            def __init__(self, **kwargs):
                pass

            def start(self):
                pass

            def write(self, chunk):
                loop.call_soon_threadsafe(playing.set)

            def stop(self):
                events.append("drain")

            def abort(self):
                events.append("abort")

            def close(self):
                events.append("close")

        client, _ = client_for(SlowResponse)
        audio = SpeechAudio()
        profile = Profile()
        with profile.capture():
            task = asyncio.create_task(
                play_pcm(
                    audio.stream(client, "Got it."),
                    SimpleNamespace(RawOutputStream=Output),
                    on_start=lambda: events.append("suspend"),
                    on_finish=lambda: events.append("resume"),
                )
            )
            await asyncio.wait_for(playing.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert events == ["suspend", "response_closed", "abort", "close", "resume"]
        assert not audio._cache
        assert not list(tmp_path.rglob("*.pcm"))

    asyncio.run(scenario())


def test_trim_service_padding_preserves_internal_pauses_and_quiet_speech():
    silence = bytes(480)
    quiet = b"\x96\x00" * 240  # 150 amplitude, intentionally quiet but audible.
    raw = silence * 10 + quiet * 10 + silence * 20 + quiet * 10 + silence * 100

    async def chunks():
        for offset in range(0, len(raw), 1024):
            yield raw[offset : offset + 1024]

    async def scenario():
        actual = b"".join([chunk async for chunk in trim_silence(chunks())])
        assert actual == silence * 2 + quiet * 10 + silence * 20 + quiet * 10 + silence * 4

    asyncio.run(scenario())


def test_speaker_is_opened_once_and_reused_across_replies():
    events = []

    class Output:
        def __init__(self, **kwargs):
            events.append("opened")

        def start(self):
            events.append("start")

        def write(self, chunk):
            events.append("write")

        def stop(self):
            events.append("drain")

        def abort(self):
            events.append("abort")

        def close(self):
            events.append("close")

    async def chunks():
        yield b"\x10\x27" * 240

    async def scenario():
        player = SpeechPlayer(SimpleNamespace(RawOutputStream=Output))
        await player.prepare()
        assert events == ["opened"]
        for _ in range(2):
            await player.play(chunks(), on_start=lambda: None, on_finish=lambda: None)
        await player.aclose()

    asyncio.run(scenario())
    assert events == ["opened", "start", "write", "drain", "start", "write", "drain", "close"]
