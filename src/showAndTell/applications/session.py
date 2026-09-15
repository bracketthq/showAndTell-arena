"""One composed session over a set of showAndTell.applications.

Capture and run are the same lifecycle at different phases, so they share this
object.  It is built from a list of application names and nothing else -- there
is no family, no stack, and no table mapping one to the other.

The lifecycle plane is reached over the fixture host agent's HTTP surface, so
the same code path serves a local auto-spawned agent and the shared VM.  The
data plane runs here, in process, and receives everything it needs through
``AppContext`` -- which is why none of this passes information between planes
through environment variables.
"""
from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .browser.runtime import origin
from .manifest import Manifest
from .registry import Registry, default_registry


@dataclass(frozen=True, slots=True)
class AppContext:
    """Everything one application's data plane needs to reach its app.

    ``secrets`` carries what only the driver can answer -- a published IMAP
    port, a connector token -- because the driver runs beside the containers
    and this does not.
    """

    manifest: Manifest
    url: str
    credentials: dict[str, str]
    secrets: dict[str, Any] = field(default_factory=dict)
    host: str = "127.0.0.1"
    demo_root: Path | None = None

    @property
    def name(self) -> str:
        return self.manifest.name

    def asset(self, relative_path: str, expected_sha256: str) -> bytes:
        """Read a digest-checked file the task carries beside its seed.

        The digest is what makes an edited draft a loud failure rather than a
        silently different starting state.
        """
        if self.demo_root is None:
            raise RuntimeError(
                f"{self.name}: asset {relative_path!r} needs a task demo root; "
                f"this session was started without one")
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError(f"invalid task asset path {relative_path!r}")
        # No resolve()-based containment: hub-cache bundles are symlink
        # farms into the content-addressed blobs/ store, so resolving the
        # file would land outside the demo root. The relative-path check
        # above blocks traversal, and the sha256 below is the integrity
        # guarantee.
        payload = (Path(self.demo_root) / relative).read_bytes()
        actual = hashlib.sha256(payload).hexdigest()
        if actual != str(expected_sha256).lower():
            raise ValueError(
                f"task asset {relative_path} does not match its recorded "
                f"sha256; the copy on disk has changed")
        return payload


class AppSession:
    """Hold a set of applications through capture or through a task run."""

    def __init__(
        self,
        applications: Sequence[str],
        *,
        client,
        registry: Registry | None = None,
        holder: str = "showAndTell",
        demo_root: Path | None = None,
        force: bool = False,
    ) -> None:
        if not applications:
            raise ValueError("a session needs at least one application")
        seen = list(dict.fromkeys(applications))
        if len(seen) != len(applications):
            raise ValueError(f"duplicate applications: {list(applications)}")
        self.registry = registry or default_registry()
        self.applications = tuple(seen)
        # Resolving now turns an unknown application into an error before any
        # container is touched.
        self.manifests = {name: self.registry.manifest(name) for name in seen}
        self.client = client
        self.holder = holder
        self.demo_root = Path(demo_root) if demo_root is not None else None
        self.force = force
        self.lease_id: str | None = None
        self._owns_lease = False
        self._keeper = None
        self._states: dict[str, Any] = {}
        self._contexts: dict[str, AppContext] = {}

    # -- lifecycle ---------------------------------------------------------
    @property
    def primary(self) -> str:
        return self.applications[0]

    def start(self, *, ensure_started=None) -> None:
        """Take one lease over every application, then resolve their contexts.

        Bringing an application up is deliberately NOT done here. A shared
        agent may already be serving other work from the state it holds, so
        cold-starting one mid-session is the caller's decision, not a side
        effect of taking a lease. ``ensure_started(apps, lease_id)`` is the
        hook a caller that owns the agent passes in.
        """
        from showAndTell.applications.host.client import LEASE_ENV, LeaseKeeper

        apps = list(self.applications)
        if self.force:
            # Operator-confirmed override for a stale lease whose keeper may
            # still be heartbeating (a closed viewer tab renews it forever).
            self.client.force_release(apps)
        ambient = (os.environ.get(LEASE_ENV)
                   if os.environ.get("SHOWANDTELL_EXECUTION_MANAGER_CHILD") == "1"
                   else None)
        if ambient:
            # A parent ExecutionManager owns and renews this fenced lease. The
            # child adopts it for reset/seed calls but must never release it.
            self.lease_id = ambient
            self._owns_lease = False
        else:
            self.lease_id = self.client.acquire_lease(apps, holder=self.holder)
            self._owns_lease = True
            self._keeper = LeaseKeeper(self.client, self.lease_id)
            self._keeper.start()
        try:
            if ensure_started is not None:
                ensure_started(apps, self.lease_id)
            self._build_contexts()
        except BaseException:
            self.close()
            raise

    def _build_contexts(self) -> None:
        for name, manifest in self.manifests.items():
            secrets = self.client.secrets(name)
            self._contexts[name] = AppContext(
                manifest=manifest,
                url=self.client.app_url(name),
                credentials=dict(manifest.credentials),
                secrets=secrets,
                host=self.client.host,
                demo_root=self.demo_root,
            )

    def context(self, name: str) -> AppContext:
        return self._contexts[name]

    def use_demo_root(self, demo_root: Path | str | None) -> None:
        """Point asset resolution at a task's demo directory.

        A capture starts without one — nothing to read yet. A trial replaying a
        draft has to resolve that draft's own ``.eml`` and ``.xlsx`` files, and
        which draft it is only becomes known at restore time.
        """
        import dataclasses

        self.demo_root = Path(demo_root) if demo_root is not None else None
        self._contexts = {
            name: dataclasses.replace(ctx, demo_root=self.demo_root)
            for name, ctx in self._contexts.items()
        }

    @property
    def urls(self) -> dict[str, str]:
        return {name: ctx.url for name, ctx in self._contexts.items()}

    @property
    def credentials(self) -> dict[str, dict[str, str]]:
        return {name: dict(ctx.credentials) for name, ctx in self._contexts.items()}

    def browser_credentials(self, primary: str | None = None) -> dict[str, Any]:
        """Credentials plus routable context for a multi-application adapter."""
        from .browser.context import CONTEXT_KEY

        primary = primary or self.primary
        if primary not in self._contexts:
            raise KeyError(primary)
        applications: dict[str, dict[str, Any]] = {}
        for name, ctx in self._contexts.items():
            metadata: dict[str, Any] = {}
            state = self._states.get(name)
            describe = getattr(state, "browser_metadata", None)
            if callable(describe):
                metadata.update(describe(ctx))
            applications[name] = {
                "url": ctx.url,
                "browser_url": metadata.pop(
                    "browser_url", self.surface_url(name)
                ),
                "credentials": dict(ctx.credentials),
                "metadata": metadata,
            }
        return {
            **self._contexts[primary].credentials,
            CONTEXT_KEY: applications,
        }

    def surface_url(self, name: str) -> str:
        """Where a browser should open this application."""
        return f"{self.urls[name].rstrip('/')}{self.manifests[name].surface_entry}"

    def runtime_url(self, name: str) -> str:
        """Base URL a task demonstration should receive for an application.

        Most browser adapters append their own routes to the application's
        published origin. Some state planes expose a different browser entry,
        such as ONLYOFFICE's connector-hosted editor. Its origin is
        authoritative once the state has been prepared or seeded; the replay
        driver remains responsible for appending its recorded route.
        """
        state = self._states.get(name)
        describe = getattr(state, "browser_metadata", None)
        if callable(describe):
            metadata = describe(self._contexts[name])
            browser_url = metadata.get("browser_url")
            if browser_url:
                return origin(str(browser_url))
        return self.urls[name]

    # -- planes ------------------------------------------------------------
    def state(self, name: str):
        """This application's data plane, built once per session."""
        if name not in self._states:
            self._states[name] = self.registry.state(name)
        return self._states[name]

    def _with_state(self):
        for name in self.applications:
            if self.registry.has_state(name):
                yield name, self.state(name), self._contexts[name]

    # -- data --------------------------------------------------------------
    def prepare(self, *, only: Sequence[str] | None = None) -> None:
        """Make each application usable for a capture, before anyone opens it."""
        selected = set(self.applications if only is None else only)
        for name, state, ctx in self._with_state():
            prepare = getattr(state, "prepare", None)
            if name in selected and callable(prepare):
                prepare(ctx)

    def reset(self, *, coarse: bool = True, only: Sequence[str] | None = None) -> None:
        """Return every application to an empty starting point.

        ``coarse`` runs the driver's own reset -- wipe the mail store, purge
        the editor cache -- which is the guarantee that nothing survives from a
        previous run. The data plane's reset then removes task-owned records
        for anything the coarse pass deliberately left alone.
        """
        selected = set(self.applications if only is None else only)
        if coarse:
            for name in self.applications:
                if name in selected:
                    self.client.reset(name, lease_id=self.lease_id)
        failure: BaseException | None = None
        for name, state, ctx in self._with_state():
            if name not in selected:
                continue
            try:
                state.reset(ctx)
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                # Every application still gets reset: a half-reset session
                # must not be able to look clean to the next seed.
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure

    def seed(self, document: Mapping[str, Any]) -> None:
        """Route application-keyed blocks to their data planes."""
        keys = set(document)
        unknown = keys - set(self.registry.names())
        outside = keys - set(self.applications)
        if unknown:
            raise ValueError(
                f"seed has non-application keys: {sorted(unknown)}"
            )
        if outside:
            raise ValueError(
                f"seed names applications outside this session: {sorted(outside)}"
            )
        for name, state, ctx in self._with_state():
            block = document.get(name)
            if block is None:
                continue
            state.seed(ctx, block)

    def export(self) -> dict[str, Any]:
        """What each application holds now, described declaratively."""
        return {name: state.export(ctx) for name, state, ctx in self._with_state()}

    # -- authored seed data ------------------------------------------------
    def seed_forms(self) -> dict[str, dict[str, Any]]:
        """The seed form each application declares, keyed by application.

        Applications that can be set up entirely through their own UI declare
        none and are absent, so a caller renders whatever it is given without
        knowing which applications exist.
        """
        from .authoring.seedform import describe

        forms = {}
        for name, state, _ctx in self._with_state():
            form = describe(state)
            if form is not None:
                forms[name] = form
        return forms

    def apply_form(self, application: str, rows: Sequence[Mapping[str, Any]]) -> int:
        """Validate authored rows and seed them into the live application.

        The rows are applied as a whole rather than appended: seeding is
        delete-by-id then write, so re-applying an edited set replaces it
        instead of leaving the earlier version behind. Returns how many rows
        survived validation -- blank ones an author started and abandoned are
        dropped rather than refused.
        """
        from .authoring.seedform import describe

        if application not in self.applications:
            raise KeyError(
                f"{application!r} is not in this session; available: "
                f"{list(self.applications)}")
        state = self.state(application)
        if describe(state) is None:
            raise ValueError(f"{application!r} declares no seed form")
        cleaned = state.SEED_FORM.validate(rows)
        block = state.form_block(self.context(application), cleaned)
        self.seed({application: block})
        return len(cleaned)

    def capture(self, directory: Path | str) -> dict[str, Any]:
        """Freeze hand-made setup state into the draft, per application.

        Two mechanisms, chosen by the application: a data plane that can
        describe its own state writes assets and returns a seed block; an
        application whose state is a database takes a binary snapshot through
        its driver instead.
        """
        from showAndTell.applications.host.client import FixtureHostUnsupported

        directory = Path(directory)
        captured: dict[str, Any] = {}
        for name, state, ctx in self._with_state():
            capture = getattr(state, "capture", None)
            if not callable(capture):
                continue
            block = capture(ctx, directory)
            if block:
                captured[name] = block

        for name in self.applications:
            if not self.manifests[name].snapshots:
                continue
            target = directory / f"{name}-state.tar"
            try:
                self.client.snapshot(name, self.holder, lease_id=self.lease_id)
                self.client.pull_snapshot(name, self.holder, target)
            except FixtureHostUnsupported:
                # The manifest claimed snapshots but the driver disagrees; the
                # driver owns the containers, so the driver wins.
                continue
            captured.setdefault(name, {})["snapshot"] = target.name
        return captured

    def restore_snapshots(self, directory: Path | str) -> list[str]:
        """Push back any binary state the draft carries. Returns what restored."""
        from showAndTell.applications.host.client import FixtureHostUnsupported

        directory = Path(directory)
        restored: list[str] = []
        for name in self.applications:
            path = directory / f"{name}-state.tar"
            if not path.is_file():
                continue
            try:
                self.client.push_snapshot(name, self.holder, path,
                                          lease_id=self.lease_id)
                self.client.restore_snapshot(name, self.holder,
                                             lease_id=self.lease_id)
            except FixtureHostUnsupported:
                continue
            restored.append(name)
        return restored

    # -- teardown ----------------------------------------------------------
    def close(self) -> None:
        if self._keeper is not None:
            self._keeper.stop()
            self._keeper = None
        if self.lease_id is not None and self._owns_lease:
            try:
                self.client.release(self.lease_id)
            except Exception:  # noqa: BLE001 - releasing is best effort
                pass
            self.lease_id = None
        else:
            self.lease_id = None
        self._owns_lease = False

    def __enter__(self) -> "AppSession":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
