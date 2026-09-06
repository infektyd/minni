"""Cooperative request budgets, never cancellation of arbitrary native work."""

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import math
import time

_deadline = ContextVar("minni_request_deadline", default=None)
_allow_expired_sql = ContextVar("minni_allow_expired_sql", default=False)


class RequestDeadlineExceeded(TimeoutError):
    """No request budget remains for this operation."""


def current_deadline():
    return _deadline.get()


def remaining_seconds():
    deadline = current_deadline()
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def check_deadline():
    """Always fail-closed at remaining 0 so the VM progress handler can interrupt.

    ``allow_expired_sql`` only skips the *entry* raise in ``request_operation``.
    It does not silence this check.
    """
    remaining = remaining_seconds()
    if remaining is not None and remaining <= 0:
        raise RequestDeadlineExceeded("search request deadline exceeded")
    return remaining


def expired_sql_allowed() -> bool:
    return bool(_allow_expired_sql.get())


@contextmanager
def allow_expired_sql():
    """Narrow entry permit for completed-ranking bookkeeping / lexical fill.

    Does not disable the SQLite progress handler. Expensive statements still
    interrupt at remaining 0. Does not open a general SQL exemption.
    """
    token = _allow_expired_sql.set(True)
    try:
        yield
    finally:
        _allow_expired_sql.reset(token)


@contextmanager
def request_deadline(deadline):
    try:
        finite = deadline is None or math.isfinite(deadline)
    except (TypeError, OverflowError):
        finite = False
    if deadline is not None and (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not finite
    ):
        raise ValueError("request deadline must be a finite monotonic timestamp or None")
    previous = current_deadline()
    effective = deadline if previous is None else (
        previous if deadline is None else min(previous, deadline)
    )
    token = _deadline.set(effective)
    try:
        yield
    finally:
        _deadline.reset(token)


def bind_copied_deadline(fn, /, *args, **kwargs):
    """Bind ``fn`` to an independent copy of the current request deadline.

    Call on the submitting thread. ThreadPoolExecutor workers do not inherit
    ContextVars; each spawn must snapshot ``copy_context()`` itself so workers
    never share one ``Context.run`` concurrently. ``Context.run`` restores the
    worker's previous context on return, so a reused thread does not leak the
    request budget into a later task. Native inference and filesystem work
    remain non-preemptible: this copies the cooperative budget, it does not
    cancel threads.
    """
    ctx = copy_context()

    def _bound():
        return ctx.run(fn, *args, **kwargs)

    return _bound


def run_bound(bound):
    """Invoke a callable produced by :func:`bind_copied_deadline`."""
    return bound()


@contextmanager
def budgeted_lock(lock):
    remaining = remaining_seconds()
    if remaining is None:
        acquired = lock.acquire()
    elif remaining <= 0:
        if not expired_sql_allowed():
            raise RequestDeadlineExceeded("search request deadline waiting for database lock")
        acquired = lock.acquire(blocking=False)
    else:
        acquired = lock.acquire(timeout=remaining)
    if not acquired:
        raise RequestDeadlineExceeded("search request deadline waiting for database lock")
    try:
        yield
    finally:
        lock.release()
