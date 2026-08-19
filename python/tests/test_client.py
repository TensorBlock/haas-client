import json
from pathlib import Path

import httpx
import pytest

from haas_client import (
    HAASAPIError,
    HAASClient,
    HAASManyRunsFailedError,
    HAASPartialBatchError,
    HAASRunFailedError,
)


def test_create_run_sends_auth_and_payload() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["idempotency_key"] = request.headers.get("idempotency-key")
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"run_id": "run_1", "status": "queued"})

    client = HAASClient("https://haas.example", token="secret", transport=httpx.MockTransport(handler))
    created = client.create_run(agent="codex", prompt="hello", timeout_seconds=120, idempotency_key="step-1")

    assert created["run_id"] == "run_1"
    assert seen["auth"] == "Bearer secret"
    assert seen["idempotency_key"] == "step-1"
    assert seen["payload"]["agent"]["type"] == "codex"
    assert seen["payload"]["input"]["prompt"] == "hello"
    assert seen["payload"]["extensions"] == []
    assert seen["payload"]["timeout_seconds"] == 120


def test_create_run_can_request_worklog_artifact() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"run_id": "run_1", "status": "queued"})

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))
    client.create_run(agent="codex", prompt="hello", worklog=True)

    assert seen["payload"]["options"] == {"worklog": True}


def test_create_run_merges_options_and_worklog_flag() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"run_id": "run_1", "status": "queued"})

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))
    client.create_run(agent="codex", prompt="hello", options={"trace": "compact"}, worklog=False)

    assert seen["payload"]["options"] == {"trace": "compact", "worklog": False}


def test_create_run_includes_document_references() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"run_id": "run_1", "status": "queued"})

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))
    reference = client.document_reference(
        url="https://storage.example/documents/source.pdf?signature=test",
        ref="cuey:source_document:doc_123",
        name="source.pdf",
        content_type="application/pdf",
        metadata={"source": "cuey"},
    )
    client.create_run(
        agent="claude-code",
        prompt="Read the referenced document.",
        document_references=[reference],
    )

    assert seen["payload"]["context"] == [
        {
            "type": "document_reference",
            "url": "https://storage.example/documents/source.pdf?signature=test",
            "ref": "cuey:source_document:doc_123",
            "name": "source.pdf",
            "content_type": "application/pdf",
            "metadata": {"source": "cuey"},
        }
    ]


def test_upload_file_sends_multipart_request(tmp_path: Path) -> None:
    upload_path = tmp_path / "input.txt"
    upload_path.write_text("hello upload", encoding="utf-8")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = request.read()
        return httpx.Response(
            200,
            json={
                "upload_id": "upl_1",
                "artifact": {
                    "type": "upload",
                    "name": "input.txt",
                    "artifact_id": "art_1",
                    "storage_backend": "local",
                },
                "context": {
                    "type": "uploaded_artifact",
                    "upload_id": "upl_1",
                    "artifact_id": "art_1",
                    "name": "input.txt",
                },
            },
        )

    client = HAASClient("https://haas.example", token="secret", transport=httpx.MockTransport(handler))
    uploaded = client.upload_file(upload_path)

    assert uploaded["context"]["type"] == "uploaded_artifact"
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/uploads"
    assert seen["auth"] == "Bearer secret"
    assert seen["content_type"].startswith("multipart/form-data")
    assert b'form-data; name="tenant_id"' in seen["body"]
    assert b'form-data; name="file"; filename="input.txt"' in seen["body"]
    assert b"hello upload" in seen["body"]


def test_create_run_with_files_sends_multipart_payload(tmp_path: Path) -> None:
    input_path = tmp_path / "paper.pdf"
    input_path.write_bytes(b"%PDF input")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["idempotency_key"] = request.headers.get("idempotency-key")
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = request.read()
        return httpx.Response(200, json={"run_id": "run_1", "status": "queued"})

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))
    created = client.create_run(
        agent="claude-code",
        prompt="summarize attached pdf",
        files=[input_path],
        extensions=[{"type": "skill", "ref": "pdf-analysis", "version": "0.1.0"}],
        idempotency_key="multipart-step-1",
    )

    assert created["run_id"] == "run_1"
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/runs"
    assert seen["idempotency_key"] == "multipart-step-1"
    assert seen["content_type"].startswith("multipart/form-data")
    assert b'form-data; name="payload"' in seen["body"]
    assert b'"prompt":"summarize attached pdf"' in seen["body"]
    assert b'"extensions":[{"type":"skill","ref":"pdf-analysis","version":"0.1.0"}]' in seen["body"]
    assert b'form-data; name="files"; filename="paper.pdf"' in seen["body"]
    assert b"%PDF input" in seen["body"]


def test_wait_run_returns_terminal_record() -> None:
    statuses = iter(["queued", "running", "succeeded"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"run_id": "run_1", "status": next(statuses), "result": {"final_message": "ok"}})

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))
    record = client.wait_run("run_1", wait_timeout_seconds=5, poll_interval_seconds=0)

    assert record["status"] == "succeeded"
    assert record["result"]["final_message"] == "ok"


def test_run_and_wait() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(200, json={"run_id": "run_1", "status": "queued"})
        return httpx.Response(200, json={"run_id": "run_1", "status": "succeeded", "result": {"final_message": "done"}})

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))
    record = client.run_and_wait(agent="claude-code", prompt="hello", poll_interval_seconds=0)

    assert record["result"]["final_message"] == "done"
    assert calls == [("POST", "/v1/runs"), ("GET", "/v1/runs/run_1")]


def test_skill_discovery_methods() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, dict(request.url.params)))
        if request.url.path == "/v1/extensions/skills":
            return httpx.Response(
                200,
                json=[
                    {
                        "ref": "spreadsheet-analysis",
                        "latest_version": "0.2.0",
                        "selected_version": None,
                        "versions": [],
                        "metadata": {"description": "Spreadsheet helpers"},
                    }
                ],
            )
        if request.url.path == "/v1/extensions/skills/spreadsheet-analysis":
            return httpx.Response(
                200,
                json={
                    "ref": "spreadsheet-analysis",
                    "latest_version": "0.2.0",
                    "selected_version": "0.1.0",
                    "versions": [],
                    "metadata": {"description": "Old spreadsheet helpers"},
                },
            )
        return httpx.Response(404)

    client = HAASClient("https://haas.example", token="secret", transport=httpx.MockTransport(handler))

    assert client.list_skills()[0]["ref"] == "spreadsheet-analysis"
    assert client.get_skill("spreadsheet-analysis", version="0.1.0")["selected_version"] == "0.1.0"
    assert calls == [
        ("GET", "/v1/extensions/skills", {}),
        ("GET", "/v1/extensions/skills/spreadsheet-analysis", {"version": "0.1.0"}),
    ]


def test_run_many_submits_dynamic_agent_contract() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(
            {
                "idempotency_key": request.headers.get("idempotency-key"),
                "payload": payload,
            }
        )
        return httpx.Response(200, json={"run_id": f"run_{len(calls)}", "status": "queued"})

    client = HAASClient("https://haas.example", token="secret", transport=httpx.MockTransport(handler))
    batch = client.run_many(
        agents=[
            "codex",
            {
                "name": "grok-deep",
                "agent": {"type": "grok", "options": {"model": "grok-4.5"}},
                "prompt": "custom grok prompt",
                "metadata": {"lane": "deep"},
                "worklog": False,
                "extensions": [{"type": "skill", "ref": "grok-extra", "version": "0.2.0"}],
            },
            {"name": "claude", "type": "claude-code", "options": {"max_turns": 3}},
        ],
        prompt="shared prompt",
        project_id="pipeline",
        metadata={"pipeline_id": "pipe_1"},
        extensions=[{"type": "skill", "ref": "common", "version": "1.0.0"}],
        options={"trace": "compact"},
        worklog=True,
        idempotency_key_prefix="pipe_1:fanout",
    )

    assert batch["run_ids"] == ["run_1", "run_2", "run_3"]
    assert calls[0]["idempotency_key"] == "pipe_1:fanout:0:codex"
    assert calls[0]["payload"]["agent"]["type"] == "codex"
    assert calls[0]["payload"]["input"]["prompt"] == "shared prompt"
    assert calls[0]["payload"]["metadata"] == {"pipeline_id": "pipe_1", "fanout_name": "codex"}
    assert calls[0]["payload"]["extensions"] == [{"type": "skill", "ref": "common", "version": "1.0.0"}]
    assert calls[0]["payload"]["options"] == {"trace": "compact", "worklog": True}
    assert calls[0]["payload"]["context"] == []

    assert calls[1]["idempotency_key"] == "pipe_1:fanout:1:grok-deep"
    assert calls[1]["payload"]["agent"]["type"] == "grok"
    assert calls[1]["payload"]["agent"]["options"]["model"] == "grok-4.5"
    assert calls[1]["payload"]["input"]["prompt"] == "custom grok prompt"
    assert calls[1]["payload"]["metadata"] == {
        "pipeline_id": "pipe_1",
        "lane": "deep",
        "fanout_name": "grok-deep",
    }
    assert calls[1]["payload"]["options"] == {"trace": "compact", "worklog": False}
    assert calls[1]["payload"]["extensions"] == [
        {"type": "skill", "ref": "common", "version": "1.0.0"},
        {"type": "skill", "ref": "grok-extra", "version": "0.2.0"},
    ]

    assert calls[2]["payload"]["agent"]["type"] == "claude-code"
    assert calls[2]["payload"]["agent"]["options"]["max_turns"] == 3
    assert calls[2]["payload"]["project_id"] == "pipeline"


def test_run_many_merges_document_references() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"run_id": f"run_{len(calls)}", "status": "queued"})

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))
    client.run_many(
        agents=[
            "codex",
            {
                "name": "claude",
                "agent": "claude-code",
                "document_references": [
                    {
                        "url": "https://storage.example/per-run.pdf?signature=test",
                        "ref": "cuey:doc:per-run",
                        "name": "per-run.pdf",
                    }
                ],
            },
        ],
        prompt="Read documents.",
        document_references=[
            {
                "url": "https://storage.example/shared.pdf?signature=test",
                "ref": "cuey:doc:shared",
                "name": "shared.pdf",
            }
        ],
    )

    assert calls[0]["context"] == [
        {
            "type": "document_reference",
            "url": "https://storage.example/shared.pdf?signature=test",
            "ref": "cuey:doc:shared",
            "name": "shared.pdf",
        }
    ]
    assert calls[1]["context"] == [
        {
            "type": "document_reference",
            "url": "https://storage.example/shared.pdf?signature=test",
            "ref": "cuey:doc:shared",
            "name": "shared.pdf",
        },
        {
            "type": "document_reference",
            "url": "https://storage.example/per-run.pdf?signature=test",
            "ref": "cuey:doc:per-run",
            "name": "per-run.pdf",
        },
    ]


def test_run_many_requires_prompt() -> None:
    client = HAASClient("https://haas.example", transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    with pytest.raises(ValueError, match="shared prompt"):
        client.run_many(agents=["codex"])


def test_run_many_and_wait_returns_ordered_batch_result() -> None:
    statuses = {
        "run_1": iter(["queued", "succeeded"]),
        "run_2": iter(["running", "failed"]),
    }
    created_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal created_count
        if request.method == "POST":
            created_count += 1
            return httpx.Response(200, json={"run_id": f"run_{created_count}", "status": "queued"})
        run_id = request.url.path.rsplit("/", 1)[-1]
        status = next(statuses[run_id])
        payload = {"run_id": run_id, "status": status}
        if status == "succeeded":
            payload["result"] = {"final_message": "ok"}
        if status == "failed":
            payload["error"] = "agent failed"
        return httpx.Response(200, json=payload)

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))
    result = client.run_many_and_wait(
        agents=["codex", {"name": "grok", "agent": "grok"}],
        prompt="hello",
        poll_interval_seconds=0,
    )

    assert result["run_ids"] == ["run_1", "run_2"]
    assert [item["name"] for item in result["runs"]] == ["codex", "grok"]
    assert [item["status"] for item in result["runs"]] == ["succeeded", "failed"]
    assert [item["run_id"] for item in result["succeeded"]] == ["run_1"]
    assert [item["run_id"] for item in result["failed"]] == ["run_2"]


def test_wait_many_can_raise_for_failed_runs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        run_id = request.url.path.rsplit("/", 1)[-1]
        status = "failed" if run_id == "run_bad" else "succeeded"
        return httpx.Response(200, json={"run_id": run_id, "status": status})

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))

    with pytest.raises(HAASManyRunsFailedError) as exc:
        client.wait_many(["run_ok", "run_bad"], poll_interval_seconds=0, raise_on_failure=True)

    assert [record["run_id"] for record in exc.value.records] == ["run_bad"]


def test_error_includes_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Unauthorized"})

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))

    with pytest.raises(HAASAPIError) as exc:
        client.list_agents()

    assert exc.value.status_code == 401
    assert "Unauthorized" in str(exc.value)


def test_create_run_retries_retryable_error_with_same_idempotency_key() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("idempotency-key"))
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"detail": "queue full"})
        return httpx.Response(200, json={"run_id": "run_1", "status": "queued"})

    client = HAASClient(
        "https://haas.example",
        transport=httpx.MockTransport(handler),
        max_retries=1,
        retry_base_seconds=0,
    )
    created = client.create_run(agent="codex", prompt="hello")

    assert created["run_id"] == "run_1"
    assert len(calls) == 2
    assert calls[0] is not None
    assert calls[0] == calls[1]


def test_create_run_with_files_retries_with_full_file_content(tmp_path: Path) -> None:
    input_path = tmp_path / "input.txt"
    input_path.write_text("retryable file body", encoding="utf-8")
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read())
        if len(bodies) == 1:
            return httpx.Response(503, json={"detail": "temporary outage"})
        return httpx.Response(200, json={"run_id": "run_1", "status": "queued"})

    client = HAASClient(
        "https://haas.example",
        transport=httpx.MockTransport(handler),
        max_retries=1,
        retry_base_seconds=0,
    )
    created = client.create_run(agent="codex", prompt="use file", files=[input_path])

    assert created["run_id"] == "run_1"
    assert len(bodies) == 2
    assert b"retryable file body" in bodies[0]
    assert b"retryable file body" in bodies[1]


def test_wait_run_retries_transient_get_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"detail": "temporary outage"})
        return httpx.Response(200, json={"run_id": "run_1", "status": "succeeded"})

    client = HAASClient(
        "https://haas.example",
        transport=httpx.MockTransport(handler),
        max_retries=1,
        retry_base_seconds=0,
    )
    record = client.wait_run("run_1", poll_interval_seconds=0)

    assert record["status"] == "succeeded"
    assert calls == 2


def test_run_many_partial_failure_reports_created_runs_and_can_cancel() -> None:
    cancelled = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/runs/run_1/cancel":
            cancelled.append("run_1")
            return httpx.Response(200, json={"run_id": "run_1", "status": "cancelled"})
        if request.url.path == "/v1/runs":
            payload = json.loads(request.content)
            if payload["agent"]["type"] == "codex":
                return httpx.Response(200, json={"run_id": "run_1", "status": "queued"})
            return httpx.Response(500, json={"detail": "submit failed"})
        return httpx.Response(404)

    client = HAASClient(
        "https://haas.example",
        transport=httpx.MockTransport(handler),
        max_retries=0,
    )

    with pytest.raises(HAASPartialBatchError) as exc:
        client.run_many(
            agents=["codex", "grok"],
            prompt="hello",
            cancel_on_submit_failure=True,
        )

    assert [item["run_id"] for item in exc.value.created_runs] == ["run_1"]
    assert exc.value.failed_index == 1
    assert cancelled == ["run_1"]


def test_list_events() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/runs/run_1/events"
        assert request.url.params["after_id"] == "10"
        assert request.url.params["limit"] == "25"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 11,
                    "run_id": "run_1",
                    "event_type": "run.succeeded",
                    "message": None,
                    "data": {},
                    "created_at": "2026-07-27T00:00:00+00:00",
                }
            ],
        )

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))
    events = client.list_events("run_1", after_id=10, limit=25)

    assert events[0]["event_type"] == "run.succeeded"


def test_get_run_summary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/runs/run_1/summary"
        return httpx.Response(
            200,
            json={
                "run_id": "run_1",
                "tenant_id": "internal",
                "project_id": "default",
                "agent": {"type": "codex", "version": "bundled", "credential_profile": "default", "options": {}},
                "status": "succeeded",
                "final_message": "done",
                "artifacts": [],
                "artifact_count": 0,
                "created_at": "2026-07-27T00:00:00+00:00",
            },
        )

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))
    summary = client.get_run_summary("run_1")

    assert summary["final_message"] == "done"


def test_get_worklog() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/runs/run_1/worklog"
        return httpx.Response(200, text="# HAAS Worklog\n")

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))

    assert client.get_worklog("run_1") == "# HAAS Worklog\n"


def test_agent_health_system_status_and_cleanup() -> None:
    seen_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append((request.method, request.url.path))
        if request.url.path == "/v1/agents/health":
            return httpx.Response(200, json=[{"type": "echo", "available": True, "auth_mode": "none"}])
        if request.url.path == "/v1/system/status":
            return httpx.Response(200, json={"ok": True, "run_status_counts": {"queued": 1}})
        if request.url.path == "/v1/system/cleanup":
            assert request.url.params["older_than_days"] == "14"
            assert request.url.params["limit"] == "10"
            assert request.url.params["dry_run"] == "true"
            return httpx.Response(200, json={"dry_run": True, "candidate_count": 0})
        return httpx.Response(404)

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))

    assert client.list_agent_health()[0]["type"] == "echo"
    assert client.get_system_status()["ok"] is True
    assert client.cleanup_completed_runs(older_than_days=14, limit=10, dry_run=True)["candidate_count"] == 0
    assert seen_paths == [
        ("GET", "/v1/agents/health"),
        ("GET", "/v1/system/status"),
        ("POST", "/v1/system/cleanup"),
    ]


def test_list_runs_supports_observability_filters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/runs"
        assert request.url.params["limit"] == "25"
        assert request.url.params["tenant_id"] == "internal"
        assert request.url.params["project_id"] == "pipeline"
        assert request.url.params["status"] == "succeeded"
        assert request.url.params["metadata_key"] == "run_group_id"
        assert request.url.params["metadata_value"] == "group_1"
        return httpx.Response(200, json=[{"run_id": "run_1", "status": "succeeded"}])

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))

    runs = client.list_runs(
        limit=25,
        tenant_id="internal",
        project_id="pipeline",
        status="succeeded",
        metadata_key="run_group_id",
        metadata_value="group_1",
    )

    assert runs == [{"run_id": "run_1", "status": "succeeded"}]


def test_list_run_group_uses_metadata_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/runs"
        assert request.url.params["project_id"] == "pipeline"
        assert request.url.params["metadata_key"] == "run_group_id"
        assert request.url.params["metadata_value"] == "group_1"
        return httpx.Response(200, json=[])

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))

    assert client.list_run_group("group_1", project_id="pipeline") == []


def test_wait_run_can_raise_on_failed_terminal_record() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"run_id": "run_1", "status": "failed", "error": "agent failed"})

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))

    with pytest.raises(HAASRunFailedError) as exc:
        client.wait_run("run_1", wait_timeout_seconds=5, poll_interval_seconds=0, raise_on_failure=True)

    assert exc.value.record["run_id"] == "run_1"
    assert "agent failed" in str(exc.value)


def test_list_deliveries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/deliveries"
        assert request.url.params["run_id"] == "run_1"
        assert request.url.params["status"] == "exhausted"
        assert request.url.params["limit"] == "10"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 7,
                    "run_id": "run_1",
                    "delivery_type": "webhook",
                    "status": "exhausted",
                    "attempts": 3,
                    "max_attempts": 3,
                    "next_attempt_at": "2026-07-27T00:00:00+00:00",
                    "last_attempt_at": "2026-07-27T00:00:00+00:00",
                    "delivered_at": None,
                    "last_error": "failed",
                    "callback_url": "https://example.com/callback",
                    "callback_headers": {},
                    "created_at": "2026-07-27T00:00:00+00:00",
                    "updated_at": "2026-07-27T00:00:00+00:00",
                }
            ],
        )

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))
    deliveries = client.list_deliveries(run_id="run_1", status="exhausted", limit=10)

    assert deliveries[0]["id"] == 7


def test_retry_delivery() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/deliveries/7/retry"
        return httpx.Response(
            200,
            json={
                "id": 7,
                "run_id": "run_1",
                "delivery_type": "webhook",
                "status": "pending",
                "attempts": 0,
                "max_attempts": 3,
                "next_attempt_at": "2026-07-27T00:00:00+00:00",
                "last_attempt_at": "2026-07-27T00:00:00+00:00",
                "delivered_at": None,
                "last_error": None,
                "callback_url": "https://example.com/callback",
                "callback_headers": {},
                "created_at": "2026-07-27T00:00:00+00:00",
                "updated_at": "2026-07-27T00:00:00+00:00",
            },
        )

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))
    delivery = client.retry_delivery(7)

    assert delivery["status"] == "pending"


def test_download_artifact() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/runs/run_1/artifacts/art_123"
        return httpx.Response(200, content=b"artifact-bytes")

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))
    content = client.download_artifact("run_1", "art_123")

    assert content == b"artifact-bytes"


def test_download_file_quotes_relative_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.raw_path == b"/v1/runs/run_1/files/output/%E9%9A%8F%E6%9C%BA%20data%23final%3F.xlsx"
        return httpx.Response(200, content=b"file-bytes")

    client = HAASClient("https://haas.example", transport=httpx.MockTransport(handler))
    content = client.download_file("run_1", "output/随机 data#final?.xlsx")

    assert content == b"file-bytes"
