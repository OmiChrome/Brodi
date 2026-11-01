import pytest

from brodi import voice
from brodi.voice import VoiceGate


@pytest.mark.parametrize("text", ["open Chrome", "random conversation", "okay", "yes", "", "  "])
def test_background_speech_cannot_activate_assistant(text):
    assert not VoiceGate().accepts(text)


@pytest.mark.parametrize("text", ["Hey bro", "Yo bro", "Hey bro, open Chrome", "Hey Brodi, open Notepad"])
def test_wake_phrase_activates(text):
    gate = VoiceGate()
    assert gate.accepts(text)
    assert gate.accepts("open notepad")


def test_conversation_window_expires(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(voice.time, "monotonic", lambda: clock[0])
    gate = VoiceGate(window_s=20)
    assert gate.accepts("Hey bro")
    clock[0] = 119
    assert gate.accepts("open Chrome")
    clock[0] = 121
    assert not gate.accepts("open Chrome")
    assert gate.accepts("Yo bro, open Chrome")


def test_pending_answer_can_follow_long_task():
    assert VoiceGate().accepts("yes", pending=True)


def test_always_active_mode_still_ignores_empty_transcripts():
    gate = VoiceGate(required=False)
    assert gate.accepts("open Chrome")
    assert not gate.accepts("")
