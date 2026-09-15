"""The lease store is what makes one warm stack safe to share."""
import pytest

from showAndTell.applications.host.leases import LeaseConflict, LeaseStore, UnknownLease


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_acquire_grants_exclusive_lease():
    store = LeaseStore(now=Clock())
    lease = store.acquire(["erpnext", "onlyoffice"], holder="david")
    assert lease.apps == frozenset({"erpnext", "onlyoffice"})
    assert store.covers(lease.lease_id, "erpnext")
    assert not store.covers(lease.lease_id, "mail")
    assert not store.covers(None, "erpnext")
    assert not store.covers("nonsense", "erpnext")


def test_overlapping_apps_conflict_and_disjoint_do_not():
    store = LeaseStore(now=Clock())
    store.acquire(["erpnext"], holder="david")
    with pytest.raises(LeaseConflict) as excinfo:
        store.acquire(["erpnext", "onlyoffice"], holder="sam")
    assert excinfo.value.held_by == "david"
    assert isinstance(excinfo.value.since, float)
    store.acquire(["mail"], holder="sam")  # disjoint apps coexist


def test_expiry_frees_the_stack_without_release():
    clock = Clock()
    store = LeaseStore(now=clock)
    lease = store.acquire(["erpnext"], holder="crashed", ttl_s=60.0)
    clock.t += 61.0
    assert not store.covers(lease.lease_id, "erpnext")
    assert store.holder_of("erpnext") is None
    store.acquire(["erpnext"], holder="next")  # no conflict


def test_heartbeat_extends_and_unknown_lease_raises():
    clock = Clock()
    store = LeaseStore(now=clock)
    lease = store.acquire(["erpnext"], holder="david", ttl_s=60.0)
    clock.t += 50.0
    renewed = store.heartbeat(lease.lease_id)
    assert renewed.expires_at == clock.t + 60.0
    clock.t += 61.0  # past renewed expiry
    with pytest.raises(UnknownLease):
        store.heartbeat(lease.lease_id)


def test_force_release_clears_overlapping_and_spares_disjoint():
    store = LeaseStore(now=Clock())
    stale = store.acquire(["erpnext", "onlyoffice"], holder="zombie")
    store.acquire(["mail"], holder="sam")
    removed = store.force_release(["erpnext"])
    assert [lease.holder for lease in removed] == ["zombie"]
    # The whole overlapping lease goes, not just the named app.
    assert store.holder_of("erpnext") is None
    assert store.holder_of("onlyoffice") is None
    assert store.holder_of("mail").holder == "sam"
    # The broken holder's keeper dies on its next heartbeat.
    with pytest.raises(UnknownLease):
        store.heartbeat(stale.lease_id)
    store.acquire(["erpnext"], holder="next")  # freed for the next capture
    assert store.force_release(["kiwix"]) == []  # free apps are a no-op


def test_release_is_idempotent_and_empty_apps_rejected():
    store = LeaseStore(now=Clock())
    lease = store.acquire(["erpnext"], holder="david")
    store.release(lease.lease_id)
    store.release(lease.lease_id)  # no error
    assert store.holder_of("erpnext") is None
    with pytest.raises(ValueError):
        store.acquire([], holder="david")


def test_active_leases_include_sanitized_execution_metadata():
    store = LeaseStore(now=Clock())
    lease = store.acquire(["erpnext"], holder="task-test04")
    updated = store.annotate(lease.lease_id, {
        "task": "test04", "kind": "run", "product": "Brackett",
        "worker": 3, "ignored": "not persisted",
    })

    assert updated.execution == {
        "task": "test04", "kind": "run", "product": "Brackett",
    }
    assert store.active()[0][0] == updated
    store.release(lease.lease_id)
    assert store.active() == []


FAMILIES = {"erpnext": ("erpnext", "erpnext_2", "erpnext_3")}


def test_family_assigns_free_instances_in_order():
    store = LeaseStore(now=Clock(), families=FAMILIES)
    first = store.acquire(["erpnext"], holder="a")
    second = store.acquire(["erpnext"], holder="b")
    assert first.assignment == {"erpnext": "erpnext"}
    assert second.assignment == {"erpnext": "erpnext_2"}
    assert store.covers(second.lease_id, "erpnext_2")
    assert not store.covers(second.lease_id, "erpnext")


def test_release_returns_an_instance_to_the_pool():
    store = LeaseStore(now=Clock(), families=FAMILIES)
    first = store.acquire(["erpnext"], holder="a")
    store.acquire(["erpnext"], holder="b")
    store.release(first.lease_id)
    again = store.acquire(["erpnext"], holder="c")
    assert again.assignment == {"erpnext": "erpnext"}


def test_exhausted_family_names_oldest_holder_and_occupancy():
    clock = Clock()
    store = LeaseStore(now=clock, families=FAMILIES)
    store.acquire(["erpnext"], holder="oldest")
    clock.t += 10
    store.acquire(["erpnext"], holder="mid")
    clock.t += 10
    store.acquire(["erpnext"], holder="newest")
    with pytest.raises(LeaseConflict) as excinfo:
        store.acquire(["erpnext"], holder="late")
    assert excinfo.value.held_by == "oldest (3/3 in use)"


def test_exact_instance_name_leases_exactly_that_instance():
    store = LeaseStore(now=Clock(), families=FAMILIES)
    surgical = store.acquire(["erpnext_2"], holder="ops")
    assert surgical.assignment == {"erpnext_2": "erpnext_2"}
    # The family skips the surgically held instance.
    assert store.acquire(["erpnext"], holder="a").assignment == \
        {"erpnext": "erpnext"}
    assert store.acquire(["erpnext"], holder="b").assignment == \
        {"erpnext": "erpnext_3"}


def test_one_request_never_grants_the_same_instance_twice():
    store = LeaseStore(now=Clock(), families=FAMILIES)
    lease = store.acquire(["erpnext_2", "erpnext"], holder="both")
    granted = set(lease.assignment.values())
    assert "erpnext_2" in granted and len(granted) == 2


def test_force_release_on_a_family_frees_only_the_oldest_slot():
    clock = Clock()
    store = LeaseStore(now=clock, families=FAMILIES)
    store.acquire(["erpnext"], holder="oldest")
    clock.t += 10
    store.acquire(["erpnext"], holder="newer")
    removed = store.force_release(["erpnext"])
    assert [lease.holder for lease in removed] == ["oldest"]
    assert store.holder_of("erpnext_2").holder == "newer"
    # The freed slot is usable again.
    assert store.acquire(["erpnext"], holder="next").assignment == \
        {"erpnext": "erpnext"}


def test_assignment_defaults_to_identity_without_families():
    store = LeaseStore(now=Clock())
    lease = store.acquire(["erpnext", "onlyoffice"], holder="d")
    assert lease.assignment == {"erpnext": "erpnext",
                                "onlyoffice": "onlyoffice"}
