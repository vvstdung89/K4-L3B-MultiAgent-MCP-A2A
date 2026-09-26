from __future__ import annotations

from student_agent.analysis import (
    analyse_payment,
    analyse_shipment,
    classify_issue,
    order_conflicts,
    resolve_entity,
    scoped_items,
    select_incident,
)

OID = "order-under-test"


def _row(purchase: str, carrier: str, delivered: str | None, estimated: str, status="delivered"):
    return {
        "order_id": OID,
        "order_status": status,
        "order_purchase_timestamp": f"{purchase}T09:00:00-03:00",
        "order_approved_at": f"{purchase}T10:00:00-03:00",
        "order_delivered_carrier_date": f"{carrier}T09:00:00-03:00",
        "order_delivered_customer_date": f"{delivered}T09:00:00-03:00" if delivered else None,
        "order_estimated_delivery_date": f"{estimated}T09:00:00-03:00",
    }


def _item(limit: str, freight="10.00", price="79.00"):
    return {
        "order_id": OID,
        "order_item_id": "item-1",
        "seller_id": "seller-1",
        "shipping_limit_date": f"{limit}T09:00:00-03:00",
        "price": price,
        "freight_value": freight,
    }


def _capture(day: str, amount: str, event_type="captured", status="confirmed"):
    return {
        "event_at": f"{day}T10:00:00-03:00",
        "event_type": event_type,
        "amount_brl": amount,
        "status": status,
    }


# The decoy incarnation is purchased after the case was opened and is on time.
DECOY = _row("2018-05-11", "2018-05-13", "2018-05-20", "2018-05-21")
CASE = {
    "case_id": "T_CASE_001",
    "opened_at": "2018-01-01T09:00:00-03:00",
    "customer_request": {"claimed_order_id": OID, "claims": []},
    "candidate_order_ids": [OID, "candidate-x"],
}


def _history(*rows):
    return {"customer_unique_id": "customer-1", "orders": list(rows)}


def test_entity_resolution_rejects_candidate_outside_customer_history() -> None:
    decision = resolve_entity(CASE, _history(DECOY))
    assert decision.status == "resolved"
    assert decision.resolved_order_ids == [OID]
    assert decision.rejected_candidates == ["candidate-x"]


def test_entity_not_found_without_history() -> None:
    assert resolve_entity(CASE, None).status == "not_found"


def test_incident_is_latest_purchase_before_opened_at() -> None:
    real = _row("2017-12-20", "2017-12-22", "2018-01-04", "2017-12-30")
    scope = select_incident(OID, _history(DECOY, real), DECOY, CASE["opened_at"])
    assert scope is not None
    assert scope.order_row is real
    assert "order_purchase_timestamp" in order_conflicts(scope, DECOY)


def test_logistics_delay_when_seller_handed_over_in_time() -> None:
    real = _row("2017-12-20", "2017-12-22", "2018-01-04", "2017-12-30")
    scope = select_incident(OID, _history(DECOY, real), DECOY, CASE["opened_at"])
    items = scoped_items([_item("2018-05-14"), _item("2017-12-23", "18.00")], scope)
    assert [i["freight_value"] for i in items] == ["18.00"]
    shipment = analyse_shipment(scope, items, {"events": []})
    assert shipment.verdict == "logistics_delay"
    payment = analyse_payment(scope, items, {"payments": [], "events": []}, None)
    assert classify_issue(scope, shipment, payment) == "late_delivery_logistics"


def test_seller_delay_when_carrier_handoff_after_limit() -> None:
    real = _row("2017-12-29", "2018-01-05", "2018-01-12", "2018-01-08")
    case = {**CASE, "opened_at": "2018-01-10T09:00:00-03:00"}
    scope = select_incident(OID, _history(real), None, case["opened_at"])
    shipment = analyse_shipment(scope, [_item("2018-01-01", "18.00")], None)
    assert shipment.verdict == "seller_delay"
    assert shipment.late_seller_ids == ["seller-1"]


def test_payment_verdicts() -> None:
    real = _row("2017-12-20", "2017-12-22", "2017-12-28", "2017-12-30")
    scope = select_incident(OID, _history(real), None, CASE["opened_at"])
    items = [_item("2017-12-23")]

    split = analyse_payment(
        scope,
        items,
        {
            "payments": [
                {
                    "payment_sequential": "1",
                    "payment_type": "credit_card",
                    "payment_value": "44.50",
                },
                {"payment_sequential": "2", "payment_type": "voucher", "payment_value": "44.50"},
            ],
            "events": [_capture("2017-12-20", "44.50"), _capture("2017-12-20", "44.50")],
        },
        None,
    )
    assert split.verdict == "reconciled" and split.split
    assert classify_issue(scope, analyse_shipment(scope, items, None), split) == (
        "valid_split_payment"
    )

    # Split legs may share payment_sequential; they are distinct rows, not a replay.
    same_sequence = analyse_payment(
        scope,
        items,
        {
            "payments": [
                {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "44.50"},
                {"payment_sequential": "1", "payment_type": "voucher", "payment_value": "44.50"},
            ],
            "events": [_capture("2017-12-20", "44.50"), _capture("2017-12-20", "44.50")],
        },
        None,
    )
    assert same_sequence.captured_total == 89.0 and same_sequence.split

    # Another incarnation sharing the window (same timestamps): its 52 capture and its
    # failed 52 refund are not part of the split incident.
    collided = analyse_payment(
        scope,
        items,
        {
            "payments": [
                {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "52.00"},
                {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "44.50"},
                {"payment_sequential": "2", "payment_type": "voucher", "payment_value": "44.50"},
            ],
            "events": [
                _capture("2017-12-20", "52.00"),
                _capture("2017-12-20", "44.50"),
                _capture("2017-12-20", "44.50"),
            ],
        },
        {"events": [{"event_at": "2017-12-21T09:00:00-03:00", "amount_brl": "52.00", "status": "failed"}]},
    )
    assert collided.captured_total == 89.0 and collided.split
    assert collided.anomalies == [] and collided.refund_events_in_scope == 0

    # A full-amount duplicate charge repeats the incident's own amount: never attributed away.
    full_duplicate = analyse_payment(
        scope,
        items,
        {
            "payments": [
                {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "89.00"},
                {"payment_sequential": "2", "payment_type": "credit_card", "payment_value": "89.00"},
            ],
            "events": [_capture("2017-12-20", "89.00"), _capture("2017-12-20", "89.00")],
        },
        None,
    )
    assert full_duplicate.captured_total == 178.0

    duplicate = analyse_payment(
        scope,
        items,
        {"payments": [], "events": [_capture("2017-12-20", "64.00")] * 2},
        None,
    )
    assert duplicate.verdict == "duplicate_capture" and duplicate.duplicate_amount == 64.0

    mismatch = analyse_payment(
        scope,
        items,
        {
            "payments": [],
            "events": [
                _capture("2017-12-20", "35.00"),
                _capture("2017-12-20", "35.00", "reconciliation_mismatch", "open"),
            ],
        },
        None,
    )
    assert mismatch.verdict == "capture_mismatch"

    failed = analyse_payment(
        scope,
        items,
        {"payments": [], "events": [_capture("2017-12-20", "52.00")]},
        {"events": [_capture("2017-12-31", "52.00", "refund_requested", "failed")]},
    )
    assert failed.verdict == "refund_failed"


def test_refund_events_outside_incident_window_are_ignored() -> None:
    real = _row("2017-12-20", "2017-12-22", "2017-12-28", "2017-12-30")
    scope = select_incident(OID, _history(DECOY, real), DECOY, CASE["opened_at"])
    payment = analyse_payment(
        scope,
        [_item("2017-12-23")],
        {"payments": [], "events": [_capture("2017-12-20", "89.00")]},
        {"events": [_capture("2018-05-25", "89.00", "refund_requested", "pending")]},
    )
    assert payment.verdict == "reconciled"


def test_canceled_order_paid() -> None:
    real = _row("2017-12-20", "2017-12-22", None, "2017-12-30", status="canceled")
    scope = select_incident(OID, _history(real), None, CASE["opened_at"])
    payment = analyse_payment(
        scope,
        [_item("2017-12-23")],
        {"payments": [], "events": [_capture("2017-12-20", "79.00")]},
        None,
    )
    assert classify_issue(scope, analyse_shipment(scope, [], None), payment) == (
        "canceled_order_paid"
    )


def test_replayed_capture_without_payment_row_is_not_a_second_charge() -> None:
    real = _row("2017-12-20", "2017-12-22", None, "2017-12-30", status="unavailable")
    scope = select_incident(OID, _history(real), None, CASE["opened_at"])
    row = {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "89.00"}
    payment = analyse_payment(
        scope,
        [_item("2017-12-23")],
        {"payments": [row], "events": [_capture("2017-12-20", "89.00")] * 2},
        None,
    )
    assert payment.captured_total == 89.0 and "duplicate_capture" not in payment.anomalies
    assert classify_issue(scope, analyse_shipment(scope, [], None), payment) == (
        "unavailable_order_paid"
    )

    # A real duplicate charge carries its own payment row and is still detected.
    duplicate = analyse_payment(
        scope,
        [_item("2017-12-23")],
        {
            "payments": [row, {**row, "payment_sequential": "2"}],
            "events": [_capture("2017-12-20", "89.00")] * 2,
        },
        None,
    )
    assert duplicate.captured_total == 178.0 and "duplicate_capture" in duplicate.anomalies

    # The source may replay the payment row itself (same payment_sequential): still one charge.
    replayed_row = analyse_payment(
        scope,
        [_item("2017-12-23")],
        {"payments": [row, dict(row)], "events": [_capture("2017-12-20", "89.00")] * 2},
        None,
    )
    assert replayed_row.captured_total == 89.0
    assert "duplicate_capture" not in replayed_row.anomalies
