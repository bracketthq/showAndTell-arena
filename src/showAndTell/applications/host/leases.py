"""In-memory exclusive leases over fixture showAndTell.applications.

A lease is what makes one warm stack safe to share: mutating agent
operations require one, so two operators cannot reset each other's
capture mid-flight.  Expiry is evaluated lazily against an injectable
clock, so a crashed holder frees the stack without a reaper thread.
"""
from __future__ import annotations

import dataclasses
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence


@dataclasses.dataclass(frozen=True)
class Lease:
    lease_id: str
    apps: frozenset[str]
    holder: str
    ttl_s: float
    expires_at: float
    # requested name -> granted instance; identity when no pool is involved.
    assignment: dict[str, str] = dataclasses.field(default_factory=dict)
    # Viewer-facing execution details supplied by the lease owner. The host
    # remains authoritative for whether the execution is active.
    execution: dict[str, object] = dataclasses.field(default_factory=dict)


class LeaseConflict(Exception):
    def __init__(self, held_by: str, since: float):
        super().__init__(f"held by {held_by}")
        self.held_by = held_by
        self.since = since


class UnknownLease(KeyError):
    """The lease does not exist or has expired."""


class LeaseStore:
    def __init__(self, now: Callable[[], float] = time.monotonic,
                 families: Mapping[str, Sequence[str]] | None = None):
        self._now = now
        self._lock = threading.Lock()
        self._leases: dict[str, Lease] = {}
        # Wall-clock grant times: shown to remote clients in "busy since".
        self._granted_at: dict[str, float] = {}
        # logical application name -> its instances, in assignment order.
        # A name absent here is its own single instance.
        self._families = {name: tuple(members)
                          for name, members in (families or {}).items()}

    def _held(self) -> dict[str, Lease]:
        """instance -> the live lease holding it (lock held, post-prune)."""
        held: dict[str, Lease] = {}
        for lease in self._leases.values():
            for app in lease.apps:
                held[app] = lease
        return held

    def _oldest(self, wanted: Iterable[Lease]) -> Lease:
        """The longest-lived of ``wanted``, by grant order (dict insertion)."""
        ids = {lease.lease_id for lease in wanted}
        return next(l for l in self._leases.values() if l.lease_id in ids)

    def _prune(self) -> None:
        cutoff = self._now()
        for lease_id in [i for i, l in self._leases.items() if l.expires_at <= cutoff]:
            del self._leases[lease_id]
            self._granted_at.pop(lease_id, None)

    def acquire(self, apps: Iterable[str], holder: str, ttl_s: float = 900.0) -> Lease:
        requested = list(dict.fromkeys(apps))
        if not requested:
            raise ValueError("a lease must name at least one app")
        with self._lock:
            self._prune()
            held = self._held()
            assignment: dict[str, str] = {}
            for name in requested:
                candidates = self._families.get(name, (name,))
                free = [c for c in candidates
                        if c not in held and c not in assignment.values()]
                if not free:
                    blocking = [held[c] for c in candidates if c in held]
                    if not blocking:  # exhausted purely by this same request
                        raise ValueError(
                            f"request names overlapping instances for {name!r}")
                    oldest = self._oldest(blocking)
                    label = oldest.holder if len(candidates) == 1 else (
                        f"{oldest.holder} "
                        f"({len(blocking)}/{len(candidates)} in use)")
                    raise LeaseConflict(label, self._granted_at[oldest.lease_id])
                assignment[name] = free[0]
            lease = Lease(
                lease_id=secrets.token_hex(8),
                apps=frozenset(assignment.values()), holder=holder,
                ttl_s=ttl_s, expires_at=self._now() + ttl_s,
                assignment=assignment,
            )
            self._leases[lease.lease_id] = lease
            self._granted_at[lease.lease_id] = time.time()
            return lease

    def heartbeat(self, lease_id: str) -> Lease:
        with self._lock:
            self._prune()
            lease = self._leases.get(lease_id)
            if lease is None:
                raise UnknownLease(lease_id)
            renewed = dataclasses.replace(lease, expires_at=self._now() + lease.ttl_s)
            self._leases[lease_id] = renewed
            return renewed

    def annotate(self, lease_id: str, execution: Mapping[str, object]) -> Lease:
        """Attach display metadata to a live lease without changing ownership."""
        allowed = {"task", "kind", "product"}
        details = {key: value for key, value in execution.items() if key in allowed}
        with self._lock:
            self._prune()
            lease = self._leases.get(lease_id)
            if lease is None:
                raise UnknownLease(lease_id)
            updated = dataclasses.replace(lease, execution=details)
            self._leases[lease_id] = updated
            return updated

    def active(self) -> list[tuple[Lease, float | None]]:
        """Snapshot all live leases with their wall-clock grant times."""
        with self._lock:
            self._prune()
            return [
                (lease, self._granted_at.get(lease.lease_id))
                for lease in self._leases.values()
            ]

    def release(self, lease_id: str) -> None:
        with self._lock:
            self._leases.pop(lease_id, None)
            self._granted_at.pop(lease_id, None)

    def force_release(self, apps: Iterable[str]) -> list[Lease]:
        """Break the oldest lease blocking each named app; returns the removed.

        A logical name frees exactly one slot of its pool — the stalest —
        so an operator override cannot kill every teammate's live run.
        The escape hatch for a stale holder whose keeper still heartbeats:
        the holder's next heartbeat gets UnknownLease and gives up.
        """
        with self._lock:
            self._prune()
            removed: dict[str, Lease] = {}
            for name in dict.fromkeys(apps):
                candidates = frozenset(self._families.get(name, (name,)))
                overlapping = [l for l in self._leases.values()
                               if l.apps & candidates]
                if overlapping:
                    oldest = self._oldest(overlapping)
                    removed[oldest.lease_id] = oldest
            for lease_id in removed:
                del self._leases[lease_id]
                self._granted_at.pop(lease_id, None)
            return list(removed.values())

    def covers(self, lease_id: str | None, app: str) -> bool:
        if not lease_id:
            return False
        with self._lock:
            self._prune()
            lease = self._leases.get(lease_id)
            return lease is not None and app in lease.apps

    def holder_of(self, app: str) -> Lease | None:
        with self._lock:
            self._prune()
            for lease in self._leases.values():
                if app in lease.apps:
                    return lease
            return None

    def held_since(self, lease_id: str) -> float | None:
        with self._lock:
            return self._granted_at.get(lease_id)
