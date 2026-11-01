"""Offline tests for the voice-brain router.

Every model call goes through ``httpx.MockTransport`` with hand-written SSE
frames; nothing here touches a live server or the network.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from relay.brain import prompts
from relay.brain.router import (
    MAX_ARGUMENT_BYTES,
    DirectAnswerPolicy,
    RouterConfig,
    RouterConfigError,
    RouterDecision,
    RouterProtocolError,
    RouterTimeout,
    RouterUnavailable,
    ToolRegistry,
    VoiceBrainRouter,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_CASES = REPO_ROOT / "scripts" / "voice_bench_utterances.json"
FIXED_NOW = datetime(2026, 11, 1, 9, 5)


# --- SSE helpers ----------------------------------------------------------


def _chunk(delta: dict, finish: str | None = None) -> dict:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _tool_frame(
    *,
    index: int = 0,
    name: str | None = None,
    arguments: str | None = None,
    call_id: str | None = None,
) -> dict:
    function: dict[str, str] = {}
    if name is not None:
        function["name"] = name
    if arguments is not None:
        function["arguments"] = arguments
    call: dict[str, object] = {"index": index, "function": function}
    if call_id is not None:
        call["id"] = call_id
    return _chunk({"tool_calls": [call]})


def _sse(*frames: dict | str) -> bytes:
    body = []
    for frame in frames:
        payload = frame if isinstance(frame, str) else json.dumps(frame)
        body.append(f"data: {payload}\n\n")
    return "".join(body).encode("utf-8")


def _mock_transport(
    payload: bytes,
    calls: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=payload,
        )

    return httpx.MockTransport(handler)


def _streaming_transport(
    items: list[bytes | float],
    calls: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    async def body() -> AsyncIterator[bytes]:
        for item in items:
            if isinstance(item, float):
                await asyncio.sleep(item)
            else:
                yield item

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body(),
        )

    return httpx.MockTransport(handler)


async def _collect(
    router: VoiceBrainRouter, utterance: str, **kwargs: object
) -> list[tuple[str, str | None, RouterDecision | None, float]]:
    started = time.perf_counter()
    events = []
    stream = router.route(utterance, **kwargs)
    try:
        async for event in stream:
            events.append((event.kind, event.text, event.decision, time.perf_counter() - started))
    finally:
        await stream.aclose()
    return events


def _router(
    transport: httpx.MockTransport,
    **config: object,
) -> VoiceBrainRouter:
    options: dict[str, object] = {
        "base_url": "http://127.0.0.1:8080",
        "first_token_timeout_s": 1.0,
        "request_timeout_s": 5.0,
    }
    options.update(config)
    return VoiceBrainRouter(RouterConfig(**options), transport=transport)


# --- tool schema ----------------------------------------------------------


def test_tools_json_matches_allowlist_and_bench_schema() -> None:
    registry = ToolRegistry()
    assert registry.names == prompts.TOOL_ALLOWLIST
    bench = json.loads(BENCH_CASES.read_text(encoding="utf-8"))
    bench_functions = [tool["function"] for tool in bench["tools"]]
    loaded = registry.schemas(attachments_pending=True, deferred=False)
    assert [tool["function"] for tool in loaded] == bench_functions


def test_registry_rejects_duplicate_names(tmp_path: Path) -> None:
    raw = json.loads((REPO_ROOT / "relay" / "brain" / "tools.json").read_text("utf-8"))
    raw["tools"].append(raw["tools"][0])
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RouterConfigError, match="duplicate"):
        ToolRegistry(path)


def test_registry_rejects_missing_name(tmp_path: Path) -> None:
    raw = json.loads((REPO_ROOT / "relay" / "brain" / "tools.json").read_text("utf-8"))
    raw["tools"] = raw["tools"][:-1]
    path = tmp_path / "missing.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RouterConfigError, match="do not match"):
        ToolRegistry(path)


def test_registry_rejects_extra_name(tmp_path: Path) -> None:
    raw = json.loads((REPO_ROOT / "relay" / "brain" / "tools.json").read_text("utf-8"))
    extra = json.loads(json.dumps(raw["tools"][3]))
    extra["function"]["name"] = "send_email"
    raw["tools"].append(extra)
    path = tmp_path / "extra.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RouterConfigError, match="do not match"):
        ToolRegistry(path)


def test_registry_rejects_bad_parameters(tmp_path: Path) -> None:
    raw = json.loads((REPO_ROOT / "relay" / "brain" / "tools.json").read_text("utf-8"))
    raw["tools"][0]["function"]["parameters"] = {"type": "array"}
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RouterConfigError, match="object schema"):
        ToolRegistry(path)


# --- router prompt --------------------------------------------------------


def test_router_prompt_has_clock_tools_and_containment_rule() -> None:
    rendered = prompts.router_system_prompt(FIXED_NOW)
    assert "Sunday, 01 November 2026" in rendered
    assert "09:05" in rendered
    for name in prompts.TOOL_ALLOWLIST:
        assert name in rendered
    assert prompts.CONTAINMENT_RULE in rendered
    assert prompts.router_system_message(FIXED_NOW)["role"] == "system"
    assert prompts.router_system_message(FIXED_NOW)["content"] == rendered


def test_router_prompt_sha_changes_with_clock() -> None:
    first = prompts.router_prompt_sha256(FIXED_NOW)
    second = prompts.router_prompt_sha256(datetime(2026, 11, 1, 9, 6))
    assert first != second
    assert len(first) == 64


def test_spoken_text_strips_markdown_and_emoji() -> None:
    raw = "## Heading\n**bold** `code` 🙂 </think> done"
    assert prompts.spoken_text(raw) == "Heading bold code done"
    long = " ".join(["word"] * 400)
    bounded = prompts.spoken_text(long)
    assert len(bounded) <= prompts.MAX_SPOKEN_CHARS + 1
    assert bounded.endswith("\u2026")


# --- direct-answer policy -------------------------------------------------


@pytest.mark.parametrize(
    ("utterance", "rule"),
    [
        ("Thanks, that's all for now.", "farewell"),
        ("Bye, talk later.", "farewell"),
        ("Good night.", "farewell"),
        ("I'm done.", "farewell"),
        ("What time is it?", "time"),
        ("What's the time?", "time"),
        ("Do you have the time?", "time"),
        ("What's the date?", "date"),
        ("What day is it?", "date"),
        ("Today's date?", "date"),
        ("Can you hear me?", "presence"),
        ("Are you there?", "presence"),
        ("Hello can you hear me?", "presence"),
    ],
)
def test_policy_hits(utterance: str, rule: str) -> None:
    policy = DirectAnswerPolicy(clock=lambda: FIXED_NOW)
    decision = policy.resolve(utterance)
    assert decision is not None
    assert decision.rule == rule
    assert decision.source == "policy"
    assert decision.ttft_ms is None


def test_policy_time_and_date_text() -> None:
    policy = DirectAnswerPolicy(clock=lambda: FIXED_NOW)
    time_decision = policy.resolve("What time is it?")
    assert time_decision is not None
    assert time_decision.tool == "answer_direct"
    assert time_decision.spoken_text == "It's 09:05."
    date_decision = policy.resolve("What's the date?")
    assert date_decision is not None
    assert date_decision.spoken_text == "Today is Sunday, 1 November 2026."


def test_policy_farewell_and_presence_text() -> None:
    policy = DirectAnswerPolicy(clock=lambda: FIXED_NOW)
    farewell = policy.resolve("Bye, talk later.")
    assert farewell is not None
    assert farewell.tool == "end_call"
    assert farewell.spoken_text == prompts.end_call_farewell()
    presence = policy.resolve("Can you hear me?")
    assert presence is not None
    assert presence.spoken_text == "Yes, I'm here."


@pytest.mark.parametrize(
    "utterance",
    [
        "I'm done with the report, email it to Anna.",
        "See you tomorrow at the meeting.",
        "That's all I need for the email.",
    ],
)
def test_farewell_phrase_mid_sentence_is_not_terminal(utterance: str) -> None:
    assert DirectAnswerPolicy(clock=lambda: FIXED_NOW).resolve(utterance) is None


@pytest.mark.parametrize(
    "utterance",
    [
        "Thanks, that's all for now.",
        "Bye, talk later.",
        "I'm done.",
        "Good night.",
        "Nothing else, thanks.",
    ],
)
def test_terminal_farewell_is_end_call(utterance: str) -> None:
    decision = DirectAnswerPolicy(clock=lambda: FIXED_NOW).resolve(utterance)
    assert decision is not None
    assert decision.tool == "end_call"
    assert decision.rule == "farewell"


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("What's seventeen times four?", "Seventeen times four is 68."),
        ("What is 12 plus 30?", "12 plus 30 is 42."),
        ("what's 17 x 4", "17 times 4 is 68."),
        ("Calculate twenty minus five.", "Twenty minus five is 15."),
        ("what is one hundred divided by four", "One hundred divided by four is 25."),
        ("how much is 7 divided by 0?", "You can't divide by zero."),
        ("what's zero plus twenty?", "Zero plus twenty is 20."),
        ("what is 9 take away 2", "9 minus 2 is 7."),
    ],
)
def test_policy_arithmetic(utterance: str, expected: str) -> None:
    policy = DirectAnswerPolicy(clock=lambda: FIXED_NOW)
    decision = policy.resolve(utterance)
    assert decision is not None
    assert decision.rule == "arithmetic"
    assert decision.tool == "answer_direct"
    assert decision.spoken_text == expected


@pytest.mark.parametrize(
    "utterance",
    [
        "what's 2 plus 2 and email Anna",
        "what's the weather",
        "what's 2 plus 2 and then email Anna",
        "Tell me a joke.",
        "What's the capital of France?",
    ],
)
def test_policy_negative_cases(utterance: str) -> None:
    assert DirectAnswerPolicy(clock=lambda: FIXED_NOW).resolve(utterance) is None


def test_policy_path_makes_no_http_request() -> None:
    calls: list[httpx.Request] = []
    router = _router(_mock_transport(_sse(_chunk({}, "stop")), calls))

    async def scenario() -> None:
        events = await _collect(router, "What time is it?")
        assert calls == []
        kinds = [event[0] for event in events]
        assert kinds == ["text_delta", "text_final", "tool_call", "done"]
        decision = events[2][2]
        assert decision is not None
        assert decision.tool == "answer_direct"
        assert decision.source == "policy"
        assert decision.rule == "time"
        assert decision.ttft_ms == 0.0
        await router.aclose()

    asyncio.run(scenario())


def test_policy_farewell_emits_ack_then_tool_call() -> None:
    calls: list[httpx.Request] = []
    router = _router(_mock_transport(_sse(_chunk({}, "stop")), calls))

    async def scenario() -> None:
        events = await _collect(router, "Thanks, that's all for now.")
        assert calls == []
        assert events[0][0] == "ack"
        assert events[0][1] == prompts.end_call_farewell()
        decision = events[-2][2]
        assert decision is not None
        assert decision.tool == "end_call"
        assert decision.rule == "farewell"
        await router.aclose()

    asyncio.run(scenario())


# --- streaming answer_direct ---------------------------------------------


def _answer_frames(pieces: list[str]) -> list[dict | str]:
    frames: list[dict | str] = [_tool_frame(name="answer_direct", arguments=pieces[0])]
    frames.extend(_tool_frame(arguments=piece) for piece in pieces[1:])
    frames.append(_chunk({}, "tool_calls"))
    frames.append("[DONE]")
    return frames


def test_streaming_answer_direct_decodes_split_escapes() -> None:
    pieces = [
        "{",
        '"text":"',
        "Hello **world** ",
        "\\",
        '"',
        "quoted",
        "\\",
        '"',
        "\\",
        "n",
        "\\",
        "u00",
        "e9",
        " \U0001f642</thi",
        "nk>",
        '"',
        "}",
    ]
    assert json.loads("".join(pieces))["text"] == (
        'Hello **world** "quoted"\n\u00e9 \U0001f642</think>'
    )
    router = _router(_mock_transport(_sse(*_answer_frames(pieces))))

    async def scenario() -> None:
        events = await _collect(router, "Tell me something nice.")
        kinds = [event[0] for event in events]
        deltas = [event[1] for event in events if event[0] == "text_delta"]
        assert len(deltas) >= 2
        assert all(delta for delta in deltas)
        assert kinds.index("text_delta") < kinds.index("text_final")
        assert kinds.index("text_final") < kinds.index("tool_call")
        assert kinds.index("tool_call") < kinds.index("done")
        final = events[kinds.index("text_final")][1]
        assert final == 'Hello world "quoted" é'
        assert "**" not in final
        assert "</think>" not in final
        assert "\U0001f642" not in final
        assert "é" in final
        decision = events[kinds.index("tool_call")][2]
        assert decision is not None
        assert decision.tool == "answer_direct"
        assert decision.arguments["text"] == ('Hello **world** "quoted"\n\u00e9 \U0001f642</think>')
        assert decision.source == "model"
        assert decision.ttft_ms is not None
        assert decision.ttft_ms >= 0.0
        await router.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["\U0001f604", "</think>"])
def test_empty_spoken_answer_direct_is_protocol_error(text: str) -> None:
    frames = [
        _tool_frame(name="answer_direct", arguments=json.dumps({"text": text})),
        _chunk({}, "tool_calls"),
        "[DONE]",
    ]
    router = _router(_mock_transport(_sse(*frames)))

    async def scenario() -> None:
        with pytest.raises(RouterProtocolError, match="answer_direct"):
            await router.decide("Say something.")
        await router.aclose()

    asyncio.run(scenario())


def test_draft_content_is_ignored_when_tool_call_follows() -> None:
    frames = [
        _chunk({"content": "draft answer "}),
        _chunk({"content": "that should be ignored"}),
        _tool_frame(name="answer_direct", arguments='{"text":"Final answer."}'),
        _chunk({}, "tool_calls"),
        "[DONE]",
    ]
    router = _router(_mock_transport(_sse(*frames)))

    async def scenario() -> None:
        events = await _collect(router, "Say something.")
        deltas = [event[1] for event in events if event[0] == "text_delta"]
        assert "".join(delta or "" for delta in deltas) == "Final answer."
        assert all("draft" not in (delta or "") for delta in deltas)
        final = [event[1] for event in events if event[0] == "text_final"]
        assert final == ["Final answer."]
        await router.aclose()

    asyncio.run(scenario())


# --- delegation -----------------------------------------------------------


def test_delegation_ack_arrives_before_arguments_finish() -> None:
    items: list[bytes | float] = [
        _sse(_tool_frame(name="delegate_to_hermes", call_id="call-1")),
        0.3,
        _sse(_tool_frame(arguments='{"instruction":"Email Anna ')),
        0.1,
        _sse(
            _tool_frame(arguments='that the build is green."}'),
            _chunk({}, "tool_calls"),
            "[DONE]",
        ),
    ]
    router = _router(_streaming_transport(items))

    async def scenario() -> None:
        events = await _collect(router, "Email Anna that the build is green.")
        kinds = [event[0] for event in events]
        assert kinds[0] == "ack"
        assert events[0][1] == prompts.delegation_ack()
        assert "tool_call" in kinds
        ack_time = events[0][3]
        tool_call_time = next(event[3] for event in events if event[0] == "tool_call")
        assert ack_time < 0.2
        assert tool_call_time > ack_time + 0.2
        decision = next(event[2] for event in events if event[0] == "tool_call")
        assert decision is not None
        assert decision.tool == "delegate_to_hermes"
        assert decision.arguments["instruction"] == "Email Anna that the build is green."
        await router.aclose()

    asyncio.run(scenario())


def test_missing_instruction_is_repaired_to_utterance() -> None:
    frames = [
        _tool_frame(name="delegate_to_hermes", arguments="{}"),
        _chunk({}, "tool_calls"),
        "[DONE]",
    ]
    router = _router(_mock_transport(_sse(*frames)))

    async def scenario() -> None:
        decision = await router.decide("Run the tests.")
        assert decision.tool == "delegate_to_hermes"
        assert decision.arguments["instruction"] == "Run the tests."
        assert decision.repaired is True
        await router.aclose()

    asyncio.run(scenario())


def test_blank_instruction_is_repaired_to_utterance() -> None:
    frames = [
        _tool_frame(name="delegate_to_hermes", arguments='{"instruction":"   "}'),
        _chunk({}, "tool_calls"),
        "[DONE]",
    ]
    router = _router(_mock_transport(_sse(*frames)))

    async def scenario() -> None:
        decision = await router.decide("Summarise my inbox.")
        assert decision.arguments["instruction"] == "Summarise my inbox."
        assert decision.repaired is True
        await router.aclose()

    asyncio.run(scenario())


def test_decide_delegation_returns_before_stream_tail() -> None:
    items: list[bytes | float] = [
        _sse(
            _tool_frame(
                name="delegate_to_hermes",
                arguments='{"instruction":"Turn off the lights."}',
            ),
            _chunk({}, "tool_calls"),
        ),
        0.5,
        _sse("[DONE]"),
    ]
    router = _router(_streaming_transport(items))

    async def scenario() -> None:
        started = time.perf_counter()
        decision = await router.decide("Turn off the lights.")
        elapsed = time.perf_counter() - started
        assert decision.tool == "delegate_to_hermes"
        assert elapsed < 0.3
        await router.aclose()

    asyncio.run(scenario())


# --- attachments and end_call --------------------------------------------


def test_attach_file_ack_phrase() -> None:
    frames = [
        _tool_frame(name="attach_file", arguments='{"filename":"photo.jpg"}'),
        _chunk({}, "tool_calls"),
        "[DONE]",
    ]
    router = _router(_mock_transport(_sse(*frames)))

    async def scenario() -> None:
        events = await _collect(router, "Attach the photo I sent.", attachments_pending=True)
        assert events[0][0] == "ack"
        assert events[0][1] == prompts.attachment_ack()
        decision = next(event[2] for event in events if event[0] == "tool_call")
        assert decision is not None
        assert decision.arguments == {"filename": "photo.jpg"}
        await router.aclose()

    asyncio.run(scenario())


def test_end_call_farewell_phrase() -> None:
    frames = [
        _tool_frame(name="end_call", arguments="{}"),
        _chunk({}, "tool_calls"),
        "[DONE]",
    ]
    router = _router(_mock_transport(_sse(*frames)))

    async def scenario() -> None:
        events = await _collect(router, "That's everything.")
        assert events[0][0] == "ack"
        assert events[0][1] == prompts.end_call_farewell()
        decision = next(event[2] for event in events if event[0] == "tool_call")
        assert decision is not None
        assert decision.tool == "end_call"
        await router.aclose()

    asyncio.run(scenario())


# --- protocol errors ------------------------------------------------------


def test_unknown_tool_name_is_protocol_error() -> None:
    frames = [
        _tool_frame(name="send_email", arguments='{"to":"a@example.com"}'),
        _chunk({}, "tool_calls"),
        "[DONE]",
    ]
    router = _router(_mock_transport(_sse(*frames)))

    async def scenario() -> None:
        with pytest.raises(RouterProtocolError, match="unknown tool"):
            await router.decide("Email Anna.")
        await router.aclose()

    asyncio.run(scenario())


def test_malformed_arguments_are_protocol_error() -> None:
    frames = [
        _tool_frame(name="delegate_to_hermes", arguments='{"instruction":'),
        _chunk({}, "tool_calls"),
        "[DONE]",
    ]
    router = _router(_mock_transport(_sse(*frames)))

    async def scenario() -> None:
        with pytest.raises(RouterProtocolError, match="not valid JSON"):
            await router.decide("Do the thing.")
        await router.aclose()

    asyncio.run(scenario())


def test_oversized_arguments_are_protocol_error() -> None:
    oversized = '{"instruction":"' + "a" * (MAX_ARGUMENT_BYTES + 100) + '"}'
    frames = [
        _tool_frame(name="delegate_to_hermes", arguments=oversized),
        _chunk({}, "tool_calls"),
        "[DONE]",
    ]
    router = _router(_mock_transport(_sse(*frames)))

    async def scenario() -> None:
        with pytest.raises(RouterProtocolError, match="exceeded"):
            await router.decide("Do the thing.")
        await router.aclose()

    asyncio.run(scenario())


def test_content_only_falls_back_to_answer_direct() -> None:
    frames = [
        _chunk({"content": "Sure, **here** is a joke 🙂"}),
        _chunk({"content": " — </think>done."}),
        _chunk({}, "stop"),
        "[DONE]",
    ]
    router = _router(_mock_transport(_sse(*frames)))

    async def scenario() -> None:
        decision = await router.decide("Tell me a joke.")
        assert decision.tool == "answer_direct"
        assert decision.source == "model"
        assert decision.rule == "content_fallback"
        assert decision.repaired is True
        assert "**" not in decision.spoken_text
        assert "🙂" not in decision.spoken_text
        assert "</think>" not in decision.spoken_text
        assert "here is a joke" in decision.spoken_text
        await router.aclose()

    asyncio.run(scenario())


def test_no_tool_call_and_no_content_names_finish_reason() -> None:
    frames = [_chunk({}, "length"), "[DONE]"]
    router = _router(_mock_transport(_sse(*frames)))

    async def scenario() -> None:
        with pytest.raises(RouterProtocolError, match="length"):
            await router.decide("Hello.")
        await router.aclose()

    asyncio.run(scenario())


def test_empty_utterance_is_protocol_error() -> None:
    router = _router(_mock_transport(_sse(_chunk({}, "stop"))))

    async def scenario() -> None:
        with pytest.raises(RouterProtocolError, match="empty utterance"):
            await router.decide("   ")
        await router.aclose()

    asyncio.run(scenario())


# --- deferred exposure ----------------------------------------------------


def test_deferred_exposure_hides_attach_file_without_attachments() -> None:
    registry = ToolRegistry()
    deferred = registry.schemas(attachments_pending=False, deferred=True)
    assert [tool["function"]["name"] for tool in deferred] == [
        "delegate_to_hermes",
        "answer_direct",
        "end_call",
    ]
    with_attachment = registry.schemas(attachments_pending=True, deferred=True)
    assert [tool["function"]["name"] for tool in with_attachment] == [
        "delegate_to_hermes",
        "answer_direct",
        "attach_file",
        "end_call",
    ]
    eager = registry.schemas(attachments_pending=False, deferred=False)
    assert len(eager) == 4


def test_deferred_router_request_body_omits_attach_file() -> None:
    calls: list[httpx.Request] = []
    frames = [
        _tool_frame(name="answer_direct", arguments='{"text":"Hi."}'),
        _chunk({}, "tool_calls"),
    ]
    router = _router(_mock_transport(_sse(*frames, "[DONE]"), calls), deferred_tools=True)

    async def scenario() -> None:
        await router.decide("Hello.")
        body = json.loads(calls[0].content)
        names = [tool["function"]["name"] for tool in body["tools"]]
        assert "attach_file" not in names
        assert len(names) == 3
        await router.aclose()

    asyncio.run(scenario())


# --- transport failures ---------------------------------------------------


def test_unreachable_endpoint_is_router_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    router = _router(
        httpx.MockTransport(handler),
        first_token_timeout_s=0.5,
        request_timeout_s=1.0,
    )

    async def scenario() -> None:
        with pytest.raises(RouterUnavailable, match="VOICE_BRAIN_BASE_URL"):
            await router.decide("Email Anna.")
        await router.aclose()

    asyncio.run(scenario())


def test_server_error_is_router_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom", request=request)

    router = _router(httpx.MockTransport(handler))

    async def scenario() -> None:
        with pytest.raises(RouterUnavailable, match="VOICE_BRAIN_BASE_URL"):
            await router.decide("Email Anna.")
        await router.aclose()

    asyncio.run(scenario())


def test_client_error_stays_router_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="unsupported tool_choice", request=request)

    router = _router(httpx.MockTransport(handler))

    async def scenario() -> None:
        with pytest.raises(RouterProtocolError, match="HTTP 400"):
            await router.decide("Email Anna.")
        await router.aclose()

    asyncio.run(scenario())


def test_stalled_stream_is_router_timeout() -> None:
    async def stalled() -> AsyncIterator[bytes]:
        await asyncio.sleep(10)
        yield b""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=stalled(),
        )

    router = _router(
        httpx.MockTransport(handler),
        first_token_timeout_s=0.05,
        request_timeout_s=0.2,
    )

    async def scenario() -> None:
        with pytest.raises(RouterTimeout):
            await router.decide("Email Anna.")
        await router.aclose()

    asyncio.run(scenario())


def test_malformed_sse_frames_are_skipped() -> None:
    frames = [
        "{not json",
        _tool_frame(name="answer_direct", arguments='{"text":"Hi."}'),
        _chunk({}, "tool_calls"),
        "[DONE]",
    ]
    router = _router(_mock_transport(_sse(*frames)))

    async def scenario() -> None:
        decision = await router.decide("Hello.")
        assert decision.tool == "answer_direct"
        await router.aclose()

    asyncio.run(scenario())


# --- config ---------------------------------------------------------------


def test_config_rejects_bad_values() -> None:
    with pytest.raises(RouterConfigError):
        RouterConfig(base_url="ftp://127.0.0.1")
    with pytest.raises(RouterConfigError):
        RouterConfig(tool_choice="none")
    with pytest.raises(RouterConfigError):
        RouterConfig(first_token_timeout_s=0)
    with pytest.raises(RouterConfigError):
        RouterConfig(request_timeout_s=1.0, first_token_timeout_s=2.0)


def test_config_from_settings_reads_voice_brain_fields() -> None:
    class Settings:
        voice_brain_base_url = "http://127.0.0.1:18091"
        voice_brain_model = "lfm3-1.7b"
        voice_brain_temperature = 0.1
        voice_brain_max_tokens = 160
        voice_brain_tool_choice = "required"
        voice_brain_deferred_tools = True
        voice_brain_first_token_timeout_s = 2.0
        voice_brain_request_timeout_s = 20.0
        voice_brain_max_utterance_chars = 2000

    config = RouterConfig.from_settings(Settings())
    assert config.base_url == "http://127.0.0.1:18091"
    assert config.model == "lfm3-1.7b"
    assert config.deferred_tools is True
