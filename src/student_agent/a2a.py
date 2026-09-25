"""Minimal agent-to-agent (A2A) messaging and case-scoped evidence access.

* ``CaseContext`` is created per case. Its evidence cache and message log never outlive
  that case, so evidence refs cannot leak across cases.
* Every MCP call goes through ``CaseContext.fetch`` which enforces the actor's tool
  permission (least privilege), de-duplicates identical calls, applies a bounded retry
  for transport failures only and emits ``tool_result_consumed`` with the evidence ref.
* ``CaseContext.send`` records an A2A envelope and emits an observable ``handoff`` or
  ``task_assigned`` event. Payloads stay in memory; the trace only carries codes.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

# Least-privilege tool permissions per actor.
TOOL_PERMISSIONS: dict[str, frozenset[str]] = {
    "entity-agent": frozenset({"get_customer_history", "get_order"}),
    "order-agent": frozenset({"get_order_items", "get_product_context", "get_sellers"}),
    "shipment-agent": frozenset({"get_shipment_summary"}),
    "payment-agent": frozenset({"get_payment_timeline", "get_refund_timeline"}),
    "policy-agent": frozenset({"get_policy"}),
}
TRANSPORT_RETRIES = 1
CALL_TIMEOUT_SECONDS = 90.0


class PermissionDenied(RuntimeError):
    pass


class TransportFailure(ConnectionError):
    """MCP transport failed after the retry budget; the case must be re-run."""


@dataclass(frozen=True)
class Evidence:
    tool: str
    ref: str
    domain: str
    data: Any
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Envelope:
    case_id: str
    message_id: str
    correlation_id: str
    sender: str
    recipient: str
    intent: str
    payload: dict[str, Any]


@dataclass
class CaseContext:
    case: dict[str, Any]
    gateway: EvidenceGateway
    trace: TraceWriter
    available_tools: frozenset[str] | None = None
    evidence: dict[tuple[str, tuple[tuple[str, str], ...]], Evidence | None] = field(
        default_factory=dict
    )
    messages: list[Envelope] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    call_count: int = 0
    _seq: itertools.count = field(default_factory=lambda: itertools.count(1))

    @property
    def case_id(self) -> str:
        return self.case["case_id"]

    # -- A2A ---------------------------------------------------------------

    def send(
        self,
        sender: str,
        recipient: str,
        intent: str,
        payload: dict[str, Any] | None = None,
        *,
        event_type: str = "handoff",
        decision_code: str | None = None,
        evidence_refs: list[str] | None = None,
    ) -> Envelope:
        seq = next(self._seq)
        envelope = Envelope(
            case_id=self.case_id,
            message_id=f"{self.case_id}:msg:{seq}",
            correlation_id=self.case_id,
            sender=sender,
            recipient=recipient,
            intent=intent,
            payload=payload or {},
        )
        self.messages.append(envelope)
        refs = sorted(set(evidence_refs or []))[:20]
        self.trace.emit(
            case_id=self.case_id,
            event_type=event_type,
            actor=sender,
            target=recipient,
            decision_code=decision_code or intent,
            evidence_refs=refs or None,
            attributes={"message_id": envelope.message_id, "intent": intent},
        )
        return envelope

    def assign(self, recipient: str, intent: str) -> Envelope:
        return self.send(
            "coordinator", recipient, intent, event_type="task_assigned", decision_code=intent
        )

    # -- Evidence ------------------------------------------------------------

    async def fetch(self, actor: str, tool: str, **arguments: str) -> Evidence | None:
        """Call one MCP tool for this case. Returns ``None`` when the tool reports no data."""
        if tool not in TOOL_PERMISSIONS.get(actor, frozenset()):
            raise PermissionDenied(f"{actor} may not call {tool}")
        if self.available_tools is not None and tool not in self.available_tools:
            self.failures.append(f"{tool}:not_discovered")
            return None
        key = (tool, tuple(sorted(arguments.items())))
        if key in self.evidence:
            return self.evidence[key]

        result: Evidence | None = None
        for attempt in range(TRANSPORT_RETRIES + 1):
            try:
                self.call_count += 1
                raw = await asyncio.wait_for(
                    self.gateway.call(tool, case_id=self.case_id, **arguments),
                    timeout=CALL_TIMEOUT_SECONDS,
                )
            except RuntimeError as exc:
                # Tool-level error (e.g. no rows for this scope): deterministic, never retried.
                self.failures.append(f"{tool}:tool_error")
                self.trace.emit(
                    case_id=self.case_id,
                    event_type="tool_result_consumed",
                    actor=actor,
                    tool_name=tool,
                    decision_code="NO_EVIDENCE",
                    attributes={"outcome": "tool_error", "detail": str(exc)[:120]},
                )
                break
            except ValueError as exc:
                # Response violated the evidence contract: do not consume it.
                self.failures.append(f"{tool}:invalid_evidence")
                self.trace.emit(
                    case_id=self.case_id,
                    event_type="tool_result_consumed",
                    actor=actor,
                    tool_name=tool,
                    decision_code="INVALID_EVIDENCE",
                    attributes={"outcome": "invalid_evidence", "detail": str(exc)[:120]},
                )
                break
            except Exception as exc:  # transport-level failure (timeout, connection reset)
                if attempt < TRANSPORT_RETRIES:
                    continue
                # Never turn missing evidence into a guess: abort the case so the runner
                # can reconnect and re-investigate it from scratch.
                raise TransportFailure(f"{tool}: {type(exc).__name__}") from exc
            else:
                result = Evidence(
                    tool=tool,
                    ref=raw["evidence_ref"],
                    domain=raw["domain"],
                    data=raw.get("data"),
                    warnings=tuple(raw.get("warnings") or ()),
                )
                self.trace.emit(
                    case_id=self.case_id,
                    event_type="tool_result_consumed",
                    actor=actor,
                    tool_name=tool,
                    evidence_refs=[result.ref],
                    attributes={"domain": result.domain, "warnings": len(result.warnings)},
                )
                break
        self.evidence[key] = result
        return result
