"""Opt-in scalar traces of reverse propagation, never forward token history.

One previous vector per signal is held transiently to measure movement. Only
scalar measurements and tree branches enter the protected result. Diagnostics
do not choose tokens, modify signals, run extra model calls, or stop traversal.
"""
import numpy as np


def _vector(value):
    return np.asarray(value, dtype=np.float64).reshape(-1)


def _unit(value):
    norm = float(np.linalg.norm(value))
    return (value / norm if norm else np.zeros_like(value)), norm


class ReverseDebug:
    signal_columns = ['norm', 'change_from_previous', 'cosine_to_previous',
                      'unit_change_from_previous']

    def __init__(self):
        self._previous = {}
        self._indices = {}
        self._paths = {}
        self._step = None
        self.signal_names = []
        self.signal_summary = {}
        self.write_directions = {}
        self.readout_paths = []
        self.steps = []

    def begin_step(self):
        if self._step is not None:
            raise RuntimeError('Reverse debug step is already open')
        self._step = dict(signals={}, conditioning={}, readout=None,
                          trajectory=None, token_id=None)

    def observe(self, name, value):
        current = _vector(value)
        unit, norm = _unit(current)
        previous = self._previous.get(name)
        change = cosine = unit_change = None
        if previous is not None:
            previous_unit, previous_norm = _unit(previous)
            change = float(np.linalg.norm(current - previous))
            unit_change = float(np.linalg.norm(unit - previous_unit))
            if norm and previous_norm:
                cosine = float(np.clip(unit @ previous_unit, -1., 1.))
        self._previous[name] = current.copy()
        if name not in self._indices:
            self._indices[name] = len(self.signal_names)
            self.signal_names.append(name)
            self.signal_summary[name] = dict(observations=0, zero=0,
                unchanged=0, minimum_unit_change=None, maximum_unit_change=None)
        summary = self.signal_summary[name]
        summary['observations'] += 1
        summary['zero'] += int(norm == 0.)
        summary['unchanged'] += int(change == 0.)
        if unit_change is not None:
            low, high = summary['minimum_unit_change'], summary['maximum_unit_change']
            summary['minimum_unit_change'] = unit_change if low is None else min(low, unit_change)
            summary['maximum_unit_change'] = unit_change if high is None else max(high, unit_change)
        self._step['signals'][name] = [norm, change, cosine, unit_change]

    def conditioning(self, name, incoming, combined, write_norms, bias):
        before, _ = _unit(_vector(incoming))
        after, _ = _unit(_vector(combined))
        self.write_directions.setdefault(name, dict(raw_write_norms=dict(write_norms),
            combined_write_norm=float(np.linalg.norm(_vector(bias)))))
        self._step['conditioning'][name] = float(np.linalg.norm(after - before))

    def readout(self, details):
        self.observe('readout.input', details['vector'])
        # The tree is fixed: keep each observed path once, not once per token.
        leaf = details['leaf']
        if leaf not in self._paths:
            self._paths[leaf] = len(self.readout_paths)
            self.readout_paths.append(dict(leaf=leaf, token_id=details['token_id'],
                                           branches=details['branches']))
        self._step['readout'] = dict(path=self._paths[leaf],
            minimum_margin=details['minimum_margin'],
            closest_split=details['closest_split'])

    def movement(self, current, delta, following, remaining_before,
                 remaining_after, step, stop_reason=None):
        value, _ = _unit(_vector(current))
        displacement = None if delta is None else _vector(delta)
        radial = None if displacement is None else float(value @ displacement)
        tangent = None if displacement is None else float(np.linalg.norm(displacement - radial * value))
        self._step['trajectory'] = dict(
            remaining_before=float(remaining_before), remaining_after=float(remaining_after),
            step=float(step), applied_step=0. if stop_reason else float(min(step,remaining_before)),
            raw_delta_norm=None if delta is None else float(np.linalg.norm(displacement)),
            radial_delta=radial, tangent_delta_norm=tangent,
            actual_state_change=float(np.linalg.norm(_vector(following) - _vector(current))),
            stop_reason=stop_reason)

    def end_step(self, token):
        self._step['token_id'] = token
        # Names and measurement columns are recorded once for compact traces.
        self._step['signals'] = [self._step['signals'].get(name) for name in self.signal_names]
        self.steps.append(self._step)
        self._step = None

    def report(self):
        return dict(schema_version=2, observational_only=True,
            stores_source_tokens=False, stores_signal_vectors=False,
            signal_columns=list(self.signal_columns), signal_names=list(self.signal_names),
            signal_summary=self.signal_summary, write_directions=self.write_directions,
            readout_branch_columns=['node', 'axis', 'threshold', 'went_left'],
            readout_paths=self.readout_paths, steps=self.steps)
