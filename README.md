# Codex Telemetry Parser v0.1

Small stdlib-only CLI that converts one completed Codex rollout turn (`rollout-*.jsonl`) into an additive PennyTel schema-v1 import JSON file.

v0.1 deliberately targets **only fields PennyTel already accepts**. It does not copy prompts, hidden/encrypted reasoning, tool commands, source excerpts, or tool output into the PennyTel artifact.

## What it derives

From Codex logs:

- canonical model/model ID for current Astra, Sol, and Luna names (raw model retained for unknown future GPT model names)
- OpenAI provider/provider ID
- thinking effort when recorded
- `task_started` / `task_complete` timestamps and active wall minutes
- local hour/day of week from the recorded Codex timezone
- fresh input, cached input, output, and reasoning tokens
- safe source provenance in `notes` (log/session/turn, CLI version, repo/branch/baseline, coarse quota snapshot)

Important token rule: Codex `input_tokens` includes cached input. PennyTel fresh input is therefore `input_tokens - cached_input_tokens`.

Quota is intentionally **not** written to PennyTel `usageBefore` / `usageAfter` in v0.1. The Codex meter observed so far is a coarse global whole-percent signal and can be contaminated by concurrent sessions. The raw snapshot is retained in notes without fabricating precise per-run attribution.

## Required operator metadata

Codex does not know PennyTel's semantic run identity. Supply:

- existing `--slice-id`
- new unique `--run-id`
- `--run-type`
- `--role`

Optional existing PennyTel fields can also be supplied (`--candidate`, `--session-mode`, `--context-mode`, `--result`, `--notes`).

## Usage

```bash
python3 apps/codex-telemetry-parser/codex_to_pennytel.py \
  ~/.codex/sessions/2026/09/14/rollout-....jsonl \
  --slice-id pennytel-s6-example \
  --run-id pt-s6-implementation-astra \
  --run-type Implementation \
  --role Implementer \
  --session-mode Fresh \
  --context-mode "Compact Packet" \
  --result Completed \
  -o /tmp/pennytel-codex-import.json
```

The output is a normal additive PennyTel dataset:

```json
{
  "schemaVersion": 1,
  "revision": 0,
  "slices": [],
  "runs": [{ "...": "one parsed run" }],
  "findings": [],
  "discoveries": [],
  "pricing": []
}
```

The referenced slice must already exist in PennyTel. PennyTel remains the authority for import validation, registry pricing snapshots, and duplicate/conflict checks.

If a rollout file contains multiple completed Codex tasks, the parser refuses to guess. Re-run with `--turn-id <id>`.

## Tests

```bash
python3 -m unittest discover -s apps/codex-telemetry-parser/tests -v
```

The repository does not store raw Codex session logs. Keep them as local source evidence; the parser output is intentionally privacy-reduced.
