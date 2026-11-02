# Hermes run fixtures

Synthetic SSE transcripts for the offline run-path tests in
`relay/tests/test_runs.py`. They are shaped exactly like the transcripts
recorded against a real gateway and quoted in
`docs/research/hermes-api.md` (section 4 event stream, section 6 approvals,
section 7 clarify, section 8 stop); the payloads, prompts and answers are
hand-written placeholders, never captured user speech.

Format: one JSON object per line.

- Event frame: `{"id": <seq>, "data": {<event envelope and payload>}}`, where
  `data.seq` repeats `id` and `data.run_id` is a synthetic fixture id.
- Comment frame: `{"comment": "<text>"}` - rendered as an SSE comment
  (`: <text>`), used to model keepalives inside a recorded stream.

The replay stub (`FixtureGateway` in `test_runs.py`) rewrites `run_id` to the
run it admitted, so fixtures stay portable. `: open` and `: stream closed`
framing belongs to the stub, not to these files.

Every fixture is under 2 MiB, contains no personal data, and must pass the
contract tests in `test_runs.py`: documented event names only, documented
payload fields only, consecutive seqs, and exactly one terminal frame at the
end.
