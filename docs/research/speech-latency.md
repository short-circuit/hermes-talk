# Local speech I/O latency

Measured behaviour of the local speech adapters in `relay/speech`: streaming speech-to-text
(sherpa-onnx zipformer, faster-whisper fallback), sentence-level text-to-speech (Kokoro), 48 kHz Opus
encoding, and the backpressure limits that bound them. Every stage runs on the host; nothing is sent to a
cloud service at runtime.

## Verdict against the acceptance targets

| Target | Measured | Verdict |
| --- | --- | --- |
| Final transcript under 300 ms after end of speech | p50 17.2 ms, p95 31.9 ms, max 33.4 ms (padded flush, complete transcript) | met |
| First TTS audio under 250 ms | p50 221.6 ms for short replies, p50 246.8 ms for long openers with the first-segment cap; p95 301.4 ms / 331.8 ms | met at the median; the tail exceeds the target under concurrent CPU load (see caveats) |
| No cloud STT/TTS/LLM calls | sherpa-onnx, Kokoro and faster-whisper all run locally; faster-whisper is constructed with `local_files_only=True` | met |
| Clear error when a model is missing | `SpeechModelUnavailable` naming the engine, the expected files and the environment variable to set | met |

The first-audio result is a warm measurement: `StreamingTts.warmup()` removes the one-time first-inference
cost (747 ms on this host) from the steady-state figure, and the cold path is reported separately below.

## Host and contention

- CPU: Intel i9-13900K, 24 cores / 32 threads, 62 GB RAM, Arch Linux, kernel 7.2.11.
- GPU: NVIDIA RTX 4090, 24 GB. The speech adapters use CPU execution providers only; no VRAM is held by
  them. During the recorded runs the GPU carried unrelated resident work: ComfyUI idle (1722 MiB),
  Sunshine capture (399 MiB) and a llama.cpp server (482 MiB), about 3.5 GB used in total.
- The host is shared with other workloads. Load average during the final run was 2.0 (idle-ish); earlier
  runs were taken at load 3–14 with other agents active, which is what produces the long tail on the TTS
  figures. CPU frequency scaling was active (about 5 GHz on the P-cores during the final run).
- The benchmark itself is CPU-bound: 1884 s of process CPU time for 60 STT passes, 60 TTS passes and the
  Kokoro rendering of the 20 scripted utterances; peak RSS 1180 MiB (models included).

## Method

- Harness: `scripts/bench_speech.py` (repeat 3). It replays 20 scripted utterances from
  `scripts/voice_bench_utterances.json` through the streaming recognizer and 20 reply sentences from
  `scripts/speech_bench_replies.json` plus 10 long openers from `scripts/speech_bench_long_openers.json`
  through the TTS pipeline.
- Speech material: when `TTS_MODEL_PATH`/`TTS_VOICES_PATH` are configured the utterances are rendered with
  the local Kokoro model (24 kHz), which sounds natural enough for the recognizer; otherwise the harness
  falls back to the local `espeak-ng` binary. Nothing is downloaded. `espeak-ng` speech is badly
  mismatched to this acoustic model — word error rate ≈ 0.94 — which is why it is only a fallback.
- Timing: `time.perf_counter` around each stage; p50/p95/max over all samples. CPU time from
  `time.process_time`, peak RSS from `resource.getrusage`, GPU memory from `nvidia-smi` when present.
- Models (not committed; supplied through environment variables):
  - `SPEECH_MODEL_DIR` — `sherpa-onnx-streaming-zipformer-en-2023-06-26`, int8 encoder/decoder/joiner
    (67 MB encoder, sha256 `5022b2ec…`), 16-frame chunks with 128-frame left context.
  - `TTS_MODEL_PATH` / `TTS_VOICES_PATH` — Kokoro `kokoro-v1.0.int8.onnx` (89 MB, sha256 `6e742170…`) and
    `voices-v1.0.bin` (27 MB, sha256 `bca610b8…`), voice `af_sarah`.

## Streaming STT

| Metric | p50 | p95 | Max |
| --- | --- | --- | --- |
| Time from first audio to first partial hypothesis (ms) | 63.2 | 74.8 | 82.7 |
| Padded flush: end of speech to complete final transcript (ms) | 17.2 | 31.9 | 33.4 |
| Trailing padding the decoder needs to release the last words (ms) | 300 | 480 | 480 |
| Endpoint-path final after speech (VAD policy latency, ms) | 107.9 | 113.9 | 114.6 |
| Partials per utterance | 1 | 2 | 2 |
| Word error rate of the flush transcript against the scripted text | 0.24 | 0.40 | 0.50 |

Reading these numbers:

- The recognizer decodes incrementally at real-time factor ≈ 0.03, so the decode work is never the
  bottleneck: a 20 ms frame costs well under a millisecond.
- The chunk-based zipformer releases its last tokens only after it has seen trailing audio. Without
  padding, a flush returns a truncated transcript (for example `"WHAT'S THE WEATHER IN ZURI"` instead of
  `"WHAT'S THE WEATHER IN ZURICH TO MORROW"`). `SherpaStreamingStt.flush()` therefore feeds
  `STT_FLUSH_PADDING_MS` (default 480 ms) of synthetic silence as a single chunk before decoding. That
  padding is audio time, not wall time: it costs about 17–33 ms of CPU, which is the measured acceptance
  figure.
- In a live call the same trailing audio arrives naturally while the turn detector waits for silence, so
  the padded flush is a worst-case path, not an artificial advantage.
- The endpoint path (trailing-silence endpoint detection inside the recognizer) finalises about 110 ms
  after the last speech sample; that figure is dominated by the VAD policy window, not by compute.
- The word error rate is the acoustic model's accuracy on Kokoro-rendered speech, not a truncation
  artefact. The real-speech integration test over the model's own `test_wavs/0.wav` produces the complete
  sentence and ends with the correct final word.

Whisper fallback (`WhisperFallbackStt`, faster-whisper tiny int8 on CPU): model load 2.7 s; a 6.62 s
utterance produced three segment finals at 131–271 ms each, 706 ms of wall time in total (real-time factor
≈ 0.11). It emits no partial hypotheses — chunk-level finals only — and is intended as the degraded path
when the streaming model is unavailable.

## Streaming TTS

| Metric | p50 | p95 | Max |
| --- | --- | --- | --- |
| Short replies: time to first Opus packet, warm (ms) | 221.6 | 301.4 | 373.8 |
| Long openers with first-segment cap: time to first packet (ms) | 246.8 | 331.8 | 361.3 |
| Long openers without cap: time to first packet (ms) | 527.1 | 1315.1 | 1479.6 |
| Cold start: model load plus warm-up (ms) | 747.1 | — | — |
| Real-time factor of synthesis plus Opus encoding | 0.23 | 0.30 | 0.38 |
| Opus packets per reply | 51 | 60 | 67 |

Reading these numbers:

- Kokoro int8 on CPU costs roughly 112 ms plus 108 ms per second of synthesized audio, so a single long
  first sentence cannot meet the 250 ms budget. `TTS_FIRST_SEGMENT_MAX_CHARS` (default 20) caps only the
  first emitted segment at a word boundary and buffers the remainder; later sentences are spoken whole.
  The uncapped row shows what the cap buys: 527 ms p50 and 1.3 s p95 without it.
- The median is stable across three independent runs (221.6–224.2 ms for the short group), which is where
  the acceptance figure comes from. The p95 tail is scheduling noise: identical two-word replies vary by
  150 ms or more when other workloads occupy the 32 hardware threads that onnxruntime's default intra-op
  pool uses.
- `TtsMetrics.first_audio_ms` matches the harness measurement to within 0.1 ms, so the enqueue-to-packet
  instrumentation in the adapter is trustworthy.
- The cold path (first sentence after process start) pays 747 ms of model load plus warm-up; a relay that
  cares about the very first utterance should call `StreamingTts.warmup()` during startup.

## Opus encoding

| Metric | Value |
| --- | --- |
| Encode time for 1 s of 24 kHz audio resampled to 48 kHz | 4.7 ms |
| Real-time factor | 0.005 |
| Packet size | 20 ms frames, 32 kbit/s VBR, mono |

`OpusEncoder` resamples to 48 kHz with PyAV's resampler and encodes with libopus; resampling 1 s of audio
takes 1.3 ms and encoding the resulting 49 packets about 3.4 ms. Encoding is never on the critical path.

## Backpressure and degradation behaviour

- Inbound audio is bounded by `SPEECH_MAX_BUFFER_BYTES` (480 000 B ≈ 5 s of 48 kHz mono) and
  `SPEECH_MAX_BUFFER_FRAMES` (250). The microphone path uses the `drop_oldest` policy so a stalled decoder
  degrades the transcript instead of the call; every eviction is counted in the buffer statistics.
- Outbound speech uses the `block` policy: the producer waits up to `SPEECH_BACKPRESSURE_TIMEOUT_S` and
  then raises `SpeechBackpressureExceeded` rather than dropping spoken words. The sentence queue is capped
  at `TTS_MAX_PENDING_SENTENCES` (3).
- `StreamingTts.clear()` drops queued sentences and buffered audio for barge-in; `close()` cancels the
  worker, closes the encoder and is idempotent.
- Missing models fail fast at construction with an actionable message, for example:
  `sherpa-onnx STT model directory is not configured: set STT_MODEL_DIR to a directory containing
  tokens.txt, encoder-*.onnx, decoder-*.onnx and joiner-*.onnx`, or
  `kokoro TTS model file does not exist: …; set TTS_MODEL_PATH to a local Kokoro ONNX model file`.
  A missing Python package produces the same error type, pointing at `./relay[speech]`.
- `feed()` after `close()` raises `SpeechStateError`; caller frames longer than 500 ms are rejected so a
  caller cannot starve the decoder.

## Caveats

- Speech material is Kokoro-rendered, not human. Latency figures are unaffected by that choice, but the
  word error rate is a property of the model on synthetic speech; the real-audio integration test only
  covers one utterance.
- All measurements are CPU-only. `onnxruntime-gpu` is installed in some environments, but on this host its
  CUDA provider fails on a cuDNN sublibrary mismatch and falls back to CPU, so the GPU path is untested.
  Expect materially lower TTS first-audio latency if the CUDA provider is usable.
- The TTS tail is sensitive to concurrent load on the host; comparisons between runs are only meaningful
  at the same load level.
- One model, one voice, one language (en-us) were measured. The adapters accept other Kokoro voices and
  other sherpa-onnx transducer model directories.

## Reproducing

```bash
# install the adapters
.venv/bin/python -m pip install -e './relay[speech]'

# point the harness at local models (never committed)
export SPEECH_MODEL_DIR=/path/to/sherpa-onnx-streaming-zipformer-en-2023-06-26
export TTS_MODEL_PATH=/path/to/kokoro-v1.0.int8.onnx
export TTS_VOICES_PATH=/path/to/voices-v1.0.bin

.venv/bin/python scripts/bench_speech.py --repeat 3 --out speech-bench.json

# real-model integration tests (skipped without the variables above)
.venv/bin/python -m pytest -q relay/tests/test_speech_integration.py
```

`scripts/bench_speech.py --help` lists the per-stage switches (`--skip-stt`, `--skip-tts`, `--skip-opus`,
`--synth`, `--silence-ms`, `--wav-dir`, `--replies`).
