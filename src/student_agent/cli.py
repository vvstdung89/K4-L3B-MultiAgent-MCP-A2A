from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .a2a import EvidenceUnavailable, PermissionDenied
from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .llm import LLMSettings, OrchestratorLLM
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case

MAX_RECONNECTS = 5


def _root(value: str) -> Path:
    return Path(value).resolve()


def _leaf(exc: BaseException) -> BaseException:
    """The first underlying error of a (nested) exception group from the MCP task groups."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


def _resumable_cases(staging: Path, staged_trace: Path) -> set[str]:
    """Cases of an interrupted run that are complete: staged output plus a finalized trace.

    The staged trace is rewritten to keep only those cases, so an interrupted run can
    continue without re-querying MCP for work that already finished."""
    if not staged_trace.exists():
        return set()
    lines = staged_trace.read_text(encoding="utf-8").splitlines(keepends=True)
    events = []
    for line in lines:
        try:
            events.append((json.loads(line), line))
        except json.JSONDecodeError:
            continue  # a torn final line from the interruption
    finalized = {e["case_id"] for e, _ in events if e.get("event_type") == "case_finalized"}
    done = {case_id for case_id in finalized if (staging / f"{case_id}.json").exists()}
    staged_trace.write_text(
        "".join(line for e, line in events if e.get("case_id") in done), encoding="utf-8"
    )
    return done


async def _run(root: Path, resume: bool = False) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    # Stage the whole run; previous artifacts are replaced only when every case succeeded.
    staging = output_root / ".staging"
    staged_trace = staging / "trace.jsonl"
    staging.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    done = _resumable_cases(staging, staged_trace) if resume else set()
    for stale in staging.iterdir():
        if stale != staged_trace and stale.stem not in done:
            stale.unlink()
    if not done and staged_trace.exists():
        staged_trace.unlink()
    trace = TraceWriter(staged_trace, contracts)

    llm_settings = LLMSettings.from_env()
    llm = OrchestratorLLM(llm_settings) if llm_settings else None
    print(
        f"LLM reviewer: {llm_settings.fast_model} -> {llm_settings.model} "
        f"(thinking={llm_settings.thinking})"
        if llm_settings
        else "LLM reviewer: disabled (ORCHESTRATOR_* not set)",
        flush=True,
    )
    pending = [case_id for case_id in case_set.case_ids if case_id not in done]
    if done:
        print(f"resuming: {len(done)} cases already staged", flush=True)
    reconnects = 0
    while pending:
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                discovered_tools = await gateway.list_tools()
                if not discovered_tools:
                    raise RuntimeError("MCP Gateway returned no tools")
                while pending:
                    case_id = pending[0]
                    case = case_set.cases[case_id]
                    trace.begin_case()
                    trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                    output = await solve_case(
                        case, gateway, trace, frozenset(discovered_tools), llm
                    )
                    contracts.validate_output(output, f"outputs/{case_id}.json")
                    if output.get("case_id") != case_id:
                        raise ValueError(f"solver returned a mismatched case_id for {case_id}")
                    target = staging / f"{case_id}.json"
                    temporary = target.with_suffix(".json.tmp")
                    temporary.write_text(
                        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                    )
                    temporary.replace(target)
                    trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
                    trace.commit_case()
                    pending.pop(0)
                    print(f"[{len(case_set.case_ids) - len(pending):3d}] {case_id}", flush=True)
        except (ValueError, KeyError, PermissionDenied, EvidenceUnavailable):
            raise
        except Exception as exc:  # the MCP session died (server disconnect, network reset)
            cause = _leaf(exc)
            if isinstance(cause, (ValueError, KeyError, PermissionDenied, EvidenceUnavailable)):
                # Not a transport failure: retrying only burns audited MCP calls.
                raise RuntimeError(f"{type(cause).__name__}: {cause}") from exc
            trace.discard_case()  # the case is re-investigated from scratch in a new session
            reconnects += 1
            if reconnects > MAX_RECONNECTS:
                raise RuntimeError(f"MCP session failed {reconnects} times") from exc
            print(
                f"session lost ({type(cause).__name__}: {cause}); reconnect {reconnects}",
                file=sys.stderr,
            )
            await asyncio.sleep(2.0 * reconnects)
    if llm is not None:
        print(f"LLM calls: {llm.calls}, tokens: {llm.tokens}", flush=True)
        await llm.aclose()

    for stale in output_root.glob("*.json"):
        stale.unlink()
    for case_id in case_set.case_ids:
        (staging / f"{case_id}.json").replace(output_root / f"{case_id}.json")
    staged_trace.replace(trace_path)
    staging.rmdir()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--resume",
        action="store_true",
        help="continue an interrupted run, keeping the cases already staged",
    )
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root, resume=args.resume))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
