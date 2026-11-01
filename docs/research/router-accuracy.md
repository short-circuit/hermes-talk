# Voice-brain router accuracy: LFM3-1.7B with the shipped four-tool router

Measured benchmark for the router card: the relay's `relay.brain.router` class, the prompt rendered by
`relay.brain.prompts.router_system_prompt`, and the tool schema in `relay/brain/tools.json`, replayed
against a live loopback `llama-server` for `LFM3-1.7B`. Every number below was produced on this host
with the shipped code; none are copied from a model card.

**Date of run:** 2026-11-01, 21:49 CET. **Endpoint:** `http://127.0.0.1:18091`, llama.cpp
0.8.0 (`llama-server --jinja`), OpenAI-compatible `/v1/chat/completions`, `tool_choice: "required"`,
`chat_template_kwargs: {"enable_thinking": false}`, temperature 0.1, `max_tokens` 160.

## What was measured

- **Tool accuracy** on the 20 scripted utterances in `scripts/voice_bench_utterances.json`, using the
  real `VoiceBrainRouter.decide()` path, the shipped prompt, and the shipped schema.
- **Policy/model split**: how many turns were settled by the deterministic `DirectAnswerPolicy`
  (time, date, arithmetic, presence, farewell) without any HTTP traffic, versus routed by the model.
- **First-token latency (TTFT)** for model-routed turns: request start to the first emitted event.
- **Delegation-ack latency**: request start to the spoken `ack` event for `delegate_to_hermes`, which
  is emitted as soon as the tool name is known and before the argument stream finishes. No Hermes call
  is awaited in the router.
- **Total latency** per turn.
- A second run with `--deferred`, which omits `attach_file` unless an attachment is pending.

## Results

| Mode | Accuracy | Correct | Policy paths | Model paths | Model TTFT p50 / p95 | Delegation-ack p50 / p95 | Total p50 / p95 |
|---|---|---|---|---|---|---|---|
| eager (four tools always exposed) | 20/20 = 100 % | 20 | 5 | 15 | 55.693 ms / 207.756 ms | 130.617 ms / 207.805 ms | 99.901 ms / 256.695 ms |
| deferred (conditional `attach_file`) | 20/20 = 100 % | 20 | 5 | 15 | 77.491 ms / 184.395 ms | 98.793 ms / 190.805 ms | 97.383 ms / 218.421 ms |

**Misses: none in either run.** Every case selected the expected tool. The model-routed cases were
eight delegations, five direct answers (small talk/general knowledge), and three attachments. The five
policy paths were `time`, `arithmetic`, `presence`, and two `farewell` turns; the policy made no HTTP
request for them.

Per-case detail for the eager run (`ttft` is milliseconds; `0.0` means the policy answered locally):

| id | utterance | expected | selected | source | ttft |
|---|---|---|---|---|---|
| 1 | What's the weather in Zurich tomorrow? | delegate_to_hermes | delegate_to_hermes | model | 207.855 |
| 2 | Email Anna that the build is green. | delegate_to_hermes | delegate_to_hermes | model | 54.488 |
| 3 | What's on my calendar this afternoon? | delegate_to_hermes | delegate_to_hermes | model | 127.553 |
| 4 | Search the web for the latest graphics card prices. | delegate_to_hermes | delegate_to_hermes | model | 207.713 |
| 5 | Run the tests in the relay repo and tell me if they pass. | delegate_to_hermes | delegate_to_hermes | model | 55.693 |
| 6 | Add milk to the shopping list. | delegate_to_hermes | delegate_to_hermes | model | 52.349 |
| 7 | Summarise my last three emails. | delegate_to_hermes | delegate_to_hermes | model | 147.631 |
| 8 | Turn off the lights in the kitchen. | delegate_to_hermes | delegate_to_hermes | model | 133.680 |
| 9 | Hey, how are you? | answer_direct | answer_direct | model | 172.627 |
| 10 | What time is it? | answer_direct | answer_direct | policy | 0.0 |
| 11 | What's seventeen times four? | answer_direct | answer_direct | policy | 0.0 |
| 12 | Tell me a joke. | answer_direct | answer_direct | model | 53.541 |
| 13 | Good morning! | answer_direct | answer_direct | model | 169.162 |
| 14 | What's the capital of France? | answer_direct | answer_direct | model | 53.549 |
| 15 | Can you hear me? | answer_direct | answer_direct | policy | 0.0 |
| 16 | I just sent you a photo, attach it to this call. | attach_file | attach_file | model | 42.972 |
| 17 | Attach the PDF I uploaded a second ago. | attach_file | attach_file | model | 43.285 |
| 18 | Here's the file, please attach it. | attach_file | attach_file | model | 42.442 |
| 19 | Thanks, that's all for now. | end_call | end_call | policy | 0.0 |
| 20 | Bye, talk later. | end_call | end_call | policy | 0.0 |

## Prompt and schema identity

The rendered prompt is the measured four-tool policy plus the containment rule. Because the clock line
is substituted per request, its SHA-256 changes with the minute. Both benchmark runs completed within
21:49 CET; the rendered prompt for that minute hashes to:

- `6b7b6338fd413428d3411daece546f3a44df2429c233d2da379913658c27b6b3`

`relay/brain/tools.json` is byte-identical in content to the `tools` array in
`scripts/voice_bench_utterances.json`; a test asserts that equality.

## Deferred-mode note

llama.cpp 0.8.0 has no `defer_loading` or tool-search support. In this codebase `--deferred` therefore
means **conditional per-request exposure**: `attach_file` is left out of the request when no attachment
is pending, and included for the three attachment cases (the benchmark marks those cases as pending).
The tool list is otherwise unchanged and never leaves `TOOL_ALLOWLIST`. Accuracy was identical in this
single deferred run; the latency difference is within run-to-run noise.

## Contention conditions

Measurements were taken with the desktop active, not on an idle card. At 21:49 CET `nvidia-smi`
reported 5 211 MiB used of 24 564 MiB on an RTX 4090 (28 % utilisation), with these compute processes
resident:

- `llama-server` for `LFM3-1.7B`: 1 678 MiB (the router endpoint used here);
- ComfyUI (idle): 1 724 MiB;
- Sunshine: 399 MiB;
- a CUDA 13 llama.cpp backend loader: 486 MiB.

The host load average was 2.08 / 2.32 / 2.37 on 32 CPUs. TTFT and throughput therefore include
desktop/ComfyUI contention rather than a clean-room figure.

## Caveats

- The set is 20 scripted utterances; one case is 5 percentage points. A single run at temperature 0.1
  can move by a case or two. Treat 20/20 here as "at or above the bar on this set", not as a
  population estimate.
- These are text-in results. The router is fed by STT in production, so recognition errors are not
  represented here.
- The policy rules are deterministic and unit-tested; the benchmark exercises `time`, `arithmetic`,
  `presence`, and `farewell`, but not the `date` rule (no date utterance in the set).
- The deferred comparison is one run per mode; the two modes were not interleaved, so the latency
  difference should not be read as a causal effect of deferral.
- Delegation-ack latency measures the router's `ack` event, not Hermes's work; the router returns the
  decision without waiting for Hermes.

## Reproduction

From the repository root:

```bash
./.venv/bin/python scripts/bench_router.py \
  --url http://127.0.0.1:18091 --model lfm3-1.7b \
  --json-out router-bench.json
./.venv/bin/python scripts/bench_router.py \
  --url http://127.0.0.1:18091 --model lfm3-1.7b --deferred \
  --json-out router-bench-deferred.json
```

The benchmark exits non-zero when accuracy is below `--min-accuracy` (default 0.9). Offline tests for
the router, including SSE parsing, split escapes, repair rules, deferred exposure, and timeouts:

```bash
cd relay && ../.venv/bin/pytest tests/test_brain_router.py -q
```
