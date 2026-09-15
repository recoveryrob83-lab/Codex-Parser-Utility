# Codex Telemetry Parser v0.1

Small stdlib-only utility that converts completed Codex rollout turns (`rollout-*.jsonl`) into additive PennyTel schema-v1 import JSON files.

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

## Semantic-label safety rule

Codex can authoritatively tell us what model ran, when it ran, where it ran, and how many tokens it used. It does **not** authoritatively know PennyOS concepts such as Implementation, Critic, Repair, or Context Scout.

Therefore the workflow is:

**inspect evidence -> confirm semantic labels -> emit import JSON -> import into PennyTel**

The UI never silently turns an ambiguous session into a confidently labeled run.

## GUI wrapper

Run from the repository root:

```bash
python3 apps/codex-telemetry-parser/codex_parser_ui.py
```

The UI supports:

1. Select one rollout file or a folder tree containing `rollout-*.jsonl` files.
2. Select an output folder.
3. Inspect model, thinking effort, active minutes, inferred slice, semantic labels, and status before parsing.
4. Supply optional overrides for slice ID, run type, role, session mode, context mode, and result.
5. Click **Parse data** to write one PennyTel JSON file per eligible rollout.

When a folder tree is selected, the output preserves its relative subfolder structure.

### Conservative folder-label presets

Exact folder names can provide operator-authored semantic labels:

- `implementation/` -> `Implementation` / `Implementer`
- `independent-critic/` or `initial-critic/` -> `Independent Critic` / `Critic`
- `re-critic/` or `recritic/` -> `Re-Critic` / `Critic`
- `repair/` -> `Repair` / `Repair`
- `context-scout/`, `scout/`, or `scouting/` -> `Context Scout` / `Context Steward`
- `master-index-mapper/`, `master-index/`, or `mapper/` -> `MASTER_INDEX Mapper` / `Context Steward`

A broad `critic/` folder infers only the **Critic role**, not whether the run was an initial critic or re-critic. The exact run type must still be supplied. This is intentional.

The UI can infer `S4`, `S5`, etc. only when Codex provenance contains an explicit `slice-N` pattern in cwd or branch. Otherwise it requires a slice override.

`codex-auto-review` sessions are shown and skipped rather than emitted as production telemetry.

Files with multiple completed turns are not guessed at in v0.1; they are flagged for turn selection instead.

## CLI

The CLI remains available for precise one-off work:

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

Required operator metadata for the CLI:

- existing `--slice-id`
- new unique `--run-id`
- `--run-type`
- `--role`

Optional existing PennyTel fields can also be supplied (`--candidate`, `--session-mode`, `--context-mode`, `--result`, `--notes`).

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

If a rollout file contains multiple completed Codex tasks, the CLI refuses to guess. Re-run with `--turn-id <id>`.

## Tests

```bash
python3 -m unittest discover -s apps/codex-telemetry-parser/tests -v
```

The repository does not store raw Codex session logs. Keep them as local source evidence; parser output is intentionally privacy-reduced.

## Versioning direction

The parser/UI is an adapter, not the telemetry authority. Future versions can add a target selector (for example, current PennyTel schema v1 vs a later telemetry schema) while preserving the raw Codex logs as source evidence. That lets us re-parse old sessions when PennyTel learns to ingest richer telemetry without having to recapture the original runs.
