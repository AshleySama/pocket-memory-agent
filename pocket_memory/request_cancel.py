# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Request-local cancellation; no model state is shared between request IDs."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import threading
import time
import uuid


class RequestCancelled(BaseException):
    """Control flow, deliberately not caught by answer-repair Exception handlers."""


@dataclass
class RequestTicket:
    request_id: str
    cancelled: threading.Event = field(default_factory=threading.Event)
    active: bool = False
    finished: bool = False
    expires: float = 0.0
    publication_lock: threading.Lock = field(default_factory=threading.Lock)


_current = ContextVar('pocket_memory_request_cancel', default=None)


def validate_request_id(value):
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError('无效的问答请求编号')
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise ValueError('无效的问答请求编号') from None
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError('问答请求编号必须是随机 UUID')
    return value


class CancellationRegistry:
    def __init__(self, ttl=120.0, limit=256, clock=time.monotonic):
        self.ttl, self.limit, self.clock = ttl, limit, clock
        self.lock = threading.Lock()
        self.entries = {}

    def _prune(self):
        now = self.clock()
        for key, item in list(self.entries.items()):
            if not item.active and item.expires <= now:
                del self.entries[key]

    def _new(self, request_id):
        if len(self.entries) >= self.limit:
            raise ValueError('问答请求过多，请稍后重试')
        item = RequestTicket(request_id, expires=self.clock() + self.ttl)
        self.entries[request_id] = item
        return item

    def begin(self, request_id):
        request_id = validate_request_id(request_id)
        with self.lock:
            self._prune()
            item = self.entries.get(request_id)
            if item is not None and (item.active or item.finished):
                raise ValueError('问答请求编号已使用，请使用新的编号')
            item = item or self._new(request_id)
            item.active = True
            return item

    def cancel(self, request_id):
        request_id = validate_request_id(request_id)
        with self.lock:
            self._prune()
            item = self.entries.get(request_id) or self._new(request_id)
            with item.publication_lock:
                if item.finished:
                    return {'request_id': request_id, 'state': 'finished'}
                item.cancelled.set()
                return {'request_id': request_id, 'state': 'cancel_requested'}

    def finish(self, item):
        if item is None:
            return
        with self.lock:
            item.active, item.finished = False, True
            item.expires = self.clock() + self.ttl


@contextmanager
def request_scope(ticket):
    token = _current.set(ticket)
    try:
        yield
    finally:
        _current.reset(token)


def check_cancelled():
    ticket = _current.get()
    if ticket is not None and ticket.cancelled.is_set():
        raise RequestCancelled()


@contextmanager
def final_response():
    """Linearize cancellation against final publication; neither can win twice."""
    ticket = _current.get()
    if ticket is None:
        yield
        return
    with ticket.publication_lock:
        check_cancelled()
        yield
        ticket.finished = True


@contextmanager
def cancellable_lock(lock):
    # A cancelled queued request leaves without preparing evidence or a model.
    while True:
        check_cancelled()
        if lock.acquire(timeout=0.05):
            break
    try:
        check_cancelled()
        yield
    finally:
        lock.release()


def cancellable_llama_class(base):
    """Keep the native generator and all completion arguments unchanged.

    The first next() includes native prefill, which cannot be interrupted here.
    Reset/close execute on the requesting thread while its model lock is held.
    """
    class CancellableLlama(base):
        def generate(self, *args, **kwargs):
            ticket = _current.get()
            if ticket is None:
                yield from super().generate(*args, **kwargs)
                return
            inner = super().generate(*args, **kwargs)
            try:
                check_cancelled()
                token = next(inner)
                while True:
                    check_cancelled()
                    more = yield token
                    check_cancelled()
                    token = inner.send(more)
            except StopIteration:
                check_cancelled()
                return
            finally:
                try:
                    inner.close()
                finally:
                    if ticket.cancelled.is_set():
                        self.reset()
    return CancellableLlama


def create_llama(base, **kwargs):
    if not isinstance(base, type) or not hasattr(base, 'generate'):
        return base(**kwargs)
    return cancellable_llama_class(base)(**kwargs)
