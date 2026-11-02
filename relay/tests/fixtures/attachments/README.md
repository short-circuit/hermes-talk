# Attachment fixtures

Synthetic binaries for the end-to-end attachment tests in
`relay/tests/test_attachments.py`. Nothing here is a recording, a screenshot or
a download: `generate.py` builds every byte locally, and `--check` proves the
committed files still match their generator.

| file | what it is |
| --- | --- |
| `photo.png` | valid 64x48 PNG carrying EXIF (Make, Model, GPS) that ingest must strip |
| `report.pdf` | valid one-page PDF whose three text lines a reader can extract |
| `memo.wav` | 0.25 s of a 440 Hz tone, 16 kHz mono 16-bit PCM |
| `broken.pdf` | `report.pdf` cut in half: `%PDF-` header intact, xref/trailer gone |
| `oversized.png` | real 1-bit PNG of 10000x10000 pixels (100 MP over the 40 MP decode cap) |

Total size is about 22 KiB, well under the 2 MiB per-file cap enforced by
`scripts/check_repo_hygiene.sh` and the 2 MB fixture budget in
`docs/TESTING.md`.

Regenerate after changing the generator:

```
.venv/bin/python relay/tests/fixtures/attachments/generate.py
.venv/bin/python relay/tests/fixtures/attachments/generate.py --check
```

The oversized image is small on disk because 1-bit pixels compress well; the
wire-size cap is exercised separately with a PNG generated at test time, so no
multi-megabyte blob enters the repository.
