from tests._viewer_fixture import load_serve


class FakeSession:
    applications = ("erpnext",)
    credentials = {"erpnext": {"email": "Administrator", "password": "secret"}}

    def __init__(self):
        self.calls = []

    def surface_url(self, name):
        return f"http://127.0.0.1:8080/{name}"

    def export(self):
        self.calls.append("export")
        return {"erpnext": {"items": [{"name": "Desk"}]}}

    def reset(self, *, only=None, coarse=True):
        self.calls.append("reset")

    def prepare(self, *, only=None):
        self.calls.append("prepare")

    def seed(self, block):
        self.calls.append(("seed", block))

    def restore_snapshots(self, _directory):
        return []

    def use_demo_root(self, _directory):
        pass

    def close(self):
        self.calls.append("close")


def test_capture_workspace_exports_and_restores_application_state(tmp_path):
    workspace = load_serve().capture.CaptureWorkspace({
        "slug": "capture", "surfaces": [{
            "application": "erpnext", "url": "http://127.0.0.1:8080/",
            "credentials": {},
        }],
    })
    session = FakeSession()
    workspace.session = session

    seed, exported = workspace.export()
    assert exported is True
    assert seed == {"erpnext": {"items": [{"name": "Desk"}]}}

    demo = tmp_path / "demo"
    demo.mkdir()
    (demo / "seed.json").write_text(
        '{"erpnext": {"items": [{"name": "Chair"}]}}')
    workspace.restore(use_export=True, state_dir=demo)
    assert session.calls[-3:] == ["reset", "prepare", (
        "seed", {"erpnext": {"items": [{"name": "Chair"}]}})]
    workspace.close()
    assert session.calls[-1] == "close"
