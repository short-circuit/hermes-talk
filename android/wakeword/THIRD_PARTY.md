# Third-party notices

This module contains or downloads the following Apache-2.0 licensed components.

## Vendored Kotlin API

The files under `src/main/java/com/k2fsa/sherpa/onnx/` are copied verbatim from
sherpa-onnx v2.0.1:

- `FeatureConfig.kt`
- `OnlineStream.kt`
- `OnlineRecognizer.kt`
- `QnnConfig.kt`
- `HomophoneReplacerConfig.kt`
- `KeywordSpotter.kt`

Upstream: <https://github.com/k2-fsa/sherpa-onnx/tree/v2.0.1/sherpa-onnx/kotlin-api>

Copyright (c) 2024 Xiaomi Corporation and the sherpa-onnx contributors.

Licensed under the Apache License, Version 2.0. The repository-level
[`LICENSE`](../../../LICENSE) contains the full license text.

## Native sherpa-onnx and onnxruntime binaries

The build task `fetchSherpaOnnxJniLibs` downloads the official Android archive:

- sherpa-onnx v2.0.1
- archive: `sherpa-onnx-v2.0.1-android.tar.bz2`
- URL: <https://github.com/k2-fsa/sherpa-onnx/releases/download/v2.0.1/sherpa-onnx-v2.0.1-android.tar.bz2>
- SHA-256: `39003753ec293425ed56723ea9b972b98841a490af3311670c3060548d654330`

Only `jniLibs/arm64-v8a/` and `jniLibs/x86_64/` are extracted. The archive
contains `libsherpa-onnx-jni.so` plus the onnxruntime shared libraries that
sherpa-onnx links against.

Upstream sherpa-onnx: <https://github.com/k2-fsa/sherpa-onnx>

The archive also contains onnxruntime binaries distributed by Microsoft under
the MIT License. See the upstream release for the exact onnxruntime version and
its license files.

## Model

The bundled keyword-spotting model is:

- `sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01`
- int8 encoder, decoder and joiner
- from the sherpa-onnx v2.0.1 release assets

Upstream: <https://github.com/k2-fsa/sherpa-onnx/releases/tag/v2.0.1>

The model is distributed with the sherpa-onnx release under the same
Apache-2.0 terms. The `tokens.txt` and `keywords.txt` files are part of the
model distribution; `keywords.txt` was edited to the single phrase
`▁HE Y ▁HER ME S`.
