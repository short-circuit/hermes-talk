"""Local-only tool router for the voice brain.

Threat model: the model is a router, not a worker.  It never executes a tool,
never edits a file, and never talks to Hermes itself.  Its only job is to pick
one of four allowlisted tools and fill in the arguments.  Everything it emits
is treated as untrusted: the tool name is checked against
:data:`relay.brain.prompts.TOOL_ALLOWLIST`, the arguments are parsed and
validated against the shipped schema, and the spoken text is sanitised before
it can reach TTS.  Anything outside the allowlist is a protocol error, not a
capability.

The only network call in this module is HTTP to the configured OpenAI-
compatible server, which is expected to be a loopback ``llama-server``.  No
cloud SDKs, no speech imports.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from relay.brain.delegation import truncate_untrusted
from relay.brain.prompts import (
    MAX_SPOKEN_CHARS,
    TOOL_ALLOWLIST,
    attachment_ack,
    delegation_ack,
    end_call_farewell,
    router_system_message,
    spoken_text,
    stream_spoken_text,
)

MAX_ARGUMENT_BYTES = 8192
MAX_SKIPPED_FRAMES = 32
_ALLOWED_TOOL_CHOICES = frozenset({"required", "auto"})
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
_TOOL_FILE = Path(__file__).with_name("tools.json")


class RouterError(RuntimeError):
    """Base class for voice-brain router failures."""


class RouterConfigError(RouterError):
    """The router configuration or tool schema is invalid."""


class RouterUnavailable(RouterError):
    """The local OpenAI-compatible endpoint could not be reached."""


class RouterTimeout(RouterError):
    """The endpoint stalled past the first-token or request budget."""


class RouterProtocolError(RouterError):
    """The endpoint returned something outside the router contract."""


def _unavailable_message(base_url: str) -> str:
    port = urlsplit(base_url).port or 80
    return (
        f"voice-brain router unavailable at {base_url}: set VOICE_BRAIN_BASE_URL "
        f"(or LOCALAI_BASE_URL) and run `llama-server --jinja --port {port}`."
    )


@dataclass(frozen=True)
class RouterConfig:
    """Runtime configuration for :class:`VoiceBrainRouter`."""

    base_url: str = "http://127.0.0.1:8080"
    model: str = "lfm3-1.7b"
    temperature: float = 0.1
    max_tokens: int = 160
    tool_choice: str = "required"
    deferred_tools: bool = False
    first_token_timeout_s: float = 2.0
    request_timeout_s: float = 20.0
    enable_thinking: bool = False
    max_utterance_chars: int = 2000
    api_key: str | None = None

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES or not parsed.hostname:
            msg = "base_url must be an http(s) URL with a host"
            raise RouterConfigError(msg)
        if not isinstance(self.model, str) or not self.model.strip():
            msg = "model must be a non-empty string"
            raise RouterConfigError(msg)
        if not math.isfinite(self.temperature) or not 0.0 <= self.temperature <= 2.0:
            msg = "temperature must be finite and between 0.0 and 2.0"
            raise RouterConfigError(msg)
        if not isinstance(self.max_tokens, int) or not 1 <= self.max_tokens <= 4096:
            msg = "max_tokens must be between 1 and 4096"
            raise RouterConfigError(msg)
        if self.tool_choice not in _ALLOWED_TOOL_CHOICES:
            msg = f"tool_choice must be one of {sorted(_ALLOWED_TOOL_CHOICES)}"
            raise RouterConfigError(msg)
        if not isinstance(self.deferred_tools, bool):
            msg = "deferred_tools must be a bool"
            raise RouterConfigError(msg)
        if not math.isfinite(self.first_token_timeout_s) or self.first_token_timeout_s <= 0:
            msg = "first_token_timeout_s must be finite and greater than zero"
            raise RouterConfigError(msg)
        if not math.isfinite(self.request_timeout_s) or self.request_timeout_s <= 0:
            msg = "request_timeout_s must be finite and greater than zero"
            raise RouterConfigError(msg)
        if self.request_timeout_s < self.first_token_timeout_s:
            msg = "request_timeout_s must be at least first_token_timeout_s"
            raise RouterConfigError(msg)
        if not isinstance(self.max_utterance_chars, int) or self.max_utterance_chars < 1:
            msg = "max_utterance_chars must be a positive integer"
            raise RouterConfigError(msg)

    @classmethod
    def from_settings(cls, settings: Any) -> RouterConfig:
        """Build a config from relay settings without importing pydantic here."""
        base_url = (
            getattr(settings, "voice_brain_base_url", None)
            or getattr(settings, "localai_base_url", None)
            or "http://127.0.0.1:8080"
        )
        api_key = getattr(settings, "voice_brain_api_key", None)
        if api_key is not None:
            api_key = str(api_key)
        return cls(
            base_url=str(base_url),
            model=str(getattr(settings, "voice_brain_model", "lfm3-1.7b")),
            temperature=float(getattr(settings, "voice_brain_temperature", 0.1)),
            max_tokens=int(getattr(settings, "voice_brain_max_tokens", 160)),
            tool_choice=str(getattr(settings, "voice_brain_tool_choice", "required")),
            deferred_tools=bool(getattr(settings, "voice_brain_deferred_tools", False)),
            first_token_timeout_s=float(
                getattr(settings, "voice_brain_first_token_timeout_s", 2.0)
            ),
            request_timeout_s=float(getattr(settings, "voice_brain_request_timeout_s", 20.0)),
            enable_thinking=bool(getattr(settings, "voice_brain_enable_thinking", False)),
            max_utterance_chars=int(getattr(settings, "voice_brain_max_utterance_chars", 2000)),
            api_key=api_key,
        )


@dataclass(frozen=True)
class ToolCall:
    """One parsed tool call selected by the router model."""

    name: str
    arguments: Mapping[str, Any]
    call_id: str


@dataclass(frozen=True)
class RouterDecision:
    """The final routing decision, whether from the policy or the model."""

    tool: str
    arguments: Mapping[str, Any]
    spoken_text: str
    source: str
    rule: str | None
    repaired: bool
    ttft_ms: float | None
    total_ms: float | None


@dataclass(frozen=True)
class RouterEvent:
    """One streamed router event.  ``kind`` is fixed to the documented set."""

    kind: str
    text: str | None
    decision: RouterDecision | None


def _validate_tool_schema(raw: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, dict):
        msg = "tools.json must contain a top-level object"
        raise RouterConfigError(msg)
    version = raw.get("version")
    if not isinstance(version, int) or version != 1:
        msg = "tools.json version must be the integer 1"
        raise RouterConfigError(msg)
    tools = raw.get("tools")
    if not isinstance(tools, list):
        msg = "tools.json tools must be a list"
        raise RouterConfigError(msg)
    functions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in tools:
        if not isinstance(entry, dict) or entry.get("type") != "function":
            msg = "every tool must be an object with type 'function'"
            raise RouterConfigError(msg)
        function = entry.get("function")
        if not isinstance(function, dict):
            msg = "every tool must carry a function object"
            raise RouterConfigError(msg)
        name = function.get("name")
        description = function.get("description")
        parameters = function.get("parameters")
        if not isinstance(name, str) or not name.strip():
            msg = "every tool needs a non-empty name"
            raise RouterConfigError(msg)
        if not isinstance(description, str) or not description.strip():
            msg = f"tool {name!r} needs a non-empty description"
            raise RouterConfigError(msg)
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            msg = f"tool {name!r} parameters must be an object schema"
            raise RouterConfigError(msg)
        if not isinstance(parameters.get("properties"), dict):
            msg = f"tool {name!r} parameters must declare properties"
            raise RouterConfigError(msg)
        required = parameters.get("required", [])
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            msg = f"tool {name!r} parameters.required must be a list of names"
            raise RouterConfigError(msg)
        if name in seen:
            msg = f"duplicate tool name: {name!r}"
            raise RouterConfigError(msg)
        seen.add(name)
        functions.append(function)
    if seen != set(TOOL_ALLOWLIST):
        msg = f"tools.json names {sorted(seen)} do not match the allowlist {sorted(TOOL_ALLOWLIST)}"
        raise RouterConfigError(msg)
    return tuple(functions)


class ToolRegistry:
    """Loads and validates the canonical four-tool schema.

    llama.cpp 0.8.0 has no ``defer_loading`` or tool-search support, so
    "deferred" here means conditional per-request exposure of a
    session-relevant tool: ``attach_file`` is omitted unless an attachment is
    actually pending.  The tool list is otherwise unchanged, and no tool
    outside :data:`relay.brain.prompts.TOOL_ALLOWLIST` is ever exposed.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else _TOOL_FILE
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            msg = f"cannot read tool schema at {self.path}: {exc}"
            raise RouterConfigError(msg) from exc
        except json.JSONDecodeError as exc:
            msg = f"tool schema at {self.path} is not valid JSON: {exc}"
            raise RouterConfigError(msg) from exc
        self._functions = _validate_tool_schema(raw)
        self._by_name = {item["name"]: item for item in self._functions}

    @property
    def names(self) -> tuple[str, ...]:
        """Tool names in schema order."""
        return tuple(item["name"] for item in self._functions)

    def required_properties(self, name: str) -> tuple[str, ...]:
        """Required parameter names for one allowlisted tool."""
        try:
            function = self._by_name[name]
        except KeyError as exc:
            msg = f"unknown tool: {name!r}"
            raise RouterConfigError(msg) from exc
        required = function["parameters"].get("required", [])
        return tuple(required)

    def schemas(self, *, attachments_pending: bool, deferred: bool) -> list[dict[str, Any]]:
        """Return the ``tools`` array for one request.

        Eager mode exposes all four tools.  Deferred mode omits
        ``attach_file`` unless ``attachments_pending`` is true; it is useless
        without an attachment and costs prompt tokens on every turn.
        """
        exposed: list[dict[str, Any]] = []
        for function in self._functions:
            if deferred and function["name"] == "attach_file" and not attachments_pending:
                continue
            exposed.append({"type": "function", "function": copy.deepcopy(function)})
        return exposed


# --- deterministic direct-answer policy -----------------------------------

_FAREWELL_PHRASES: tuple[str, ...] = (
    "bye",
    "bye bye",
    "goodbye",
    "good night",
    "talk later",
    "talk to you later",
    "see you",
    "that's all",
    "that is all",
    "that's everything",
    "i'm done",
    "im done",
    "nothing else",
    "hang up",
    "end the call",
)
_TIME_PHRASES: tuple[str, ...] = (
    "what time is it",
    "what's the time",
    "what is the time",
    "current time",
    "do you have the time",
)
_DATE_PHRASES: tuple[str, ...] = (
    "what's the date",
    "what is the date",
    "what day is it",
    "what's today's date",
    "today's date",
)
_PRESENCE_PHRASES: tuple[str, ...] = (
    "can you hear me",
    "are you there",
    "are you listening",
    "you there",
    "hello can you hear me",
)


def _phrase_pattern(phrases: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(phrase) for phrase in phrases)
    return re.compile(rf"\b(?:{alternatives})\b")


_FAREWELL_RE = _phrase_pattern(_FAREWELL_PHRASES)
_TIME_RE = _phrase_pattern(_TIME_PHRASES)
_DATE_RE = _phrase_pattern(_DATE_PHRASES)
_PRESENCE_RE = _phrase_pattern(_PRESENCE_PHRASES)

_ARITHMETIC_PREFIX_RE = re.compile(
    r"^(?:what's|what is|how much is|calculate|compute|how many is)\s+",
    re.IGNORECASE,
)
_OPERATOR_RE = re.compile(
    r"\b(?:times|multiplied\s+by|x|plus|and|added\s+to|minus|take\s+away|"
    r"divided\s+by|over)\b",
    re.IGNORECASE,
)

_NUMBER_WORDS: dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_SCALES: dict[str, int] = {"hundred": 100, "thousand": 1000}


def _parse_number(phrase: str) -> int | None:
    phrase = phrase.strip().lower().replace("-", " ")
    if not phrase:
        return None
    if phrase.isdigit():
        value = int(phrase)
        return value if 0 <= value <= 9999 else None
    tokens = phrase.split()
    total = 0
    current = 0
    saw_word = False
    for token in tokens:
        if token in _SCALES:
            current = (current or 1) * _SCALES[token]
            saw_word = True
        elif token in _NUMBER_WORDS:
            current += _NUMBER_WORDS[token]
            saw_word = True
        else:
            return None
    total += current
    if not saw_word or total > 9999:
        return None
    return total


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _canonical_operator(operator: str) -> str:
    normalized = re.sub(r"\s+", " ", operator.strip().lower())
    if normalized in {"times", "multiplied by", "x"}:
        return "times"
    if normalized in {"plus", "and", "added to"}:
        return "plus"
    if normalized in {"minus", "take away"}:
        return "minus"
    return "divided by"


class DirectAnswerPolicy:
    """Deterministic, no-model shortcuts for utterances the router should own.

    Each rule exists because the model either cannot answer reliably or wastes a
    round trip on a question the relay can settle locally:

    * ``farewell``: "Bye, talk later" is answered instead of ending the call in
      roughly half of measured runs; ending a call is a local session decision.
    * ``time``/``date``: the relay already knows the local clock; a model cannot
      know it without being told and tends to echo the context line.
    * ``arithmetic``: full-match simple expressions are cheaper and exact
      locally, while anything with extra words stays on the model path.
    * ``presence``: "Can you hear me?" was misrouted to delegation in about a
      third of measured runs; a hearing check is always an immediate answer.

    The policy is pure and synchronous so it can be unit-tested without I/O.
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or datetime.now

    def resolve(self, utterance: str) -> RouterDecision | None:
        """Return a policy decision, or ``None`` to let the model route."""
        text = " ".join(utterance.lower().split())
        if not text:
            return None
        now = self.clock()
        if _FAREWELL_RE.search(text):
            spoken = end_call_farewell()
            return RouterDecision(
                tool="end_call",
                arguments={},
                spoken_text=spoken,
                source="policy",
                rule="farewell",
                repaired=False,
                ttft_ms=None,
                total_ms=None,
            )
        if _TIME_RE.search(text):
            spoken = f"It's {now.strftime('%H:%M')}."
            return self._answer(spoken, "time")
        if _DATE_RE.search(text):
            spoken = f"Today is {now:%A}, {now.day} {now:%B} {now:%Y}."
            return self._answer(spoken, "date")
        arithmetic = self._arithmetic(text)
        if arithmetic is not None:
            return arithmetic
        if _PRESENCE_RE.search(text):
            return self._answer("Yes, I'm here.", "presence")
        return None

    def _answer(self, spoken: str, rule: str) -> RouterDecision:
        return RouterDecision(
            tool="answer_direct",
            arguments={"text": spoken},
            spoken_text=spoken,
            source="policy",
            rule=rule,
            repaired=False,
            ttft_ms=None,
            total_ms=None,
        )

    def _arithmetic(self, text: str) -> RouterDecision | None:
        expression = _ARITHMETIC_PREFIX_RE.sub("", text).strip()
        expression = expression.rstrip("?.!").strip()
        if not expression:
            return None
        operators = list(_OPERATOR_RE.finditer(expression))
        if len(operators) != 1:
            return None
        operator = operators[0]
        left = _parse_number(expression[: operator.start()])
        right = _parse_number(expression[operator.end() :])
        if left is None or right is None:
            return None
        symbol = _canonical_operator(operator.group(0))
        if symbol == "times":
            result: float = left * right
        elif symbol == "plus":
            result = left + right
        elif symbol == "minus":
            result = left - right
        else:
            if right == 0:
                return self._answer("You can't divide by zero.", "arithmetic")
            result = left / right
        left_text = expression[: operator.start()].strip()
        right_text = expression[operator.end() :].strip()
        spoken = (
            f"{left_text[:1].upper()}{left_text[1:]} {symbol} "
            f"{right_text} is {_format_number(result)}."
        )
        return self._answer(spoken, "arithmetic")


# --- streaming argument decoding ------------------------------------------

_TEXT_KEY_RE = re.compile(r'"text"\s*:\s*"')
_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


def _decode_text_value(raw: str) -> tuple[str | None, bool]:
    """Decode the ``text`` string of an answer_direct argument object.

    Returns the decoded prefix and whether the closing quote was seen.  An
    escape split across fragments is withheld until it is complete.
    """
    match = _TEXT_KEY_RE.search(raw)
    if match is None:
        return None, False
    out: list[str] = []
    index = match.end()
    length = len(raw)
    while index < length:
        char = raw[index]
        if char == '"':
            return "".join(out), True
        if char != "\\":
            out.append(char)
            index += 1
            continue
        if index + 1 >= length:
            break
        escape = raw[index + 1]
        if escape == "u":
            if index + 6 > length:
                break
            digits = raw[index + 2 : index + 6]
            if len(digits) != 4 or any(c not in "0123456789abcdefABCDEF" for c in digits):
                break
            code = int(digits, 16)
            index += 6
            if (
                0xD800 <= code <= 0xDBFF
                and index + 6 <= length
                and raw[index] == "\\"
                and raw[index + 1] == "u"
            ):
                low_digits = raw[index + 2 : index + 6]
                if len(low_digits) == 4 and all(c in "0123456789abcdefABCDEF" for c in low_digits):
                    low = int(low_digits, 16)
                    if 0xDC00 <= low <= 0xDFFF:
                        code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                        index += 6
            out.append(chr(code))
            continue
        out.append(_ESCAPES.get(escape, escape))
        index += 2
    return "".join(out), False


@dataclass
class _CallState:
    name: str = ""
    args: str = ""
    call_id: str = ""


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


class VoiceBrainRouter:
    """Streams router events for one spoken utterance at a time."""

    def __init__(
        self,
        config: RouterConfig,
        *,
        registry: ToolRegistry | None = None,
        policy: DirectAnswerPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(config, RouterConfig):
            msg = "config must be a RouterConfig"
            raise RouterConfigError(msg)
        self.config = config
        self.registry = registry if registry is not None else ToolRegistry()
        self.policy = policy if policy is not None else DirectAnswerPolicy()
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        """Close the shared HTTP client, if one was created."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(self.config.request_timeout_s),
            )
        return self._client

    async def route(
        self,
        utterance: str,
        *,
        attachments_pending: bool = False,
    ) -> AsyncIterator[RouterEvent]:
        """Yield router events for one utterance.

        An empty utterance raises :class:`RouterProtocolError`; it is a caller
        bug, not something to speak.  A policy hit completes without HTTP.
        """
        started = time.perf_counter()
        normalized = truncate_untrusted(utterance, self.config.max_utterance_chars).text
        if not normalized:
            msg = "empty utterance: nothing to route"
            raise RouterProtocolError(msg)

        policy_decision = self.policy.resolve(normalized)
        if policy_decision is not None:
            decision = _with_timing(policy_decision, 0.0, _elapsed_ms(started))
            if decision.tool == "answer_direct":
                yield RouterEvent("text_delta", decision.spoken_text, decision)
                yield RouterEvent("text_final", decision.spoken_text, decision)
            else:
                yield RouterEvent("ack", decision.spoken_text, decision)
            yield RouterEvent("tool_call", decision.spoken_text, decision)
            yield RouterEvent("done", None, decision)
            return

        now = self.policy.clock()
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                router_system_message(now),
                {"role": "user", "content": normalized},
            ],
            "tools": self.registry.schemas(
                attachments_pending=attachments_pending,
                deferred=self.config.deferred_tools,
            ),
            "tool_choice": self.config.tool_choice,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": self.config.enable_thinking},
        }
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.request_timeout_s
        first_deadline = loop.time() + self.config.first_token_timeout_s
        frames = self._sse_frames(body, headers)
        calls: dict[int, _CallState] = {}
        draft: list[str] = []
        announced = False
        emitted = ""
        ttft_ms: float | None = None
        finish_reason: str | None = None
        saw_frame = False
        skipped = 0

        try:
            while True:
                budget = deadline - loop.time()
                if budget <= 0:
                    raise RouterTimeout(
                        f"voice-brain router exceeded request_timeout_s="
                        f"{self.config.request_timeout_s} at {self.config.base_url}"
                    )
                if not saw_frame:
                    budget = min(budget, first_deadline - loop.time())
                    if budget <= 0:
                        raise RouterTimeout(
                            f"voice-brain router produced no first event within "
                            f"first_token_timeout_s={self.config.first_token_timeout_s} "
                            f"at {self.config.base_url}; check `llama-server --jinja "
                            f"--port <port>`"
                        )
                try:
                    payload = await asyncio.wait_for(frames.__anext__(), timeout=max(budget, 0.001))
                except StopAsyncIteration:
                    break
                except TimeoutError as exc:
                    raise RouterTimeout(
                        f"voice-brain router stream stalled past its budget at "
                        f"{self.config.base_url}"
                    ) from exc
                saw_frame = True
                try:
                    frame = json.loads(payload)
                except json.JSONDecodeError:
                    skipped += 1
                    if skipped > MAX_SKIPPED_FRAMES:
                        msg = (
                            f"voice-brain router skipped more than "
                            f"{MAX_SKIPPED_FRAMES} malformed SSE frames"
                        )
                        raise RouterProtocolError(msg) from None
                    continue
                if not isinstance(frame, dict):
                    skipped += 1
                    if skipped > MAX_SKIPPED_FRAMES:
                        msg = "voice-brain router saw too many non-object SSE frames"
                        raise RouterProtocolError(msg)
                    continue
                for choice in frame.get("choices") or []:
                    if not isinstance(choice, dict):
                        continue
                    reason = choice.get("finish_reason")
                    if reason:
                        finish_reason = str(reason)
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        continue
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        draft.append(content)
                    for call in delta.get("tool_calls") or []:
                        if not isinstance(call, dict):
                            continue
                        try:
                            index = int(call.get("index", 0))
                        except (TypeError, ValueError):
                            index = 0
                        state = calls.setdefault(index, _CallState())
                        if isinstance(call.get("id"), str) and call["id"]:
                            state.call_id += call["id"]
                        function = call.get("function") or {}
                        if not isinstance(function, dict):
                            continue
                        name = function.get("name")
                        if isinstance(name, str) and name:
                            state.name += name
                        arguments = function.get("arguments")
                        if isinstance(arguments, str) and arguments:
                            state.args += arguments
                            if len(state.args.encode("utf-8")) > MAX_ARGUMENT_BYTES:
                                msg = (
                                    f"tool call {state.name!r} arguments exceeded "
                                    f"{MAX_ARGUMENT_BYTES} bytes"
                                )
                                raise RouterProtocolError(msg)
                        if index != 0:
                            continue
                        if not announced and state.name in TOOL_ALLOWLIST:
                            announced = True
                            if state.name == "delegate_to_hermes":
                                spoken = delegation_ack()
                                ttft_ms = _elapsed_ms(started)
                                yield RouterEvent("ack", spoken, None)
                            elif state.name == "attach_file":
                                spoken = attachment_ack()
                                ttft_ms = _elapsed_ms(started)
                                yield RouterEvent("ack", spoken, None)
                            elif state.name == "end_call":
                                spoken = end_call_farewell()
                                ttft_ms = _elapsed_ms(started)
                                yield RouterEvent("ack", spoken, None)
                        if state.name == "answer_direct":
                            decoded, _complete = _decode_text_value(state.args)
                            if decoded is None:
                                continue
                            candidate = stream_spoken_text(decoded)[:MAX_SPOKEN_CHARS]
                            if candidate.startswith(emitted):
                                piece = candidate[len(emitted) :]
                                if piece:
                                    emitted = candidate
                                    if ttft_ms is None:
                                        ttft_ms = _elapsed_ms(started)
                                    yield RouterEvent("text_delta", piece, None)
                if finish_reason is not None:
                    break
        except RouterError:
            raise
        except httpx.HTTPError as exc:
            raise RouterUnavailable(_unavailable_message(self.config.base_url)) from exc
        except TimeoutError as exc:
            raise RouterTimeout(f"voice-brain router timed out at {self.config.base_url}") from exc
        finally:
            await frames.aclose()

        total_ms = _elapsed_ms(started)
        if not calls:
            draft_text = "".join(draft)
            if draft_text.strip():
                text = spoken_text(draft_text)
                if not text:
                    msg = "model returned content that sanitised to nothing"
                    raise RouterProtocolError(msg)
                if ttft_ms is None:
                    ttft_ms = total_ms
                yield RouterEvent("text_delta", text, None)
                yield RouterEvent("text_final", text, None)
                decision = RouterDecision(
                    tool="answer_direct",
                    arguments={"text": text},
                    spoken_text=text,
                    source="model",
                    rule="content_fallback",
                    repaired=True,
                    ttft_ms=ttft_ms,
                    total_ms=total_ms,
                )
                yield RouterEvent("tool_call", text, decision)
                yield RouterEvent("done", None, decision)
                return
            msg = f"model returned no tool call and no content (finish_reason={finish_reason!r})"
            raise RouterProtocolError(msg)

        state = calls[min(calls)]
        if state.name not in TOOL_ALLOWLIST:
            msg = f"model chose unknown tool {state.name!r}; allowlist is {TOOL_ALLOWLIST}"
            raise RouterProtocolError(msg)
        try:
            parsed = json.loads(state.args) if state.args.strip() else {}
        except json.JSONDecodeError as exc:
            msg = f"tool call {state.name!r} arguments are not valid JSON: {exc}"
            raise RouterProtocolError(msg) from exc
        if not isinstance(parsed, dict):
            msg = f"tool call {state.name!r} arguments must be a JSON object"
            raise RouterProtocolError(msg)
        arguments: dict[str, Any] = dict(parsed)
        repaired = False
        for prop in self.registry.required_properties(state.name):
            value = arguments.get(prop)
            if prop == "instruction" and (not isinstance(value, str) or not value.strip()):
                arguments["instruction"] = normalized
                repaired = True
            elif prop == "filename" and (not isinstance(value, str) or not value.strip()):
                arguments.pop("filename", None)
                repaired = True
            elif not isinstance(value, str) or not value.strip():
                msg = f"tool call {state.name!r} is missing required property {prop!r}"
                raise RouterProtocolError(msg)

        if state.name == "answer_direct":
            final_text = spoken_text(arguments["text"])
            if ttft_ms is None:
                ttft_ms = total_ms
            if final_text.startswith(emitted) and len(final_text) > len(emitted):
                piece = final_text[len(emitted) :]
                emitted = final_text
                yield RouterEvent("text_delta", piece, None)
            yield RouterEvent("text_final", final_text, None)
            spoken = final_text
            rule = None
        elif state.name == "delegate_to_hermes":
            spoken = delegation_ack()
            rule = None
        elif state.name == "attach_file":
            spoken = attachment_ack()
            rule = None
        else:
            spoken = end_call_farewell()
            rule = None

        if not announced:
            yield RouterEvent("ack", spoken, None)
            if ttft_ms is None:
                ttft_ms = total_ms

        decision = RouterDecision(
            tool=state.name,
            arguments=arguments,
            spoken_text=spoken,
            source="model",
            rule=rule,
            repaired=repaired,
            ttft_ms=ttft_ms,
            total_ms=total_ms,
        )
        yield RouterEvent("tool_call", spoken, decision)
        yield RouterEvent("done", None, decision)

    async def decide(
        self,
        utterance: str,
        *,
        attachments_pending: bool = False,
    ) -> RouterDecision:
        """Drain :meth:`route` up to the tool_call event and return the decision."""
        events = self.route(utterance, attachments_pending=attachments_pending)
        try:
            async for event in events:
                if event.kind == "tool_call" and event.decision is not None:
                    return event.decision
        finally:
            await events.aclose()
        msg = "voice-brain router produced no tool_call event"
        raise RouterProtocolError(msg)

    async def _sse_frames(
        self, body: dict[str, Any], headers: dict[str, str]
    ) -> AsyncIterator[str]:
        client = self._client_or_create()
        url = self.config.base_url.rstrip("/") + "/v1/chat/completions"
        async with client.stream("POST", url, json=body, headers=headers) as response:
            if response.status_code >= 400:
                raw = await response.aread()
                detail = raw.decode("utf-8", "replace")[:300]
                msg = f"voice-brain router endpoint returned HTTP {response.status_code}: {detail}"
                raise RouterProtocolError(msg)
            async for line in response.aiter_lines():
                line = line.strip()
                if not line or line.startswith(":") or line.startswith("event:"):
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    return
                yield payload


def _with_timing(
    decision: RouterDecision, ttft_ms: float | None, total_ms: float | None
) -> RouterDecision:
    return RouterDecision(
        tool=decision.tool,
        arguments=decision.arguments,
        spoken_text=decision.spoken_text,
        source=decision.source,
        rule=decision.rule,
        repaired=decision.repaired,
        ttft_ms=ttft_ms,
        total_ms=total_ms,
    )


__all__ = [
    "MAX_ARGUMENT_BYTES",
    "DirectAnswerPolicy",
    "RouterConfig",
    "RouterConfigError",
    "RouterDecision",
    "RouterError",
    "RouterEvent",
    "RouterProtocolError",
    "RouterTimeout",
    "RouterUnavailable",
    "ToolCall",
    "ToolRegistry",
    "VoiceBrainRouter",
]
