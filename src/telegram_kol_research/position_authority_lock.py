"""Serialize ownership reconciliation with exchange mutations in this service process."""

from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
from threading import RLock


_POSITION_AUTHORITY_LOCK = RLock()


@contextmanager
def position_authority_lock():
    """Hold the single-process authority boundary across validation and mutation."""

    with _POSITION_AUTHORITY_LOCK:
        yield


def serialized_position_authority_mutation(function):
    """Keep ownership state stable until the protected exchange call returns."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        with position_authority_lock():
            return function(*args, **kwargs)

    return wrapped
