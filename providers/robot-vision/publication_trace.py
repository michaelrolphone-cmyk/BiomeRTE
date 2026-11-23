"""Opt-in, bounded local publication spans; never part of stream metadata.

Only robot.vision.trace arms this process. No application lock is taken by the
recorder or GC callback. Stage wall time includes descheduling and GC; it is not
proof of lock ownership or CPU use. Fanout is the last instrumented boundary,
not completion of a subscriber's socket write.
"""
from collections import deque
from datetime import datetime, timezone
import gc
import json
import math
import os
import re
import threading
import time

SCHEMA = "biome.vision.publication-trace.v1"
MAX_DURATION_S = 150.
MAX_ACTIVE = 128
MAX_THREADS = 64
MAX_DEPTH = 16
MAX_SLOW = 2048
MAX_GC = 2048
MAX_ID_CHARS = 96
MAX_EXPORT_BYTES = 8 * 1024 * 1024
SLOW_NS = 20_000_000
STAGES = (
    "front.raw_metadata", "prepare", "prepare.lidar", "prepare.enrich",
    "prepare.inventory", "enrich.wait", "enrich.held", "enrich.tof_copy",
    "enrich.orientation", "enrich.display_fit", "enrich.project",
    "lidar.status.wait", "lidar.status.held", "lidar.status.copy",
    "publish", "publish.observe", "publish.serialize", "publish.fanout",
    "backend.start", "backend.close",
)
IDENTITY_FIELDS = ("camera_id", "source_id", "frame_id", "receiver_id",
                   "boot_id", "map_id", "sensor_tick", "processed_monotonic",
                   "capture_monotonic_us", "served_monotonic_us")
class _Noop:
    def __enter__(self):
        return self
    def finish(self, _error_type=None):
        pass
    def __exit__(self, _kind, _value, _tb):
        return False


_NOOP = _Noop()


def _identity(metadata):
    """Copy only bounded scalars; never retain metadata, arrays or image bytes."""
    if not isinstance(metadata, dict):
        return (), False
    packet = metadata.get("sensor_packet")
    packet = packet if isinstance(packet, dict) else {}
    result, truncated = [], False
    for key in IDENTITY_FIELDS:
        value = metadata.get(key, packet.get(key))
        if isinstance(value, str):
            truncated |= len(value) > MAX_ID_CHARS
            value = value[:MAX_ID_CHARS]
        elif type(value) not in (int, float, bool, type(None)):
            value = None
        elif isinstance(value, float) and not math.isfinite(value):
            value = None
        elif type(value) is int and not -(1 << 63) <= value < (1 << 63):
            value = None
        result.append((key, value))
    return tuple(result), truncated


class _Span:
    def __init__(self, owner, stage, metadata, lock, held):
        self.owner, self.stage, self.metadata = owner, stage, metadata
        self.lock, self.held = lock, held
        self.state = self.row = None
        self.finished = False

    def __enter__(self):
        try:
            self.owner._begin(self)
        except Exception:
            self.owner._record_error()
        finally:
            # Even a failed admission must not keep an input packet alive.
            self.metadata = None
        return self

    def finish(self, error_type=None):
        if self.finished:
            return
        self.finished = True
        try:
            self.owner._end(self, error_type)
        except Exception:
            self.owner._record_error()

    def __exit__(self, kind, _value, _tb):
        self.finish(kind.__name__[:MAX_ID_CHARS] if kind else None)
        return False


class _Epoch:
    def __init__(self, epoch, start, duration, pid):
        self.epoch, self.pid, self.duration = epoch, pid, float(duration)
        self.start, self.deadline = start, start + int(duration * 1e9)
        self.started_utc = datetime.now(timezone.utc).isoformat()
        self.freeze_ns = None
        self.freeze_reason = None
        self.active = {}
        self.stacks = {}
        self.contexts = {}
        self.owners = {}
        self.next_token = 0
        self.slow = deque(maxlen=MAX_SLOW)
        self.gc_events = deque(maxlen=MAX_GC)
        self.gc_open = None
        self.gc_sequence = 0
        self.gc_busy = False
        self.frozen_active = None
        self.frozen_gc = None
        self.frozen_gc_open = None
        self.gc_snapshot_in_progress = False
        self.counts = dict(admitted=0, completed=0, slow_overwritten=0,
                          active_overflow=0, thread_overflow=0, depth_overflow=0,
                          after_deadline=0, errors=0, gc_events=0,
                          gc_overwritten=0, gc_unmatched_stop=0,
                          gc_reentry=0, gc_errors=0)
        self.stats = {name: [0, 0, 0, 0, 0, 0] for name in STAGES}
        self.timer = None


class PublicationTrace:
    def __init__(self, *, clock=time.monotonic_ns, cpu=time.thread_time_ns,
                 timer_factory=threading.Timer):
        self.clock, self.cpu, self.timer_factory = clock, cpu, timer_factory
        self._lock = threading.RLock()  # Bookkeeping only; never spans app work.
        self.state = None
        self._gc_callback = self._gc_event

    def _record_error(self):
        state = self.state
        if state is not None:
            state.counts["errors"] += 1

    def span(self, stage, metadata=None, *, lock=None, held=False):
        state = self.state
        if state is None or state.freeze_ns is not None or state.pid != os.getpid():
            return _NOOP
        if stage not in state.stats:
            self._record_error()
            return _NOOP
        if self.clock() >= state.deadline:
            state.counts["after_deadline"] += 1
            return _NOOP
        return _Span(self, stage, metadata, lock, held)

    def _begin(self, span):
        state = self.state
        now, cpu = self.clock(), self.cpu()
        ident, native_id = threading.get_ident(), threading.get_native_id()
        with self._lock:
            if (state is not self.state or state is None or state.freeze_ns is not None
                    or state.pid != os.getpid() or now >= state.deadline):
                return
            stack = state.stacks.get(ident)
            if len(state.active) >= MAX_ACTIVE:
                state.counts["active_overflow"] += 1
                return
            if stack is None:
                if len(state.stacks) >= MAX_THREADS:
                    state.counts["thread_overflow"] += 1
                    return
                stack = []
                state.stacks[ident] = stack
            if len(stack) >= MAX_DEPTH:
                state.counts["depth_overflow"] += 1
                return
            parent = state.active.get(stack[-1]) if stack else None
            identity, truncated = (_identity(span.metadata) if span.metadata is not None
                                   else (parent["identity"], parent["identity_truncated"])
                                   if parent else ((), False))
            state.next_token += 1
            token = state.next_token
            # Lock objects remain alive in their real with-block, so this is
            # epoch-local identity, not an id(bytes) side table.
            key = (id(span.lock) if span.lock is not None else None)
            previous_owner = state.owners.get(key) if key is not None else None
            row = dict(token=token, parent_token=parent["token"] if parent else None,
                       stage=span.stage, ident=ident, native_id=native_id,
                       started_ns=now, cpu_started_ns=cpu, identity=identity,
                       identity_truncated=truncated, lock_key=key,
                       owner_at_start=previous_owner,
                       held=span.held, previous_owner=previous_owner)
            state.active[token] = row
            stack.append(token)
            state.contexts[ident] = (token, span.stage, identity)
            if span.held and key is not None:
                state.owners[key] = token
            state.counts["admitted"] += 1
            span.state, span.row = state, row

    def _end(self, span, error_type):
        state, row = span.state, span.row
        if state is None or row is None:
            return
        end, cpu = self.clock(), self.cpu()
        with self._lock:
            token, ident, key = row["token"], row["ident"], row["lock_key"]
            if token not in state.active:
                return
            same_thread = ident == threading.get_ident()
            wall = max(0, end-row["started_ns"])
            cpu_elapsed = max(0, cpu-row["cpu_started_ns"]) if same_thread else None
            crossed = end > state.deadline
            if state.freeze_ns is None:
                stat = state.stats[row["stage"]]
                stat[0] += 1
                stat[1] += wall
                stat[2] += cpu_elapsed or 0
                stat[3] = max(stat[3], wall)
                stat[4] += bool(error_type)
                stat[5] += crossed or not same_thread
                state.counts["completed"] += 1
                if wall >= SLOW_NS or error_type or crossed or not same_thread:
                    if len(state.slow) == MAX_SLOW:
                        state.counts["slow_overwritten"] += 1
                    state.slow.append(dict(row, ended_ns=end, cpu_elapsed_ns=cpu_elapsed,
                                           error_type=error_type, crossed_deadline=crossed,
                                           same_thread=same_thread))
            state.active.pop(token)
            if row["held"] and state.owners.get(key) == token:
                previous = row["previous_owner"]
                if previous in state.active:
                    state.owners[key] = previous
                else:
                    state.owners.pop(key, None)
            stack = state.stacks.get(ident, [])
            if token in stack:
                stack.remove(token)
            if stack:
                top = state.active.get(stack[-1])
                if top:
                    state.contexts[ident] = (top["token"], top["stage"], top["identity"])
            else:
                state.stacks.pop(ident, None)
                state.contexts.pop(ident, None)

    def _gc_event(self, phase, info):
        # No locks, application calls, frames, logging, JSON or I/O here.
        state = self.state
        if state is None or state.pid != os.getpid() or state.freeze_ns is not None:
            return
        if state.gc_busy:
            state.counts["gc_reentry"] += 1
            return
        state.gc_busy = True
        try:
            now = self.clock()
            ident, native = threading.get_ident(), threading.get_native_id()
            if phase == "start":
                if now >= state.deadline:
                    return
                state.gc_sequence += 1
                context = state.contexts.get(ident)
                state.gc_open = (state.gc_sequence, now, info.get("generation"),
                                 ident, native, context)
                event = ("start", state.gc_sequence, now, info.get("generation"),
                         ident, native, context, None, None)
            elif phase == "stop":
                pending = state.gc_open
                if pending is None:
                    state.counts["gc_unmatched_stop"] += 1
                    return
                event = ("stop", pending[0], now, info.get("generation"),
                         ident, native, pending[5],
                         info.get("collected"), info.get("uncollectable"))
                state.gc_open = None
            else:
                return
            if state.freeze_ns is None:
                if len(state.gc_events) == MAX_GC:
                    state.counts["gc_overwritten"] += 1
                state.gc_events.append(event)
                state.counts["gc_events"] += 1
        except Exception:
            state.counts["gc_errors"] += 1
        finally:
            state.gc_busy = False

    def _freeze(self, state, reason):
        with self._lock:
            if state is not self.state or state.freeze_ns is not None:
                return
            state.freeze_ns = self.clock()
            state.freeze_reason = reason
            # Preserve open operations even when their finally runs much later.
            state.frozen_active = [dict(row, owner_at_freeze=state.owners.get(row["lock_key"]))
                                   for row in state.active.values()]
            state.gc_snapshot_in_progress = state.gc_busy
            state.frozen_gc = tuple(state.gc_events)
            state.frozen_gc_open = state.gc_open
            if state.timer is not None:
                state.timer.cancel()
            # Never replace gc.callbacks or disturb callbacks owned by others.
            try:
                gc.callbacks.remove(self._gc_callback)
            except ValueError:
                pass

    def shutdown(self):
        state = self.state
        if state is not None and state.pid == os.getpid():
            self._freeze(state, "service_shutdown")

    def command(self, inputs):
        if not isinstance(inputs, dict):
            raise ValueError("trace_inputs_must_be_object")
        action = inputs.get("action")
        allowed = {"action", "epoch", "expected_pid"} | (
            {"duration_s"} if action == "arm" else set())
        if set(inputs) - allowed or action not in ("arm", "freeze", "read"):
            raise ValueError("invalid_trace_action_or_fields")
        epoch = inputs.get("epoch")
        if not isinstance(epoch, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", epoch):
            raise ValueError("invalid_trace_epoch")
        if type(inputs.get("expected_pid")) is not int or inputs["expected_pid"] != os.getpid():
            raise ValueError("trace_pid_mismatch")
        if action == "arm":
            duration = inputs.get("duration_s", 135.)
            if (type(duration) not in (int, float) or not math.isfinite(duration)
                    or not 0 < duration <= MAX_DURATION_S):
                raise ValueError("invalid_trace_duration")
            with self._lock:
                previous = self.state
                if previous is not None and previous.epoch == epoch:
                    if previous.duration != float(duration):
                        raise ValueError("trace_epoch_parameters_mismatch")
                    state = previous  # Idempotent: never move the original deadline.
                else:
                    if previous is not None and (previous.freeze_ns is None or previous.active):
                        raise ValueError("trace_epoch_busy")
                    state = _Epoch(epoch, self.clock(), duration, os.getpid())
                    self.state = state
                    gc.callbacks.append(self._gc_callback)
                    try:
                        state.timer = self.timer_factory(duration, self._freeze, args=(state, "deadline"))
                        state.timer.daemon = True
                        state.timer.start()
                    except Exception:
                        self._freeze(state, "timer_start_failed")
                        raise
        else:
            state = self.state
            if state is None or state.epoch != epoch or state.pid != os.getpid():
                raise ValueError("trace_epoch_mismatch")
            if action == "freeze":
                self._freeze(state, "requested")
        return self.snapshot(state)

    @staticmethod
    def _row(row, observed, deadline, frozen):
        ended = row.get("ended_ns")
        return dict(token=row["token"], parent_token=row["parent_token"],
                    stage=row["stage"], ident=row["ident"], native_id=row["native_id"],
                    identity=dict(row["identity"]), identity_truncated=row["identity_truncated"],
                    started_monotonic_s=row["started_ns"]/1e9,
                    ended_monotonic_s=None if ended is None else ended/1e9,
                    wall_s=None if ended is None else (ended-row["started_ns"])/1e9,
                    thread_cpu_s=None if row.get("cpu_elapsed_ns") is None
                    else row["cpu_elapsed_ns"]/1e9,
                    age_at_observation_s=max(0, observed-row["started_ns"])/1e9,
                    incomplete=(ended is None or row.get("crossed_deadline", False)
                                or row.get("same_thread") is False),
                    crossed_nominal_deadline=(ended or observed) > deadline,
                    error_type=row.get("error_type"), lock_key=row["lock_key"],
                    lock_state="held" if row["held"] else "wait" if row["lock_key"] else None,
                    observed_owner_token=row.get("owner_at_freeze", row["owner_at_start"]),
                    owner_scope="enrich_all_holders; lidar_status_only_other_holders_unknown",
                    frozen=frozen)

    def snapshot(self, state):
        observed = self.clock()
        with self._lock:
            frozen = state.freeze_ns is not None
            active = list(state.frozen_active if frozen else
                          [dict(row, owner_at_freeze=state.owners.get(row["lock_key"]))
                           for row in state.active.values()])
            slow = list(state.slow)
            events = state.frozen_gc if frozen else tuple(state.gc_events)
            pending_gc = state.frozen_gc_open if frozen else state.gc_open
            stats = {name: dict(count=s[0], wall_total_s=s[1]/1e9,
                               thread_cpu_total_s=s[2]/1e9, wall_max_s=s[3]/1e9,
                               exceptions=s[4], incomplete=s[5])
                     for name, s in state.stats.items()}
            counts = dict(state.counts)
        value = dict(schema=SCHEMA, pid=state.pid, epoch=state.epoch,
                     enabled=not frozen and observed < state.deadline, frozen=frozen,
                     started_utc=state.started_utc, started_monotonic_s=state.start/1e9,
                     deadline_monotonic_s=state.deadline/1e9,
                     freeze_monotonic_s=None if not frozen else state.freeze_ns/1e9,
                     freeze_reason=state.freeze_reason,
                     observation_started_monotonic_s=observed/1e9,
                     counts=counts, stage_stats=stats,
                     limits=dict(active_spans=MAX_ACTIVE, threads=MAX_THREADS, depth=MAX_DEPTH,
                                 slow_spans=MAX_SLOW, gc_events=MAX_GC,
                                 identity_chars=MAX_ID_CHARS, export_bytes=MAX_EXPORT_BYTES,
                                 slow_threshold_s=SLOW_NS/1e9),
                     event_observed_through_monotonic_s=(state.freeze_ns if frozen else observed)/1e9,
                     active=[self._row(r, state.freeze_ns if frozen else observed, state.deadline, frozen) for r in active],
                     slow_spans=[self._row(r, observed, state.deadline, frozen) for r in slow],
                     gc_events=[dict(phase=e[0], sequence=e[1], monotonic_s=e[2]/1e9,
                                     generation=e[3], ident=e[4], native_id=e[5],
                                     context_token=e[6][0] if e[6] else None,
                                     context_stage=e[6][1] if e[6] else None,
                                     identity=dict(e[6][2]) if e[6] else {},
                                     collected=e[7], uncollectable=e[8],
                                     after_nominal_deadline=e[2] > state.deadline)
                                for e in events],
                     gc_incomplete_start=None if pending_gc is None else dict(
                         sequence=pending_gc[0], started_monotonic_s=pending_gc[1]/1e9,
                         generation=pending_gc[2], ident=pending_gc[3], native_id=pending_gc[4]),
                     gc_snapshot_in_progress=state.gc_snapshot_in_progress,
                     boundary="fanout_not_subscriber_write", export_dropped=0)
        value["counts"]["active_at_snapshot"] = len(active)
        value["observation_finished_monotonic_s"] = self.clock()/1e9
        # Export is an explicit diagnostic operation, never in a publication/GC hook.
        while len(json.dumps(value, allow_nan=False).encode()) > MAX_EXPORT_BYTES:
            key = "slow_spans" if value["slow_spans"] else "gc_events"
            if not value[key]:
                raise ValueError("trace_export_bound")
            count = max(1, len(value[key])//4)
            del value[key][:count]
            value["export_dropped"] += count
        return value


TRACE = PublicationTrace()


def span(stage, metadata=None, *, lock=None, held=False):
    return TRACE.span(stage, metadata, lock=lock, held=held)


def command(inputs):
    return TRACE.command(inputs)


def shutdown():
    TRACE.shutdown()
