"""Choose which fixture host agent a capture talks to — local or remote.

Once ``AppSession`` holds the applications, this module has almost nothing to
do: the session takes its own lease, and each application's URL, credentials
and secrets travel to the data plane inside an ``AppContext``.  The whole
``SHOWANDTELL_<FAMILY>_<APP>_URL`` environment dance the old wiring performed is
gone with it.

Configured-but-unreachable fails loudly; there is deliberately no silent
fallback to local Docker.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Sequence

from showAndTell.applications.host import client as host_client

# Applications whose baseline is expensive enough that a cold local agent
# should bake and freeze it once rather than reinstalling per capture.
_GOLDEN_APPS = ("erpnext",)


@dataclasses.dataclass(frozen=True)
class CaptureClient:
    """Which agent capture should use, and whether cold-bootstrapping it is safe.

    ``is_local`` is True only for the auto-spawned loopback agent that this
    process itself started. A configured/shared agent (``SHOWANDTELL_FIXTURE_
    HOST_URL`` set — e.g. the team VM) must never be cold-started mid-capture:
    other captures or trials may already depend on the state it holds, so an
    unreachable or unhealthy configured agent stays a loud failure instead.
    """

    client: host_client.FixtureHostClient
    is_local: bool


def client_for_capture() -> CaptureClient:
    # local_agent_client publishes its own URL for downstream adapters.  On a
    # later capture that URL is therefore "configured" even though the agent
    # is still private to this process and remains safe to cold-bootstrap.
    spawned = host_client.auto_spawned_client()
    if spawned is not None:
        spawned.health()
        return CaptureClient(spawned, is_local=True)
    configured = host_client.configured_client()
    if configured is not None:
        configured.health()  # unreachable ⇒ FixtureHostError naming the URL
        return CaptureClient(configured, is_local=False)
    return CaptureClient(host_client.local_agent_client(), is_local=True)


def acquire_execution(applications: Sequence[str], holder: str, **kwargs):
    """Acquire an execution on the chosen agent, with the bootstrap policy.

    Only the auto-spawned local agent is ours to cold-start; a shared or
    remote agent must never be bootstrapped mid-capture, so it gets no hook
    at all (see ``CaptureClient``).
    """
    from showAndTell.applications.session import AppSession
    from showAndTell.execution import ExecutionManager

    held = client_for_capture()
    bootstrap = None
    if held.is_local:
        def bootstrap(apps, lease_id):
            bootstrap_local(held.client, apps, lease_id=lease_id)
    return ExecutionManager(held.client, session_factory=AppSession).acquire(
        applications, holder=holder, ensure_started=bootstrap, **kwargs)


def bootstrap_local(client: host_client.FixtureHostClient, apps: Sequence[str],
                    *, lease_id: str) -> None:
    """Cold-start a freshly auto-spawned local agent before first use.

    On a clean agent, ``start`` runs the slow first-time install, then
    ``baseline``/``golden`` bake and freeze the expensive baselines once so
    local resets become snapshot restores too — otherwise every local capture
    pays the reinstall cost the VM was built to avoid. Never called for a
    configured/remote agent (see ``CaptureClient``).
    """
    for app in apps:
        if client.status(app)["state"] != "healthy":
            client.start(app, wait=True)
    for app in apps:
        if app not in _GOLDEN_APPS:
            continue
        if "golden" not in client.status(app).get("snapshots", []):
            client.baseline(app, lease_id=lease_id)
            client.golden(app, lease_id=lease_id)
