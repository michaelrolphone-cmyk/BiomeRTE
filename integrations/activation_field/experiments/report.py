"""Paired episode bootstrap; seeds and probes are not independent episodes."""
import json
from pathlib import Path
import numpy as np
from .storage import atomic_json


def score(row, corrected=False):
    answers = [a for a in row['answers'] if not corrected or a.get('corrected')]
    return float(np.mean([a['exact'] for a in answers])) if answers else None


def paired(rows, left, right, gap, corrected=False, bootstrap_seed=42, draws=10000):
    values = {left: {}, right: {}}
    for row in rows:
        if row.get('status') != 'complete' or row.get('split') != 'heldout' or row.get('gap') != gap:
            continue
        condition = row['condition']
        if condition not in values:
            continue
        key = (row['episode'], row.get('seed', 42))
        if key in values[condition]:
            raise ValueError('Duplicate trial identity in report')
        value = score(row, corrected)
        if value is not None:
            values[condition][key] = value
    common = sorted(values[left].keys() & values[right].keys())
    by_episode = {}
    for key in common:
        by_episode.setdefault(key[0], []).append(values[left][key] - values[right][key])
    delta = np.array([np.mean(v) for v in by_episode.values()])
    result = dict(left=left, right=right, gap=gap, metric='correction' if corrected else 'accuracy',
                  paired_episodes=len(delta), paired_trials=len(common),
                  unpaired_left=len(values[left]) - len(common), unpaired_right=len(values[right]) - len(common))
    if len(delta) < 2:
        return dict(result, status='insufficient_evidence', difference=None, ci95=None)
    rng = np.random.default_rng(bootstrap_seed)
    samples = np.mean(delta[rng.integers(0, len(delta), (draws, len(delta)))], axis=1)
    return dict(result, status='measured', difference=float(np.mean(delta)),
                ci95=np.quantile(samples, [.025, .975]).tolist(),
                bootstrap_unit='episode; paired seeds averaged within episode', bootstrap_draws=draws)


def report(run_dir, output):
    root = Path(run_dir)
    rows = json.loads((root / 'results.json').read_text())
    config = json.loads((root / 'config.json').read_text())
    conditions = {}
    for row in rows:
        bucket = conditions.setdefault(row['condition'], dict(complete=0, unexecuted=[]))
        if row['status'] == 'complete':
            bucket['complete'] += 1
        else:
            bucket['unexecuted'].append(row.get('reason', row['status']))
    comparisons = [paired(rows, 'C2', other, gap, correction)
                   for gap in config.get('retention_gaps', [0, 512, 2048, 8192])
                   for other in ('C1', 'C3', 'C4', 'C6') for correction in (False, True)]
    complete = [r for r in rows if r.get('status') == 'complete']
    summary = dict(schema_version=2, conditions=conditions, comparisons=comparisons,
                   completed_trials=len(complete), scientific_success='not_established',
                   reason='Per-gap comparisons are descriptive. Confirmatory coverage, matched controls, stability and descriptor qualification must all be assessed separately.',
                   source_identity=json.loads((root / 'identity.json').read_text()) if (root / 'identity.json').exists() else None)
    atomic_json(Path(output) / 'report.json', summary)
    return summary
