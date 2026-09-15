// Read-only adapter to CURRENT PennyTel 0.2.1 and its canonical registry.
// Usage: node validate_pennytel.cjs /path/to/PennyTel < datasets.json
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const authority = path.resolve(process.argv[2]);
const version = JSON.parse(fs.readFileSync(path.join(authority, 'package.json'), 'utf8')).version;
assert.equal(version, '0.2.1', 'Use accepted PennyTel 0.2.1, not the older Slice 6 authority');
const ts = require(path.join(authority, 'node_modules/typescript'));
require.extensions['.ts'] = (module, filename) => {
  const text = fs.readFileSync(filename, 'utf8');
  module._compile(ts.transpileModule(text, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
  }).outputText, filename);
};
const { validateExecutionEvidence } = require(path.join(authority, 'src/shared/execution-evidence.ts'));
const { normalizeDataset, mergeImport } = require(path.join(authority, 'src/shared/data.ts'));
const { emptyDataset } = require(path.join(authority, 'src/shared/types.ts'));
const { parseRegistry, resolveIdentity } = require(path.join(authority, 'src/shared/registry.ts'));
const registry = parseRegistry(fs.readFileSync(path.join(authority, 'docs/PennyTel_Model_Registry_v0.2_Canonical_Seed_2026-09-12.json'), 'utf8'));
const datasets = JSON.parse(fs.readFileSync(0, 'utf8'));
for (const [inputTokens, contextWindowTokens] of [[3000, 4000], [3000, 3000], [0, 3000]]) {
  validateExecutionEvidence({ kind: 'codex-rollout', formatVersion: 1,
    peakInvocation: { inputTokens, contextWindowTokens, cachedInputTokens: 0 } });
}
const aboveWindow = { kind: 'codex-rollout', formatVersion: 1,
  peakInvocation: { inputTokens: 3001, contextWindowTokens: 3000 } };
assert.throws(() => validateExecutionEvidence(aboveWindow), /exceeds paired context window/);
const invalidDataset = structuredClone(datasets.find(d => d.schemaVersion === 2));
invalidDataset.runs[0].executionEvidence = aboveWindow;
assert.throws(() => normalizeDataset(invalidDataset), /exceeds paired context window/);
assert.throws(() => mergeImport({ ...emptyDataset(), registry }, JSON.stringify(invalidDataset)), /exceeds paired context window/);
let evidence = 0, runs = 0, resolved = 0, priced = 0, v1 = 0;
for (const input of datasets) {
  const normalized = normalizeDataset(input);
  assert.equal(normalized.runs.length, input.runs.length);
  assert.deepEqual(normalized.runs, input.runs, 'Normalization must preserve evidence and omissions');
  if (input.schemaVersion === 1) v1++;
  for (const run of input.runs) {
    runs++;
    if (run.executionEvidence) { validateExecutionEvidence(run.executionEvidence); evidence++; }
    const identity = resolveIdentity(run, registry);
    if (['openai:gpt-6-astra', 'openai:gpt-5.6-sol', 'openai:gpt-5.6-luna'].includes(run.modelId)) {
      assert.ok(identity, 'Known model/provider must resolve');
      assert.equal(identity.provider.id, 'openai-api');
      resolved++;
    }
  }
  const merged = mergeImport({ ...emptyDataset(), registry }, JSON.stringify(input)).data;
  for (const run of merged.runs) {
    if (resolveIdentity(run, registry)) {
      assert.ok(run.priceSnapshot, 'Eligible dated registered run must receive pricing');
      assert.equal(run.priceSnapshot.providerId, 'openai-api');
      assert.equal(run.priceSnapshot.source, 'Registry');
      priced++;
    }
  }
  assert.deepEqual(normalizeDataset(merged).runs, merged.runs, 'Historical price snapshots must survive normalization');
  const reimported = mergeImport({ ...emptyDataset(), registry }, JSON.stringify(merged)).data;
  assert.deepEqual(reimported.runs, merged.runs, 'Historical price snapshots must survive import');
}
console.log(JSON.stringify({ version, datasets: datasets.length, runs, evidence, v1, resolved, priced,
  historicalSnapshotsPreserved: true,
  occupancy: { valid: true, equality: true, zero: true, aboveWindowRejected: true } }));
