"""Shared helpers for tests that construct an application plane."""
def app_manifest(name: str):
    """The real application folder's manifest — ports and credentials included."""
    from showAndTell.applications.registry import default_registry

    return default_registry().manifest(name)


class NoopState:
    """A data plane that does nothing, for tests about orchestration.

    The real planes talk to live containers over HTTP and IMAP; a viewer test
    that exercised them would be testing Docker, not the viewer.
    """

    def __init__(self, manifest):
        self.manifest = manifest
        self.application = manifest.name
        self.calls: list[tuple] = []

    def prepare(self, ctx):
        self.calls.append(("prepare",))

    def reset(self, ctx):
        self.calls.append(("reset",))

    def seed(self, ctx, block):
        self.calls.append(("seed", block))

    def export(self, ctx):
        return {}

    def capture(self, ctx, directory):
        return {}


def stub_state_planes(monkeypatch):
    """Replace every application's data plane with NoopState."""
    from showAndTell.applications.registry import Registry

    monkeypatch.setattr(
        Registry, "state",
        lambda self, name, **kw: NoopState(self.manifest(name)))
