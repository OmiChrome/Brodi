"""Unified OpenAI Decisions API wrapper.

Every judgment the assistant makes is a *choice* or a *predicate* answered by the OpenAI
Decisions API (``gpt-6-luna``), batched into one request. No text is generated for decisions.

If the configured OpenAI SDK/key does not expose ``/v1/decisions`` (older SDK, or a key
without access), :class:`DecisionsClient` transparently falls back to a single
``responses.parse`` call against a dynamic batch schema, so runtime never breaks.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import openai
from pydantic import BaseModel, Field, create_model

from .models import cache_key, model

log = logging.getLogger("brodi.decisions")

DECISIONS_MODEL = os.environ.get("BRODI_DECISIONS_MODEL", "gpt-6-luna")
FAST_FALLBACK_MODEL = model("fast_fallback")

# The endpoint accepts 2-255 choices per question; a single candidate is decided locally.
MIN_CHOICES = 2
MAX_CHOICES = 255


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    confidence: float = 1.0
    probabilities: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class PredicateAnswer:
    probability: float

    @property
    def noul(self) -> float:
        """Backward-compatible alias for the old Noul-style answer."""
        return self.probability


@dataclass
class DecisionResponse:
    choices: dict[str, ChoiceAnswer] = field(default_factory=dict)
    predicates: dict[str, PredicateAnswer] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def nouls(self) -> dict[str, PredicateAnswer]:
        return self.predicates


def _describe(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


@dataclass
class ChoiceQuestion:
    instructions: str | list[str]
    criteria: dict[str, Any]

    def to_payload(self, name: str) -> dict[str, Any]:
        instructions = self.instructions if isinstance(self.instructions, str) else "\n".join(self.instructions)
        return {
            "type": "choice",
            "name": name,
            "instructions": instructions,
            "choices": [{"value": key, "description": _describe(value)} for key, value in self.criteria.items()],
        }


@dataclass
class PredicateQuestion:
    instructions: str
    criteria: dict[str, str] | None = None

    def to_payload(self, name: str) -> dict[str, Any]:
        instructions = self.instructions
        if self.criteria and "true" in self.criteria:
            instructions += f" (True: {self.criteria['true']}; False: {self.criteria.get('false', 'anything else')})"
        return {"type": "predicate", "name": name, "instructions": instructions}


Question = ChoiceQuestion | PredicateQuestion


def _get(raw: Any, key: str, default: Any = None) -> Any:
    if isinstance(raw, dict):
        return raw.get(key, default)
    return getattr(raw, key, default)


def _answer_sequence(raw: Any) -> list[tuple[str | None, Any]]:
    """``decision.answers`` is an ordered list of answers (also tolerate a name-keyed dict)."""
    answers = _get(raw, "answers", raw)
    if isinstance(answers, dict):
        return [(str(name), answer) for name, answer in answers.items()]
    return [(None, item) for item in answers or []]


def _probabilities(answer: Any) -> dict[str, float]:
    """Probabilities arrive as ``[{value, probability}]`` (also tolerate a dict)."""
    raw = _get(answer, "probabilities") or {}
    if isinstance(raw, dict):
        return {str(k): float(v) for k, v in raw.items()}
    out: dict[str, float] = {}
    for item in raw:
        value = _get(item, "value", _get(item, "label"))
        if value is None:
            continue
        out[str(value)] = float(_get(item, "probability", 0.0))
    return out


def _usage(raw: Any) -> dict[str, int]:
    usage = _get(raw, "usage")
    if usage is None:
        return {}
    return {
        "input_tokens": int(_get(usage, "input_tokens", 0) or 0),
        "output_tokens": int(_get(usage, "output_tokens", 0) or 0),
        "total_tokens": int(_get(usage, "total_tokens", 0) or 0),
    }


def _response(raw: Any, questions: dict[str, Question]) -> DecisionResponse:
    order = list(questions)
    choices: dict[str, ChoiceAnswer] = {}
    predicates: dict[str, PredicateAnswer] = {}
    for index, (name, answer) in enumerate(_answer_sequence(raw)):
        if not name:  # unnamed answer: answers come back in question order
            name = order[index] if index < len(order) else None
        if not name or name not in questions or answer is None:
            continue
        if str(_get(answer, "type", "")) == "refusal":
            continue
        question = questions[name]
        if isinstance(question, ChoiceQuestion):
            choice = _get(answer, "choice")
            if choice is None:
                choice = _get(answer, "value", "")
            probabilities = _probabilities(answer)
            confidence = _get(answer, "confidence")
            if confidence is None:
                confidence = probabilities.get(str(choice), 0.0)
            if not probabilities:
                probabilities = {str(choice): float(confidence)}
            choices[name] = ChoiceAnswer(str(choice), float(confidence), probabilities)
        else:
            probability = _get(answer, "probability")
            if probability is None:
                probability = _get(answer, "noul")
            if probability is None:
                probability = _get(answer, "confidence", 0.0)
            predicates[name] = PredicateAnswer(float(probability))
    return DecisionResponse(choices, predicates, _usage(raw))


def _serialise_state(state: Any) -> Any:
    """The endpoint takes a text string or an ordered message list, never a bare object."""
    if isinstance(state, (str, list, tuple)):
        return state
    return json.dumps(state, ensure_ascii=False, default=str)


def _batch_schema(questions: dict[str, Question]):
    return _cached_batch_schema(
        tuple(
            (
                name,
                tuple(q.criteria) if isinstance(q, ChoiceQuestion) else None,
                json.dumps(q.to_payload(name), sort_keys=True),
            )
            for name, q in questions.items()
        )
    )


@lru_cache(maxsize=64)
def _cached_batch_schema(specs: tuple):
    fields: dict[str, Any] = {}
    for name, criteria, description in specs:
        if criteria is not None:
            allowed = ", ".join(criteria)
            fields[name] = (
                str,
                Field(default="", description=f"{description}. One of: {allowed}. Copy the value exactly."),
            )
        else:
            fields[name] = (bool, Field(default=False, description=description + " True or false."))
    return create_model("DynamicBatchSchema", **fields)


class DecisionsClient:
    """Thin async client for batched choices/predicates with a Responses-API fallback."""

    def __init__(
        self,
        client: Any | None = None,
        model: str | None = None,
        fallback_model: str | None = None,
        not_found_errors: tuple[type[BaseException], ...] = (),
    ):
        self._openai = client
        self.model = model or DECISIONS_MODEL
        self.fallback_model = fallback_model or FAST_FALLBACK_MODEL
        self._not_found_errors = not_found_errors
        self.usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "calls": 0}

    @property
    def openai_client(self) -> Any:
        if self._openai is None:
            self._openai = openai.AsyncOpenAI()
        return self._openai

    async def aclose(self) -> None:
        if self._openai is not None and hasattr(self._openai, "close"):
            await self._openai.close()

    async def evaluate(self, state: Any, questions: dict[str, Question]) -> DecisionResponse:
        local = DecisionResponse()
        sent: dict[str, Question] = {}
        for name, question in questions.items():
            if isinstance(question, ChoiceQuestion):
                criteria = dict(list(question.criteria.items())[:MAX_CHOICES])
                if len(criteria) < MIN_CHOICES:
                    value = str(next(iter(criteria), ""))
                    local.choices[name] = ChoiceAnswer(value, 1.0, {value: 1.0} if value else {})
                    continue
                question = ChoiceQuestion(question.instructions, criteria)
            sent[name] = question
        if not sent:
            return local

        payload = [question.to_payload(name) for name, question in sent.items()]
        decisions = getattr(self.openai_client, "decisions", None)
        if decisions is not None:
            try:
                raw = await decisions.create(model=self.model, input=_serialise_state(state), questions=payload)
                response = _response(raw, sent)
            except Exception as exc:
                if not self._should_fallback(exc):
                    raise
                log.warning("decisions endpoint unavailable (%s); using responses.parse", exc)
                response = await self._fallback(state, sent)
        else:
            response = await self._fallback(state, sent)

        self._record_usage(response.usage)
        return DecisionResponse(
            {**local.choices, **response.choices},
            {**local.predicates, **response.predicates},
            response.usage,
        )

    def _record_usage(self, usage: dict[str, int]) -> None:
        if not usage:
            return
        self.usage["calls"] += 1
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            self.usage[key] += int(usage.get(key, 0))
        log.debug("decisions usage: %s (session %s)", usage, self.usage)

    def _should_fallback(self, exc: BaseException) -> bool:
        if isinstance(exc, (AttributeError, TypeError)):
            return True
        if self._not_found_errors and isinstance(exc, self._not_found_errors):
            return True
        not_found = getattr(openai, "NotFoundError", None)
        if not_found is not None and isinstance(exc, not_found):
            return True
        return getattr(exc, "status_code", None) == 404

    async def _fallback(self, state: Any, questions: dict[str, Question]) -> DecisionResponse:
        schema = _batch_schema(questions)
        user = _serialise_state(state)
        user = user if isinstance(user, str) else json.dumps(user, ensure_ascii=False, default=str)
        resp = await self.openai_client.responses.parse(
            model=self.fallback_model,
            input=[
                {"role": "system", "content": "Answer every question. Copy choice values exactly."},
                {"role": "user", "content": user},
            ],
            text_format=schema,
            reasoning={"effort": "none"},
            prompt_cache_key=cache_key("decisions-fallback", schema.model_json_schema()),
        )
        parsed = getattr(resp, "output_parsed", None)
        if parsed is None:
            parsed = json.loads(getattr(resp, "output_text", "") or "{}")
        data = parsed.model_dump() if isinstance(parsed, BaseModel) else parsed
        if not isinstance(data, dict):
            data = json.loads(str(data))
        choices: dict[str, ChoiceAnswer] = {}
        predicates: dict[str, PredicateAnswer] = {}
        for name, question in questions.items():
            value = data.get(name)
            if isinstance(question, ChoiceQuestion):
                choice = str(value) if value is not None else next(iter(question.criteria), "")
                choices[name] = ChoiceAnswer(choice, 1.0, {choice: 1.0})
            else:
                predicates[name] = PredicateAnswer(1.0 if bool(value) else 0.0)
        return DecisionResponse(choices, predicates)
