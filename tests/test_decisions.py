"""Tests for brodi.decisions: question serialization, answer parsing, fallback."""

import asyncio
from types import SimpleNamespace

from brodi.decisions import (
    DECISIONS_MODEL,
    ChoiceQuestion,
    DecisionsClient,
    PredicateQuestion,
)


def test_choice_question_serializes_to_decisions_schema():
    q = ChoiceQuestion(
        instructions=["Pick the tool.", "Prefer the frontmost app."],
        criteria={"open_app": "Open an application", "chat": None},
    )
    payload = q.to_payload("intent")
    assert payload == {
        "type": "choice",
        "name": "intent",
        "instructions": "Pick the tool.\nPrefer the frontmost app.",
        "choices": [
            {"value": "open_app", "description": "Open an application"},
            {"value": "chat", "description": ""},
        ],
    }


def test_choice_question_jsonifies_structured_descriptions():
    payload = ChoiceQuestion(instructions="q", criteria={"t": {"what": "do it"}}).to_payload("intent")
    assert payload["choices"][0]["description"] == '{"what": "do it"}'


def test_predicate_question_serializes_criteria_suffix():
    q = PredicateQuestion(instructions="Did it work?", criteria={"true": "yes", "false": "no"})
    assert q.to_payload("works") == {
        "type": "predicate",
        "name": "works",
        "instructions": "Did it work? (True: yes; False: no)",
    }
    assert PredicateQuestion(instructions="plain").to_payload("p")["instructions"] == "plain"


class _FakeDecisionsEndpoint:
    def __init__(self, answers):
        self.answers = answers
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(answers=self.answers)


class _FakeAsyncOpenAI:
    def __init__(self, answers):
        self.decisions = _FakeDecisionsEndpoint(answers)
        self.closed = False

    async def close(self):
        self.closed = True


def _dict_answers():
    return {
        "intent": {"choice": "browse", "confidence": 0.82, "probabilities": {"browse": 0.82, "chat": 0.18}},
        "works": {"probability": 0.91},
    }


def test_evaluate_parses_dict_answers_and_sends_gpt_6_luna():
    fake = _FakeAsyncOpenAI(_dict_answers())
    client = DecisionsClient(client=fake)
    questions = {
        "intent": ChoiceQuestion(instructions="pick", criteria={"browse": "go", "chat": "talk"}),
        "works": PredicateQuestion(instructions="worked?"),
    }
    resp = asyncio.run(client.evaluate(state={"utterance": "open it"}, questions=questions))

    assert DECISIONS_MODEL == "gpt-6-luna"
    assert fake.decisions.kwargs["model"] == "gpt-6-luna"
    # The real endpoint only accepts a text string or an ordered message list.
    assert isinstance(fake.decisions.kwargs["input"], str)
    assert "open it" in fake.decisions.kwargs["input"]
    assert fake.decisions.kwargs["questions"][0]["type"] == "choice"
    assert resp.choices["intent"].choice == "browse"
    assert resp.choices["intent"].confidence == 0.82
    assert resp.choices["intent"].probabilities["chat"] == 0.18
    assert resp.predicates["works"].probability == 0.91
    assert resp.nouls["works"].noul == 0.91


def test_evaluate_parses_list_of_answer_objects():
    answers = [
        {"name": "intent", "choice": "chat", "confidence": 0.7},
        {"name": "works", "probability": 0.2},
    ]
    client = DecisionsClient(client=_FakeAsyncOpenAI(answers))
    resp = asyncio.run(
        client.evaluate(
            state={},
            questions={
                "intent": ChoiceQuestion(instructions="i", criteria={"chat": None, "stop": None}),
                "works": PredicateQuestion(instructions="w"),
            },
        )
    )
    assert resp.choices["intent"].choice == "chat"
    assert resp.choices["intent"].probabilities["chat"] == 0.7
    assert resp.predicates["works"].probability == 0.2


def test_evaluate_parses_live_answer_shape_and_typed_probabilities():
    """Shape returned by POST /v1/decisions: ordered answers, probabilities as a list."""
    answers = [
        {
            "type": "choice",
            "name": "intent",
            "choice": "set_volume",
            "confidence": 1.0,
            "probabilities": [
                {"value": "set_volume", "probability": 1.0},
                {"value": "open_app", "probability": 0.0},
            ],
        },
        {"type": "predicate", "name": "needs_confirm", "probability": 0.0},
        {"type": "refusal", "name": "works"},
    ]
    resp = asyncio.run(
        DecisionsClient(client=_FakeAsyncOpenAI(answers)).evaluate(
            state={"utterance": "mute the volume"},
            questions={
                "intent": ChoiceQuestion(instructions="pick", criteria={"set_volume": "mute", "open_app": "open"}),
                "needs_confirm": PredicateQuestion(instructions="risky?"),
                "works": PredicateQuestion(instructions="worked?"),
            },
        )
    )
    assert resp.choices["intent"].choice == "set_volume"
    assert resp.choices["intent"].probabilities == {"set_volume": 1.0, "open_app": 0.0}
    assert resp.predicates["needs_confirm"].probability == 0.0
    assert "works" not in resp.predicates  # refusals are not answers


def test_evaluate_maps_unnamed_answers_by_question_order():
    answers = [{"choice": "chat", "confidence": 0.4, "probabilities": []}, {"probability": 0.6}]
    resp = asyncio.run(
        DecisionsClient(client=_FakeAsyncOpenAI(answers)).evaluate(
            state="hi",
            questions={
                "intent": ChoiceQuestion(instructions="i", criteria={"chat": None, "stop": None}),
                "works": PredicateQuestion(instructions="w"),
            },
        )
    )
    assert resp.choices["intent"].choice == "chat"
    assert resp.predicates["works"].probability == 0.6


def test_evaluate_answers_single_choice_question_without_calling_the_api():
    """The endpoint requires 2-255 choices; a lone candidate is answered locally."""
    fake = _FakeAsyncOpenAI([{"type": "predicate", "name": "works", "probability": 1.0}])
    resp = asyncio.run(
        DecisionsClient(client=fake).evaluate(
            state={},
            questions={
                "app": ChoiceQuestion(instructions="which app", criteria={"Slack": None}),
                "works": PredicateQuestion(instructions="w"),
            },
        )
    )
    assert resp.choices["app"].choice == "Slack"
    assert [q["name"] for q in fake.decisions.kwargs["questions"]] == ["works"]


class _FakeResponses:
    def __init__(self, output_parsed):
        self.output_parsed = output_parsed
        self.kwargs = None

    async def parse(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(output_parsed=self.output_parsed)


class _FakeLegacyOpenAI:
    """An OpenAI SDK without the decisions endpoint (older key or SDK)."""

    def __init__(self, output_parsed):
        self.responses = _FakeResponses(output_parsed)

    async def close(self):
        pass


def test_evaluate_falls_back_to_responses_parse_without_decisions():
    fake = _FakeLegacyOpenAI({"intent": "chat", "works": True})
    client = DecisionsClient(client=fake)
    resp = asyncio.run(
        client.evaluate(
            state={"utterance": "hi"},
            questions={
                "intent": ChoiceQuestion(instructions="i", criteria={"chat": "talk", "stop": "quit"}),
                "works": PredicateQuestion(instructions="w"),
            },
        )
    )
    assert fake.responses.kwargs["model"] == "gpt-5.4-nano"
    assert fake.responses.kwargs["reasoning"] == {"effort": "none"}
    assert fake.responses.kwargs["text_format"].__name__ == "DynamicBatchSchema"
    assert resp.choices["intent"].choice == "chat"
    assert resp.choices["intent"].confidence == 1.0
    assert resp.predicates["works"].probability == 1.0


def test_evaluate_falls_back_on_not_found_error():
    class _NotFound(Exception):
        pass

    class _BrokenDecisions:
        async def create(self, **kwargs):
            raise _NotFound("404")

    fake = _FakeLegacyOpenAI({"intent": "chat", "works": False})
    original = fake.decisions if hasattr(fake, "decisions") else None
    fake.decisions = _BrokenDecisions()
    client = DecisionsClient(client=fake, not_found_errors=(_NotFound,))
    resp = asyncio.run(
        client.evaluate(
            state={},
            questions={
                "intent": ChoiceQuestion(instructions="i", criteria={"chat": None}),
                "works": PredicateQuestion(instructions="w"),
            },
        )
    )
    assert resp.choices["intent"].choice == "chat"
    assert resp.predicates["works"].probability == 0.0
    assert original is None
