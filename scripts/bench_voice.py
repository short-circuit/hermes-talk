#!/usr/bin/env python3
"""Benchmark a local voice-brain router candidate.

Runs the 20 scripted utterances in scripts/voice_bench_utterances.json against an
OpenAI-compatible endpoint (llama-server or LocalAI), measures first-token latency and
decode throughput client-side, and scores tool selection against the ground truth.

Stdlib only. Local-only by design: point --url at loopback or the inference host.

Examples:
    python3 scripts/bench_voice.py --url http://127.0.0.1:8080 --model lfm3-1.7b --no-think
    python3 scripts/bench_voice.py --url http://127.0.0.1:8080 --model lfm3-audio-2b --mode audio \
        --audio-dir /path/to/wavs
"""

from __future__ import annotations

import argparse
import base64
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SYSTEM_PROMPT = (
    "You are the voice brain of a hands-free assistant. You never do work yourself. "
    "Choose exactly one tool per turn and never reply in plain text.\n"
    "- delegate_to_hermes: anything needing tools, files, memory, live data (weather, calendar, "
    "email, web), device actions (lights, printer), or multi-step work. If the answer needs data "
    "you do not already hold, delegate instead of apologising.\n"
    "- answer_direct: small talk, greetings, opinions, jokes, the current time and date, and "
    "arithmetic on numbers you already know.\n"
    "- attach_file: when the user refers to a file they just sent.\n"
    "- end_call: when the user signals they are finished.\n"
    "Today is {date}. It is {time} local time."
)


def load_cases(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def build_payload(args, system_prompt: str, utterance: dict, audio_b64: str | None) -> dict:
    if audio_b64 is not None:
        content = [
            {"type": "input_audio", "input_audio": {"data": audio_b64, "format": "wav"}},
        ]
    else:
        content = utterance["text"]
    payload = {
        "model": args.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "tools": args.tools,
        "tool_choice": args.tool_choice,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "stream": not args.no_stream,
        "stream_options": {"include_usage": True},
    }
    if args.no_think:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    return payload


def run_case(url: str, payload: dict, timeout: float) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    first_token = None
    chunks = 0
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    usage: dict | None = None
    timings: dict | None = None
    error = None
    streaming = payload.get("stream", True)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if not streaming:
                whole = resp.read()
                event = json.loads(whole)
                message = (event.get("choices") or [{}])[0].get("message") or {}
                text_parts.append(message.get("content") or "")
                for call in message.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    tool_calls.append({"name": fn.get("name") or "", "arguments": fn.get("arguments") or ""})
                usage = event.get("usage")
                timings = event.get("timings")
            else:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if event.get("usage"):
                        usage = event["usage"]
                    if event.get("timings"):
                        timings = event["timings"]
                    for choice in event.get("choices", []):
                        delta = choice.get("delta", {})
                        piece = delta.get("content")
                        if piece:
                            if first_token is None:
                                first_token = time.perf_counter()
                            chunks += 1
                            text_parts.append(piece)
                        for call in delta.get("tool_calls") or []:
                            if first_token is None:
                                first_token = time.perf_counter()
                            idx = call.get("index", 0)
                            while len(tool_calls) <= idx:
                                tool_calls.append({"name": "", "arguments": ""})
                            fn = call.get("function") or {}
                            tool_calls[idx]["name"] += fn.get("name") or ""
                            tool_calls[idx]["arguments"] += fn.get("arguments") or ""
    except urllib.error.HTTPError as exc:
        error = f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:400]}"
    except Exception as exc:  # noqa: BLE001 - benchmark harness reports, never raises
        error = f"{type(exc).__name__}: {exc}"
    ended = time.perf_counter()

    ttft = (first_token - started) if first_token else None
    decode = (ended - first_token) if first_token else None
    tokens = (usage or {}).get("completion_tokens")
    tps = None
    if tokens and decode and decode > 0:
        tps = max(tokens - 1, 0) / decode if tokens > 1 else None
    elif tokens and not streaming and tokens > 1:
        # non-streaming backend: throughput over the whole request is the only honest figure
        tps = (tokens - 1) / (ended - started)
    return {
        "ttft_s": ttft,
        "total_s": ended - started,
        "completion_tokens": tokens,
        "tokens_per_s": tps,
        "text": "".join(text_parts).strip(),
        "tool_calls": tool_calls,
        "server_timings": timings,
        "error": error,
    }


def score(case: dict, result: dict) -> dict:
    expected = case["expect"]
    got = result["tool_calls"][0]["name"].strip() if result["tool_calls"] else None
    args_ok = None
    if result["tool_calls"]:
        raw_args = result["tool_calls"][0]["arguments"]
        try:
            parsed = json.loads(raw_args) if raw_args.strip() else {}
            args_ok = isinstance(parsed, dict)
        except json.JSONDecodeError:
            args_ok = False
    return {
        "id": case["id"],
        "utterance": case["text"],
        "expected": expected,
        "selected": got,
        "correct": got == expected,
        "args_valid": args_ok,
        "no_tool_call": got is None,
        "ttft_s": result["ttft_s"],
        "total_s": result["total_s"],
        "tokens_per_s": result["tokens_per_s"],
        "completion_tokens": result["completion_tokens"],
        "reply": result["text"][:160],
        "error": result["error"],
    }


def summarise(rows: list[dict]) -> dict:
    valid = [r for r in rows if not r["error"]]
    correct = sum(1 for r in valid if r["correct"])
    ttfts = [r["ttft_s"] for r in valid if r["ttft_s"] is not None]
    tps = [r["tokens_per_s"] for r in valid if r["tokens_per_s"]]
    totals = [r["total_s"] for r in valid if r["total_s"]]
    return {
        "cases": len(rows),
        "scored": len(valid),
        "errors": len(rows) - len(valid),
        "tool_accuracy": round(correct / len(valid), 3) if valid else None,
        "correct": correct,
        "wrong": len(valid) - correct,
        "ttft_p50_s": round(statistics.median(ttfts), 3) if ttfts else None,
        "ttft_min_s": round(min(ttfts), 3) if ttfts else None,
        "ttft_max_s": round(max(ttfts), 3) if ttfts else None,
        "tokens_per_s_p50": round(statistics.median(tps), 2) if tps else None,
        "total_p50_s": round(statistics.median(totals), 3) if totals else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", choices=("text", "audio"), default="text")
    parser.add_argument("--audio-dir", type=Path, help="directory with <id>.wav files for --mode audio")
    parser.add_argument("--cases", type=Path, default=HERE / "voice_bench_utterances.json")
    parser.add_argument("--no-think", action="store_true", help="disable model reasoning (chat template)")
    parser.add_argument("--tool-choice", default="required", choices=("auto", "required", "none"),
                        help="force a tool call (default) or let the model answer in text")
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--no-stream", action="store_true",
                        help="non-streaming request (for backends that emit no SSE deltas)")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    cases = load_cases(args.cases)
    args.tools = cases["tools"]
    now = time.localtime()
    system_prompt = SYSTEM_PROMPT.format(
        date=time.strftime("%A %d %B %Y", now), time=time.strftime("%H:%M", now)
    )

    rows = []
    for case in cases["utterances"]:
        audio_b64 = None
        if args.mode == "audio":
            if not args.audio_dir:
                parser.error("--mode audio requires --audio-dir")
            wav = args.audio_dir / f"{case['id']:02d}.wav"
            if not wav.exists():
                print(f"missing audio for case {case['id']}: {wav}", file=sys.stderr)
                continue
            audio_b64 = base64.b64encode(wav.read_bytes()).decode()
        payload = build_payload(args, system_prompt, case, audio_b64)
        result = run_case(args.url, payload, args.timeout)
        row = score(case, result)
        rows.append(row)
        mark = "ok " if row["correct"] else ("ERR" if row["error"] else "MISS")
        ttft = row["ttft_s"]
        ttft_s = "n/a" if ttft is None else f"{ttft:.3f}s"
        print(
            f"{mark} {row['id']:>2} expected={row['expected']:<17} got={str(row['selected']):<17}"
            f" ttft={ttft_s} total={row['total_s']:.2f}s tok/s={row['tokens_per_s']}"
        )

    summary = summarise(rows)
    print(json.dumps(summary, indent=2))
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps({"model": args.model, "url": args.url, "mode": args.mode,
                        "summary": summary, "rows": rows}, indent=2),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
