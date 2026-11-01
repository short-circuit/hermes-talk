"""Synthetic attachment fixtures for the relay test suite.

Everything is generated in memory and written under ``tmp_path`` by the tests;
no binary fixture is committed.  ``build_pdf`` writes a genuinely valid PDF
(correct xref offsets) so pypdf can extract its text, and ``big_png`` writes a
real 1-bit PNG whose pixel dimensions are honest rather than patched headers.
"""

from __future__ import annotations

import io
import struct
import zipfile

from PIL import Image
from pypdf import PdfReader, PdfWriter


def build_pdf(pages: int = 1, text: str = "Hello") -> bytes:
    """Return a minimal, structurally valid PDF with one text run per page."""
    page_ids = [3 + 2 * index for index in range(pages)]
    content_ids = [4 + 2 * index for index in range(pages)]
    font_id = 3 + 2 * pages
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            f"<< /Type /Pages /Kids [{' '.join(f'{pid} 0 R' for pid in page_ids)}] "
            f"/Count {pages} >>"
        ).encode(),
    ]
    for content_id in content_ids:
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode()
        )
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode()
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

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
    return bytes(out)


def truncated_pdf() -> bytes:
    """Return a PDF whose tail (including the xref) has been cut off."""
    data = build_pdf(3)
    return data[: len(data) // 2]


def encrypted_pdf() -> bytes:
    """Return a one-page PDF encrypted with a password."""
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(build_pdf())))
    writer.encrypt("pw")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def big_png(width: int = 10000, height: int = 10000) -> bytes:
    """Return a real 1-bit PNG with ``width * height`` pixels."""
    buffer = io.BytesIO()
    Image.new("1", (width, height)).save(buffer, format="PNG")
    return buffer.getvalue()


def medium_png(width: int = 4000, height: int = 3000) -> bytes:
    """Return a solid RGB PNG large enough to require downscaling."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (12, 34, 56)).save(buffer, format="PNG")
    return buffer.getvalue()


def zip_bomb(entries: int = 10, entry_bytes: int = 20 * 1024 * 1024) -> bytes:
    """Return a zip that declares hundreds of MB but stores only a few KB."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index in range(entries):
            archive.writestr(f"entry-{index}.bin", b"\x00" * entry_bytes)
    return buffer.getvalue()


def many_entries_zip(count: int = 2000) -> bytes:
    """Return a zip with ``count`` tiny members."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index in range(count):
            archive.writestr(f"note-{index}.txt", "hi\n")
    return buffer.getvalue()


def encrypted_entry_zip() -> bytes:
    """Return a zip whose single member is flagged as password protected.

    ``zipfile`` cannot write encrypted archives, so the general-purpose flag
    bit is set in both the local and central headers; reading the member then
    raises ``RuntimeError`` exactly as it does for a real ``zip -e`` archive.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("locked.txt", "hidden\n")
    data = bytearray(buffer.getvalue())
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = data.find(signature)
        if start >= 0:
            current = struct.unpack_from("<H", data, start + flag_offset)[0]
            struct.pack_into("<H", data, start + flag_offset, current | 0x1)
    return bytes(data)


def valid_zip() -> bytes:
    """Return a small, ordinary zip with two text members."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("first.txt", "one\n")
        archive.writestr("second.txt", "two\n")
    return buffer.getvalue()


def png_with_exif() -> bytes:
    """Return a PNG carrying an EXIF tag that must be stripped on storage."""
    image = Image.new("RGB", (8, 8), (90, 80, 70))
    exif = Image.Exif()
    exif[0x010F] = "SyntheticCam"
    exif[0x8825] = {1: "N", 2: (1.0, 2.0, 3.0)}
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", exif=exif)
    return buffer.getvalue()


__all__ = [
    "big_png",
    "build_pdf",
    "encrypted_entry_zip",
    "encrypted_pdf",
    "many_entries_zip",
    "medium_png",
    "png_with_exif",
    "truncated_pdf",
    "valid_zip",
    "zip_bomb",
]
