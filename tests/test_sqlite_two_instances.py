"""Two API instances sharing a single SQLite file (Docker Compose profile).

Each instance gets its own engine/session factory over the same database file,
exactly like two containers mounting one volume. The contract under test:

* a bundle that passes ownership validation on stale state must never commit
  after another instance commits a conflicting bundle; the loser is rejected
  with a FHIR business error (422) or a retryable 503, never a silent merge;
* the final state must equal one of the two sequential submission orders;
* rejected bundles leave nothing behind (resources, patient identifiers,
  report links, versions) and the accession number can be reused;
* bundles that only update existing resources are validated under the write
  lock, so no other instance can slip an unrelated patient's bundle in
  between validation and commit.
"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy.orm import sessionmaker

from app.database import build_engine, init_db
from app.services import bundle as bundle_service
from tests.conftest import (
    make_bundle,
    observation_entry,
    patient_entry,
    report_entry,
    urn,
)


class Instance:
    """One API instance: independent engine/session factory, same file."""

    def __init__(self, url: str):
        self.engine = build_engine(url)
        self._write = (
            self.engine.execution_options(sqlite_write=True)
            if url.startswith("sqlite")
            else self.engine
        )
        self.Session = sessionmaker(
            bind=self._write, autoflush=False, expire_on_commit=False, future=True
        )

    def submit(self, key: str, bundle: dict):
        from app.errors import FhirError

        session = self.Session()
        try:
            return ("ok", bundle_service.process_bundle(session, bundle, key))
        except FhirError as exc:
            session.rollback()
            return ("fhir-error", exc)
        finally:
            session.close()


@pytest.fixture()
def instances(tmp_path):
    url = f"sqlite:///{tmp_path}/shared.db"
    eng = build_engine(url)
    init_db(bind=eng)
    inst = [Instance(url) for _ in range(2)]
    yield inst
    for i in inst:
        i.engine.dispose()


def _run_pair(a: Instance, b: Instance, fn_a, fn_b):
    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def wrap(fn, name):
        barrier.wait()
        results[name] = fn()

    ta = threading.Thread(target=wrap, args=(fn_a, "a"))
    tb = threading.Thread(target=wrap, args=(fn_b, "b"))
    ta.start()
    tb.start()
    ta.join(timeout=60)
    tb.join(timeout=60)
    assert not ta.is_alive() and not tb.is_alive(), "deadlock in concurrent submission"
    return results["a"], results["b"]


def _receipt_ids(receipt):
    return {m["resourceType"]: m["id"] for m in receipt["resourceMap"]}


def _seed(inst: Instance, key: str, entries: list[dict]) -> dict:
    status, receipt = inst.submit(key, make_bundle(entries))
    assert status == "ok", receipt
    return receipt


def test_two_instances_do_not_break_ownership_validation(instances):
    """A repoint of a report and a reassignment of the new observation race."""
    a, b = instances
    p1_urn, o1_urn, r1_urn = urn(), urn(), urn()
    p1, _ = patient_entry(p1_urn, value="RACE-A1")
    seed = _seed(a, "A-SEED", [p1, observation_entry(o1_urn, p1_urn),
                               report_entry(r1_urn, p1_urn, [o1_urn])])
    ids = _receipt_ids(seed)
    pid1, oid1, rid = ids["Patient"], ids["Observation"], ids["DiagnosticReport"]

    p2, _ = patient_entry(urn(), value="RACE-A2")
    pid2 = _receipt_ids(_seed(a, "A-SEED2", [p2]))["Patient"]

    o2urn = urn()
    oid2 = _receipt_ids(
        _seed(b, "A-SEED3", [observation_entry(o2urn, f"Patient/{pid1}")])
    )["Observation"]

    def repoint():
        return a.submit(
            "A-RACE-REPORT",
            make_bundle([{
                "resource": {
                    "resourceType": "DiagnosticReport", "id": rid, "status": "final",
                    "code": {"coding": [{"code": "R"}]},
                    "subject": {"reference": f"Patient/{pid1}"},
                    "result": [{"reference": f"Observation/{oid2}"}],
                },
                "request": {"method": "PUT", "url": f"DiagnosticReport/{rid}",
                            "ifMatch": 'W/"1"'},
            }]),
        )

    def move():
        return b.submit(
            "A-RACE-MOVE",
            make_bundle([{
                "resource": {
                    "resourceType": "Observation", "id": oid2, "status": "final",
                    "code": {"coding": [{"code": "GLU"}]},
                    "subject": {"reference": f"Patient/{pid2}"},
                    "valueQuantity": {"value": 9.0, "unit": "mmol/L"},
                },
                "request": {"method": "PUT", "url": f"Observation/{oid2}",
                            "ifMatch": 'W/"1"'},
            }]),
        )

    (rep_status, rep_val), (mov_status, mov_val) = _run_pair(a, b, repoint, move)
    assert rep_status in ("ok", "fhir-error")
    assert mov_status in ("ok", "fhir-error")
    if mov_status == "fhir-error":
        assert mov_val.status_code in (422, 503)

    reader = a.Session()
    try:
        from app.models import DiagnosticReport, Observation

        report = reader.get(DiagnosticReport, rid)
        obs2 = reader.get(Observation, oid2)
        # Ownership invariant: a report and every observation it links must be
        # in the same patient. Whatever interleaving happened, never violated.
        linked = {link.observation_id for link in report.links}
        for oid in linked:
            obs = reader.get(Observation, oid)
            assert obs.patient_id == report.patient_id, (
                f"report {rid} and observation {oid} ended in different patients"
            )
        assert obs2 is not None
    finally:
        reader.close()


def test_losing_bundle_leaves_no_rows_and_key_is_reusable(instances):
    """A rejected concurrent move must not leak resources/identifiers/versions."""
    a, b = instances
    p1_urn, o_urn, r_urn = urn(), urn(), urn()
    p1, _ = patient_entry(p1_urn, value="LEAK-RACE-P1")
    seed = _seed(a, "L1", [p1, observation_entry(o_urn, p1_urn),
                           report_entry(r_urn, p1_urn, [o_urn])])
    ids = _receipt_ids(seed)
    pid1, oid, rid = ids["Patient"], ids["Observation"], ids["DiagnosticReport"]
    p2, _ = patient_entry(urn(), value="LEAK-RACE-P2")
    pid2 = _receipt_ids(_seed(a, "L2", [p2]))["Patient"]

    def move():
        return b.submit(
            "L-RACE-MOVE",
            make_bundle([{
                "resource": {
                    "resourceType": "Observation", "id": oid, "status": "final",
                    "code": {"coding": [{"code": "GLU"}]},
                    "subject": {"reference": f"Patient/{pid2}"},
                },
                "request": {"method": "PUT", "url": f"Observation/{oid}",
                            "ifMatch": 'W/"1"'},
            }]),
        )

    def repoint():
        # Unrelated, valid bundle on instance A that creates a new patient and
        # a new observation: must commit freely.
        return a.submit(
            "L-RACE-OTHER",
            make_bundle([patient_entry(urn(), value="LEAK-RACE-P3")[0]]),
        )

    (mov_status, mov_val), (oth_status, _) = _run_pair(a, b, move, repoint)
    assert oth_status == "ok"
    assert mov_status in ("ok", "fhir-error")
    if mov_status == "fhir-error":
        assert mov_val.status_code in (422, 503)

    reader = a.Session()
    try:
        from app.models import (DiagnosticReport, Observation, Patient,
                                PatientIdentifier, ReportLink, ResourceVersion)

        obs = reader.get(Observation, oid)
        report = reader.get(DiagnosticReport, rid)
        # The move either committed (obs now with P2 + report still attached is
        # impossible because the report is not corrected) -> must have been
        # rejected and left untouched.
        assert obs.patient_id == pid1
        assert obs.version == 1
        assert report.patient_id == pid1
        assert {link.observation_id for link in report.links} == {oid}
        counts = {
            "obs": reader.query(Observation).count(),
            "reports": reader.query(DiagnosticReport).count(),
            "links": reader.query(ReportLink).count(),
            "versions_obs": reader.query(ResourceVersion).filter_by(
                resource_type="Observation", resource_id=oid).count(),
            "patients": reader.query(Patient).count(),
            "idents": reader.query(PatientIdentifier).count(),
        }
        assert counts["obs"] == 1
        assert counts["reports"] == 1
        assert counts["links"] == 1
        assert counts["versions_obs"] == 1
        assert counts["patients"] == 3
        assert counts["idents"] == 3
    finally:
        reader.close()

    # Same accession key can be reused for a corrected bundle.
    fixed = b.submit(
        "L-RACE-MOVE",
        make_bundle([{
            "resource": {
                "resourceType": "Observation", "id": oid, "status": "final",
                "code": {"coding": [{"code": "GLU"}]},
                "subject": {"reference": f"Patient/{pid1}"},
                "valueQuantity": {"value": 7.5, "unit": "mmol/L"},
            },
            "request": {"method": "PUT", "url": f"Observation/{oid}",
                        "ifMatch": 'W/"1"'},
        }]),
    )
    if mov_status == "fhir-error":
        assert fixed[0] == "ok", fixed
        assert fixed[1]["resourceMap"][0]["version"] == 2
    else:  # move committed; corrected retry under the same key replays
        assert fixed[0] == "ok"


def test_update_only_bundle_is_validated_under_the_write_lock(instances):
    """Instance B commits an unrelated patient bundle while A validates an
    update-only bundle. A must see a consistent snapshot (all-or-nothing).

    The key property: B's bundle creates identifiers/resources; A's bundle
    only references existing resources, so A cannot rely on writing anything
    to acquire the lock. Validation must happen under the same lock as commit.
    """
    a, b = instances
    p1_urn, o_urn = urn(), urn()
    p1, _ = patient_entry(p1_urn, value="LOCK-A")
    seed = _seed(a, "LOCK-SEED", [p1, observation_entry(o_urn, p1_urn)])
    ids = _receipt_ids(seed)
    pid1, oid = ids["Patient"], ids["Observation"]

    def update_only():
        return a.submit(
            "LOCK-UPD",
            make_bundle([{
                "resource": {
                    "resourceType": "Observation", "id": oid, "status": "final",
                    "code": {"coding": [{"code": "GLU"}]},
                    "subject": {"reference": f"Patient/{pid1}"},
                    "valueQuantity": {"value": 6.2, "unit": "mmol/L"},
                },
                "request": {"method": "PUT", "url": f"Observation/{oid}",
                            "ifMatch": 'W/"1"'},
            }]),
        )

    def unrelated_patient():
        return b.submit(
            "LOCK-OTHER",
            make_bundle([patient_entry(urn(), value="LOCK-B")[0]]),
        )

    (upd_status, upd_val), (oth_status, _) = _run_pair(
        a, b, update_only, unrelated_patient
    )
    assert upd_status == "ok", upd_val
    assert oth_status == "ok"
    assert upd_val["resourceMap"][0]["action"] == "updated"
    assert upd_val["resourceMap"][0]["version"] == 2

    reader = a.Session()
    try:
        from app.models import Observation, PatientIdentifier

        obs = reader.get(Observation, oid)
        assert obs.version == 2
        assert obs.resource["valueQuantity"]["value"] == 6.2
        assert reader.query(PatientIdentifier).filter_by(value="LOCK-B").count() == 1
    finally:
        reader.close()


def test_rejected_bundle_rolls_back_every_table(instances):
    """Misaligned report/observation: no patient, identifier, report link or
    version may survive, and the same accession works after correction."""
    a, _ = instances
    p1_urn, p2_urn, o_urn = urn(), urn(), urn()
    p1, (s1, v1) = patient_entry(p1_urn, value="RB-P1")
    p2, (s2, v2) = patient_entry(p2_urn, value="RB-P2")
    obs = observation_entry(o_urn, p1_urn)
    # Report claims patient 2 but its observation belongs to patient 1.
    bad = report_entry(urn(), p2_urn, [o_urn])

    key = "RB-KEY"
    status, err = a.submit(key, make_bundle([p1, p2, obs, bad]))
    assert status == "fhir-error" and err.status_code == 422

    reader = a.Session()
    try:
        from app.models import (DiagnosticReport, IdempotencyRecord, Observation,
                                Patient, PatientIdentifier, ReportLink,
                                ResourceVersion)

        assert reader.query(Patient).count() == 0
        assert reader.query(PatientIdentifier).count() == 0
        assert reader.query(Observation).count() == 0
        assert reader.query(DiagnosticReport).count() == 0
        assert reader.query(ReportLink).count() == 0
        assert reader.query(ResourceVersion).count() == 0
        # Idempotency slot released.
        assert reader.get(IdempotencyRecord, key) is None
    finally:
        reader.close()

    # Corrected bundle (observation and report both with patient 2) retried
    # with the same accession number is accepted.
    fixed_obs = observation_entry(o_urn, p2_urn)
    fixed_obs["fullUrl"] = o_urn
    fixed_report = report_entry(urn(), p2_urn, [o_urn])
    status, receipt = a.submit(key, make_bundle([p1, p2, fixed_obs, fixed_report]))
    assert status == "ok", receipt
    assert len(receipt["resourceMap"]) == 4


def test_busy_error_is_retried_without_double_apply(instances, monkeypatch):
    """A transient SQLite 'database is locked' mid-bundle is retried from a
    clean state; the bundle is applied exactly once and the accession works."""
    from sqlalchemy.exc import OperationalError

    a, _ = instances
    original = bundle_service._apply_bundle
    calls = {"n": 0}

    def flaky(session, entries, key):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("BEGIN IMMEDIATE", {}, Exception("database is locked"))
        return original(session, entries, key)

    monkeypatch.setattr(bundle_service, "_apply_bundle", flaky)
    p1_urn, o_urn = urn(), urn()
    p1, _ = patient_entry(p1_urn, value="BUSY-A")
    status, receipt = a.submit(
        "BUSY-1", make_bundle([p1, observation_entry(o_urn, p1_urn)])
    )
    monkeypatch.setattr(bundle_service, "_apply_bundle", original)

    assert status == "ok", receipt
    assert calls["n"] == 2
    reader = a.Session()
    try:
        from app.models import Observation, Patient, PatientIdentifier

        assert reader.query(Patient).count() == 1
        assert reader.query(PatientIdentifier).count() == 1
        assert reader.query(Observation).count() == 1
    finally:
        reader.close()


def test_exhausted_busy_retries_returns_503_and_releases_key(instances, monkeypatch):
    from sqlalchemy.exc import OperationalError

    a, _ = instances
    original = bundle_service._apply_bundle

    def always_busy(session, entries, key):
        raise OperationalError("BEGIN IMMEDIATE", {}, Exception("database is locked"))

    monkeypatch.setattr(bundle_service, "_apply_bundle", always_busy)
    p, _ = patient_entry(urn(), value="BUSY-EXHAUST")
    status, err = a.submit("BUSY-EXHAUST", make_bundle([p]))
    monkeypatch.setattr(bundle_service, "_apply_bundle", original)

    assert status == "fhir-error"
    assert err.status_code == 503
    # The pending slot was released: a corrected/retried submission works.
    status2, receipt = a.submit("BUSY-EXHAUST", make_bundle([p]))
    assert status2 == "ok", receipt
    assert receipt["resourceMap"][0]["action"] == "created"


def test_validation_and_commit_share_one_write_lock(instances, monkeypatch):
    """Deterministic interleaving: pause instance A *after* its ownership
    validation but before its writes; instance B tries to commit an unrelated
    patient bundle. B must block until A commits/rolls back, proving A's
    validate-then-write window is not exposed to other instances.
    """
    import threading
    import time

    a, b = instances
    p1_urn, o_urn = urn(), urn()
    p1, _ = patient_entry(p1_urn, value="DET-A")
    seed = _seed(a, "DET-SEED", [p1, observation_entry(o_urn, p1_urn)])
    ids = _receipt_ids(seed)
    pid, oid = ids["Patient"], ids["Observation"]

    original = bundle_service._apply_observations
    entered = threading.Event()
    release = threading.Event()

    def paused_apply(*args, **kwargs):
        # Now inside the write transaction, ownership validation already done.
        entered.set()
        release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(bundle_service, "_apply_observations", paused_apply)

    body = {
        "resource": {
            "resourceType": "Observation", "id": oid, "status": "final",
            "code": {"coding": [{"code": "GLU"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "valueQuantity": {"value": 6.2, "unit": "mmol/L"},
        },
        "request": {"method": "PUT", "url": f"Observation/{oid}", "ifMatch": 'W/"1"'},
    }

    updater_result: dict = {}

    def run_updater():
        updater_result["value"] = a.submit("DET-UPD", make_bundle([body]))

    other_result: dict = {}
    other_done = threading.Event()

    def run_other():
        other_result["value"] = b.submit(
            "DET-OTHER", make_bundle([patient_entry(urn(), value="DET-B")[0]])
        )
        other_done.set()

    t1 = threading.Thread(target=run_updater)
    t1.start()
    assert entered.wait(10), "update never reached the write phase"

    t2 = threading.Thread(target=run_other)
    t2.start()
    # While A is between validation and commit, B must not be able to commit.
    time.sleep(0.3)
    blocked = not other_done.is_set()

    release.set()
    t1.join(20)
    t2.join(20)
    monkeypatch.setattr(bundle_service, "_apply_observations", original)

    assert blocked, "another instance committed between validation and commit"
    assert updater_result["value"][0] == "ok", updater_result
    assert other_result["value"][0] == "ok", other_result
    # Serialized outcome: both applied, no lost update.
    assert updater_result["value"][1]["resourceMap"][0]["version"] == 2
    reader = a.Session()
    try:
        from app.models import Observation, PatientIdentifier

        assert reader.get(Observation, oid).version == 2
        assert reader.query(PatientIdentifier).filter_by(value="DET-B").count() == 1
    finally:
        reader.close()
