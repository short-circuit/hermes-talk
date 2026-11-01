#!/usr/bin/env python3
"""Benchmark the shipped voice-brain router against a live local endpoint.

Replays the 20 scripted utterances in ``scripts/voice_bench_utterances.json``
through :class:`relay.brain.router.VoiceBrainRouter` (the real class the relay
uses) and scores the selected tool against the ground truth.  It reports
accuracy, the policy/model split, first-token latency, delegation-ack latency
and total latency.

Local-only: point ``--url`` at a loopback OpenAI-compatible ``llama-server``.
The router itself only ever talks to that endpoint.

Examples:
    python scripts/bench_router.py --url http://127.0.0.1:8080 --model lfm3-1.7b
    python scripts/bench_router.py --url http://127.0.0.1:18091 --model lfm3-1.7b \
        --deferred --json-out router-bench.json --min-accuracy 0.9
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from relay.brain.router import (
    RouterConfig,
    RouterError,
    VoiceBrainRouter,
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * fraction
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def _latency(values: list[float]) -> dict[str, float | None]:
    return {
        "p50": None if not values else round(statistics.median(values), 3),
        "p95": None if not values else round(_percentile(values, 0.95) or 0.0, 3),
        "min": None if not values else round(min(values), 3),
        "max": None if not values else round(max(values), 3),
        "n": len(values),
    }


async def _run(args: argparse.Namespace, cases: dict) -> dict:
    config = RouterConfig(
        base_url=args.url,
        model=args.model,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        tool_choice=args.tool_choice,
        deferred_tools=args.deferred,
        first_token_timeout_s=args.first_token_timeout,
        request_timeout_s=args.timeout,
        max_utterance_chars=2000,
    )
    router = VoiceBrainRouter(config)
    rows: list[dict] = []
    try:
        for case in cases["utterances"]:
            row: dict = {
                "id": case["id"],
                "utterance": case["text"],
                "expected": case["expect"],
                "selected": None,
                "correct": False,
                "source": None,
                "rule": None,
                "repaired": None,
                "ttft_ms": None,
                "total_ms": None,
                "ack_ms": None,
                "arguments": None,
                "error": None,
            }
            try:
                decision = await router.decide(
                    case["text"],
                    attachments_pending=case["expect"] == "attach_file",
                )
                row.update(
                    {
                        "selected": decision.tool,
                        "correct": decision.tool == case["expect"],
                        "source": decision.source,
                        "rule": decision.rule,
                        "repaired": decision.repaired,
                        "ttft_ms": decision.ttft_ms,
                        "total_ms": decision.total_ms,
                        "arguments": dict(decision.arguments),
                    }
                )
                if decision.tool == "delegate_to_hermes":
                    row["ack_ms"] = decision.ttft_ms
            except RouterError as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
            rows.append(row)
    finally:
        await router.aclose()

    valid = [row for row in rows if row["error"] is None]
    correct = sum(1 for row in valid if row["correct"])
    accuracy = correct / len(rows) if rows else 0.0
    model_rows = [row for row in valid if row["source"] == "model"]
    policy_rows = [row for row in valid if row["source"] == "policy"]
    delegation_rows = [
        row
        for row in valid
        if row["selected"] == "delegate_to_hermes" and row["source"] == "model"
    ]
    ttft = [float(row["ttft_ms"]) for row in model_rows if row["ttft_ms"] is not None]
    ack = [float(row["ack_ms"]) for row in delegation_rows if row["ack_ms"] is not None]
    total = [float(row["total_ms"]) for row in valid if row["total_ms"] is not None]
    misses = [
        {
            "id": row["id"],
            "utterance": row["utterance"],
            "expected": row["expected"],
            "selected": row["selected"],
            "source": row["source"],
            "rule": row["rule"],
        }
        for row in rows
        if row["error"] is not None or not row["correct"]
    ]
    return {
        "model": args.model,
        "url": args.url,
        "deferred": args.deferred,
        "tool_choice": args.tool_choice,
        "cases": len(rows),
        "scored": len(valid),
        "errors": len(rows) - len(valid),
        "correct": correct,
        "accuracy": round(accuracy, 3),
        "misses": misses,
        "policy_cases": len(policy_rows),
        "model_cases": len(model_rows),
        "policy_rules": sorted({row["rule"] for row in policy_rows if row["rule"]}),
        "ttft_ms": _latency(ttft),
        "delegation_ack_ms": _latency(ack),
        "total_ms": _latency(total),
        "rows": rows,
    }


def _print_rows(rows: list[dict]) -> None:
    for row in rows:
        mark = "ok " if row["correct"] else ("ERR" if row["error"] else "MISS")
        ttft = "n/a" if row["ttft_ms"] is None else f"{row['ttft_ms']:.1f}ms"
        total = "n/a" if row["total_ms"] is None else f"{row['total_ms']:.1f}ms"
        print(
            f"{mark} {row['id']:>2} expected={row['expected']:<17} "
            f"got={row['selected']!s:<17} source={row['source']!s:<7} "
            f"ttft={ttft:<9} total={total}"
        )
        if row["error"]:
            print(f"     error: {row['error']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--model", default="lfm3-1.7b")
    parser.add_argument(
        "--deferred",
        action="store_true",
        help="expose attach_file only when an attachment is pending",
    )
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--min-accuracy", type=float, default=0.9)
    parser.add_argument(
        "--cases", type=Path, default=HERE / "voice_bench_utterances.json"
    )
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--max-tokens", type=int, default=160)
    parser.add_argument(
        "--tool-choice", default="required", choices=("required", "auto")
    )
    parser.add_argument("--first-token-timeout", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    host = urlsplit(args.url).hostname
    if host not in _LOOPBACK_HOSTS:
        print(
            f"warning: --url host {host!r} is not loopback; this benchmark is local-only",
            file=sys.stderr,
        )

    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    summary = asyncio.run(_run(args, cases))
    _print_rows(summary["rows"])

    print()
    print(
        f"accuracy: {summary['correct']}/{summary['cases']} = {summary['accuracy']:.1%}"
    )
    print(
        f"policy paths: {summary['policy_cases']} "
        f"(rules: {', '.join(summary['policy_rules']) or 'none'}); "
        f"model paths: {summary['model_cases']}"
    )
    ttft = summary["ttft_ms"]
    ack = summary["delegation_ack_ms"]
    total = summary["total_ms"]
    print(f"model TTFT p50/p95: {ttft['p50']}ms / {ttft['p95']}ms (n={ttft['n']})")
    print(f"delegation-ack p50/p95: {ack['p50']}ms / {ack['p95']}ms (n={ack['n']})")
    print(f"total p50/p95: {total['p50']}ms / {total['p95']}ms (n={total['n']})")
    if summary["misses"]:
        print("misses:")
        for miss in summary["misses"]:
            print(
                f"  {miss['id']:>2} {miss['expected']} -> {miss['selected']} "
                f"(source={miss['source']})"
            )

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(summary, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"wrote {args.json_out}")

    return 0 if summary["accuracy"] >= args.min_accuracy else 1


if __name__ == "__main__":
    raise SystemExit(main())
