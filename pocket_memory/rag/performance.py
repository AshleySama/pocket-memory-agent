# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Content-free, request-local measurements. All times are monotonic seconds.

Inclusive stages nest; self_seconds excludes timed children and can be summed.
No query, source content, path or answer is retained by this module.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
from functools import wraps
import importlib
import math
import time

_current = ContextVar('pocket_memory_performance', default=None)
_active_completions = ContextVar('pocket_memory_active_completions', default=())
_PHASES = frozenset(('answer', 'query_rewrite', 'intent', 'classify', 'other', 'verification'))

class Measurements:
    def __init__(self):
        self.started = time.perf_counter()
        self.stages = {}
        self.counts = {}
        self.stack = []
        self.native_phases = {}
        self.completion_wall = {}

    def snapshot(self):
        return {'schema_version': 2, 'elapsed_seconds': round(time.perf_counter()-self.started, 6),
                'timing_semantics': 'inclusive stages nest; self_seconds excludes measured children',
                'stages': {k: {n: round(v, 6) if isinstance(v, float) else v for n,v in row.items()}
                           for k,row in self.stages.items()}, 'counts': dict(self.counts),
                'native_semantics': 'llama_perf_context deltas; native times are separate from wall stages; '
                                    'prefill_tokens=n_p_eval; decode_tokens=n_eval; no KV reuse estimate',
                'native_phases': {phase: {k: dict(v) if isinstance(v, dict) else
                                         round(v, 6) if isinstance(v, float) else v
                                         for k, v in row.items()}
                                  for phase, row in self.native_phases.items()},
                'completion_wall_semantics': 'active_seconds sums create/iterator operations; '
                                             'stream_lifetime_seconds includes consumer pauses',
                'completion_wall': {phase: {k: round(v, 6) if isinstance(v, float) else v
                                           for k, v in row.items()}
                                    for phase, row in self.completion_wall.items()}}

@contextmanager
def capture():
    measurements = Measurements()
    token = _current.set(measurements)
    try:
        yield measurements
    finally:
        _current.reset(token)

def snapshot():
    current = _current.get()
    return current.snapshot() if current is not None else None

def metric(name, value=1):
    current = _current.get()
    if current is not None:
        current.counts[name] = current.counts.get(name, 0) + value

@contextmanager
def span(name):
    current = _current.get()
    if current is None:
        yield
        return
    frame = [name, 0.0]
    current.stack.append(frame)
    began = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter()-began
        current.stack.pop()
        row = current.stages.setdefault(name, {'calls': 0, 'inclusive_seconds': 0.0, 'self_seconds': 0.0})
        row['calls'] += 1
        row['inclusive_seconds'] += elapsed
        row['self_seconds'] += max(0.0, elapsed-frame[1])
        if current.stack:
            current.stack[-1][1] += elapsed

def timed(name):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with span(name):
                return fn(*args, **kwargs)
        return wrapped
    return decorate

def request_profile(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        if self.path.split('?', 1)[0] != '/api/ask':
            return fn(self, *args, **kwargs)
        with capture():
            return fn(self, *args, **kwargs)
    return wrapped

@contextmanager
def measured_lock(lock, name):
    with span(name):
        lock.acquire()
    try:
        yield
    finally:
        lock.release()

def _best_effort(fn, *args):
    """Instrumentation must never replace an inference result or exception."""
    try:
        return fn(*args)
    except Exception:
        return None


def _native_api():
    # Lazy import keeps ordinary requests and non-native test doubles independent
    # of the optional binding. Reading counters never resets or prints them.
    return importlib.import_module('llama_cpp.llama_cpp')


def _native_snapshot(llm):
    try:
        ctx = getattr(getattr(llm, '_ctx', None), 'ctx', None)
        if not ctx:
            return {'status': 'unsupported'}
        if getattr(getattr(llm, 'context_params', None), 'no_perf', False):
            return {'status': 'disabled'}
        reader = getattr(_native_api(), 'llama_perf_context', None)
        if not callable(reader):
            return {'status': 'unsupported'}
        raw = reader(ctx)
        values = {name: float(getattr(raw, name)) for name in
                  ('t_start_ms', 't_p_eval_ms', 't_eval_ms', 'n_p_eval', 'n_eval')}
        if any(not math.isfinite(value) or value < 0 for value in values.values()):
            return {'status': 'error'}
        if any(not values[name].is_integer() for name in ('n_p_eval', 'n_eval')):
            return {'status': 'error'}
        return {'status': 'ok', 'values': values}
    except (ImportError, AttributeError):
        return {'status': 'unsupported'}
    except Exception:
        return {'status': 'error'}


def _model_key(llm):
    # Transparent wrappers such as CoverageGeneration forward the same _ctx.
    return id(getattr(llm, '_ctx', None) or llm)


class _CompletionMeasurement:
    def __init__(self, current, llm, phase, key):
        self.current, self.llm, self.phase, self.key = current, llm, phase, key
        self.done = False
        self.started = _best_effort(time.perf_counter)
        self.before = _best_effort(_native_snapshot, llm)
        self.usage_seen = False
        _best_effort(self._register)

    def _register(self):
        current = self.current
        current.counts['model.completion_calls'] = current.counts.get('model.completion_calls', 0) + 1
        row = current.stages.setdefault('model.completion',
                                        {'calls': 0, 'inclusive_seconds': 0.0, 'self_seconds': 0.0})
        row['calls'] += 1
        wall = current.completion_wall.setdefault(self.phase, {
            'calls': 0, 'active_seconds': 0.0, 'stream_lifetime_seconds': 0.0,
            'streams': 0, 'completed_streams': 0, 'closed_streams': 0, 'errors': 0})
        wall['calls'] += 1
        native = current.native_phases.setdefault(self.phase, {
            'calls': 0, 'pending_calls': 0, 'status_counts': {}, 'prefill_seconds': 0.0,
            'prefill_tokens': 0, 'decode_seconds': 0.0, 'decode_tokens': 0})
        native['calls'] += 1
        native['pending_calls'] += 1

    def _begin(self):
        began = time.perf_counter()
        frame = ['model.completion', 0.0]
        stack = self.current.stack if _current.get() is self.current else None
        parent = stack[-1] if stack else None
        if stack is not None:
            stack.append(frame)
        return began, frame, stack, parent

    def _end(self, timing):
        if timing is None:
            return
        began, frame, stack, parent = timing
        # Remove the frame even when reading the clock fails. No frame remains
        # open while a streamed item is being handled by the caller.
        if stack is not None and stack and stack[-1] is frame:
            stack.pop()
        elapsed = max(0.0, time.perf_counter() - began)
        row = self.current.stages['model.completion']
        row['inclusive_seconds'] += elapsed
        row['self_seconds'] += max(0.0, elapsed - frame[1])
        if parent is not None:
            parent[1] += elapsed
        self.current.completion_wall[self.phase]['active_seconds'] += elapsed

    def invoke(self, fn, *args, **kwargs):
        if self.done:
            return fn(*args, **kwargs)
        token = _active_completions.set(_active_completions.get() + (self.key,))
        timing = _best_effort(self._begin)
        try:
            return fn(*args, **kwargs)
        finally:
            _best_effort(self._end, timing)
            _active_completions.reset(token)

    def usage(self, result):
        if self.usage_seen or not isinstance(result, dict):
            return
        usage = result.get('usage')
        if not isinstance(usage, dict):
            return
        self.usage_seen = True
        for name in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            value = usage.get(name, 0)
            if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                key = 'model.' + name
                self.current.counts[key] = self.current.counts.get(key, 0) + value

    def _finish_native(self):
        after = _best_effort(_native_snapshot, self.llm)
        before = self.before or {'status': 'error'}
        after = after or {'status': 'error'}
        status = before['status'] if before['status'] != 'ok' else after['status']
        row = self.current.native_phases[self.phase]
        if status == 'ok':
            old, new = before['values'], after['values']
            fields = ('t_p_eval_ms', 't_eval_ms', 'n_p_eval', 'n_eval')
            # A changed start epoch catches resets even if new totals have
            # already exceeded old totals. Zero may be the initial lazy epoch.
            if (old['t_start_ms'] and new['t_start_ms'] != old['t_start_ms']) or any(
                    new[name] < old[name] for name in fields):
                status = 'reset'
            else:
                row['prefill_seconds'] += (new['t_p_eval_ms'] - old['t_p_eval_ms']) / 1000.0
                row['decode_seconds'] += (new['t_eval_ms'] - old['t_eval_ms']) / 1000.0
                row['prefill_tokens'] += int(new['n_p_eval'] - old['n_p_eval'])
                row['decode_tokens'] += int(new['n_eval'] - old['n_eval'])
        row['pending_calls'] -= 1
        row['status_counts'][status] = row['status_counts'].get(status, 0) + 1

    def _finish_wall(self, outcome, streamed):
        wall = self.current.completion_wall[self.phase]
        if outcome == 'error':
            wall['errors'] += 1
        if streamed:
            wall['streams'] += 1
            if outcome in ('completed', 'closed'):
                wall[outcome + '_streams'] += 1
            if self.started is not None:
                wall['stream_lifetime_seconds'] += max(0.0, time.perf_counter() - self.started)

    def finish(self, outcome, streamed=False):
        if self.done:
            return
        self.done = True
        _best_effort(self._finish_native)
        _best_effort(self._finish_wall, outcome, streamed)


class _CompletionStream:
    def __init__(self, iterator, measurement):
        self._iterator, self._measurement = iterator, measurement

    def __iter__(self):
        return self

    def __getattr__(self, name):
        return getattr(self._iterator, name)

    def _advance(self, method, *args):
        try:
            result = self._measurement.invoke(method, *args)
        except StopIteration:
            self._measurement.finish('completed', streamed=True)
            raise
        except BaseException:
            self._measurement.finish('error', streamed=True)
            raise
        _best_effort(self._measurement.usage, result)
        return result

    def __next__(self):
        return self._advance(self._iterator.__next__)

    def send(self, value):
        return self._advance(self._iterator.send, value)

    def throw(self, *args):
        return self._advance(self._iterator.throw, *args)

    def close(self):
        try:
            close = getattr(self._iterator, 'close', None)
            result = self._measurement.invoke(close) if close is not None else None
        except BaseException:
            self._measurement.finish('error', streamed=True)
            raise
        self._measurement.finish('closed', streamed=True)
        return result


def completion(llm, *args, **kwargs):
    """Call the model unchanged; consume only the reserved telemetry phase.

    Native counters are sampled before the call and after its result/iterator
    completes, closes or fails. Do not interleave calls on one native context.
    """
    phase = kwargs.pop('_perf_phase', 'answer')
    phase = phase if isinstance(phase, str) and phase in _PHASES else 'other'
    current = _current.get()
    if current is None:
        return llm.create_chat_completion(*args, **kwargs)
    key = _best_effort(_model_key, llm)
    if key is not None and key in _active_completions.get():
        return llm.create_chat_completion(*args, **kwargs)
    measurement = _CompletionMeasurement(current, llm, phase, key)
    try:
        result = measurement.invoke(llm.create_chat_completion, *args, **kwargs)
    except BaseException:
        measurement.finish('error')
        raise
    if isinstance(result, Iterator):
        return _CompletionStream(result, measurement)
    _best_effort(measurement.usage, result)
    measurement.finish('completed')
    return result
