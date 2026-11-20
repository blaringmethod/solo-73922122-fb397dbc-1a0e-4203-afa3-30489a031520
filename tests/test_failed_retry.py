"""A rejected bundle must release its idempotency slot so the corrected
payload can be retried with the same accession number."""
from __future__ import annotations

from app.database import SessionLocal
from app.models import IdempotencyRecord
from tests.conftest import make_bundle, patient_entry, report_entry, urn


def test_failed_bundle_releases_idempotency_key(client):
    p_urn = urn()
    p, _ = patient_entry(p_urn, value="RETRY-1")

    bad_bundle = make_bundle([
        p,
        report_entry(urn(), p_urn, ["Observation/does-not-exist"]),
    ])
    first = client.post(
        "/fhir", json=bad_bundle, headers={"Idempotency-Key": "ACC-RETRY"}
    )
    assert first.status_code == 422

    # Same accession key, corrected content: must be accepted, not 409.
    good_bundle = make_bundle([p])
    second = client.post(
        "/fhir", json=good_bundle, headers={"Idempotency-Key": "ACC-RETRY"}
    )
    assert second.status_code == 200, second.text
    assert second.json()["resourceMap"][0]["action"] == "created"


def test_conflict_with_different_content_preserves_completed_receipt(client):
    """A 409 must not delete the earlier completed receipt for the key."""
    p1, _ = patient_entry(urn(), value="PRESERVE-A")
    first = client.post(
        "/fhir", json=make_bundle([p1]), headers={"Idempotency-Key": "ACC-PRESERVE"}
    )
    assert first.status_code == 200

    p2, _ = patient_entry(urn(), value="PRESERVE-B")
    conflict = client.post(
        "/fhir", json=make_bundle([p2]), headers={"Idempotency-Key": "ACC-PRESERVE"}
    )
    assert conflict.status_code == 409

    # The completed ledger row and its receipt survive the conflicting request.
    with SessionLocal() as session:
        record = session.get(IdempotencyRecord, "ACC-PRESERVE")
        assert record is not None
        assert record.state == "completed"
    # An identical retry still replays the original receipt.
    replay = client.post(
        "/fhir", json=make_bundle([p1]), headers={"Idempotency-Key": "ACC-PRESERVE"}
    )
    assert replay.status_code == 200
    assert replay.json()["resourceMap"] == first.json()["resourceMap"]
