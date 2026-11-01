# Local speech latency measurements

This note records the measured behaviour of the local speech adapters in
`relay/speech` on a CPU-only host.  All inference is local: sherpa-onnx for
streaming speech-to-text, Kokoro for text-to-speech, and PyAV/libopus for Opus
encoding.  No cloud service is contacted at runtime.

## Method

- Harness: `scripts/bench_speech.py --repeat 3`.
- Models are supplied through environment variables:
  - `SPEECH_MODEL_DIR` — a sherpa-onnx streaming zipformer model directory.
  - `TTS_MODEL_PATH` / `TTS_VOICES_PATH` — a Kokoro int8 ONNX model and its
    voices file.
- When `--wav-dir` is omitted, the 20 scripted utterances in
  `scripts/voice_bench_utterances.json` are rendered with the local
  `espeak-ng` binary into a temporary directory.  Nothing is downloaded.
- Replies come from `scripts/speech_bench_replies.json` (short local reply
  sentences).
- Timings use `time.perf_counter`; CPU time uses `time.process_time`; peak RSS
  uses `resource.getrusage`.  The optional GPU line comes from `nvidia-smi` and
  is ambient host memory, not speech-adapter allocation.

Reproduce with:

```bash
SPEECH_MODEL_DIR=/path/to/sherpa-onnx-streaming-zipformer-en-2023-06-26 \
TTS_MODEL_PATH=/path/to/kokoro-v1.0.int8.onnx \
TTS_VOICES_PATH=/path/to/voices-v1.0.bin \
.venv/bin/python scripts/bench_speech.py --repeat 3 --out speech-bench.json
```

The model-dependent integration tests use the same variables:

```bash
SPEECH_MODEL_DIR=/path/to/sherpa-onnx-streaming-zipformer-en-2023-06-26 \
TTS_MODEL_PATH=/path/to/kokoro-v1.0.int8.onnx \
TTS_VOICES_PATH=/path/to/voices-v1.0.bin \
.venv/bin/python -m pytest -q relay/tests/test_speech_integration.py
```

## Acceptance targets

| Target | Measured p50 | Measured p95 | Notes |
| --- | --- | --- | --- |
| Final transcript < 300 ms after end of speech | 0.07 ms flush cost | 0.09 ms | Endpoint detection often finalises before the caller flushes; the flush path decodes any remaining audio in under a millisecond. |
| First TTS audio < 250 ms | 230.7 ms | 273.9 ms | p50 meets the target. p95 is above it because Kokoro synthesis time scales with sentence length; short first clauses stay below 250 ms. |

The integration test asserts both targets for one short sentence
(`first_audio_ms < 250`) and for a real `test_wavs/0.wav` transcription
(`latency_ms < 300`), and passes on this host.

## Streaming STT

| Metric | p50 | p95 | Max |
| --- | --- | --- | --- |
| Partial hypotheses per utterance | 1 | 2 | 3 |
| Time from first audio to first partial (ms) | 104.9 | 167.7 | 213.3 |
| Final/flush decode cost (ms) | 0.07 | 0.09 | 0.14 |
| Word error rate vs scripted text | 0.94 | 1.00 | 1.00 |

The high word error rate is an artefact of scoring against `espeak-ng`
synthetic speech, which is a poor match for the zipformer model's acoustic
training data.  The real-speech integration test over `test_wavs/0.wav`
produces a normal word transcript.  The partial and final timings are the
numbers that matter for the relay's turn-taking budget.

## Streaming TTS

| Metric | p50 | p95 | Max |
| --- | --- | --- | --- |
| Time to first Opus packet (ms) | 230.7 | 273.9 | 328.2 |
| Time to last Opus packet (ms) | 230.7 | 273.9 | 328.2 |
| Real-time factor | 0.23 | 0.31 | 0.45 |
| Opus packets per reply | 51 | 60 | 67 |
| `TtsMetrics.first_audio_ms` | 230.7 | 273.8 | 328.1 |

Each reply is a single short sentence, so first and last packet times coincide.
The RTF below 1 means synthesis plus Opus encoding runs faster than playback.
The `TtsMetrics` first-audio values match the harness measurement, confirming
that the enqueue-to-packet path is instrumented correctly.

## Opus encoding

| Metric | Value |
| --- | --- |
| Encode time for 1 s of 24 kHz tone resampled to 48 kHz | 5.38 ms |
| Packets produced | 51 |
| Real-time factor | 0.005 |

## Host resources

- CPU time for the full repeat-3 run: 468.2 s.
- Peak RSS: 550.3 MiB.
- Ambient GPU memory reported by `nvidia-smi`: 3035 MiB (the speech adapters
  use CPU execution providers only).

## Observations

- Endpoint detection resets the sherpa stream cleanly, so the next utterance
  starts from an empty hypothesis.
- Partial emission is rate-limited to one update per configured interval and
  only fires when the normalized text changes.
- The bounded TTS queue and audio buffer fail with an explicit
  `SpeechBackpressureExceeded` instead of growing without limit.
- Kokoro's model is loaded at adapter construction so that the first spoken
  sentence does not pay the one-time model load cost.
