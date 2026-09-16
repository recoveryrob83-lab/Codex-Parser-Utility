# Codex Telemetry Parser v0.2

Standalone, stdlib-first, privacy-reducing adapter for PennyTel:

`Codex rollout JSONL -> reviewed semantic labels -> PennyTel import JSON`

This repository was extracted from the Penny-Utilities monorepo with its parser history preserved. It supports explicit PennyTel schema `v1` and `v2` targets. Selecting `v2` adds the PennyTel 0.2.1 `Run.executionEvidence` object; `v1` never receives v2 fields.

## What it does

The parser reads local Codex rollout logs and turns trustworthy machine evidence into PennyTel import JSON while keeping PennyOS workflow semantics explicit.

Codex evidence may supply:

- model and thinking level;
- lifecycle timestamps and wall time;
- fresh, cached, output, and reasoning token counts;
- session and turn IDs;
- runtime/originator metadata;
- working directory, repository, branch, and baseline commit;
- time to first token;
- model invocation and tool-call counts when identities are trustworthy;
- context-window and peak-occupancy evidence;
- quota-window observations;
- recorded sandbox, approval, reviewer, and network constraints;
- exact source-log SHA-256 provenance.

It does **not** infer `sliceId`, `runType`, `role`, `result`, or `contextMode` from raw telemetry. Those are operator/orchestrator semantics and must be supplied or confirmed explicitly.

Raw prompts, messages, AGENTS/system instructions, reasoning text, source excerpts, tool commands/output, and arbitrary payloads are not copied into normal PennyTel output.

## Requirements

- Python 3.11+ recommended
- Tkinter for the desktop UI
- POSIX environment for the hardened harvesting/locking path (`flock`, `O_NOFOLLOW`, directory fsync/hard-link transaction behavior)
- PennyTel 0.2.1 checkout only if you want to run the cross-project smoke validator

The parser itself uses the Python standard library only.

## Quick start

Clone the repository and launch the UI from the repository root:

```bash
git clone https://github.com/recoveryrob83-lab/Codex-Parser-Utility.git
cd Codex-Parser-Utility
python3 codex_parser_ui.py
```

The steady-state UI workflow is:

1. **Find New Runs** scans `~/.codex/sessions` and, when present, `~/.codex/archived_sessions`.
2. **Review & Label** opens the first completed turn still missing explicit PennyTel semantics.
3. Confirm or enter `sliceId`, `runType`, `role`, target schema, and other operator metadata.
4. **Save to Inbox** writes an ordinary PennyTel import JSON under `~/PennyTel-Inbox/YYYY-MM-DD/`.
5. Import that JSON through PennyTel's **Data & portability** surface.

Control/guardian-review sessions and previously harvested session+turn identities are skipped after persisted evidence-integrity checks. Ambiguous or unlabeled rows remain non-emittable.

## CLI

For a precise one-off conversion:

```bash
python3 codex_to_pennytel.py rollout.jsonl \
  --target v2 \
  --slice-id S6 \
  --run-id my-run \
  --run-type Implementation \
  --role Implementer \
  --result Completed \
  -o /tmp/my-run.json
```

Pass `--turn-id` for a multi-turn file. Folder input is searched recursively and requires `--output` as a directory. Folder runs receive persisted opaque UUIDs unless an explicit supported single-file Run ID is supplied.

`--quota-attribution Clean|Contaminated` is an explicit operator review; the default is `Unknown`.

## Token semantics

Codex raw input includes cached input. PennyTel stores fresh and cached input separately:

```text
inputTokens        = Codex input_tokens - cached_input_tokens
cachedInputTokens  = cached_input_tokens
outputTokens       = output_tokens
reasoningTokens    = reasoning_output_tokens
```

`outputTokens` already includes reasoning, so reasoning is never billed twice.

The parser uses the last matching `token_usage_record.turn_token_usage` snapshot inside the selected completed turn. It does not manufacture totals from ambiguous cumulative counters. Missing values remain Unknown; explicit recorded zero remains zero.

## Schema-v2 execution evidence

When present, v2 output may include:

- `sourceLog.fileName` and exact-byte SHA-256 `contentHash`;
- `sessionId` and `turnId`;
- `runtimeVersion` and `originator`;
- `workingDirectory`;
- repository URL, branch, and full baseline commit SHA;
- `timeToFirstTokenMs`;
- `modelInvocationCount`;
- `toolCallCount`;
- `modelContextWindowTokens`;
- `peakInvocation` with paired context-window evidence;
- `quotaWindows`;
- supported execution-environment enums.

Optional evidence is omitted when it cannot be established safely. A paired peak is emitted only when `peakInvocation.inputTokens <= peakInvocation.contextWindowTokens`; invalid optional occupancy evidence is omitted rather than clamped.

Codex `used_percent` is quota evidence only. It is not mapped into PennyTel `usageBefore` / `usageAfter`, which represent a different remaining-percentage measurement contract.

## Source integrity and harvesting

The hardened path preserves a strict evidence boundary:

- source bytes are read once, hashed, and retained as immutable authority;
- emission and curation recheck containment and exact-byte equality;
- source session/turn identity is provenance, not PennyTel Run identity;
- a random persisted UUIDv4 is used for generated PennyTel Run IDs;
- changed evidence for an already-harvested turn fails closed;
- unrelated appended turns remain safe;
- inbox publication uses private staging, a durable journal, exclusive publication, fsync, and recovery under the state lock;
- invalid `sliceId` values are rejected before any inbox, journal, or harvested-state mutation;
- curation copies source logs; it never moves or deletes originals;
- curated raw files and manifests are private (`0600`) inside private staging directories;
- repository URLs reject userinfo and strip query/fragment material;
- no raw source bytes enter the harvest state or PennyTel import JSON.

Current stable PennyTel IDs are nonempty, exactly trimmed strings of at most 200 JavaScript UTF-16 characters.

## Tests

Run the complete parser suite from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

The suite uses reduced synthetic fixtures. Raw local Codex logs are not stored in this repository.

## PennyTel compatibility smoke

The smoke validator checks generated v1/v2 datasets against the actual accepted PennyTel 0.2.1 validation, normalization, import, registry identity, pricing, and execution-evidence code.

First ensure the PennyTel checkout has its Node dependencies installed. Then run from this repository root:

```bash
python3 tests/smoke_pennytel.py \
  /path/to/PennyTel-AX \
  --sessions "$HOME/.codex/sessions" \
  --sample 50
```

The smoke:

- writes no PennyTel files;
- copies no real Codex logs;
- prints aggregate results only;
- synthesizes parent slices and semantic labels solely for validation;
- exercises valid/equal/zero/above-window occupancy cases;
- verifies corrected inbox retry behavior at the stable-ID boundary;
- tests both v1 and v2 imports against PennyTel 0.2.1.

`tests/validate_pennytel.cjs` intentionally requires PennyTel package version `0.2.1` and uses PennyTel's installed TypeScript compiler to load the real validation modules.

## Known limits

- Hardened harvesting currently depends on POSIX filesystem semantics; Windows support is not verified/supported in v0.2.
- Unknown/new telemetry shapes are not guessed.
- Incomplete JSONL and unpaired lifecycle records remain non-emittable.
- Legacy cumulative-only logs retain Unknown run-token totals.
- Multi-turn manual input requires explicit turn selection.
- An actively appending source must be refreshed before emission.
- Full visual Tk smoke requires a working desktop display.
- Referenced PennyTel slices must already exist in the receiving dataset, or be included in the same import.

## Project relationship

This utility is intentionally separate from PennyTel's live storage authority. It emits ordinary PennyTel import artifacts; PennyTel remains responsible for schema validation, relationship checks, registry pricing, conflict detection, and durable persistence.

PennyTel: https://github.com/recoveryrob83-lab/PennyTel-AX

Historical hardening and repair details from the original implementation cycle are preserved in [`REPAIR_NOTES.md`](REPAIR_NOTES.md).
