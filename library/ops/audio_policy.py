"""Pure capacity policy for optional original audio in the weekly workflow.

No function reads a clock, filesystem, environment, configuration or queue.
The caller supplies measured remaining time and conservative next-backup sizes.
The existing user-selected schedule is outside this module.
"""
from __future__ import annotations

import math
from numbers import Real

DEFAULT_FINALIZATION_RESERVE_SECONDS = 300
DEFAULT_SHUTDOWN_RESERVE_SECONDS = 10
DEFAULT_BACKUP_OVERHEAD_BYTES = 16 * 1024**2


def _seconds(value, name, *, remaining=False):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(name + " must be a finite number")
    if not remaining and value < 0:
        raise ValueError(name + " must not be negative")
    return float(value)


def _bytes(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(name + " must be a non-negative integer")
    return value


def remaining_audio_seconds(
    remaining_seconds,
    *,
    cap_seconds=600,
    finalization_reserve_seconds=DEFAULT_FINALIZATION_RESERVE_SECONDS,
    shutdown_reserve_seconds=DEFAULT_SHUTDOWN_RESERVE_SECONDS,
):
    """Return a whole-second audio work budget, or zero to defer audio.

`remaining_seconds` is the outer workflow's monotonic deadline minus now.
Run the child with this budget as its own limit and a parent timeout no greater
than budget + shutdown_reserve_seconds. The finalization reserve is then still
available for export, publication and both verified backups. Zero never means
that the media queue is empty; the caller must record a deferred/partial result.
"""
    remaining = _seconds(remaining_seconds, "remaining_seconds", remaining=True)
    cap = _seconds(cap_seconds, "cap_seconds")
    reserve = _seconds(finalization_reserve_seconds, "finalization_reserve_seconds")
    shutdown = _seconds(shutdown_reserve_seconds, "shutdown_reserve_seconds")
    return math.floor(max(0.0, min(cap, remaining - reserve - shutdown)))


def remaining_collection_seconds(
    remaining_seconds,
    phase_remaining_seconds,
    *,
    cap_seconds=1200,
    finalization_reserve_seconds=DEFAULT_FINALIZATION_RESERVE_SECONDS,
    shutdown_reserve_seconds=DEFAULT_SHUTDOWN_RESERVE_SECONDS,
):
    """Bound a text-collection call without resetting its shared phase budget.

The caller establishes one collection-phase deadline, then passes its remaining
    time for BOTH collect and collect_current, including each child's shutdown
    allowance. A resumed old window cannot grant a second full allocation. If a current-window check receives zero,
record it as deferred and keep the overall result partial; do not mark it checked.
Any desired current-window allocation must be reserved before starting backlog.
"""
    phase = _seconds(phase_remaining_seconds, "phase_remaining_seconds", remaining=True)
    cap = _seconds(cap_seconds, "cap_seconds")
    shutdown = _seconds(shutdown_reserve_seconds, "shutdown_reserve_seconds")
    return remaining_audio_seconds(
        remaining_seconds,
        cap_seconds=max(0.0, min(cap, phase - shutdown)),
        finalization_reserve_seconds=finalization_reserve_seconds,
        shutdown_reserve_seconds=shutdown_reserve_seconds,
    )


def estimate_disk_reserve(
    neutral_backup_bytes,
    native_backup_bytes,
    *,
    minimum_free_bytes,
    queue_backup_bytes=0,
    candidate_bytes=0,
    native_candidate_copies=1,
    overhead_bytes=DEFAULT_BACKUP_OVERHEAD_BYTES,
    other_growth_bytes=0,
):
    """Return conservative additional free-space requirements and their parts.

Supply the FULL estimated sizes of the next neutral/native backups, excluding
the new candidate and excluding queue/prefix files counted in queue_backup_bytes.
These are logical copied sizes, not `du` allocation or old manifest sizes unless
the caller has proved those still cover the current data. Existing live files
already reduce measured free space and are not counted again as new live copies.

For a candidate of B bytes, reserve B each for staging, neutral immutable storage,
neutral backup, and a resumable-prefix backup, plus B*n each for native publication
and its backup. Thus n=1 reserves 6B. This intentionally does not assume that a
completed staging file is deleted before backups, or credit a retained prefix.
Increase native_candidate_copies when internal aliases or publication layout make
the native backup/publication duplicate source bytes. `other_growth_bytes` lets the
caller reserve bounded text-growth or other writes BEFORE collection begins.

The caller admits work only if measured free_bytes >= required_free_bytes.
For pre-collection finalization admission, use candidate_bytes=0 and include the
text-growth reserve; insufficient space should fail before collector mutation.
For optional audio, use the candidate's full byte cap; insufficiency defers audio
without blocking publication/backups of completed text work. This is a reservation,
not an implementation of retention, backup inclusion, or free-space monitoring.
"""
    values = {
        "neutral_backup_bytes": neutral_backup_bytes,
        "native_backup_bytes": native_backup_bytes,
        "minimum_free_bytes": minimum_free_bytes,
        "queue_backup_bytes": queue_backup_bytes,
        "candidate_bytes": candidate_bytes,
        "overhead_bytes": overhead_bytes,
        "other_growth_bytes": other_growth_bytes,
    }
    for name, value in values.items():
        _bytes(value, name)
    _bytes(native_candidate_copies, "native_candidate_copies")
    if native_candidate_copies < 1:
        raise ValueError("native_candidate_copies must be at least one")
    existing_backups = {
        "neutral": neutral_backup_bytes,
        "native": native_backup_bytes,
        "queue_and_prefixes": queue_backup_bytes,
    }
    candidate = {
        "staging": candidate_bytes,
        "neutral_object": candidate_bytes,
        "native_publication": candidate_bytes * native_candidate_copies,
        "neutral_backup": candidate_bytes,
        "native_backup": candidate_bytes * native_candidate_copies,
        "queued_prefix_backup": candidate_bytes,
    }
    backup_total = sum(existing_backups.values())
    candidate_total = sum(candidate.values())
    required = minimum_free_bytes + backup_total + candidate_total + overhead_bytes + other_growth_bytes
    return {
        "required_free_bytes": required,
        "minimum_free_bytes": minimum_free_bytes,
        "existing_next_backup_bytes": existing_backups,
        "existing_next_backup_total_bytes": backup_total,
        "candidate_copy_bytes": candidate,
        "candidate_total_bytes": candidate_total,
        "candidate_bytes": candidate_bytes,
        "native_candidate_copies": native_candidate_copies,
        "overhead_bytes": overhead_bytes,
        "other_growth_bytes": other_growth_bytes,
        "policy": "Conservative reservation; no deletion or assumed deduplication between live data and separate backups",
    }
