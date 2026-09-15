"""The data planes in applications/<app>/state.py, against AppContext.

These cover the half of a capture that no declarative export can describe: the
mailbox and the workbook an operator builds by hand during setup.
"""
import hashlib

import pytest

from showAndTell.applications.onlyoffice.state import BLANK_WORKBOOK, State as OnlyOfficeState
from showAndTell.applications.roundcube.imap import IMAPSeeder
from showAndTell.applications.roundcube.state import State as RoundcubeState
from showAndTell.applications.session import AppContext
from tests._app_fixtures import app_manifest

MAILBOX = {"email": "agent@showAndTell.test", "password": "showAndTell-mail",
           "folder": "INBOX"}


def _ctx(name, *, demo_root=None, secrets=None, credentials=None):
    manifest = app_manifest(name)
    return AppContext(
        manifest=manifest, url=f"http://agent.test:{manifest.app_port.host}",
        credentials=credentials or dict(manifest.credentials),
        secrets=secrets or {}, host="agent.test", demo_root=demo_root)


# ==== Roundcube ===========================================================

def _raw(message_id: str, subject: str) -> bytes:
    return (f"Message-ID: {message_id}\r\nFrom: Boss <boss@showAndTell.test>\r\n"
            f"To: agent@showAndTell.test\r\nSubject: {subject}\r\n\r\n"
            f"Body of {subject}.\r\n").encode()


def _message_id_of(raw: bytes) -> str:
    for line in raw.decode(errors="replace").splitlines():
        if line.lower().startswith("message-id:"):
            return line.split(":", 1)[1].strip()
    return ""


class _FakeIMAP:
    """An in-memory INBOX: append/search/fetch behave like one mailbox."""

    def __init__(self, host, port, box=None):
        self.host, self.port, self.box = host, port, box if box is not None else []

    def starttls(self, ssl_context=None):
        return "OK", []

    def login(self, user, password):
        return "OK", []

    def select(self, mailbox):
        return "OK", []

    def search(self, charset, *criteria):
        if criteria and criteria[0] == "ALL":
            return "OK", [b" ".join(str(i + 1).encode()
                                    for i in range(len(self.box)))]
        wanted = criteria[-1].strip('"')
        return "OK", [b" ".join(str(i + 1).encode()
                                for i, row in enumerate(self.box)
                                if row["message_id"] == wanted)]

    def fetch(self, message_set, message_parts):
        """Mirrors dovecot 2.4.3, including RFC822's \\Seen side effect.

        A fake that returned the body either way would let the capture-marks-
        mail-as-read bug back in without failing anything.
        """
        row = self.box[int(message_set) - 1]
        body = None
        if "BODY.PEEK[" in message_parts:
            body, label = row["raw"], "BODY[]"
        elif "RFC822" in message_parts:
            body, label = row["raw"], "RFC822"
            row["seen"] = True          # the side effect the real server has
        flags = r"\Seen" if row["seen"] else ""
        if body is None:
            return "OK", [f"1 (FLAGS ({flags}))".encode()]
        return "OK", [(f"1 (FLAGS ({flags}) {label} "
                       f"{{{len(body)}}}".encode(), body), b")"]

    def store(self, message_set, command, flags):
        return "OK", []

    def expunge(self):
        return "OK", []

    def append(self, mailbox, flags, date_time, message):
        self.box.append({"message_id": _message_id_of(message), "raw": message,
                         "seen": bool(flags and "Seen" in flags)})
        return "OK", []

    def logout(self):
        return "BYE", []


def _roundcube(box):
    return RoundcubeState(
        app_manifest("roundcube"),
        seeder_factory=lambda ctx: IMAPSeeder(
            "imap.local", 1143, ssl=False,
            factory=lambda host, port: _FakeIMAP(host, port, box)))


def test_capture_writes_every_message_as_a_verbatim_eml_asset(tmp_path):
    box = [{"message_id": "<a@x>", "raw": _raw("<a@x>", "First"), "seen": False},
           {"message_id": "<b@x>", "raw": _raw("<b@x>", "Second"), "seen": True}]
    block = _roundcube(box).capture(_ctx("roundcube"), tmp_path)

    assert [row["asset"] for row in block["messages"]] == [
        "roundcube/0001.eml", "roundcube/0002.eml"]
    assert (tmp_path / "roundcube" / "0001.eml").read_bytes() == box[0]["raw"]


def test_capture_keys_messages_on_their_real_message_id(tmp_path):
    """Roundcube-composed mail already has a Message-ID; none is synthesised."""
    box = [{"message_id": "<real@roundcube.local>",
            "raw": _raw("<real@roundcube.local>", "First"), "seen": False}]
    block = _roundcube(box).capture(_ctx("roundcube"), tmp_path)
    assert block["messages"][0]["message_id"] == "<real@roundcube.local>"


def test_capture_preserves_read_state(tmp_path):
    box = [{"message_id": "<a@x>", "raw": _raw("<a@x>", "U"), "seen": False},
           {"message_id": "<b@x>", "raw": _raw("<b@x>", "R"), "seen": True}]
    block = _roundcube(box).capture(_ctx("roundcube"), tmp_path)
    assert [row["seen"] for row in block["messages"]] == [False, True]


def test_capture_of_an_empty_inbox_claims_nothing(tmp_path):
    """An empty block is how the draft honestly reports no captured mail."""
    assert _roundcube([]).capture(_ctx("roundcube"), tmp_path) == {}


def test_capture_then_seed_round_trips_the_inbox(tmp_path):
    original = [{"message_id": "<a@x>", "raw": _raw("<a@x>", "First"), "seen": False},
                {"message_id": "<b@x>", "raw": _raw("<b@x>", "Second"), "seen": True}]
    block = _roundcube(list(original)).capture(_ctx("roundcube"), tmp_path)

    restored: list = []
    _roundcube(restored).seed(_ctx("roundcube", demo_root=tmp_path), block)
    assert [r["raw"] for r in restored] == [r["raw"] for r in original]
    assert [r["seen"] for r in restored] == [r["seen"] for r in original]


def test_seed_refuses_a_message_whose_asset_digest_moved(tmp_path):
    (tmp_path / "roundcube").mkdir()
    (tmp_path / "roundcube" / "0001.eml").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="sha256"):
        _roundcube([]).seed(_ctx("roundcube", demo_root=tmp_path), {
            "mailboxes": [MAILBOX],
            "messages": [{"external_id": "captured-0001", "message_id": "<a@x>",
                          "mailbox": MAILBOX["email"],
                          "asset": "roundcube/0001.eml",
                          "sha256": hashlib.sha256(b"original").hexdigest()}]})


def test_seed_restores_from_inline_content_when_there_is_no_task_root():
    """A viewer trial restores in-session, before a draft becomes a task."""
    raw = _raw("<a@x>", "First")
    restored: list = []
    _roundcube(restored).seed(_ctx("roundcube"), {
        "mailboxes": [MAILBOX],
        "messages": [{"external_id": "captured-0001", "message_id": "<a@x>",
                      "mailbox": MAILBOX["email"], "content": raw}]})
    assert [r["raw"] for r in restored] == [raw]


def test_starttls_follows_what_the_driver_reported():
    """Derived from assume_running this would say plaintext, which is wrong."""
    seen = {}

    class Recorder(RoundcubeState):
        def _seeder(self, ctx):
            seeder = super()._seeder(ctx)
            seen["starttls"] = seeder.starttls
            seen["port"] = seeder.port
            seen["host"] = seeder.host
            seeder.factory = lambda host, port: _FakeIMAP(host, port, [])
            return seeder

    Recorder(app_manifest("roundcube")).capture(
        _ctx("roundcube", secrets={"imap_port": 2143, "imap_starttls": True}), None)
    assert seen == {"starttls": True, "port": 2143, "host": "agent.test"}


# ==== ONLYOFFICE ==========================================================

class _FakeConnector:
    def __init__(self, documents=()):
        self.registered: list = []
        self.cleared = 0
        self._documents = list(documents)
        self.saved: list[str] = []

    def register(self, block):
        self.registered.append(block)
        return tuple(str(row["document_id"]) for row in block["workbooks"])

    def documents(self):
        return list(self._documents)

    def clear(self):
        self.cleared += 1

    def capture_documents(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        rows = []
        for row in self._documents:
            payload = row["payload"]
            (directory / f"{row['document_id']}.xlsx").write_bytes(payload)
            rows.append({"document_id": row["document_id"],
                         "title": row.get("title"), "format": "xlsx",
                         "asset": f"{row['document_id']}.xlsx",
                         "sha256": hashlib.sha256(payload).hexdigest()})
        return rows


def _onlyoffice(connector):
    return OnlyOfficeState(app_manifest("onlyoffice"),
                           client_factory=lambda ctx: connector)


def test_prepare_registers_a_blank_workbook_to_edit():
    """A Docs surface with no document is a file manager, not an editor."""
    connector = _FakeConnector()
    state = _onlyoffice(connector)
    state.prepare(_ctx("onlyoffice"))
    assert connector.registered == [BLANK_WORKBOOK]
    assert state.document_ids == ("task-setup",)


def test_the_editor_url_points_at_the_registered_document():
    connector = _FakeConnector()
    state = _onlyoffice(connector)
    ctx = _ctx("onlyoffice", secrets={"connector_url": "http://agent.test:8085"})
    state.prepare(ctx)
    assert state.editor_url(ctx) == \
        "http://agent.test:8085/onlyoffice/editor/task-setup"


def test_the_connector_url_falls_back_to_the_manifests_port():
    state = _onlyoffice(_FakeConnector())
    assert state.connector_url(_ctx("onlyoffice")) == "http://agent.test:8086"


def test_capture_namespaces_workbook_assets(tmp_path):
    connector = _FakeConnector([
        {"document_id": "task-setup", "title": "Sheet", "payload": b"PK\x03\x04a"}])
    block = _onlyoffice(connector).capture(_ctx("onlyoffice"), tmp_path)
    assert block["workbooks"][0]["asset"] == "onlyoffice/task-setup.xlsx"
    assert (tmp_path / "onlyoffice" / "task-setup.xlsx").is_file()


def test_capture_with_no_live_editor_claims_nothing(tmp_path):
    assert _onlyoffice(_FakeConnector()).capture(_ctx("onlyoffice"), tmp_path) == {}


def test_seed_restores_a_captured_workbook_from_its_digest_checked_asset(tmp_path):
    payload = b"PK\x03\x04captured"
    (tmp_path / "onlyoffice").mkdir()
    (tmp_path / "onlyoffice" / "task-setup.xlsx").write_bytes(payload)
    connector = _FakeConnector()
    _onlyoffice(connector).seed(_ctx("onlyoffice", demo_root=tmp_path), {
        "workbooks": [{"document_id": "task-setup", "title": "Sheet",
                       "format": "xlsx", "asset": "onlyoffice/task-setup.xlsx",
                       "sha256": hashlib.sha256(payload).hexdigest()}]})
    assert connector.registered[0]["workbooks"][0]["content"] == payload


def test_seed_passes_a_declarative_workbook_through_untouched():
    """Authored tasks describe rows and columns rather than carrying bytes."""
    connector = _FakeConnector()
    source = {"tab": "Sheet1", "columns": ["SKU"], "rows": [["A-1"]]}
    _onlyoffice(connector).seed(_ctx("onlyoffice"), {"workbooks": [
        {"document_id": "plan", "filename": "Plan.xlsx", "format": "xlsx",
         "source": source}]})
    assert connector.registered[0]["workbooks"][0]["source"] == source


def test_seed_of_an_empty_block_registers_nothing():
    connector = _FakeConnector()
    _onlyoffice(connector).seed(_ctx("onlyoffice"), {"workbooks": []})
    assert connector.registered == []


def test_dump_reads_messages_without_marking_them_read():
    """Verified against dovecot:2.4.3: a plain RFC822 fetch sets \\Seen.

    Capture would then rewrite the very read state it is trying to record, and
    mark the operator's whole inbox as read on the way past.
    """
    requested: list[str] = []

    class Peeking(_FakeIMAP):
        def fetch(self, message_set, message_parts):
            requested.append(message_parts)
            return super().fetch(message_set, message_parts)

    box = [{"message_id": "<a@x>", "raw": _raw("<a@x>", "First"), "seen": False}]
    IMAPSeeder("imap.local", 1143, ssl=False,
               factory=lambda h, p: Peeking(h, p, box)).dump(
                   {**MAILBOX, "folder": "INBOX"})
    assert requested == ["(FLAGS BODY.PEEK[])"]
    assert all("RFC822" not in part for part in requested)


def test_an_authored_message_carries_the_id_the_seeder_searches_by():
    """Verified against dovecot 2.4.3: omitting it breaks reset and export.

    message_id() computes the search key, but only _message() writes headers.
    An author who does not repeat the Message-ID by hand gets mail that shows
    correctly in the mailbox, that reset can never find to delete, and that
    export always reports as missing.
    """
    box: list = []
    _roundcube(box).seed(_ctx("roundcube"), {
        "mailboxes": [MAILBOX],
        "messages": [{"external_id": "po-4471", "mailbox": MAILBOX["email"],
                      "headers": {"From": "Ops <ops@showAndTell.test>",
                                  "Subject": "PO 4471 needs approval"},
                      "body_text": "Waiting on your approval."}]})
    assert box[0]["message_id"] == "<po-4471@showAndTell.seed>"
    assert b"Message-ID: <po-4471@showAndTell.seed>" in box[0]["raw"]


def test_an_authored_message_may_still_declare_its_own_id():
    box: list = []
    _roundcube(box).seed(_ctx("roundcube"), {
        "mailboxes": [MAILBOX],
        "messages": [{"external_id": "po-4471", "mailbox": MAILBOX["email"],
                      "headers": {"Message-ID": "<real@upstream.test>",
                                  "Subject": "PO 4471"},
                      "body_text": "x"}]})
    assert box[0]["message_id"] == "<real@upstream.test>"
    assert box[0]["raw"].count(b"Message-ID:") == 1


# ==== Unified ERPNext + Frappe HR ========================================

def test_erpnext_prepare_seeds_erp_and_hr_baselines_without_task_data(
        monkeypatch):
    """A capture opens the recruiting surface before anything is seeded.

    The driver's baseline completes HRMS setup at the bench level; prepare
    must establish the setup-wizard masters (company baseline, Gender) an
    operator needs to author employees and requisitions by hand — and no
    task-owned records.
    """
    from contextlib import nullcontext

    from showAndTell.applications.erpnext import state as erpnext_state

    baselined = []
    baseline = object()
    monkeypatch.setattr(
        erpnext_state, "ensure_baseline",
        lambda client: (baselined.append(client), baseline)[1])
    erp_seeded = []
    monkeypatch.setattr(
        erpnext_state, "seed_erp_demo_dataset",
        lambda client, context: erp_seeded.append((client, context)))
    hr_seeded = []
    monkeypatch.setattr(
        erpnext_state, "seed_hr_demo_dataset",
        lambda client, context: hr_seeded.append((client, context)))
    ensured = []
    updated = []

    class Client:
        def ensure(self, doctype, filters, values):
            ensured.append((doctype, filters, values))

        def find_one(self, doctype, filters):
            assert doctype == "Installed Application"
            return erpnext_state.ResourceRef(doctype, filters["app_name"])

        def update(self, ref, values):
            updated.append((ref, values))

    client = Client()
    state = erpnext_state.State(
        app_manifest("erpnext"),
        client_factory=lambda ctx: nullcontext(client))
    state._hr_block = {"stale": "seed"}
    state.prepare(_ctx("erpnext"))

    assert baselined == [client]
    assert erp_seeded == [(client, baseline)]
    assert hr_seeded == [(client, baseline)]
    assert [(row[0], row[2]["gender"]) for row in ensured] == [
        ("Gender", "Unspecified"),
    ]
    assert updated == [
        (erpnext_state.ResourceRef("Installed Application", "frappe"),
         {"is_setup_complete": 1}),
        (erpnext_state.ResourceRef("Installed Application", "erpnext"),
         {"is_setup_complete": 1}),
        (erpnext_state.ResourceRef("Installed Application", "hrms"),
         {"is_setup_complete": 1}),
    ]
    assert state._hr_block is None
    assert state._last_result is None


def test_erpnext_routes_hrms_task_data_through_the_shared_site(monkeypatch):
    from contextlib import nullcontext

    from showAndTell.applications.erpnext import state as erpnext_state

    baseline = object()
    monkeypatch.setattr(erpnext_state, "ensure_baseline", lambda client: baseline)
    seeded = []
    monkeypatch.setattr(
        erpnext_state, "seed_erp_demo_dataset",
        lambda client, context: seeded.append(("erp", context)))
    monkeypatch.setattr(
        erpnext_state, "seed_hr_demo_dataset",
        lambda client, context: seeded.append(("hr", context)))
    task_blocks = []
    monkeypatch.setattr(
        erpnext_state, "seed_frappe_hr",
        lambda client, block: task_blocks.append(block))
    completed = []
    monkeypatch.setattr(
        erpnext_state, "_finish_setup_wizard",
        lambda client: completed.append(client))

    class Client:
        def __init__(self):
            self.updated = []

        def update(self, ref, values):
            self.updated.append((ref, values))

    client = Client()
    state = erpnext_state.State(
        app_manifest("erpnext"),
        client_factory=lambda ctx: nullcontext(client))
    hrms = {
        "company": {
            "name": "Vantage Global", "default_currency": "INR",
            "country": "India",
        },
        "job_requisitions": [],
    }

    state.seed(_ctx("erpnext"), {"hrms": hrms})

    assert seeded == [("erp", baseline), ("hr", baseline)]
    assert task_blocks == [hrms]
    assert completed == [client]
    assert state.export(_ctx("erpnext")) == {"hrms": hrms}
