"""Adversarial snapshots, durable transactions and current 0.2.1 regressions."""
import copy
from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import codex_to_pennytel as core
import harvest
from test_codex_to_pennytel import fixture
import test_codex_to_pennytel as parser_tests
from test_repairs import rows, rendered, usage_rows


class Crash(BaseException):
    """Skip ordinary exception recovery, as abrupt process termination would."""


class HardeningTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'rollout-safe.jsonl'
        self.state = self.root / 'state.json'
        self.inbox = self.root / 'inbox'
        self.curated = self.root / 'curated'

    def prepare(self, data=None, target='v2'):
        self.source.write_text(rendered(rows() if data is None else data))
        candidate = harvest.find_new_runs([self.root], {'harvested': {}})[0]
        run_id = harvest.ensure_run_id(candidate.records, candidate.turn, self.state)
        args = parser_tests.ParserTests.make_args(self, run_id=run_id, target=target)
        run = core.build_run(candidate.records, candidate.path, args)
        return candidate, core.dataset_for_run(run, target), args

    def publish(self, candidate, dataset):
        return harvest.save_to_inbox(candidate, dataset, self.inbox, self.state)

    def assert_recovered(self, candidate, dataset):
        state = harvest.load_state(self.state)  # also runs crash recovery
        output = self.inbox / harvest.output_name(candidate)
        committed = candidate.identity in state['harvested']
        self.assertEqual(committed, output.exists())
        self.assertFalse(harvest._journal_path(self.state).exists())
        self.assertEqual(list(self.inbox.glob('*.tmp')), [])
        if not committed:
            self.publish(candidate, dataset)
        self.assertEqual(json.loads(output.read_text()), dataset)
        self.assertIn(candidate.identity, harvest.load_state(self.state)['harvested'])

    def test_caller_mutation_cannot_change_emission_identity_or_curation(self):
        candidate, dataset, args = self.prepare()
        original = self.source.read_bytes()
        identity = candidate.identity
        candidate.session.update(session_id='forged', cwd='/forged')
        candidate.context['collaboration_mode']['settings']['reasoning_effort'] = 'max'
        candidate.turn.end_record.payload.update(duration_ms=1, time_to_first_token_ms=999)
        for r in candidate.records:
            if r.type == 'token_usage_record': r.payload['turn_token_usage']['input_tokens'] = 999999
        candidate.records._records.reverse()
        candidate.records._records.clear()
        self.assertEqual(candidate.identity, identity)
        self.assertEqual(core.build_run(candidate.records, candidate.path, args), dataset['runs'][0])
        evidence = core.build_execution_evidence(candidate.records, candidate.path, candidate.turn, {'session_id': 'forged'}, {'model': 'forged'})
        self.assertEqual(evidence, dataset['runs'][0]['executionEvidence'])
        with self.assertRaises(FrozenInstanceError): candidate.records.digest = '0' * 64
        with self.assertRaises(FrozenInstanceError): candidate.source_hash = '0' * 64
        manifest = json.loads(harvest.curate_candidates([candidate], self.curated).read_text())
        entry = manifest['runs'][0]
        self.assertEqual(entry['evidence'], evidence)
        self.assertEqual((entry['model'], entry['thinking'], entry['wallMinutes']), ('GPT-5.6 Sol', 'Medium', 2))
        copied = (self.curated / entry['curatedFile']).read_bytes()
        self.assertEqual(copied, original)
        self.assertEqual(hashlib.sha256(copied).hexdigest(), evidence['sourceLog']['contentHash']['value'])
        self.publish(candidate, dataset)

    def test_returned_run_mutation_does_not_poison_later_emission(self):
        c, dataset, args = self.prepare()
        run = core.build_run(c.records, c.path, args)
        run['executionEvidence']['peakInvocation']['inputTokens'] = 99
        run['outputTokens'] = 0
        self.assertEqual(core.build_run(c.records, c.path, args), dataset['runs'][0])

    def test_inbox_detaches_dataset_before_publication(self):
        c, dataset, _ = self.prepare()
        original = copy.deepcopy(dataset)
        def mutate(name):
            if name == 'inbox_prepared':
                dataset['runs'][0]['outputTokens'] = 0
                dataset['runs'][0]['executionEvidence']['sessionId'] = 'forged'
                c.records._records.clear()
        with patch.object(harvest, '_checkpoint', side_effect=mutate): output = self.publish(c, dataset)
        self.assertEqual(json.loads(output.read_text()), original)

    def test_inbox_rejects_empty_extra_missing_forged_and_invalid_runs(self):
        c, original, _ = self.prepare()
        variants = []
        for runs in ([], None, [original['runs'][0], original['runs'][0]], [{}]):
            d = copy.deepcopy(original); d['runs'] = runs; variants.append(d)
        for key, value in [('id', 'wrong'), ('role', 'nonsense'), ('sliceId', ''), ('outputTokens', 0), ('unknown', True), ('executionEvidence', None)]:
            d = copy.deepcopy(original); d['runs'][0][key] = value; variants.append(d)
        for key, value in [('turnId', 'other'), ('sessionId', 'other'), ('sourceLog', {})]:
            d = copy.deepcopy(original); d['runs'][0]['executionEvidence'][key] = value; variants.append(d)
        for key, value in [('schemaVersion', True), ('revision', False)]:
            d = copy.deepcopy(original); d[key] = value; variants.append(d)
        for d in variants:
            with self.subTest(dataset=d), self.assertRaises(core.ParseError): self.publish(c, d)
            self.assertNotIn(c.identity, harvest.load_state(self.state)['harvested'])
            self.assertFalse(self.inbox.exists())

    def test_v1_inbox_proves_the_intended_run_without_v2_evidence(self):
        c, dataset, _ = self.prepare(target='v1')
        self.publish(c, dataset)
        self.assert_recovered(c, dataset)

    def test_inbox_invalid_slice_id_has_no_side_effects_and_corrected_retry_emits_once(self):
        invalid_ids = {
            '201 characters': 's' * 201,
            'leading whitespace': ' slice',
            'trailing whitespace': 'slice ',
            'empty': '',
            'whitespace only': '\t \n',
            'non-string': 123,
            'null': None,
            'leading BOM whitespace': '\ufeffslice',
            'trailing BOM whitespace': 'slice\ufeff',
            '201 UTF-16 units': '\U0001f680' * 100 + 's',
        }
        for target in ('v1', 'v2'):
            for label, slice_id in invalid_ids.items():
                with self.subTest(target=target, case=label):
                    self.setUp()
                    c, dataset, _ = self.prepare(target=target)
                    intended = copy.deepcopy(dataset)
                    dataset['runs'][0]['sliceId'] = slice_id
                    before_state = self.state.read_bytes()
                    before_files = set(self.root.rglob('*'))
                    with patch.object(harvest, 'state_transaction', wraps=harvest.state_transaction) as transaction, \
                         patch.object(harvest, 'mark_harvested', wraps=harvest.mark_harvested) as mark, \
                         patch.object(harvest, '_atomic_json', wraps=harvest._atomic_json) as atomic, \
                         patch.object(harvest, '_private_write', wraps=harvest._private_write) as stage, \
                         patch.object(harvest.os, 'link', wraps=harvest.os.link) as publish:
                        with self.assertRaisesRegex(core.ParseError, 'sliceId'):
                            self.publish(c, dataset)
                        for operation in (transaction, mark, atomic, stage, publish):
                            operation.assert_not_called()
                    self.assertEqual(self.state.read_bytes(), before_state)
                    self.assertEqual(set(self.root.rglob('*')), before_files)
                    self.assertFalse(self.inbox.exists())
                    self.assertFalse(harvest._journal_path(self.state).exists())
                    state = harvest.load_state(self.state)
                    self.assertNotIn(c.identity, state['harvested'])
                    self.assertEqual([item.identity for item in harvest.find_new_runs([self.root], state)], [c.identity])

                    dataset['runs'][0]['sliceId'] = intended['runs'][0]['sliceId']
                    output = self.publish(c, dataset)
                    self.assertEqual(json.loads(output.read_text()), intended)
                    self.assertEqual(list(self.inbox.iterdir()), [output])
                    self.assertFalse(harvest._journal_path(self.state).exists())
                    state = harvest.load_state(self.state)
                    self.assertEqual(list(state['harvested']), [c.identity])
                    self.assertEqual(state['runIds'], json.loads(before_state)['runIds'])
                    self.assertEqual(harvest.find_new_runs([self.root], state), [])
                    committed_state = self.state.read_bytes()
                    with self.assertRaisesRegex(core.ParseError, 'already harvested'):
                        self.publish(c, dataset)
                    self.assertEqual(self.state.read_bytes(), committed_state)
                    self.assertEqual(list(self.inbox.iterdir()), [output])

    def test_inbox_accepts_trimmed_slice_id_at_stable_id_limit(self):
        for target in ('v1', 'v2'):
            for slice_id in ('s' * 200, '\U0001f680' * 100, 'slice with internal spaces'):
                with self.subTest(target=target, slice_id=slice_id):
                    self.setUp()
                    c, dataset, _ = self.prepare(target=target)
                    dataset['runs'][0]['sliceId'] = slice_id
                    self.assertEqual(dataset['slices'], [])
                    output = self.publish(c, dataset)
                    self.assertEqual(json.loads(output.read_text()), dataset)
                    self.assertEqual(list(self.inbox.iterdir()), [output])
                    self.assertIn(c.identity, harvest.load_state(self.state)['harvested'])
                    self.assertFalse(harvest._journal_path(self.state).exists())

    def test_every_inbox_commit_boundary_exception_and_crash_recovers(self):
        boundaries = [('atomic_file_synced', 1), ('atomic_replaced', 1), ('atomic_directory_synced', 1),
                      ('inbox_prepared', 1), ('inbox_staged', 1), ('inbox_linked', 1), ('inbox_output_synced', 1),
                      ('atomic_file_synced', 2), ('atomic_replaced', 2), ('atomic_directory_synced', 2),
                      ('inbox_state_committed', 1), ('inbox_stage_removed', 1), ('inbox_journal_removed', 1)]
        for error in (OSError, Crash):
            for boundary, occurrence in boundaries:
                with self.subTest(error=error.__name__, boundary=boundary, occurrence=occurrence):
                    self.setUp()
                    c, dataset, _ = self.prepare()
                    seen = 0
                    def fail(name):
                        nonlocal seen
                        if name == boundary:
                            seen += 1
                            if seen == occurrence: raise error('injected')
                    with patch.object(harvest, '_checkpoint', side_effect=fail), self.assertRaises(error): self.publish(c, dataset)
                    self.assertGreaterEqual(seen, occurrence)
                    self.assert_recovered(c, dataset)

    def test_inbox_write_link_replace_and_directory_failures_recover(self):
        for operation in ('write', 'link', 'replace', 'fsync'):
            with self.subTest(operation=operation):
                self.setUp(); c, dataset, _ = self.prepare()
                owner, method = {'write': (harvest, '_private_write'), 'link': (os, 'link'), 'replace': (os, 'replace'), 'fsync': (os, 'fsync')}[operation]
                original = getattr(owner, method)
                fired = False
                def fail_once(*args, **kwargs):
                    nonlocal fired
                    if not fired:
                        fired = True
                        if operation == 'write': original(args[0], args[1][:12])
                        raise OSError('injected')
                    return original(*args, **kwargs)
                with patch.object(owner, method, side_effect=fail_once), self.assertRaises(OSError): self.publish(c, dataset)
                self.assert_recovered(c, dataset)

    def test_interrupted_rollback_recovers_on_next_attempt(self):
        c, dataset, _ = self.prepare()
        def fail(name):
            if name == 'inbox_linked': raise OSError('publication failed')
            if name == 'inbox_rollback_unlinked': raise Crash('rollback interrupted')
        with patch.object(harvest, '_checkpoint', side_effect=fail), self.assertRaises(Crash): self.publish(c, dataset)
        self.assert_recovered(c, dataset)

    def test_abrupt_process_exit_recovers_at_commit_boundaries(self):
        script = '''
import json, os, sys
from pathlib import Path
import harvest
root, boundary, occurrence = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
c = harvest.find_new_runs([root], {'harvested': {}})[0]
seen = 0
def crash(name):
    global seen
    if name == boundary:
        seen += 1
        if seen == occurrence: os._exit(73)
harvest._checkpoint = crash
harvest.save_to_inbox(c, json.loads(sys.stdin.read()), root / 'inbox', root / 'state.json')
'''
        for boundary, occurrence in [('atomic_file_synced', 1), ('atomic_replaced', 1),
                                     ('inbox_staged', 1), ('inbox_linked', 1),
                                     ('atomic_file_synced', 2), ('atomic_replaced', 2),
                                     ('inbox_state_committed', 1), ('inbox_stage_removed', 1), ('inbox_journal_removed', 1)]:
            with self.subTest(boundary=boundary, occurrence=occurrence):
                self.setUp(); c, dataset, _ = self.prepare()
                env = dict(os.environ, PYTHONPATH=str(Path(harvest.__file__).parent))
                process = subprocess.run([sys.executable, '-c', script, str(self.root), boundary, str(occurrence)],
                                         input=json.dumps(dataset), text=True, capture_output=True, env=env)
                self.assertEqual(process.returncode, 73, process.stderr)
                self.assert_recovered(c, dataset)

    def test_inbox_does_not_remove_a_concurrent_output_collision(self):
        c, dataset, _ = self.prepare()
        output = self.inbox / harvest.output_name(c)
        def collide(name):
            if name == 'inbox_staged': output.write_text('existing owner')
        with patch.object(harvest, '_checkpoint', side_effect=collide), self.assertRaises(FileExistsError): self.publish(c, dataset)
        self.assertEqual(output.read_text(), 'existing owner')
        self.assertNotIn(c.identity, harvest.load_state(self.state)['harvested'])
        self.assertFalse(harvest._journal_path(self.state).exists())

    def test_inbox_symlink_collision_is_preserved_without_harvest(self):
        c, dataset, _ = self.prepare()
        output = self.inbox / harvest.output_name(c)
        def collide(name):
            if name == 'inbox_staged': output.symlink_to(self.source)
        with patch.object(harvest, '_checkpoint', side_effect=collide), self.assertRaises(FileExistsError): self.publish(c, dataset)
        self.assertTrue(output.is_symlink())
        self.assertNotIn(c.identity, harvest.load_state(self.state)['harvested'])
        self.assertFalse(harvest._journal_path(self.state).exists())

    def test_private_files_even_under_permissive_umask(self):
        c, dataset, _ = self.prepare()
        os.chmod(self.source, 0o600)
        old_umask = os.umask(0o002)
        try:
            manifest = harvest.curate_candidates([c], self.curated)
            self.publish(c, dataset)
        finally: os.umask(old_umask)
        for path in [manifest, *self.curated.rglob('*.jsonl'), self.state, self.inbox / harvest.output_name(c)]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.source.stat().st_mode), 0o600)

    def test_curation_failures_publish_no_batch_and_preserve_sources(self):
        for failure in ('copy', 'manifest', 'second_copy', 'source_drift', 'published'):
            with self.subTest(failure=failure):
                self.setUp(); c, _, _ = self.prepare()
                other = self.root / 'rollout-other.jsonl'
                data = rows(); data[0]['payload']['session_id'] = 'session-2'
                other.write_text(rendered(data))
                candidates = harvest.find_new_runs([self.root], {'harvested': {}})
                before = {p: p.read_bytes() for p in (self.source, other)}
                original_write = harvest._private_write
                copies = 0
                def write(path, raw):
                    nonlocal copies
                    if path.suffix == '.jsonl': copies += 1
                    if (failure == 'copy' and copies == 1 or failure == 'second_copy' and copies == 2 or failure == 'manifest' and path.name == 'manifest.json'):
                        original_write(path, raw[:20]); raise OSError('injected copy/manifest failure')
                    original_write(path, raw)
                def checkpoint(name):
                    if failure == 'source_drift' and name == 'curation_manifest': self.source.write_bytes(before[self.source] + b'\n')
                    if failure == 'published' and name == 'curation_published': raise OSError('injected publication failure')
                with patch.object(harvest, '_private_write', side_effect=write), patch.object(harvest, '_checkpoint', side_effect=checkpoint), self.assertRaises((OSError, core.ParseError)):
                    harvest.curate_candidates(candidates, self.curated)
                self.assertFalse(self.curated.exists())
                self.assertEqual(list(self.root.glob('.curation-*')), [])
                for p, raw in before.items(): self.assertEqual(p.read_bytes(), raw + (b'\n' if failure == 'source_drift' and p == self.source else b''))

    def test_curated_batch_collision_preserves_previous_batch(self):
        c, _, _ = self.prepare()
        path = harvest.curate_candidates([c], self.curated)
        before = path.read_bytes()
        with self.assertRaises(FileExistsError): harvest.curate_candidates([c], self.curated)
        self.assertEqual(path.read_bytes(), before)

    def test_harvested_identical_and_append_only_skip_but_changed_turn_conflicts(self):
        c, dataset, _ = self.prepare()
        self.publish(c, dataset)
        state = harvest.load_state(self.state)
        self.assertEqual(harvest.find_new_runs([self.root], state), [])
        additional = [json.loads(line) for line in fixture(two_turns=True).splitlines()][len(rows()):]
        self.source.write_bytes(self.source.read_bytes() + rendered(additional).encode())
        self.assertEqual([x.turn.turn_id for x in harvest.find_new_runs([self.root], state)], ['turn-2'])
        data = rows(); usage_rows(data)[0]['usage']['output_tokens'] += 1
        self.source.write_text(rendered(data + additional))
        with self.assertRaisesRegex(core.ParseError, 'Harvested evidence conflict'): harvest.find_new_runs([self.root], state)

    def test_harvested_conflict_checked_before_auto_review_skip(self):
        c, dataset, _ = self.prepare(); self.publish(c, dataset)
        data = rows(); data[0]['payload']['thread_source'] = 'guardian_review'
        self.source.write_text(rendered(data))
        with self.assertRaises(core.ParseError): harvest.find_new_runs([self.root], harvest.load_state(self.state))

    def test_harvested_missing_or_malformed_turn_fails_closed(self):
        c, dataset, _ = self.prepare(); self.publish(c, dataset)
        for text in (rendered(rows()[:-1]), '{bad\n'):
            self.source.write_text(text)
            with self.assertRaisesRegex(core.ParseError, 'Harvested evidence conflict'): harvest.find_new_runs([self.root], harvest.load_state(self.state))

    def test_fingerprint_excludes_private_payloads_and_file_growth(self):
        data = rows()
        data.insert(-1, {'type': 'response_item', 'payload': {'message': 'PRIVATE_PROMPT_SENTINEL', 'command': 'PRIVATE_COMMAND_SENTINEL'}})
        c, dataset, _ = self.prepare(data); self.publish(c, dataset)
        manifest = harvest.curate_candidates([c], self.curated).read_text()
        for text in (manifest, json.dumps(dataset)):
            for marker in ('PRIVATE_', 'payload', 'command', 'message'): self.assertNotIn(marker, text)
        state = harvest.load_state(self.state)
        entry = state['harvested'][c.identity]
        self.assertEqual(set(entry), {'sessionId', 'turnId', 'sourceFile', 'sourceHash', 'fingerprintVersion', 'evidenceHash'})
        for marker in ('PRIVATE_', 'payload', 'command', 'message', 'turnUsage'): self.assertNotIn(marker, self.state.read_text())
        data[-2]['payload']['message'] = 'ANOTHER_PRIVATE_PROMPT'
        self.source.write_text(rendered(data))
        self.assertEqual(harvest.find_new_runs([self.root], state), [])

    def test_legacy_state_does_not_guess_append_integrity(self):
        c, _, _ = self.prepare()
        state = harvest.load_state(self.state); harvest.mark_harvested(c, state)
        entry = state['harvested'][c.identity]
        del entry['evidenceHash']; del entry['fingerprintVersion']
        self.assertEqual(harvest.find_new_runs([self.root], state), [])
        self.source.write_bytes(self.source.read_bytes() + b'\n')
        with self.assertRaisesRegex(core.ParseError, 'legacy'): harvest.find_new_runs([self.root], state)

    def test_state_fsync_order_file_replace_directory_and_mode(self):
        events = []
        real_fsync, real_replace = os.fsync, os.replace
        def sync(fd):
            events.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
            return real_fsync(fd)
        def replace(*args):
            events.append('replace'); return real_replace(*args)
        with patch.object(os, 'fsync', side_effect=sync), patch.object(os, 'replace', side_effect=replace):
            harvest.save_state({'version': 3, 'harvested': {}, 'runIds': {}}, self.state)
        self.assertEqual(events, ['directory', 'file', 'replace', 'directory'])
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)

    def test_quota_reset_transition_and_ordinary_same_window(self):
        c, dataset, _ = self.prepare()
        quota = dataset['runs'][0]['executionEvidence']['quotaWindows'][0]
        self.assertEqual(quota['first']['resetsAt'], quota['last']['resetsAt'])
        data = rows()
        limits = [r['payload']['rate_limits'] for r in data if 'rate_limits' in r['payload']]
        limits[-1]['primary'].update(resets_at=1800604800, used_percent=0)
        c, dataset, _ = self.prepare(data)
        quota = dataset['runs'][0]['executionEvidence']['quotaWindows'][0]
        self.assertNotEqual(quota['first']['resetsAt'], quota['last']['resetsAt'])
        self.assertEqual((quota['first']['usedPercent'], quota['last']['usedPercent']), (29, 0))
        self.assertEqual(quota['attribution'], 'Unknown')

    def test_peak_valid_equal_zero_and_above_window_021(self):
        for tokens, window in ((3000, 4000), (3000, 3000), (0, 3000), (3000, 2999)):
            data = rows()
            for p in usage_rows(data):
                p['usage'] = {'input_tokens': tokens, 'cached_input_tokens': 0}
                p['model_context_window'] = window
            _, dataset, _ = self.prepare(data)
            evidence = dataset['runs'][0]['executionEvidence']
            if tokens > window: self.assertNotIn('peakInvocation', evidence)
            else: self.assertEqual(evidence['peakInvocation'], {'inputTokens': tokens, 'cachedInputTokens': 0, 'contextWindowTokens': window})


if __name__ == '__main__': unittest.main()
