"""Read-only real-log + current PennyTel 0.2.1 smoke; aggregate results only."""
import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_to_pennytel as core
import harvest
from test_codex_to_pennytel import fixture


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument('authority', type=Path)
    cli.add_argument('--sessions', type=Path)
    cli.add_argument('--sample', type=int, default=50)
    opts = cli.parse_args()
    datasets, counts, models = [], Counter(), Counter()
    inbox_cases = 0

    def collect(path, records, turn, variant=None):
        args = argparse.Namespace(turn_id=turn.turn_id, run_id=str(uuid.uuid4()), slice_id='qa-slice', run_type='Repair', role='Repair', target='v2')
        run = core.build_run(records, path, args)
        evidence = run['executionEvidence']
        if variant in ('paired', 'equal', 'zero'):
            expected = {'paired': (3000, 4000), 'equal': (3000, 3000), 'zero': (0, 3000)}[variant]
            assert (evidence['peakInvocation']['inputTokens'], evidence['peakInvocation']['contextWindowTokens']) == expected
        if variant == 'above': assert 'peakInvocation' not in evidence
        if variant == 'reset':
            quota = evidence['quotaWindows'][0]
            assert quota['first']['resetsAt'] != quota['last']['resetsAt'] and quota['last']['usedPercent'] == 0
        for version in ('v2', 'v1'):
            dataset = core.dataset_for_run(run, version)
            dataset['slices'] = [{'id': 'qa-slice', 'title': 'Local parser QA'}]
            datasets.append(dataset)
        models[run.get('model', 'Unknown')] += 1
        counts['emitted_turns'] += 1
        counts['turns_with_exact_tokens'] += int('inputTokens' in run)

    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / 'rollout-synthetic.jsonl'
        for model in core.MODEL_MAP:
            for variant in ('complete', 'partial', 'tokenless', 'paired', 'equal', 'zero', 'above', 'reset'):
                data = [json.loads(line) for line in fixture(model=model).splitlines()]
                if variant == 'tokenless':
                    data = [r for r in data if r['type'] != 'token_usage_record' and r['payload'].get('type') != 'token_count']
                if variant == 'partial':
                    for r in data:
                        if r['type'] == 'token_usage_record': r['payload']['turn_token_usage'] = {'output_tokens': 12}
                if variant in ('paired', 'equal', 'zero', 'above'):
                    tokens, window = {'paired': (3000, 4000), 'equal': (3000, 3000), 'zero': (0, 3000), 'above': (3000, 2999)}[variant]
                    for r in data:
                        if r['type'] == 'token_usage_record':
                            r['payload']['usage'] = {'input_tokens': tokens, 'cached_input_tokens': 0}
                            r['payload']['model_context_window'] = window
                if variant == 'reset':
                    limits = [r['payload']['rate_limits'] for r in data if 'rate_limits' in r['payload']]
                    limits[-1]['primary'].update(resets_at=1800604800, used_percent=0)
                path.write_text('\n'.join(json.dumps(r) for r in data) + '\n')
                records = core.load_records(path)
                collect(path, records, core.find_turns(records)[0], variant)
        # Import actual inbox outputs at the stable-ID boundary after a refused
        # attempt. All state, sources and parent slices are synthetic QA data.
        for target in ('v1', 'v2'):
            for slice_id in ('s' * 200, '\U0001f680' * 100, 'slice with internal spaces'):
                case = Path(temp) / f'inbox-case-{inbox_cases}'
                case.mkdir()
                source = case / 'rollout-synthetic.jsonl'
                source.write_text(fixture())
                candidate = harvest.find_new_runs([case], {'harvested': {}})[0]
                state_path = case / 'state.json'
                run_id = harvest.ensure_run_id(candidate.records, candidate.turn, state_path)
                args = argparse.Namespace(turn_id=candidate.turn.turn_id, run_id=run_id,
                                          slice_id='s' * 201, run_type='Repair', role='Repair', target=target)
                dataset = core.dataset_for_run(core.build_run(candidate.records, source, args), target)
                before = state_path.read_bytes()
                destination = case / 'inbox'
                try:
                    harvest.save_to_inbox(candidate, dataset, destination, state_path)
                except core.ParseError as exc:
                    assert 'sliceId' in str(exc)
                else:
                    raise AssertionError('Invalid sliceId was published')
                assert state_path.read_bytes() == before and not destination.exists()
                assert not harvest._journal_path(state_path).exists()
                dataset['runs'][0]['sliceId'] = slice_id
                output = harvest.save_to_inbox(candidate, dataset, destination, state_path)
                emitted = json.loads(output.read_text())
                assert emitted == dataset and list(destination.iterdir()) == [output]
                assert list(harvest.load_state(state_path)['harvested']) == [candidate.identity]
                emitted['slices'] = [{'id': slice_id, 'title': 'Synthetic inbox QA'}]
                datasets.append(emitted)
                inbox_cases += 1
    synthetic = dict(counts); counts.clear(); models.clear()
    if opts.sessions:
        paths = sorted(core.discover_logs(opts.sessions), key=lambda p: p.stat().st_mtime, reverse=True)[:opts.sample]
        for path in paths:
            counts['files_read'] += 1
            try:
                records = core.load_records(path, opts.sessions)
                turns = core.find_turns(records)
                if not turns: counts['files_without_completed_turns'] += 1
                for turn in turns:
                    context = core.matching_turn_context(records, turn.turn_id)
                    if core.is_auto_review(core.first_payload(records, 'session_meta'), context):
                        counts['control_turns_skipped'] += 1; continue
                    collect(path, records, turn)
            except core.ParseError as exc:
                if 'stale' in str(exc): counts['stale_files_refused'] += 1
                elif 'Invalid JSON' in str(exc): counts['malformed_files_refused'] += 1
                else: raise
    result = subprocess.run(['node', str(Path(__file__).with_name('validate_pennytel.cjs')), str(opts.authority)], input=json.dumps(datasets), text=True, capture_output=True, check=True)
    print(json.dumps({'synthetic': synthetic, 'inbox_corrected_retries': inbox_cases,
                      'real': dict(counts), 'real_models': dict(models), 'pennytel': json.loads(result.stdout)}, indent=2))


if __name__ == '__main__': main()
