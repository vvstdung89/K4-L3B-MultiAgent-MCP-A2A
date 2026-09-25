"""Deterministic, side-effect free analysis used by the specialist agents.

Every function works on MCP ``data`` payloads that were already fetched for one case.
Nothing here calls MCP or writes trace events, so the rules can be unit-tested offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import combinations
from typing import Any

ORDER_FIELDS = (
    "order_status",
    "order_purchase_timestamp",
    "order_delivered_carrier_date",
    "order_delivered_customer_date",
    "order_estimated_delivery_date",
)
SHIPPING_LIMIT_OFFSET = timedelta(days=3)
MONEY_TOLERANCE = 0.01


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def money(value: Any) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def same_amount(left: float, right: float) -> bool:
    return abs(left - right) <= MONEY_TOLERANCE


@dataclass(frozen=True)
class Window:
    """Time scope of the order incarnation that the complaint is about."""

    start: datetime
    end: datetime | None

    def contains(self, value: Any) -> bool:
        moment = parse_ts(value)
        if moment is None:
            return False
        return moment >= self.start and (self.end is None or moment < self.end)


# ---------------------------------------------------------------------------
# Entity resolution
# ---------------------------------------------------------------------------


@dataclass
class EntityDecision:
    status: str
    resolved_order_ids: list[str]
    rejected_candidates: list[str]
    confidence: float
    customer_unique_id: str | None
    related_order_ids: list[str]


def resolve_entity(case: dict[str, Any], history: dict[str, Any] | None) -> EntityDecision:
    """Rank candidates against the scoped customer history; never invent an order."""
    candidates = [c for c in case.get("candidate_order_ids", []) if isinstance(c, str)]
    claimed = case.get("customer_request", {}).get("claimed_order_id")
    history_ids: list[str] = []
    customer_unique_id = None
    if isinstance(history, dict):
        customer_unique_id = history.get("customer_unique_id")
        for row in history.get("orders", []) or []:
            order_id = row.get("order_id")
            if isinstance(order_id, str) and order_id not in history_ids:
                history_ids.append(order_id)
    if claimed and claimed not in candidates:
        candidates.insert(0, claimed)

    matched = [c for c in candidates if c in history_ids]
    rejected = [c for c in candidates if c not in matched]
    if len(matched) == 1:
        confidence = 0.95 if matched[0] == claimed else 0.85
        return EntityDecision(
            "resolved", matched, rejected, confidence, customer_unique_id, history_ids
        )
    if len(matched) > 1:
        # Prefer the explicitly claimed order when it is one of several customer orders.
        if claimed in matched:
            others = [c for c in matched if c != claimed]
            return EntityDecision(
                "resolved", [claimed], others + rejected, 0.7, customer_unique_id, history_ids
            )
        return EntityDecision("ambiguous", matched, rejected, 0.4, customer_unique_id, history_ids)
    return EntityDecision("not_found", [], candidates, 0.3, customer_unique_id, history_ids)


# ---------------------------------------------------------------------------
# Incident scoping: several conflicting rows can exist for the same order id.
# ---------------------------------------------------------------------------


@dataclass
class IncidentScope:
    order_row: dict[str, Any]
    window: Window
    row_count: int
    ambiguous: bool
    matches_order_row: bool = False


def _row_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(f) for f in (*ORDER_FIELDS, "order_approved_at"))


def candidate_scopes(
    order_id: str,
    history: dict[str, Any] | None,
    order_row: dict[str, Any] | None,
    opened_at: str,
) -> list[IncidentScope]:
    """All distinct incarnations of one order id, most plausible incident first.

    The order history can hold several rows for the same order id. The row echoed by
    ``get_order`` is the current record; a history row that disagrees with it is the
    incarnation the complaint refers to. Among the rest, rows purchased before the case
    was opened rank before rows purchased after it.
    """
    rows: list[dict[str, Any]] = []
    if isinstance(history, dict):
        rows = [r for r in history.get("orders", []) or [] if r.get("order_id") == order_id]
    order_key = _row_key(order_row) if isinstance(order_row, dict) else None
    if isinstance(order_row, dict) and order_row.get("order_id") == order_id:
        rows.append(order_row)  # de-duplicated by key below
    distinct: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        distinct.setdefault(_row_key(row), row)
    dated = [(parse_ts(r.get("order_purchase_timestamp")), r) for r in distinct.values()]
    dated = [(ts, r) for ts, r in dated if ts is not None]
    if not dated:
        return []
    opened = parse_ts(opened_at)
    starts = sorted({ts for ts, _ in dated})
    ranked: list[tuple[tuple[bool, bool, float], IncidentScope]] = []
    for ts, row in dated:
        later = [s for s in starts if s > ts]
        before = opened is None or ts <= opened
        scope = IncidentScope(
            row,
            Window(ts, later[0] if later else None),
            len(rows),
            ambiguous=len(dated) > 1,
            matches_order_row=order_key is not None and _row_key(row) == order_key,
        )
        rank = (scope.matches_order_row, not before, -ts.timestamp() if before else ts.timestamp())
        ranked.append((rank, scope))
    ranked.sort(key=lambda pair: pair[0])
    scopes = [scope for _, scope in ranked]
    return scopes


def select_incident(
    order_id: str,
    history: dict[str, Any] | None,
    order_row: dict[str, Any] | None,
    opened_at: str,
) -> IncidentScope | None:
    scopes = candidate_scopes(order_id, history, order_row, opened_at)
    return scopes[0] if scopes else None


def order_conflicts(incident: IncidentScope, order_row: dict[str, Any] | None) -> list[str]:
    """Fields where the get_order row disagrees with the incident-scoped history row."""
    if not isinstance(order_row, dict):
        return []
    return [f for f in ORDER_FIELDS if order_row.get(f) != incident.order_row.get(f)]


def scoped_items(
    items: list[dict[str, Any]] | None, incident: IncidentScope
) -> list[dict[str, Any]]:
    """Item rows whose shipping limit belongs to the incident incarnation."""
    unique: dict[tuple[tuple[str, Any], ...], dict[str, Any]] = {}
    for row in items or []:
        if isinstance(row, dict):
            unique.setdefault(tuple(sorted(row.items())), row)
    rows = list(unique.values())
    if len(rows) <= 1:
        return rows
    expected = incident.window.start + SHIPPING_LIMIT_OFFSET
    in_window = [r for r in rows if incident.window.contains(r.get("shipping_limit_date"))]
    if in_window:
        return in_window

    def distance(row: dict[str, Any]) -> float:
        moment = parse_ts(row.get("shipping_limit_date"))
        return abs((moment - expected).total_seconds()) if moment else float("inf")

    return [min(rows, key=distance)]


# ---------------------------------------------------------------------------
# Shipment analysis
# ---------------------------------------------------------------------------


@dataclass
class ShipmentFinding:
    verdict: str
    late_seller_ids: list[str]
    timeline_complete: bool
    event_actor: str | None = None
    events_in_scope: int = 0  # carrier events inside the incident window


def _analyse_shipment(
    incident: IncidentScope,
    items: list[dict[str, Any]],
    shipment: dict[str, Any] | None,
) -> ShipmentFinding:
    row = incident.order_row
    status = row.get("order_status")
    carrier = parse_ts(row.get("order_delivered_carrier_date"))
    delivered = parse_ts(row.get("order_delivered_customer_date"))
    estimated = parse_ts(row.get("order_estimated_delivery_date"))
    timeline_complete = all(parse_ts(row.get(f)) for f in ORDER_FIELDS[1:]) and bool(
        row.get("order_approved_at")
    )
    events = [
        e
        for e in (shipment or {}).get("events", []) or []
        if incident.window.contains(e.get("event_at"))
    ]
    event_actor = next(
        (e.get("actor") for e in events if e.get("event_type") == "delivered_late"), None
    )

    if status in {"canceled", "unavailable"}:
        return ShipmentFinding("insufficient_evidence", [], False, event_actor)
    if status != "delivered" or delivered is None or estimated is None:
        return ShipmentFinding("insufficient_evidence", [], timeline_complete, event_actor)

    late_sellers: list[str] = []
    for item in items:
        limit = parse_ts(item.get("shipping_limit_date"))
        seller = item.get("seller_id")
        if carrier and limit and carrier > limit and seller and seller not in late_sellers:
            late_sellers.append(seller)
    late = delivered > estimated
    if late_sellers:
        verdict = "seller_delay"
    elif late:
        verdict = "logistics_delay"
    else:
        verdict = "on_time"
    # A confirmed carrier event that contradicts the computed verdict is a real source conflict.
    if event_actor == "seller" and verdict == "logistics_delay":
        verdict = "conflicting"
    if event_actor == "logistics_provider" and verdict == "seller_delay":
        verdict = "conflicting"
    return ShipmentFinding(verdict, late_sellers, timeline_complete, event_actor)


def analyse_shipment(
    incident: IncidentScope,
    items: list[dict[str, Any]],
    shipment: dict[str, Any] | None,
) -> ShipmentFinding:
    finding = _analyse_shipment(incident, items, shipment)
    finding.events_in_scope = sum(
        1
        for e in (shipment or {}).get("events", []) or []
        if incident.window.contains(e.get("event_at"))
    )
    return finding


# ---------------------------------------------------------------------------
# Payment / refund analysis
# ---------------------------------------------------------------------------


@dataclass
class PaymentFinding:
    verdict: str
    captured_total: float | None
    refunded_total: float | None
    captures: list[dict[str, Any]] = field(default_factory=list)
    payment_rows: list[dict[str, Any]] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)  # payment verdicts, precedence order
    mismatch_amount: float = 0.0
    duplicate_amount: float = 0.0
    refund_amount: float = 0.0
    split_rows: list[dict[str, Any]] = field(default_factory=list)
    refund_events_in_scope: int = 0

    @property
    def split(self) -> bool:
        return bool(self.split_rows)


def _match_rows(captures: list[dict[str, Any]], payments: list[dict[str, Any]]):
    """Map capture events back onto payment rows by amount (rows carry sequence/type)."""
    pool = list(payments)
    rows: list[dict[str, Any]] = []
    for capture in captures:
        for index, row in enumerate(pool):
            if same_amount(money(row.get("payment_value")), money(capture.get("amount_brl"))):
                rows.append(pool.pop(index))
                break
    return rows


def _drop_replayed_captures(
    captures: list[dict[str, Any]], payments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Drop captures with no payment row left that repeat the amount of a backed capture.

    Unbacked captures of a different amount (e.g. a reconciliation mismatch) are kept."""
    pool = list(payments)
    backed_amounts: list[float] = []
    kept: list[dict[str, Any]] = []
    for capture in captures:
        amount = money(capture.get("amount_brl"))
        for index, row in enumerate(pool):
            if same_amount(money(row.get("payment_value")), amount):
                pool.pop(index)
                backed_amounts.append(amount)
                kept.append(capture)
                break
        else:
            if not any(same_amount(amount, a) for a in backed_amounts):
                kept.append(capture)
    return kept


def _split_subset(rows: list[dict[str, Any]], expected: float) -> list[dict[str, Any]]:
    """Smallest set of >=2 payment rows with distinct methods that settles the order total."""
    if expected <= 0:
        return []
    for size in range(2, len(rows) + 1):
        for combo in combinations(rows, size):
            methods = {r.get("payment_type") for r in combo}
            total = sum(money(r.get("payment_value")) for r in combo)
            if len(methods) >= 2 and same_amount(total, expected):
                return list(combo)
    return []


def analyse_payment(
    incident: IncidentScope,
    items: list[dict[str, Any]],
    payment_timeline: dict[str, Any] | None,
    refund_timeline: dict[str, Any] | None,
) -> PaymentFinding:
    if not isinstance(payment_timeline, dict):
        return PaymentFinding("insufficient_evidence", None, None)
    events = [
        e
        for e in payment_timeline.get("events", []) or []
        if incident.window.contains(e.get("event_at"))
    ]
    captures = [
        e for e in events if e.get("event_type") == "captured" and e.get("status") == "confirmed"
    ]
    mismatches = [e for e in events if e.get("event_type") == "reconciliation_mismatch"]
    refunds = [
        e
        for e in (refund_timeline or {}).get("events", []) or []
        if incident.window.contains(e.get("event_at"))
    ]
    payments = list(payment_timeline.get("payments", []) or [])
    rows = _match_rows(captures, payments)
    if payments and len(rows) < len(captures):
        # A capture with no payment row behind it is a replayed timeline record, not a
        # second charge: a real duplicate charge carries its own payment row.
        captures = _drop_replayed_captures(captures, payments)
    captured_total = round(sum(money(e.get("amount_brl")) for e in captures), 2)
    refunded_total = round(
        sum(
            money(e.get("amount_brl"))
            for e in refunds
            if e.get("status") in {"completed", "succeeded", "confirmed", "refunded"}
        ),
        2,
    )
    expected = round(sum(money(i.get("price")) + money(i.get("freight_value")) for i in items), 2)

    finding = PaymentFinding("reconciled", captured_total, refunded_total, captures, rows)
    finding.refund_events_in_scope = len(refunds)
    failed = [e for e in refunds if e.get("status") == "failed"]
    pending = [e for e in refunds if e.get("status") in {"pending", "open", "processing"}]
    if failed:
        finding.anomalies.append("refund_failed")
        finding.refund_amount = money(failed[-1].get("amount_brl"))
    if pending:
        finding.anomalies.append("refund_pending")
        finding.refund_amount = finding.refund_amount or money(pending[-1].get("amount_brl"))
    # Duplicate: the same amount captured twice where the pair does not settle the order.
    amounts = [money(e.get("amount_brl")) for e in captures]
    repeated = sorted({a for a in amounts if amounts.count(a) > 1}, reverse=True)
    for amount in repeated:
        if not (expected and same_amount(2 * amount, expected)):
            finding.anomalies.append("duplicate_capture")
            finding.duplicate_amount = amount
            break
    if mismatches:
        finding.anomalies.append("capture_mismatch")
        finding.mismatch_amount = money(mismatches[-1].get("amount_brl"))
    finding.split_rows = _split_subset(rows, expected)

    if finding.anomalies:
        finding.verdict = finding.anomalies[0]
    elif refunded_total > 0:
        finding.verdict = "refunded"
    elif not captures:
        finding.verdict = "insufficient_evidence"
    return finding


def focus_payment(finding: PaymentFinding, issue: str) -> PaymentFinding:
    """Restrict the payment view to the anomaly behind the selected primary issue."""
    verdict = {
        "refund_failed": "refund_failed",
        "refund_pending": "refund_pending",
        "duplicate_charge": "duplicate_capture",
        "payment_mismatch": "capture_mismatch",
    }.get(issue)
    if verdict:
        finding.verdict = verdict
    elif issue == "valid_split_payment" and finding.split_rows:
        finding.verdict = "reconciled"
        finding.payment_rows = finding.split_rows
        finding.captured_total = round(
            sum(money(r.get("payment_value")) for r in finding.split_rows), 2
        )
    elif finding.verdict in PAYMENT_ANOMALY_TO_ISSUE and finding.captures:
        finding.verdict = "reconciled"
    return finding


# ---------------------------------------------------------------------------
# Issue classification
# ---------------------------------------------------------------------------

PAYMENT_ANOMALY_TO_ISSUE = {
    "refund_failed": "refund_failed",
    "refund_pending": "refund_pending",
    "duplicate_capture": "duplicate_charge",
    "capture_mismatch": "payment_mismatch",
}


def issue_candidates(
    incident: IncidentScope | None, shipment: ShipmentFinding | None, payment: PaymentFinding
) -> list[str]:
    """Every primary issue the evidence of one incident supports, in precedence order."""
    if incident is None:
        return []
    status = incident.order_row.get("order_status")
    captured = payment.captured_total or 0.0
    if status == "canceled" and captured > 0:
        return ["canceled_order_paid"]
    if status == "unavailable" and captured > 0:
        return ["unavailable_order_paid"]
    issues = [PAYMENT_ANOMALY_TO_ISSUE[a] for a in payment.anomalies]
    if shipment is not None:
        if shipment.verdict == "seller_delay":
            issues.append("late_delivery_seller")
        elif shipment.verdict == "logistics_delay":
            issues.append("late_delivery_logistics")
        elif shipment.verdict == "conflicting":
            issues.append(
                "late_delivery_seller"
                if shipment.event_actor == "seller"
                else "late_delivery_logistics"
            )
    if payment.split:
        issues.append("valid_split_payment")
    if (
        not issues
        and shipment is not None
        and shipment.verdict == "on_time"
        and payment.verdict == "reconciled"
    ):
        issues.append("unsupported_claim")
    return issues


def classify_issue(
    incident: IncidentScope | None, shipment: ShipmentFinding | None, payment: PaymentFinding
) -> str:
    issues = issue_candidates(incident, shipment, payment)
    return issues[0] if issues else "insufficient_evidence"
