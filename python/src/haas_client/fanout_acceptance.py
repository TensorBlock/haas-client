#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from collections import Counter
from datetime import datetime
from typing import Any, Mapping, Sequence

from haas_client import HAASClient, HAASPartialBatchError


def main() -> None:
    args = parse_args()
    token = args.token or os.environ.get("HAAS_API_TOKEN")
    if not token:
        raise SystemExit("HAAS_API_TOKEN is required")

    agents = parse_agents(args.agents)
    if args.groups < 1:
        raise SystemExit("--groups must be >= 1")
    run_group_prefix = args.run_group_id or f"fanout-acceptance-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    extra_metadata = parse_json_object(args.metadata, "--metadata")
    extensions = parse_json_list(args.extensions, "--extensions")

    created_batches: list[dict[str, Any]] = []
    run_id_to_group: dict[str, str] = {}
    started_at = time.monotonic()

    with HAASClient(
        args.base_url,
        token=token,
        request_timeout_seconds=args.request_timeout_seconds,
        max_retries=args.max_retries,
    ) as client:
        for group_index in range(args.groups):
            run_group_id = run_group_prefix if args.groups == 1 else f"{run_group_prefix}-{group_index + 1}"
            metadata = {
                **extra_metadata,
                "run_group_id": run_group_id,
                "acceptance_kind": "fanout",
                "acceptance_group_index": str(group_index),
            }
            idempotency_prefix = args.idempotency_key_prefix or run_group_id
            try:
                created = client.run_many(
                    agents=agents,
                    prompt=args.prompt,
                    tenant_id=args.tenant_id,
                    project_id=args.project_id,
                    metadata=metadata,
                    extensions=extensions,
                    timeout_seconds=args.timeout_seconds,
                    idempotency_key_prefix=f"{idempotency_prefix}:fanout",
                    cancel_on_submit_failure=True,
                )
            except HAASPartialBatchError as exc:
                print_json(
                    {
                        "event": "submit_failed",
                        "group_id": run_group_id,
                        "failed_index": exc.failed_index,
                        "created_run_ids": [item.get("run_id") for item in exc.created_runs],
                        "error": str(exc.cause),
                    }
                )
                raise

            created["run_group_id"] = run_group_id
            created_batches.append(created)
            for item in created["runs"]:
                run_id_to_group[item["run_id"]] = run_group_id
            print_json(
                {
                    "event": "submitted",
                    "group_id": run_group_id,
                    "run_ids": created["run_ids"],
                }
            )

        if args.submit_only:
            print_json({"event": "submit_only", "run_groups": [batch["run_group_id"] for batch in created_batches]})
            return

        wait_entries = [
            item
            for batch in created_batches
            for item in batch["runs"]
        ]
        result = client.wait_many(
            wait_entries,
            wait_timeout_seconds=args.wait_timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
            raise_on_failure=False,
        )

        observed_groups: dict[str, list[dict[str, Any]]] = {}
        for batch in created_batches:
            group_id = batch["run_group_id"]
            observed_groups[group_id] = client.list_run_group(
                group_id,
                tenant_id=args.tenant_id,
                project_id=args.project_id,
                limit=max(50, len(agents) * args.groups),
            )

    summary = build_summary(
        result=result,
        observed_groups=observed_groups,
        run_id_to_group=run_id_to_group,
        elapsed_seconds=time.monotonic() - started_at,
        expected_substring=args.expected_substring,
    )
    print_json({"event": "summary", **summary})

    if summary["failed_count"] or summary["missing_observed_run_ids"] or summary["unexpected_final_messages"]:
        raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a production HAAS fanout acceptance test.")
    parser.add_argument("--base-url", default=os.environ.get("HAAS_BASE_URL", "https://haas-api-production.up.railway.app"))
    parser.add_argument("--token", default=None)
    parser.add_argument("--agents", default=os.environ.get("HAAS_FANOUT_AGENTS", "codex,claude-code,grok"))
    parser.add_argument("--prompt", default="Reply exactly: HAAS_FANOUT_OK")
    parser.add_argument("--expected-substring", default="HAAS_FANOUT_OK")
    parser.add_argument("--tenant-id", default="internal")
    parser.add_argument("--project-id", default="pipeline")
    parser.add_argument("--groups", type=int, default=1)
    parser.add_argument("--run-group-id", default=None)
    parser.add_argument("--idempotency-key-prefix", default=None)
    parser.add_argument("--metadata", default="{}")
    parser.add_argument("--extensions", default="[]")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--wait-timeout-seconds", type=float, default=1800)
    parser.add_argument("--poll-interval-seconds", type=float, default=5)
    parser.add_argument("--request-timeout-seconds", type=float, default=30)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--submit-only", action="store_true")
    return parser.parse_args()


def parse_agents(raw: str) -> list[str | Mapping[str, Any]]:
    raw = raw.strip()
    if not raw:
        raise SystemExit("--agents must not be empty")
    if raw.startswith("["):
        value = json.loads(raw)
        if not isinstance(value, list):
            raise SystemExit("--agents JSON must be a list")
        return value
    return [agent.strip() for agent in raw.split(",") if agent.strip()]


def parse_json_object(raw: str, label: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise SystemExit(f"{label} must be a JSON object")
    return value


def parse_json_list(raw: str, label: str) -> list[dict[str, Any]]:
    value = json.loads(raw)
    if not isinstance(value, list):
        raise SystemExit(f"{label} must be a JSON list")
    return value


def build_summary(
    *,
    result: Mapping[str, Any],
    observed_groups: Mapping[str, Sequence[Mapping[str, Any]]],
    run_id_to_group: Mapping[str, str],
    elapsed_seconds: float,
    expected_substring: str,
) -> dict[str, Any]:
    runs = list(result["runs"])
    status_counts = Counter(str(item["status"]) for item in runs)
    failed = list(result["failed"])
    missing_observed_run_ids: list[str] = []
    unexpected_final_messages: list[dict[str, str | None]] = []

    observed_by_group = {
        group_id: {str(record["run_id"]) for record in records}
        for group_id, records in observed_groups.items()
    }
    for run_id, group_id in run_id_to_group.items():
        if run_id not in observed_by_group.get(group_id, set()):
            missing_observed_run_ids.append(run_id)

    if expected_substring:
        for item in runs:
            record = item["record"]
            final_message = ((record.get("result") or {}).get("final_message") or "")
            if item["status"] == "succeeded" and expected_substring not in final_message:
                unexpected_final_messages.append(
                    {
                        "run_id": item["run_id"],
                        "agent": agent_name(item),
                        "final_message": final_message,
                    }
                )

    return {
        "elapsed_seconds": round(elapsed_seconds, 3),
        "total_count": len(runs),
        "succeeded_count": len(result["succeeded"]),
        "failed_count": len(failed),
        "status_counts": dict(status_counts),
        "runs": [summarize_run(item, run_id_to_group) for item in runs],
        "observed_group_counts": {group_id: len(records) for group_id, records in observed_groups.items()},
        "missing_observed_run_ids": missing_observed_run_ids,
        "unexpected_final_messages": unexpected_final_messages,
    }


def summarize_run(item: Mapping[str, Any], run_id_to_group: Mapping[str, str]) -> dict[str, Any]:
    record = item["record"]
    result = record.get("result") or {}
    return {
        "run_group_id": run_id_to_group.get(str(item["run_id"])),
        "run_id": item["run_id"],
        "name": item.get("name"),
        "agent": agent_name(item),
        "status": item["status"],
        "duration_seconds": duration_seconds(record),
        "artifact_count": len(result.get("artifacts") or []),
        "error": record.get("error"),
    }


def agent_name(item: Mapping[str, Any]) -> str | None:
    agent = item.get("agent")
    if isinstance(agent, str):
        return agent
    if isinstance(agent, Mapping):
        return str(agent.get("type"))
    return None


def duration_seconds(record: Mapping[str, Any]) -> float | None:
    started_at = parse_datetime(record.get("started_at"))
    completed_at = parse_datetime(record.get("completed_at"))
    if started_at is None or completed_at is None:
        return None
    return round((completed_at - started_at).total_seconds(), 3)


def parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def print_json(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
