# End-to-end voice pipeline latency

Measured, not estimated. `relay/tests/bench_latency.py` replays the 20 scripted
utterances in `scripts/voice_bench_utterances.json` through the shipped pipeline
and reports p50/p95 per stage and end-to-end, with CPU and GPU sampling during
the run. Two consecutive runs are reported below, including the rerun comparison
that the harness produces from its own JSON reports.

## Verdict against the acceptance criterion

| Run | Perceived response p50 | Perceived response p95 | Criterion (p50 < 1.2 s) |
| --- | --- | --- | --- |
| 1 | 1050.8 ms | 1165.0 ms | met |
| 2 | 1054.3 ms | 1156.0 ms | met |
| 3 | 1042.2 ms | 1147.5 ms | met |

Perceived response is measured from the moment the user stops speaking to the
first Opus packet leaving the pipeline, including the end-of-turn silence window
the shipped policy waits for before it commits a turn. The median is inside the
1.2 s budget on this host under the contention described below; the p95 tail is
not, and the margin is roughly 150 ms, so it is not a comfortable pass.

## Method

Every utterance goes through the real classes, in the order a live call would:

1. The scripted text is rendered to speech with the local Kokoro engine
   (24 kHz PCM16). This stands in for the user's microphone; the audio is not
   paced, because the measurement starts at end-of-speech.
2. `relay.speech.stt.SherpaStreamingStt` decodes 20 ms frames of that audio and
   reports the final transcript on flush.
3. `relay.brain.router.VoiceBrainRouter` routes the transcript against the local
   llama.cpp endpoint over the OpenAI chat-completions API and streams the ack
   or the direct answer.
4. `relay.speech.tts.StreamingTts` synthesises and encodes the spoken text to
   48 kHz Opus as the router streams it, so the TTS work overlaps the router
   work instead of following it.

The harness paces the trailing silence at real time: the shipped end-of-turn
policy commits after 600 ms of silence (`EndOfTurnConfig.silence_commit_ms`), and
that wait is wall-clock time the user experiences. Feeding the same 600 ms of
audio instantly would have understated the perceived response by about 570 ms.

Stages are timestamped with `time.perf_counter` at: end of speech, turn commit,
final transcript, first spoken text, first Opus packet, last Opus packet. The
harness also computes word error rate of the STT transcript against the scripted
text and whether the routed tool matches the expected one.

## Environment and GPU contention

| Item | Value |
| --- | --- |
| Host | i9-13900K, 24 cores / 32 threads, 62 GB RAM |
| GPU | RTX 4090, 24 GB, driver 615.78.08 |
| Speech stack | CPU onnxruntime, sherpa-onnx streaming zipformer, Kokoro int8 |
| Router | local llama.cpp 0.8.0-dev build, LFM3-1.7B Q4_K_M, `--jinja`, loopback |
| Pipeline VRAM | 0 MiB (STT and TTS never touch the GPU) |

VRAM was occupied by other resident workloads for the whole run, not by the
measured pipeline. `nvidia-smi` reported a constant 5213 MiB used of 24564 MiB
and these compute processes at both the start and the end of each run:

| Process | VRAM |
| --- | --- |
| ComfyUI (idle) | 1724 MiB |
| llama.cpp server used for routing | 1666 MiB |
| sunshine | 399 MiB |
| llama.cpp CPU backend | 486 MiB |

GPU utilisation sampled during the runs was p50 7% (run 1) and p50 6% (run 2),
with peaks of 64% and 74% while the router was generating. The GPU was therefore
lightly loaded but not idle; the router shared it with ComfyUI and the streaming
host.

The CPU side is the real contention story: system CPU busy was p50 80.8% (run 1)
and 79.8% (run 2), peaking at 100%, and the benchmark process itself accumulated
712.6 s and 725.5 s of CPU time over roughly 35 s of wall time each — about 20
hardware threads busy on average. Kokoro synthesis uses onnxruntime's default
intra-op thread pool, which is sized to the machine, so the pipeline competes
with itself: TTS synthesis runs while the router stream and the STT decoder are
also active. Load average rose from 3.1 to 11.4 in run 1 and from 6.3 to 15.1 in
run 2.

## Results

All values in milliseconds.

| Stage | Run 1 p50 | Run 1 p95 | Run 1 max | Run 2 p50 | Run 2 p95 | Run 2 max |
| --- | --- | --- | --- | --- | --- | --- |
| turn commit (paced silence) | 643.3 | 652.4 | 658.9 | 642.8 | 657.7 | 661.1 |
| STT finalize | 16.6 | 33.0 | 33.5 | 16.8 | 32.6 | 35.2 |
| router first spoken text | 60.6 | 219.1 | 221.0 | 58.9 | 218.4 | 232.8 |
| TTS first audio packet | 299.8 | 460.3 | 518.3 | 310.3 | 433.1 | 454.0 |
| compute path (commit to first audio) | 408.3 | 506.5 | 531.7 | 421.1 | 512.8 | 525.6 |
| perceived response | 1050.8 | 1165.0 | 1183.8 | 1054.3 | 1156.0 | 1171.1 |
| reply synthesis complete | 1085.2 | 1271.0 | 1592.8 | 1062.0 | 1320.2 | 1677.7 |
| spoken audio length | 746.7 | 2219.7 | 3861.3 | 746.7 | 2219.7 | 3861.3 |

Perceived response by routed tool (run 1 / run 2, p50):

| Routed tool | Run 1 | Run 2 |
| --- | --- | --- |
| delegate_to_hermes (9) | 1040.2 | 1077.1 |
| answer_direct (6) | 1084.6 | 1105.9 |
| attach_file (3) | 1009.0 | 1023.1 |
| end_call (2) | 1026.6 | 1019.4 |

Model warmup: cold start 484.8 ms (run 1) and 517.9 ms (run 2) for the first
synthesis; a warm first segment costs 226.7 ms and 225.9 ms. Cold start is paid
once per process, before the first utterance, and is excluded from the measured
responses.

## Where the time goes

The turn-commit window is 61% of the perceived response. It is a policy constant,
not compute: shortening `silence_commit_ms`, or committing on semantic evidence
rather than pure silence, moves the median almost one-to-one.

The compute path is 408–421 ms at the median and 507–513 ms at p95, and it is
dominated by TTS first audio (300–310 ms p50, 433–460 ms p95). That is 80–90 ms
slower than the isolated speech benchmark (`docs/research/speech-latency.md`,
221.6 ms p50 for a short reply) and the difference is the pipeline's own CPU
contention: in the isolated benchmark Kokoro synthesises alone, here it runs
while the STT decoder and the router stream are active and while onnxruntime
holds most of the 32 hardware threads. Capping the TTS intra-op thread count is
the obvious next tuning step; it is not changed in this task because it would
change shipped behaviour, not just the measurement.

STT finalisation is 17 ms p50 / 33 ms p95 — the padded flush described in the
speech report, unchanged at the pipeline level. Router first spoken text is
59–61 ms p50, consistent with the text-only router bench (delegation ack p50
124 ms there) because the ack is emitted as soon as the tool name is known; the
p95 tail of 218 ms is the model's slower turns.

## Routing and recognition under real pipeline conditions

The pipeline routed 19 of 20 utterances to the expected tool in both runs
(95%), with 4 policy hits and 16 model hits. The miss is the same one the
text-only router bench reports: "What's the weather in Zurich tomorrow?" is
answered directly instead of delegated. Recognition word error rate on the
Kokoro-rendered speech was p50 0.00, p95 0.41, max 0.50 — half the transcripts
were clean, and the failures are the expected synthetic-speech confusions
("Email Anna" transcribed as "FEMALE ANNA", "repo" as "RIPOT"). Routing survived
those errors, which is the point of measuring the pipeline rather than the text
path alone.

## Rerun comparability

Both runs used the same utterance set (SHA-256
`4aabbd877d3da80b5423ffbedc5a7c2cfece894aeaf9d53d5a03fdc50285f5b1`), the same
models, voice, turn window, and repeat count, so the harness reports them as
comparable. A third run reproduced the same picture (perceived p50 1042.2 ms,
p95 1147.5 ms, routing 19/20, no stage above the 10% regression threshold). The
measured drift between runs 1 and 2:

| Stage | p50 change | p95 change |
| --- | --- | --- |
| perceived response | +3.5 ms (+0.3%) | -9.0 ms (-0.8%) |
| compute path | +12.8 ms (+3.1%) | +6.3 ms (+1.2%) |
| TTS first audio | +10.5 ms (+3.5%) | -27.2 ms (-5.9%) |
| router first spoken text | -1.8 ms (-2.9%) | -0.6 ms (-0.3%) |
| STT finalize | +0.2 ms (+1.3%) | -0.5 ms (-1.4%) |

No stage crossed the harness's 10% regression threshold. The router total time
p50 moved +9.0% between runs, which is run-to-run model noise at temperature 0.1
rather than a pipeline change; the harness flags it as below threshold but it is
the noisiest number in the table.

`--compare` refuses a baseline whose schema version differs and prints whether
the two runs are comparable before showing deltas, so a future run cannot quietly
compare against a different utterance set, model, voice, or turn window.

## Limitations

- The utterances are Kokoro-rendered, not human. Latency is unaffected by the
  speaker, but the word error rates are a property of the recogniser on synthetic
  speech.
- One STT model, one TTS voice (`af_sarah`), one router model (LFM3-1.7B Q4_K_M),
  one repeat per run. The router's tool choice is stochastic at temperature 0.1;
  routing accuracy is reported per run rather than pooled.
- The speech path is CPU-only; onnxruntime-gpu is not used, so the 24 GB of VRAM
  is not available to STT or TTS. The GPU figures describe contention, not
  pipeline headroom.
- The distributed llama.cpp build shipped by the distribution (r4433) cannot load
  the `lfm3` architecture, so the router was measured against a locally built
  0.8.0-dev server on loopback. The harness never starts or restarts a service;
  point `--url` at a running `llama-server --jinja`.
- First audio is the first Opus packet, not the first sample out of a speaker;
  device buffering adds a few milliseconds that are not in these numbers.
- The harness itself perturbs the host: it samples CPU and GPU every 250 ms and
  keeps an `nvidia-smi` loop process alive for the run.

## Reproduce

```sh
SPEECH_MODEL_DIR=/path/to/sherpa-onnx-streaming-model \
TTS_MODEL_PATH=/path/to/kokoro-v1.0.int8.onnx \
TTS_VOICES_PATH=/path/to/voices-v1.0.bin \
.venv/bin/python relay/tests/bench_latency.py \
    --url http://127.0.0.1:8080 --model lfm3-1.7b \
    --json-out latency-run.json
```

To check a rerun:

```sh
.venv/bin/python relay/tests/bench_latency.py ... --compare latency-run.json
```

The exit status is 1 when the perceived-response median misses
`--max-perceived-p50-ms` (default 1200), so the benchmark can gate a change.

## Follow-ups

- Bound the TTS intra-op thread count so synthesis stops competing with STT and
  with the rest of the host; the isolated speech bench suggests 80–90 ms of the
  TTS first-audio p50 is self-inflicted contention.
- Re-measure with a shorter silence window or a semantic end-of-turn commit; the
  commit window is the single largest term in the perceived response.
- Add a human-speech variant of the scripted set so recognition errors, and their
  effect on routing, are not a property of Kokoro's voice.
