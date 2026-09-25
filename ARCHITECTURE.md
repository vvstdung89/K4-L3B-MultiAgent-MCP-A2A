# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

```text
case input
   │ case_received
   ▼
coordinator ──task_assigned──▶ entity-agent ── get_customer_history, get_order
   │                               │ rank candidates, build order incarnations (scopes)
   │◀──────────handoff─────────────┘
   ├─task──▶ order-agent ──── get_order_items, get_product_context
   ├─task──▶ shipment-agent ─ get_shipment_summary
   ├─task──▶ payment-agent ── get_payment_timeline, get_refund_timeline
   │          (each hands its verdict back to coordinator)
   ├─ select_hypothesis: re-scope the same evidence per incarnation, test the claim
   ├─task──▶ llm-reviewer (optional) ── fast model, escalate to reasoning model
   ├─task──▶ order-agent (seller issues only) ── get_sellers
   ├─task──▶ policy-agent ── get_policy ──policy_decided──▶ conflict-resolver
   ├─task──▶ conflict-resolver ──handoff──▶ verifier
   └─task──▶ verifier ──verification_completed──▶ coordinator ──▶ outputs/<case_id>.json
                                                                   case_finalized
```

Source:

| Module | Vai trò |
| --- | --- |
| `workflow.py` | coordinator, specialist agents, conflict resolver, verifier, output builder |
| `analysis.py` | business rules thuần (không I/O), unit-test tại `tests/test_analysis.py` |
| `a2a.py` | A2A envelope, `CaseContext` (evidence cache theo case, tool permission, retry) |
| `llm.py` | client OpenAI-compatible cho LLM reviewer (tuỳ chọn) |
| `cli.py` | runner, reconnect MCP session có giới hạn |

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | case input (candidates, claimed id, customer hint) | resolve order, reject candidate ngoài history, dựng các scope (incarnation) của order | `get_customer_history`, `get_order` | `entity_resolved` → coordinator |
| Coordinator | handoff của specialist | giao task, re-scope evidence theo từng incarnation, kiểm chứng claim, quyết định escalate LLM | không gọi MCP | task_assigned / hypothesis_selected |
| Order/product | order id, scope | lấy item/seller/product, lọc item thuộc incident | `get_order_items`, `get_product_context`, `get_sellers` | `order_items_scoped`, `seller_confirmed` |
| Shipment | scope, items | on_time / seller_delay / logistics_delay / conflicting | `get_shipment_summary` | `shipment_analysed` |
| Payment/refund | scope, items | capture, split, duplicate, mismatch, refund lifecycle | `get_payment_timeline`, `get_refund_timeline` | `payment_analysed` |
| LLM reviewer | fact sheet có cấu trúc (không có free-text khiếu nại) | review độc lập; chỉ được chọn trong tập issue có evidence | không gọi MCP | handoff `LLM_*` → coordinator |
| Policy | primary issue | áp rule `EC_POLICY_V2`: status, action, refund, responsible party | `get_policy` | `policy_decided` |
| Conflict resolver | get_order vs history, anomaly cạnh tranh | ghi `data_conflicts`, chọn source | không gọi MCP | `conflicts_resolved` → verifier |
| Verifier | output nháp | kiểm invariant, sửa khi chứng minh được sai, hạ confidence | không gọi MCP | `verification_completed` |

Permission được enforce trong `a2a.TOOL_PERMISSIONS`; gọi tool ngoài quyền raise `PermissionDenied`. Tool phải có trong danh sách discovery (`list_tools`) mới được gọi.

## 3. Entity resolution và A2A protocol

- **Xếp hạng candidate**: candidate nằm trong `get_customer_history` của `customer_unique_id_hint` được chấp nhận; claimed order được ưu tiên khi có nhiều match. Candidate không có trong history bị reject mà **không gọi MCP** (tránh call thừa, tránh evidence của entity sai).
- **Confidence**: 0.95 (khớp claimed id), 0.85 (khớp candidate khác), 0.7 (nhiều match, chọn claimed), 0.4 ambiguous, 0.3 not_found. Not found → fallback 1 lần `get_order(claimed_order_id)`.
- **Incarnation / scope**: cùng một order id có thể có nhiều dòng mâu thuẫn trong history. Mỗi dòng khác nhau là một scope với cửa sổ thời gian `[purchase, purchase kế tiếp)`. Thứ tự ưu tiên: dòng **khác** dòng `get_order` → mua trước `opened_at` → gần `opened_at` nhất.
- **Kiểm chứng claim**: coordinator phân tích lại evidence đã có cho từng scope (không gọi thêm MCP), liệt kê mọi issue mà evidence hỗ trợ. Claim chỉ được chấp nhận nếu nằm trong tập đó; nếu không, dùng issue ưu tiên cao nhất của scope đầu (`CLAIM_NOT_SUPPORTED`, confidence 0.65).
- **Envelope**: `Envelope(case_id, message_id, correlation_id=case_id, sender, recipient, intent, payload)`. Payload chỉ nằm trong bộ nhớ; trace chỉ ghi `intent`, `decision_code`, `message_id` và evidence refs.
- **Chống vòng lặp**: luồng tuyến tính, mỗi specialist được giao tối đa một lần (order-agent thêm một lần cho seller check), LLM tối đa 2 model × (1 + 1 retry).

## 4. Evidence và conflict lifecycle

1. `EvidenceGateway.call` validate response theo `mcp-evidence-response-v1`; response sai contract bị bỏ (`INVALID_EVIDENCE`), không dùng.
2. `CaseContext.fetch` cache theo `(tool, arguments)` trong phạm vi một case và emit `tool_result_consumed` kèm `evidence_ref`. Context bị huỷ khi case kết thúc nên evidence không thể dùng chéo case.
3. Tool báo lỗi (ví dụ order không có refund event) được ghi `NO_EVIDENCE`, không retry, không bịa dữ liệu.
4. Output `evidence_refs` chỉ gồm các domain liên quan đến primary issue (core: customer, order, item, policy, product; thêm shipment / payment / refund / seller theo issue). Claim assessment dẫn đến evidence cụ thể.
5. `data_conflicts`:
   - `get_order` vs `get_customer_history` khác nhau ở status/timestamp → chọn history, `TEMPORAL_SCOPE_BEFORE_OPENED_AT`;
   - nhiều anomaly cạnh tranh trong một scope → `SCOPED_TO_CLAIMED_INCIDENT`;
   - carrier event mâu thuẫn với shipping limit → `UNRESOLVED_SOURCE_CONFLICT` (`selected_source: null`);
   - claim không được evidence hỗ trợ → `EVIDENCE_OVERRIDES_CLAIM`.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / transport | 1 retry / call; 5 reconnect / run | hết budget → `TransportFailure`, runner reconnect và làm lại case từ đầu (không finalize với evidence thiếu) | stderr `session lost`, case chạy lại |
| MCP tool error | 0 | coi như không có evidence cho domain đó | `tool_result_consumed` / `NO_EVIDENCE` |
| Entity not found/ambiguous | 1 (`get_order` claimed id) | `insufficient_evidence`, `needs_investigation`, confidence 0.35 | `ENTITY_NOT_FOUND` / `ENTITY_AMBIGUOUS` |
| Source conflict | 0 | chọn source theo thứ tự ở mục 4, không resolve được → `conflicting` | `CONFLICTS_RESOLVED`, `data_conflicts` |
| Invalid specialist / LLM result | LLM: 1 retry / model | LLM trả issue ngoài tập cho phép hoặc lỗi → bỏ qua, giữ kết quả rule | `LLM_UNAVAILABLE` |

**Query budget**: 8 MCP call cho mỗi case (history, order, items, product, shipment, payment timeline, refund timeline, policy), thêm `get_sellers` chỉ khi seller chịu trách nhiệm. Không gọi `get_order_payments` (payment timeline đã chứa các dòng payment) và không gọi MCP cho candidate bị reject. Mọi phân tích lại theo scope đều dùng evidence đã cache.

## 6. Verification invariants

Verifier (`workflow.verifier`) kiểm tra trước khi finalize:

- evidence ownership: mọi ref trong output và claim đều do chính case này gọi MCP;
- entity scope: `resolved_order_ids ∩ rejected_candidates = ∅`;
- `no_action` ⇒ refund = 0 và không có refund line;
- tổng refund lines = `recommended_refund_brl`; refund ≤ số tiền đã capture;
- `action_required` ⇒ có resolution action;
- responsible seller ∈ `affected_entities.seller_ids`;
- confidence nằm trong [0.05, 0.97]; mỗi lỗi invariant hạ confidence 0.15 (`VERIFIED_WITH_REPAIRS`).

CLI validate lại output theo JSON Schema trước khi ghi file.

**LLM guardrail**: LLM không tính tiền, không chọn policy và không tạo evidence ref. Model FAST review mọi case; leo thang lên `ORCHESTRATOR_MODEL` (có thinking) khi model FAST không đồng ý hoặc case mơ hồ. LLM chỉ được override khi rule có hoà thật sự (scope được chọn có nhiều issue, hoặc claim không được hỗ trợ). Ngoài ra LLM chỉ điều chỉnh confidence (+0.02 khi đồng ý, −0.1 khi không đồng ý).

## 7. Reproducibility

- Python ≥ 3.11, dependency pin theo range trong `pyproject.toml` (`mcp>=2,<3`, `httpx2`, `jsonschema`).
- MCP: một session, xử lý tuần tự (concurrency = 1), timeout 90 s mỗi call.
- LLM (tuỳ chọn): `ORCHESTRATOR_BASE_URL`, `ORCHESTRATOR_FAST_MODEL`, `ORCHESTRATOR_MODEL`, `ORCHESTRATOR_THINKING`, `ORCHESTRATOR_API_KEY` trong `.env`, `temperature=0`, JSON mode. Không có env → chạy thuần deterministic và kết quả tái lập 100%.
- Không có random seed; event id của trace là ngẫu nhiên theo thiết kế của starter.
- Lệnh:

```bash
pip install -e ".[dev]"
pytest -q tests/test_analysis.py tests/test_starter.py
day09 run && day09 validate
day09 package --output dist/submission.zip
```
