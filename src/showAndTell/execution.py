"""Application-session ownership for one ShowAndTell execution."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from showAndTell.applications.session import AppSession


@dataclass(slots=True)
class ExecutionLease:
    """Ownership of the application session used by one execution."""

    session: AppSession
    manager: "ExecutionManager"
    _closed: bool = False

    def annotate(self, *, task: str, kind: str, product: str) -> None:
        self.manager.annotate(self, task=task, kind=kind, product=product)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.session.close()


class ExecutionManager:
    """Own execution acquisition, description, release, and stale clearing."""

    def __init__(self, client: Any, *, session_factory=AppSession) -> None:
        self.client = client
        self.session_factory = session_factory

    def annotate(self, lease: ExecutionLease, *, task: str, kind: str,
                 product: str) -> None:
        client = getattr(lease.session, "client", self.client)
        annotate = getattr(client, "annotate_lease", None)
        if callable(annotate):
            try:
                annotate(lease.session.lease_id, {
                    "task": task,
                    "kind": kind,
                    "product": product,
                })
            except Exception:
                pass

    def list_active(self) -> list[dict]:
        return self.client.list_leases()

    def force_release(self, lease_id: str) -> None:
        self.client.release(lease_id)

    def acquire(
        self,
        applications: Sequence[str],
        *,
        holder: str,
        ensure_started=None,
        on_session=None,
        clear_stale: bool = False,
        demo_root=None,
    ) -> ExecutionLease:
        """Acquire the requested applications as one execution."""
        session = None
        try:
            session = self.session_factory(
                applications, client=self.client, holder=holder,
                demo_root=demo_root, force=False,
            )
            if on_session is not None:
                on_session(session)
            if clear_stale:
                self.clear_stale(applications)
            session.start(ensure_started=ensure_started)
        except BaseException:
            if session is not None:
                session.close()
            raise
        return ExecutionLease(session=session, manager=self)

    def clear_stale(self, applications: Sequence[str]) -> list[dict]:
        return self.client.force_release(list(applications))
