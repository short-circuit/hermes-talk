"""Deterministic generator for the committed attachment fixtures.

Run it to (re)create the binaries in this directory:

    .venv/bin/python relay/tests/fixtures/attachments/generate.py

``--check`` regenerates in memory and reports any file that no longer matches
its generator, without writing.  Every fixture is synthetic: no personal data,
no recording, no external download.  Sizes stay far below the 2 MiB cap that
``scripts/check_repo_hygiene.sh`` enforces per file.

The set covers the end-to-end attachment story in
``relay/tests/test_attachments.py``:

=====================  =====================================================
``photo.png``          valid PNG (64x48) carrying EXIF that ingest strips
``report.pdf``         valid one-page PDF with three extractable text lines
``memo.wav``           0.25 s of a 440 Hz tone, 16 kHz mono 16-bit PCM
``broken.pdf``         ``report.pdf`` cut in half: header intact, no xref
``oversized.png``      10000x10000 1-bit PNG: 100 MP over the 40 MP cap
=====================  =====================================================
"""

from __future__ import annotations

import io
import math
import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw

_HERE = Path(__file__).resolve().parent

_PHOTO_SIZE = (64, 48)
_PHOTO_EXIF = {
    0x010F: "FixtureCam",  # Make
    0x0110: "Synthetic 1",  # Model
    0x8825: {1: "N", 2: (47.0, 22.0, 0.0)},  # GPS IFD, fixed values only
}
_REPORT_LINES = (
    "Quarterly report",
    "Units shipped: 42",
    "Region: North",
)
_WAV_RATE = 16_000
_WAV_SECONDS = 0.25
_WAV_TONE_HZ = 440.0
_WAV_AMPLITUDE = 0.2
_OVERSIZED_PIXELS = (10_000, 10_000)


def _pdf(objects: list[bytes], xref_entries: int) -> bytes:
    """Assemble objects, a correct xref table, and a trailer."""
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (len(objects) + 1)
    for number, body in enumerate(objects, start=1):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for number in range(1, len(objects) + 1):
        out += f"{offsets[number]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    ).encode()
    if len(objects) + 1 != xref_entries:
        raise SystemExit("xref entry count does not match the object list")
    return bytes(out)


def _escape_pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_report_pdf() -> bytes:
    """Return a one-page PDF whose text a reader can extract."""
    content = b"".join(
        f"BT /F1 12 Tf 72 {720 - 20 * index} Td ({_escape_pdf_text(line)}) Tj ET\n".encode()
        for index, line in enumerate(_REPORT_LINES)
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    return _pdf(objects, len(objects) + 1)


def build_broken_pdf() -> bytes:
    """Return the first half of :func:`build_report_pdf` (xref cut off)."""
    data = build_report_pdf()
    return data[: len(data) // 2]


def build_photo_png() -> bytes:
    """Return a valid PNG carrying EXIF that ingest must strip."""
    image = Image.new("RGB", _PHOTO_SIZE, (18, 52, 86))
    draw = ImageDraw.Draw(image)
    draw.rectangle((8, 8, 31, 31), fill=(200, 120, 40))
    draw.line((0, _PHOTO_SIZE[1] - 1, _PHOTO_SIZE[0] - 1, 0), fill=(240, 240, 240), width=2)
    exif = Image.Exif()
    for tag, value in _PHOTO_EXIF.items():
        exif[tag] = value
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", exif=exif)
    return buffer.getvalue()


def build_oversized_png() -> bytes:
    """Return a real 1-bit PNG of 100 MP, far over the 40 MP decode cap."""
    buffer = io.BytesIO()
    Image.new("1", _OVERSIZED_PIXELS, 0).save(buffer, format="PNG")
    return buffer.getvalue()


def build_memo_wav() -> bytes:
    """Return 0.25 s of a 440 Hz tone as 16 kHz mono 16-bit PCM."""
    frames = int(_WAV_RATE * _WAV_SECONDS)
    samples = bytearray()
    for index in range(frames):
        value = math.sin(2.0 * math.pi * _WAV_TONE_HZ * index / _WAV_RATE) * _WAV_AMPLITUDE
        samples += struct.pack("<h", round(value * 32767))
    data_bytes = len(samples)
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + data_bytes)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, _WAV_RATE, _WAV_RATE * 2, 2, 16)
        + b"data"
        + struct.pack("<I", data_bytes)
    )
    return header + bytes(samples)


def build_all() -> dict[str, bytes]:
    """Return every fixture as ``name -> bytes``."""
    return {
        "photo.png": build_photo_png(),
        "report.pdf": build_report_pdf(),
        "memo.wav": build_memo_wav(),
        "broken.pdf": build_broken_pdf(),
        "oversized.png": build_oversized_png(),
    }


def _check(payloads: dict[str, bytes]) -> int:
    drift = []
    for name, data in payloads.items():
        path = _HERE / name
        if not path.is_file():
            drift.append(f"{name}: missing")
        elif path.read_bytes() != data:
            drift.append(f"{name}: differs from the generator")
    for line in drift:
        print(line, file=sys.stderr)
    if drift:
        return 1
    print(f"fixtures match the generator ({len(payloads)} files)")
    return 0


def _write(payloads: dict[str, bytes]) -> int:
    total = 0
    for name, data in payloads.items():
        (_HERE / name).write_bytes(data)
        total += len(data)
        print(f"{name}: {len(data)} bytes")
    print(f"total: {total} bytes")
    return 0


def main(argv: list[str]) -> int:
    payloads = build_all()
    if "--check" in argv[1:]:
        return _check(payloads)
    return _write(payloads)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
