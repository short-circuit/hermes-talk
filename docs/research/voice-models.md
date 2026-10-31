# Voice-brain candidates: measured on the relay host

Benchmark run for the P0 "model benchmark" exit criterion in `docs/ROADMAP.md`. Every number below was
produced on the relay host, not copied from a vendor page. Licences are quoted from the model cards,
because they are not performance claims.

**Date of run:** 2026-10-31. **Hardware:** single RTX 4090, 24 GB VRAM (24 564 MiB), driver 615.78.08,
CUDA 13.4.

## What was measured

- **Tool-call accuracy** on the 20 scripted utterances in `scripts/voice_bench_utterances.json`, against the
  four tools defined in `docs/ARCHITECTURE.md` (`delegate_to_hermes`, `answer_direct`, `attach_file`,
  `end_call`). One system prompt, identical for every model, temperature 0.1, `tool_choice: required`
  (a router must always pick a tool), thinking disabled where the template supports it.
- **First-token latency (TTFT)** as seen by a loopback client: request → first streamed delta (text or tool
  call). This is the latency the relay would experience.
- **Decode throughput** from the server's own token accounting over the same requests.
- **VRAM footprint** per model process, read from `nvidia-smi` while the model was resident and serving.
- **Moshi** is measured differently: it has no tool calling, so the meaningful figure is the full-duplex
  frame latency against its 80 ms frame budget.

The benchmark harness is `scripts/bench_voice.py` (stdlib only); the runner used for the isolated per-model
pass starts one server at a time, records VRAM, runs the 20 utterances, then stops the server.

Measurements were taken while the GPU was also serving the desktop, ComfyUI (idle, 1 722 MiB) and a game
(~9.6 GB) — that is the realistic contention case for this host, not a clean-room figure. TTFT and
throughput therefore sit slightly below what the same models reach on an idle card.

## Results

| Candidate | Runtime | VRAM (MiB) | TTFT p50 | Decode tok/s p50 | Tool accuracy, text in | Tool accuracy, audio in | Licence |
|---|---|---|---|---|---|---|---|
| `LFM3-1.7B` Q4_K_M | llama.cpp 0.8.0 (CUDA) | 1 664 | 35–45 ms across repeats | 212–390 across repeats | 18/20 = 90 % (17/20 = 85 % second run; 10/20 with `tool_choice: auto`) | n/a | LFM Open License v1.0 |
| `LFM2-1.2B-Tool` Q4_K_M | llama.cpp 0.8.0 (CUDA) | 1 302 | 30 ms | 861 | 8/20 = 40 % (`tool_choice: auto`: 7/20 = 35 %) | n/a | LFM Open License v1.0 |
| `LFM3-Audio-2B` Q4_K_M + Q8 audio projector | llama.cpp 0.8.0 (CUDA) | 2 376 | 44 ms text / 61 ms audio | 528 text / 497 audio | 16/20 = 80 % | 16/20 = 80 % | LFM Open License v1.0 |
| `LFM2.5-Audio-1.5B` Q8 + Q8 projector | llama.cpp 0.8.0 (CUDA) | 2 234 | 15 ms text / 28 ms audio | 349 | 0/20 | 0/20 | LFM Open License v1.0 |
| `lfm2.5-audio-1.5b-realtime` (as deployed) | LocalAI 4.8.2, `liquid-audio` backend, f16, s2s | 6 562 | not measurable: backend emits no SSE deltas | n/a | 0/20 | n/a | LFM Open License v1.0 |
| `moshiko` q8 (Moshi reference) | PyTorch, full-duplex | 9 530 (9.65 GiB reserved) | 767 ms cold first frame | 9.16 frames/s (80 ms/frame budget = 12.5 frames/s) | no tool calling | n/a | weights CC-BY-4.0 |

Accuracy rows are single runs of 20 cases; at temperature 0.1 a repeat run moves by one or two cases
(±5–10 points), which is the resolution of a 20-utterance set. Treat 90 % and 85 % as the same model
performing at the edge of the acceptance threshold, not as a difference between them.

### Per-candidate notes

**`LFM3-1.7B` — usable router.** With `tool_choice: auto` it answers in plain text instead of calling a tool on
half the set (10/20 with the same prompt), which is exactly the failure mode the relay cannot tolerate: the
text never reaches the delegation path. Forcing `required` fixes the format and lifts it to 85–90 %. Remaining
misses are judgement errors ("Bye, talk later" answered instead of `end_call`), not format errors. Tool-call
parsing through llama.cpp's LFM3 template worked without a custom grammar.

**`LFM2-1.2B-Tool` — too weak for four tools.** It collapses to `delegate_to_hermes` for nearly every turn,
including greetings and arithmetic, and sometimes emits prose with no tool call at all. It is fast
(861 tok/s, 30 ms) and small (1.3 GB), but it does not meet the router bar. Note the naming: the HF repo is
`LiquidAI/LFM2-1.2B-Tool`; there is no `LFM2.5-1.2B-Tool` (the LFM2.5 1.2B line is Instruct/Thinking only),
so the closest existing model was used.

**`LFM3-Audio-2B` — the only audio-native candidate that calls tools.** It accepts speech directly and emits
tool calls; 80 % on audio input, the same 80 % on text input, so the misses are routing judgement, not
transcription. llama.cpp flags audio input as experimental, and the projector adds ~1 GB. With espeak-ng
synthetic speech (16 kHz mono) verbatim transcription was correct on 3 of 4 probes; the fourth
("What's the weather in Zurich tomorrow?") came back paraphrased, which is a synthetic-voice artefact worth
re-testing with real recordings before drawing conclusions about ASR quality.

**`LFM2.5-Audio-1.5B` — no tool calling at all.** Under llama.cpp it never emits a tool call: it replies
"*I'm sorry, I don't have a tool to …*" in speech or text. The model sees the tool list but is not trained to
act on it. The deployed LocalAI endpoint behaves the same way and additionally does not stream (empty SSE
deltas, so TTFT cannot be measured there; median non-streamed request time was 151 ms). It is a good
speech-to-speech conversational model, not a router.

**Moshi — reference only.** Full-duplex, no tool calling, 9.5 GB resident. Measured steady-state 104 ms per
frame against an 80 ms budget (9.16 frames/s), i.e. it does not sustain real time in this configuration.
Caveat: CUDA-graph capture did not fit in the VRAM left by the game and ComfyUI, so the run used
`NO_CUDA_GRAPH=1`, which costs a few ms per frame. The model also needed float32 (the q8 checkpoint rejects
bf16) and could not be loaded alongside a game holding ~10 GB.

## Supporting measurements

| Measurement | Value |
|---|---|
| ComfyUI resident, idle | 1 722 MiB |
| ComfyUI peak process VRAM, 1024² SDXL, 8 steps | 6 842 MiB (GPU total 23 944 MiB with a game resident) |
| `qwen3-tts-cpp-1.7b-base` backend resident | 3 758 MiB |
| `qwen3-tts-cpp-1.7b-base` `/tts`, one short sentence | 0.60–0.68 s total, non-streaming endpoint |
| `whisper-1` transcription of a 3.3 s utterance | correct text; no persistent GPU process (CPU backend) |

The TTS and STT figures are context for the latency budget below; the authoritative speech benchmarks are
`docs/research/speech-latency.md` (owned by the speech-I/O task).

## Path A (audio-native) vs Path B (cascade)

| | Path A: `LFM3-Audio-2B` alone | Path B: `LFM3-1.7B` + separate STT/TTS |
|---|---|---|
| VRAM | 2.4 GB | 1.7 GB router + 3.8 GB TTS (measured) + STT (CPU backend measured, ~0 GPU) ≈ 5.5 GB |
| Decision latency | 61 ms first token on speech in | STT final transcript (target 300 ms, see speech task) + 37 ms router |
| Spoken output | generated by the same model | + 0.60 s non-streaming TTS measured; streaming adapter target is 250 ms |
| Tool accuracy | 80 % | 85–90 % |
| Failure mode | routing judgement errors, experimental audio path | transcription errors feed the router; two models to keep in sync |
| Extra moving parts | one model, one process | three adapters, format conversion, backpressure |

**Recommendation: Path B, with `LFM3-1.7B` as the router.** The router acceptance criterion is 90 % tool
selection; the audio-native model sits at 80 %, and its audio input path is still marked experimental in
llama.cpp. The cascade meets the accuracy bar, keeps the router testable as text, and — because STT here runs
on CPU — costs less VRAM than Path A once TTS is included only if the router is the only GPU resident
component. Latency is the price: Path A is ~600 ms ahead once the measured non-streaming TTS is added.

`LFM3-1.7B` at 85–90 % is itself marginal against the 90 % gate. Two cheap mitigations before the router task
starts: keep `tool_choice: required` (same prompt, it moves the model from 50 % to 85–90 %), and score two
samples per utterance and take the agreement, which removes the single-sample noise at the cost of ~2× decode
time (still ~80 ms for a tool call).

Revisit Path A when either (a) `LFM3-Audio-2B` clears 90 % on real recorded speech rather than synthetic
audio, or (b) barge-in and sub-300 ms acknowledgement become a product requirement — full-duplex audio-native
is the only design that gets there without a TTS round trip. Moshi is a useful reference for that future, not
a candidate now: no tool calling and 9.5 GB.

## VRAM budget with image generation on the same GPU

Card capacity 24 564 MiB. Measured residents:

| Component | Idle | Peak measured |
|---|---|---|
| Desktop (Xorg, compositor, remote-desktop helper) | ~600 MiB | ~600 MiB |
| ComfyUI | 1 722 MiB | 6 842 MiB during a 1024² SDXL generation |
| Path B voice stack | ~5 500 MiB | ~5 500 MiB (static after load) |
| Path A voice stack | 2 376 MiB | 2 376 MiB |

- **ComfyUI + Path B, no game:** 6 842 + 600 + 5 500 ≈ 12.9 GB, leaving ~11.6 GB headroom. Comfortable.
- **ComfyUI + Path A, no game:** ≈ 9.8 GB, ~14.7 GB headroom.
- **ComfyUI + Path B + a game:** the measured game footprint was 9.6 GB, so 6 842 + 600 + 5 500 + 9 600 ≈
  22.5 GB — fits, but a generation that spikes pushes the card to its limit (a measured SDXL run reached
  23.9 GB with the game resident, 250 MiB free). Anything else wanting VRAM at that moment fails.
- **Moshi anywhere in that mix:** 9.5 GB resident does not fit next to a game and ComfyUI; the load failed
  with 107 MiB free until ComfyUI's models were unloaded.

Practical guidance: budget 7 GB for ComfyUI peak, 6 GB for Path B (router + TTS), and treat the remaining
~11 GB as the margin that a game or a second generation will eat. Path A halves the voice-side footprint and
is the safer choice when image generation must run concurrently with a call.

## Reproducing

```bash
# llama.cpp 0.8.0+ built with CUDA; LFM3 needs a recent build (audio support is experimental)
scripts/bench_voice.py --url http://127.0.0.1:8080 --model lfm3-1.7b --no-think
scripts/bench_voice.py --url http://127.0.0.1:8080 --model lfm3-audio-2b --no-think \
    --mode audio --audio-dir <dir with 01.wav … 20.wav>
```

`--tool-choice auto` reproduces the "answers in plain text" failure mode; `--no-stream` is for backends that
emit no SSE deltas. Wavs for the audio runs were synthesised locally with `espeak-ng` at 16 kHz mono; the
relay will feed real microphone audio, so re-run the audio rows against recorded speech before locking the
design.

## Caveats

- One run per model, 20 utterances: ±1 case is 5 points. Ranking within 5 points is not meaningful.
- Numbers taken with a game and ComfyUI resident; an idle card will be faster.
- The LocalAI `liquid-audio` deployment of `LFM2.5-Audio` is f16 and 6.5 GB, roughly 3× the llama.cpp Q8
  footprint for the same model, and it does not stream.
- Audio input in llama.cpp is marked experimental; LFM3-Audio output audio was not exercised here (only the
  tool-call and text paths), so first-audio latency for Path A is not yet measured.
