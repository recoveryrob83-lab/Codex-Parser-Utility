"""Reduced synthetic regressions for accepted independent-critic findings."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

import codex_to_pennytel as core
import codex_parser_ui as ui
import harvest
from test_codex_to_pennytel import fixture
import test_codex_to_pennytel as parser_tests


def rows():
    return [json.loads(line) for line in fixture().splitlines()]


def rendered(data):
    return '\n'.join(json.dumps(row) for row in data) + '\n'


def usage_rows(data):
    return [r['payload'] for r in data if r['type'] == 'token_usage_record']


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'rollout-safe.jsonl'
        self.state = self.root / 'state.json'

    def write(self, data=None):
        self.source.write_text(rendered(rows() if data is None else data))
        return core.load_records(self.source, self.root)

    def run_for(self, data=None, **kwargs):
        return core.build_run(self.write(data), self.source, parser_tests.ParserTests.make_args(self, **kwargs))

    def candidates(self):
        return harvest.find_new_runs([self.root], {'version': 2, 'harvested': {}, 'runIds': {}})

    def test_exact_turn_survives_cumulative_reset_and_compaction(self):
        data = rows()
        for row in data:
            if row['payload'].get('type') == 'token_count':
                row['payload']['info']['total_token_usage'] = dict.fromkeys(core.TOKEN_KEYS, 1)
        data.insert(-1, {'type': 'compacted', 'payload': {}})
        run = self.run_for(data)
        self.assertEqual([run[k] for k in ('inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningTokens')], [800, 3200, 400, 100])

    def test_partial_exact_snapshot_does_not_backfill_or_zero(self):
        data = rows(); usage_rows(data)[-1]['turn_token_usage'] = {'input_tokens': 4000}
        run = self.run_for(data)
        for k in ('inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningTokens'): self.assertNotIn(k, run)
        usage_rows(data)[-1]['turn_token_usage'] = {'output_tokens': 0}
        self.assertEqual(self.run_for(data)['outputTokens'], 0)

    def test_tokenless_completed_turn_and_unsafe_thread_fallback(self):
        for data in ([r for r in rows() if r['type'] != 'token_usage_record'],
                     [r for r in rows() if r['type'] != 'token_usage_record' and r['payload'].get('type') != 'token_count']):
            run = self.run_for(data)
            for k in ('inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningTokens'): self.assertNotIn(k, run)
            self.assertIn('endAt', run)

    def test_provider_canonical(self):
        run = self.run_for()
        self.assertEqual((run['provider'], run['providerId']), ('OpenAI', 'openai-api'))

    def test_payload_lifecycle_and_explicit_duration_ttft(self):
        data = rows()
        for row in data:
            if row['payload'].get('type') == 'task_started': row['payload']['started_at'] = '2026-09-15T01:00:00Z'
            if row['payload'].get('type') == 'task_complete': row['payload']['completed_at'] = '2026-09-15T01:10:00Z'
        run = self.run_for(data)
        self.assertEqual((run['startAt'], run['endAt'], run['wallMinutes']), ('2026-09-15T01:00:00.000Z', '2026-09-15T01:10:00.000Z', 2))
        self.assertEqual(run['executionEvidence']['timeToFirstTokenMs'], 345.5)

    def test_epoch_lifecycle_timestamps_from_real_shape(self):
        data = rows()
        for r in data:
            if r['payload'].get('type') == 'task_started': r['payload']['started_at'] = 1789434000
            if r['payload'].get('type') == 'task_complete': r['payload']['completed_at'] = 1789434060
        run = self.run_for(data)
        self.assertEqual(run['startAt'], '2026-09-15T01:00:00.000Z')
        self.assertEqual(run['endAt'], '2026-09-15T01:01:00.000Z')
        self.assertEqual(run['wallMinutes'], 2)

    def test_duplicate_tuple_and_conflicting_sources(self):
        self.write(); other = self.root / 'rollout-copy.jsonl'; other.write_bytes(self.source.read_bytes())
        self.assertEqual(len(self.candidates()), 1)
        other.write_bytes(other.read_bytes() + b'\n')
        with self.assertRaisesRegex(core.ParseError, 'Conflicting duplicate'): self.candidates()

    def test_duplicate_tuple_inside_same_file(self):
        data = rows(); data += [r for r in rows() if r['type'] != 'session_meta']
        self.write(data)
        self.assertEqual(len(self.candidates()), 1)
        data[-1]['payload']['duration_ms'] = 12
        self.write(data)
        with self.assertRaisesRegex(core.ParseError, 'Conflicting duplicate'): self.candidates()

    def test_delimiter_collisions_are_distinct(self):
        self.assertNotEqual(harvest.identity_key('a:b', 'c'), harvest.identity_key('a', 'b:c'))
        for name, session, turn in [('first', 'a:b', 'c'), ('second', 'a', 'b:c')]:
            data = rows()
            for r in data:
                if r['type'] == 'session_meta': r['payload']['id'] = session; r['payload']['session_id'] = session
                if 'turn_id' in r['payload']: r['payload']['turn_id'] = turn
            (self.root / f'rollout-{name}.jsonl').write_text(rendered(data))
        candidates = self.candidates()
        self.assertEqual(len(candidates), 2)
        self.assertNotEqual(harvest.output_name(candidates[0]), harvest.output_name(candidates[1]))

    def test_source_drift_blocks_build_and_inbox(self):
        records = self.write(); candidate = self.candidates()[0]
        self.source.write_bytes(self.source.read_bytes() + b'\n')
        with self.assertRaisesRegex(core.ParseError, 'stale'): core.build_run(records, self.source, parser_tests.ParserTests.make_args(self))
        with self.assertRaisesRegex(core.ParseError, 'stale'): harvest.save_to_inbox(candidate, {}, self.root / 'out', self.state)
        self.assertTrue(candidate.stale); self.assertFalse((self.root / 'out').exists())

    def test_detected_drift_requires_reparse_even_if_bytes_restored(self):
        records = self.write(); original = self.source.read_bytes()
        self.source.write_bytes(original + b'\n')
        with self.assertRaises(core.ParseError): core.build_run(records, self.source, parser_tests.ParserTests.make_args(self))
        self.source.write_bytes(original)
        with self.assertRaisesRegex(core.ParseError, 'refresh/reparse'): core.build_run(records, self.source, parser_tests.ParserTests.make_args(self))
        self.assertIn('id', core.build_run(core.load_records(self.source), self.source, parser_tests.ParserTests.make_args(self)))

    def test_source_drift_blocks_curation(self):
        self.write(); candidate = self.candidates()[0]; self.source.write_text('{}\n')
        with self.assertRaisesRegex(core.ParseError, 'stale'): harvest.curate_candidates([candidate], self.root / 'curated')
        self.assertFalse((self.root / 'curated').exists())

    def test_digest_and_copy_exact_including_crlf(self):
        self.write(); self.source.write_bytes(self.source.read_bytes().replace(b'\n', b'\r\n'))
        candidate = self.candidates()[0]
        manifest = json.loads(harvest.curate_candidates([candidate], self.root / 'curated').read_text())
        copy = self.root / 'curated' / manifest['runs'][0]['curatedFile']
        self.assertEqual(copy.read_bytes(), self.source.read_bytes())
        self.assertEqual(candidate.source_hash, hashlib.sha256(copy.read_bytes()).hexdigest())

    def test_repository_sensitive_components(self):
        for url, expected in [('https://example.test/repo?token=PRIVATE#PRIVATE', 'https://example.test/repo'), ('https://user:PRIVATE@example.test/repo', None), ('ssh://PRIVATE@example.test/repo', None), ('https://[invalid', None)]:
            self.assertEqual(core._safe_repository_url(url), expected)
            data = rows(); data[0]['payload']['git']['repository_url'] = url
            self.assertNotIn('PRIVATE', json.dumps(self.run_for(data)))

    def test_invalid_metadata_never_leaks_to_notes(self):
        data = rows()
        data[0]['payload']['id'] = 'PRIVATE\nINVALID'; data[0]['payload']['session_id'] = 'PRIVATE\nINVALID'
        for r in data:
            if 'turn_id' in r['payload']: r['payload']['turn_id'] = 'PRIVATE\nTURN'
        for target in ('v1', 'v2'):
            self.assertNotIn('PRIVATE', json.dumps(self.run_for(data, target=target)))

    def test_symlink_escape_discovery_and_curation(self):
        selected = self.root / 'selected'; selected.mkdir(); self.write()
        link = selected / 'rollout-link.jsonl'; link.symlink_to(self.source)
        self.assertEqual(harvest.find_new_runs([selected], {'harvested': {}}), [])
        with self.assertRaisesRegex(core.ParseError, 'escapes'): core.load_records(link, selected)
        link.unlink(); link.write_bytes(self.source.read_bytes())
        candidate = harvest.find_new_runs([selected], {'harvested': {}})[0]
        link.unlink(); link.symlink_to(self.source)
        with self.assertRaises(core.ParseError): harvest.curate_candidates([candidate], self.root / 'curated')

    def test_opaque_stable_run_id_review_and_persistence(self):
        self.write(); item = ui.inspect_log(self.source)
        first = ui.stable_run_id(item, self.state)
        self.assertEqual(uuid.UUID(first).version, 4)
        self.assertNotIn('session-1', first); self.assertNotIn('turn-1', first)
        self.assertEqual(first, ui.stable_run_id(ui.inspect_log(self.source), self.state))
        candidate = self.candidates()[0]
        run = core.build_run(item.records, self.source, parser_tests.ParserTests.make_args(self, run_id=first))
        output = harvest.save_to_inbox(candidate, core.dataset_for_run(run, 'v2'), self.root / 'inbox', self.state)
        before = output.read_bytes()
        with self.assertRaisesRegex(core.ParseError, 'already harvested'): harvest.save_to_inbox(candidate, {}, output.parent, self.state)
        self.assertEqual(before, output.read_bytes())
        self.assertEqual(harvest.find_new_runs([self.root], harvest.load_state(self.state)), [])
        self.assertNotIn('payload', self.state.read_text()); self.assertNotIn('usage', self.state.read_text())

    def test_repeated_fallback_invocation_snapshot_unknown_count(self):
        data = [r for r in rows() if r['type'] != 'token_usage_record']
        for r in data:
            if r['payload'].get('type') == 'token_count': r['payload']['info']['last_token_usage'] = {'input_tokens': 12}
        evidence = self.run_for(data)['executionEvidence']
        self.assertNotIn('modelInvocationCount', evidence)
        self.assertEqual(evidence['peakInvocation'], {'inputTokens': 12})

    def test_response_and_tool_identity_dedupe(self):
        data = rows()
        data.insert(-1, next(r for r in data if r['type'] == 'token_usage_record'))
        data.insert(-1, next(r for r in data if r['payload'].get('type') == 'item_completed'))
        evidence = self.run_for(data)['executionEvidence']
        self.assertEqual((evidence['modelInvocationCount'], evidence['toolCallCount']), (2, 1))

    def test_peak_window_pair_and_invalid_evidence(self):
        data = rows(); usage_rows(data)[0]['model_context_window'] = 9999; usage_rows(data)[1]['model_context_window'] = 4000
        evidence = self.run_for(data)['executionEvidence']
        self.assertEqual((evidence['peakInvocation']['inputTokens'], evidence['peakInvocation']['contextWindowTokens']), (3000, 4000))
        usage_rows(data)[1]['model_context_window'] = 2000
        self.assertNotIn('peakInvocation', self.run_for(data)['executionEvidence'])

    def test_quota_identity_changes_omit_aggregate(self):
        for container, key, value in [('limits', 'plan_type', 'other'), ('limits', 'limit_id', 'other'), ('limits', 'meter_id', 'other'), ('limits', 'window_id', 'other'), ('reading', 'window_minutes', 60), ('reading', 'window_id', 'other'), ('reading', 'meter_id', 'other')]:
            data = rows(); limits = [r['payload']['rate_limits'] for r in data if 'rate_limits' in r['payload']][-1]
            (limits if container == 'limits' else limits['primary'])[key] = value
            self.assertNotIn('quotaWindows', self.run_for(data)['executionEvidence'])

    def test_corrupt_state_fails_closed_and_preserves_bytes(self):
        for content in ('{bad', '{}', '[]', '{"version":99,"harvested":{}}', '{"version":2,"harvested":[],"runIds":{}}', '{"harvested":{},"harvested":{}}'):
            self.state.write_text(content)
            with self.assertRaises(core.ParseError): harvest.load_state(self.state)
            self.assertEqual(self.state.read_text(), content)
        self.write()
        with self.assertRaises(core.ParseError): harvest.ensure_run_id(core.load_records(self.source), core.find_turns(core.load_records(self.source))[0], self.state)

    def test_concurrent_uuid_reservations_keep_one_mapping(self):
        from concurrent.futures import ThreadPoolExecutor
        records = self.write(); turn = core.find_turns(records)[0]
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda _: harvest.ensure_run_id(records, turn, self.state), range(8)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(harvest.load_state(self.state)['runIds']), 1)

    def test_selected_root_replaced_by_symlink_is_refused(self):
        selected = self.root / 'selected'; selected.mkdir()
        source = selected / 'rollout-safe.jsonl'; source.write_text(fixture())
        candidate = harvest.find_new_runs([selected], {'harvested': {}})[0]
        selected.rename(self.root / 'moved')
        selected.symlink_to(self.root / 'moved', target_is_directory=True)
        with self.assertRaises(core.ParseError): candidate.verified_bytes()

    def test_ui_emission_preserves_folders_and_persistent_identity(self):
        from types import SimpleNamespace
        self.write(); item = ui.inspect_log(self.source)
        def var(value): return SimpleNamespace(get=lambda: value)
        app = SimpleNamespace(input_root=self.root, items=[item], target_var=var('v2'),
            output_var=var(str(self.root / 'out')), session_mode_var=var(''),
            context_mode_var=var(''), result_var=var(''), quota_attribution_var=var('Unknown'),
            harvest_candidates={}, effective_labels=lambda _: ('qa', 'Repair', 'Repair'),
            refresh_tree=lambda: None, status_var=SimpleNamespace(set=lambda _: None))
        with patch.object(harvest, 'state_file', return_value=self.state), patch.object(ui.messagebox, 'showinfo'), patch.object(ui.messagebox, 'showwarning') as warning:
            ui.ParserUI.parse_data(app)
            output = ui.output_path_for(item, self.root, self.root / 'out')
            self.assertTrue(output.exists()); self.assertFalse(warning.called)
            first = json.loads(output.read_text())['runs'][0]['id']
            app.output_var = var(str(self.root / 'inbox'))
            ui.ParserUI.parse_data(app, mark_harvested=True, inbox=True)
            saved = next((self.root / 'inbox').glob('*.json'))
            self.assertEqual(first, json.loads(saved.read_text())['runs'][0]['id'])
            self.source.write_bytes(self.source.read_bytes() + b'\n')
            ui.ParserUI.parse_data(app)
            self.assertTrue(warning.called); self.assertTrue(item.status.startswith('ERROR'))

    def test_v1_helper_strips_v2_without_mutating_input(self):
        run = self.run_for()
        self.assertNotIn('executionEvidence', core.dataset_for_run(run, 'v1')['runs'][0])
        self.assertIn('executionEvidence', run)

    def test_filename_collision_and_extreme_length(self):
        self.write(); candidate = self.candidates()[0]
        names = []
        for value in ('a/b', 'a?b', 'a'*200):
            data = rows(); data[0]['payload']['session_id'] = value
            self.write(data); candidate = self.candidates()[0]
            name = harvest.output_name(candidate); names.append(name)
            self.assertLess(len(name.encode()), 120)
        self.assertEqual(len(set(names)), 3)
        long_path = self.root / ('x'*248 + '.jsonl')
        self.assertLess(len(core.output_filename(long_path)), 120)
        destination = self.root / 'output.json'
        core.write_output(destination, 'first')
        with self.assertRaises(FileExistsError): core.write_output(destination, 'second')
        self.assertEqual(destination.read_text(), 'first')


if __name__ == '__main__': unittest.main()
