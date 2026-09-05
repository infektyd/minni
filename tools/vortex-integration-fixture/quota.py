"""Pure capacity checks for a disposable integration fixture.

Counts are nonnegative integers. A reservation is allowed when the resulting
usage is at most the capacity, including exactly filling the available space.
No state, network, filesystem, or production Minni imports are involved.
"""


def can_reserve(used: int, requested: int, capacity: int) -> bool:
    """Return whether an inclusive capacity limit permits the reservation."""
    if min(used, requested, capacity) < 0:
        raise ValueError("counts must be nonnegative")
    return used + requested <= capacity


def remaining_capacity(used: int, capacity: int) -> int:
    """Return nonnegative available capacity, even if already over capacity."""
    if min(used, capacity) < 0:
        raise ValueError("counts must be nonnegative")
    return max(0, capacity - used)
