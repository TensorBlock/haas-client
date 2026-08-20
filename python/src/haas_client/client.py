from __future__ import annotations

from email.utils import parsedate_to_datetime
import json
import mimetypes
import random
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

import httpx


TERMINAL_STATUSES = {"cancelled", "succeeded", "failed", "timed_out"}


class HAASAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str, response_text: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


class HAASTimeoutError(TimeoutError):
    pass


class HAASRunFailedError(RuntimeError):
    def __init__(self, record: Mapping[str, Any]) -> None:
        message = f"HAAS run {record.get('run_id')} finished with status={record.get('status')}"
        error = record.get("error")
        if error:
            message = f"{message}: {error}"
        super().__init__(message)
        self.record = dict(record)


class HAASManyRunsFailedError(RuntimeError):
    def __init__(self, records: Sequence[Mapping[str, Any]]) -> None:
        self.records = [dict(record) for record in records]
        summary = ", ".join(
            f"{record.get('run_id')}={record.get('status')}" for record in self.records
        )
        super().__init__(f"HAAS fanout runs failed: {summary}")


class HAASPartialBatchError(RuntimeError):
    def __init__(self, *, created_runs: Sequence[Mapping[str, Any]], failed_index: int, cause: BaseException) -> None:
        self.created_runs = [dict(item) for item in created_runs]
        self.failed_index = failed_index
        self.cause = cause
        run_ids = ", ".join(str(item.get("run_id")) for item in self.created_runs) or "none"
        super().__init__(
            f"HAAS fanout submit failed at index {failed_index}; "
            f"created run ids before failure: {run_ids}; cause: {cause}"
        )


class HAASClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        request_timeout_seconds: float = 30.0,
        max_retries: int = 3,
        retry_base_seconds: float = 0.5,
        retry_max_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.max_retries = max(0, max_retries)
        self.retry_base_seconds = max(0.0, retry_base_seconds)
        self.retry_max_seconds = max(0.0, retry_max_seconds)
        self._client = httpx.Client(timeout=request_timeout_seconds, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HAASClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def list_agents(self) -> list[str]:
        payload = self._request("GET", "/v1/agents")
        return list(payload["agents"])

    def list_agent_health(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/agents/health")

    def list_skills(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/extensions/skills")

    def get_skill(self, ref: str, *, version: str = "latest") -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/extensions/skills/{quote(ref, safe='')}",
            params={"version": version},
        )

    def get_system_status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/system/status")

    def cleanup_completed_runs(
        self,
        *,
        older_than_days: int | None = None,
        limit: int | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"dry_run": str(dry_run).lower()}
        if older_than_days is not None:
            params["older_than_days"] = older_than_days
        if limit is not None:
            params["limit"] = limit
        return self._request("POST", "/v1/system/cleanup", params=params)

    def upload_file(
        self,
        path: str | Path,
        *,
        tenant_id: str = "internal",
        project_id: str = "default",
        name: str | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        file_path = Path(path)
        upload_name = name or file_path.name
        guessed_content_type = content_type or mimetypes.guess_type(upload_name)[0] or "application/octet-stream"
        data = {
            "tenant_id": tenant_id,
            "project_id": project_id,
        }
        if name is not None:
            data["name"] = name
        with file_path.open("rb") as handle:
            return self._request(
                "POST",
                "/v1/uploads",
                data=data,
                files={"file": (upload_name, handle, guessed_content_type)},
            )

    def document_reference(
        self,
        *,
        url: str,
        ref: str | None = None,
        name: str | None = None,
        content_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        context: dict[str, Any] = {"type": "document_reference", "url": url}
        if ref is not None:
            context["ref"] = ref
        if name is not None:
            context["name"] = name
        if content_type is not None:
            context["content_type"] = content_type
        if metadata:
            context["metadata"] = dict(metadata)
        return context

    def create_run(
        self,
        *,
        agent: str | Mapping[str, Any] = "codex",
        prompt: str,
        tenant_id: str = "internal",
        project_id: str = "default",
        user_id: str | None = None,
        credential_profile: str = "default",
        agent_options: Mapping[str, Any] | None = None,
        context: list[Mapping[str, Any]] | None = None,
        extensions: list[Mapping[str, Any]] | None = None,
        tools: Mapping[str, Any] | None = None,
        memory: Mapping[str, Any] | None = None,
        delivery: Mapping[str, Any] | None = None,
        timeout_seconds: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
        worklog: bool | None = None,
        idempotency_key: str | None = None,
        document_references: list[Mapping[str, Any]] | None = None,
        files: list[str | Path] | None = None,
        auto_idempotency_key: bool = True,
    ) -> dict[str, Any]:
        run_options = dict(options or {})
        if worklog is not None:
            run_options["worklog"] = worklog
        run_context = list(context or [])
        if document_references:
            run_context.extend(self._normalize_document_references(document_references))
        payload: dict[str, Any] = {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "input": {"prompt": prompt},
            "context": run_context,
            "extensions": list(extensions or []),
            "metadata": dict(metadata or {}),
        }
        if run_options:
            payload["options"] = run_options
        if user_id is not None:
            payload["user_id"] = user_id
        if timeout_seconds is not None:
            payload["timeout_seconds"] = timeout_seconds
        if tools is not None:
            payload["tools"] = dict(tools)
        if memory is not None:
            payload["memory"] = dict(memory)
        if delivery is not None:
            payload["delivery"] = dict(delivery)

        if isinstance(agent, str):
            payload["agent"] = {
                "type": agent,
                "credential_profile": credential_profile,
            }
        else:
            payload["agent"] = dict(agent)

        if agent_options is not None:
            payload["agent"].setdefault("options", {}).update(dict(agent_options))

        resolved_idempotency_key = idempotency_key
        if resolved_idempotency_key is None and auto_idempotency_key:
            resolved_idempotency_key = f"haas-client:{uuid.uuid4().hex}"
        extra_headers = {"Idempotency-Key": resolved_idempotency_key} if resolved_idempotency_key else None
        if files:
            return self._create_run_with_files(payload, files, extra_headers=extra_headers)
        return self._request("POST", "/v1/runs", json=payload, extra_headers=extra_headers)

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{quote(run_id, safe='')}")

    def get_run_summary(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{quote(run_id, safe='')}/summary")

    def get_worklog(self, run_id: str) -> str:
        return self._request_text("GET", f"/v1/runs/{quote(run_id, safe='')}/worklog")

    def list_runs(
        self,
        *,
        limit: int = 50,
        tenant_id: str | None = None,
        project_id: str | None = None,
        status: str | None = None,
        metadata_key: str | None = None,
        metadata_value: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if tenant_id is not None:
            params["tenant_id"] = tenant_id
        if project_id is not None:
            params["project_id"] = project_id
        if status is not None:
            params["status"] = status
        if metadata_key is not None:
            params["metadata_key"] = metadata_key
        if metadata_value is not None:
            params["metadata_value"] = metadata_value
        return self._request("GET", "/v1/runs", params=params)

    def list_run_group(
        self,
        run_group_id: str,
        *,
        metadata_key: str = "run_group_id",
        limit: int = 50,
        tenant_id: str | None = None,
        project_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.list_runs(
            limit=limit,
            tenant_id=tenant_id,
            project_id=project_id,
            status=status,
            metadata_key=metadata_key,
            metadata_value=run_group_id,
        )

    def get_run_group(
        self,
        run_group_id: str,
        *,
        limit: int = 100,
        tenant_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if tenant_id is not None:
            params["tenant_id"] = tenant_id
        if project_id is not None:
            params["project_id"] = project_id
        return self._request(
            "GET",
            f"/v1/run-groups/{quote(run_group_id, safe='')}",
            params=params,
        )

    def list_events(self, run_id: str, *, after_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/v1/runs/{quote(run_id, safe='')}/events",
            params={"after_id": after_id, "limit": limit},
        )

    def list_deliveries(
        self,
        *,
        run_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if run_id is not None:
            params["run_id"] = run_id
        if status is not None:
            params["status"] = status
        return self._request("GET", "/v1/deliveries", params=params)

    def retry_delivery(self, delivery_id: int) -> dict[str, Any]:
        return self._request("POST", f"/v1/deliveries/{delivery_id}/retry")

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/runs/{quote(run_id, safe='')}/cancel")

    def wait_run(
        self,
        run_id: str,
        *,
        wait_timeout_seconds: float = 600.0,
        poll_interval_seconds: float = 2.0,
        raise_on_failure: bool = False,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + wait_timeout_seconds
        last_record: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            record = self.get_run(run_id)
            last_record = record
            if record["status"] in TERMINAL_STATUSES:
                if raise_on_failure:
                    self.raise_for_run_failure(record)
                return record
            time.sleep(poll_interval_seconds)

        status = last_record["status"] if last_record else "unknown"
        raise HAASTimeoutError(f"Timed out waiting for run {run_id}; last status={status}")

    def run_and_wait(
        self,
        *,
        agent: str | Mapping[str, Any] = "codex",
        prompt: str,
        timeout_seconds: int | None = None,
        wait_timeout_seconds: float = 600.0,
        poll_interval_seconds: float = 2.0,
        idempotency_key: str | None = None,
        raise_on_failure: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        created = self.create_run(
            agent=agent,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            idempotency_key=idempotency_key,
            **kwargs,
        )
        return self.wait_run(
            created["run_id"],
            wait_timeout_seconds=wait_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            raise_on_failure=raise_on_failure,
        )

    def run_many(
        self,
        *,
        agents: Sequence[str | Mapping[str, Any]],
        prompt: str | None = None,
        tenant_id: str = "internal",
        project_id: str = "default",
        user_id: str | None = None,
        credential_profile: str = "default",
        agent_options: Mapping[str, Any] | None = None,
        context: list[Mapping[str, Any]] | None = None,
        extensions: list[Mapping[str, Any]] | None = None,
        tools: Mapping[str, Any] | None = None,
        memory: Mapping[str, Any] | None = None,
        delivery: Mapping[str, Any] | None = None,
        timeout_seconds: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
        worklog: bool | None = None,
        run_group_id: str | None = None,
        auto_run_group_id: bool = True,
        idempotency_key_prefix: str | None = None,
        document_references: list[Mapping[str, Any]] | None = None,
        files: list[str | Path] | None = None,
        auto_idempotency_key: bool = True,
        cancel_on_submit_failure: bool = False,
    ) -> dict[str, Any]:
        """Submit a dynamic fanout batch.

        Each item in ``agents`` may be:
        - an agent type string, e.g. ``"codex"``;
        - an HAAS agent object, e.g. ``{"type": "grok", "options": {...}}``;
        - a run spec with an ``agent`` key plus optional per-run overrides.
        """
        created_runs: list[dict[str, Any]] = []
        generated_idempotency_key_prefix = None
        if idempotency_key_prefix is None and auto_idempotency_key:
            generated_idempotency_key_prefix = f"haas-client:fanout:{uuid.uuid4().hex}"
        shared_metadata = dict(metadata or {})
        resolved_run_group_id = run_group_id or shared_metadata.get("run_group_id")
        if resolved_run_group_id is None and auto_run_group_id:
            resolved_run_group_id = f"haas-client:fanout:{uuid.uuid4().hex}"
        for index, entry in enumerate(agents):
            spec = self._normalize_run_many_entry(entry, index)
            run_prompt = spec.get("prompt", prompt)
            if run_prompt is None:
                raise ValueError("run_many requires a shared prompt or a prompt on every agent spec")

            name = spec.get("name")
            run_metadata = self._merge_mapping(shared_metadata, spec.get("metadata"))
            if resolved_run_group_id is not None:
                run_metadata.setdefault("run_group_id", resolved_run_group_id)
            if name is not None:
                run_metadata.setdefault("fanout_name", name)
            run_agent_options = self._merge_mapping(agent_options, spec.get("agent_options"))
            run_context = self._merge_sequence(context, spec.get("context"))
            run_extensions = self._merge_sequence(extensions, spec.get("extensions"))
            run_document_references = self._merge_sequence(document_references, spec.get("document_references"))
            run_files = self._merge_sequence(files, spec.get("files"))
            run_options = self._merge_mapping(options, spec.get("options"))
            run_worklog = spec.get("worklog", worklog)
            idempotency_key = spec.get("idempotency_key")
            key_prefix = idempotency_key_prefix or generated_idempotency_key_prefix
            if idempotency_key is None and key_prefix:
                idempotency_key = f"{key_prefix}:{index}:{name or 'run'}"

            try:
                created = self.create_run(
                    agent=spec["agent"],
                    prompt=run_prompt,
                    tenant_id=spec.get("tenant_id", tenant_id),
                    project_id=spec.get("project_id", project_id),
                    user_id=spec.get("user_id", user_id),
                    credential_profile=spec.get("credential_profile", credential_profile),
                    agent_options=run_agent_options or None,
                    context=run_context or None,
                    extensions=run_extensions or None,
                    tools=self._merge_mapping(tools, spec.get("tools")) or None,
                    memory=self._merge_mapping(memory, spec.get("memory")) or None,
                    delivery=self._merge_mapping(delivery, spec.get("delivery")) or None,
                    timeout_seconds=spec.get("timeout_seconds", timeout_seconds),
                    metadata=run_metadata,
                    options=run_options or None,
                    worklog=run_worklog,
                    idempotency_key=idempotency_key,
                    document_references=run_document_references or None,
                    files=run_files or None,
                    auto_idempotency_key=auto_idempotency_key,
                )
            except Exception as exc:
                if cancel_on_submit_failure:
                    self._cancel_created_runs_best_effort(created_runs)
                raise HAASPartialBatchError(
                    created_runs=created_runs,
                    failed_index=index,
                    cause=exc,
                ) from exc
            created_runs.append(
                {
                    "name": name,
                    "agent": spec["agent"],
                    "run_id": created["run_id"],
                    "run_group_id": run_metadata.get("run_group_id"),
                    "created": created,
                }
            )
        return self._created_many_result(created_runs)

    def wait_many(
        self,
        runs: Sequence[str | Mapping[str, Any]] | Mapping[str, Any],
        *,
        wait_timeout_seconds: float = 600.0,
        poll_interval_seconds: float = 2.0,
        raise_on_failure: bool = False,
    ) -> dict[str, Any]:
        entries = self._normalize_wait_many_entries(runs)
        pending = {entry["run_id"] for entry in entries}
        records: dict[str, dict[str, Any]] = {}
        deadline = time.monotonic() + wait_timeout_seconds

        while pending and time.monotonic() < deadline:
            for entry in entries:
                run_id = entry["run_id"]
                if run_id not in pending:
                    continue
                record = self.get_run(run_id)
                if record["status"] in TERMINAL_STATUSES:
                    records[run_id] = record
                    pending.remove(run_id)
            if pending:
                time.sleep(poll_interval_seconds)

        if pending:
            pending_list = ", ".join(sorted(pending))
            raise HAASTimeoutError(f"Timed out waiting for HAAS fanout runs: {pending_list}")

        result = self._completed_many_result(entries, records)
        if raise_on_failure and result["failed"]:
            raise HAASManyRunsFailedError([item["record"] for item in result["failed"]])
        return result

    def run_many_and_wait(
        self,
        *,
        agents: Sequence[str | Mapping[str, Any]],
        prompt: str | None = None,
        wait_timeout_seconds: float = 600.0,
        poll_interval_seconds: float = 2.0,
        raise_on_failure: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        created = self.run_many(agents=agents, prompt=prompt, **kwargs)
        return self.wait_many(
            created,
            wait_timeout_seconds=wait_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            raise_on_failure=raise_on_failure,
        )

    def get_logs(self, run_id: str) -> str:
        return self._request_text("GET", f"/v1/runs/{quote(run_id, safe='')}/logs")

    def download_file(self, run_id: str, relative_path: str) -> bytes:
        encoded_path = quote(relative_path.lstrip("/"), safe="/")
        return self._request_bytes("GET", f"/v1/runs/{quote(run_id, safe='')}/files/{encoded_path}")

    def download_artifact(self, run_id: str, artifact_id: str) -> bytes:
        return self._request_bytes(
            "GET",
            f"/v1/runs/{quote(run_id, safe='')}/artifacts/{quote(artifact_id, safe='')}",
        )

    def raise_for_run_failure(self, record: Mapping[str, Any]) -> None:
        if record.get("status") in TERMINAL_STATUSES and record.get("status") != "succeeded":
            raise HAASRunFailedError(record)

    def _headers(self, extra_headers: Mapping[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra_headers:
            headers.update(dict(extra_headers))
        return headers

    def _request(self, method: str, path: str, *, extra_headers: Mapping[str, str] | None = None, **kwargs: Any) -> Any:
        response = self._request_with_retries(method, path, extra_headers=extra_headers, **kwargs)
        self._raise_for_error(response)
        return response.json()

    def _request_text(self, method: str, path: str, *, extra_headers: Mapping[str, str] | None = None, **kwargs: Any) -> str:
        response = self._request_with_retries(method, path, extra_headers=extra_headers, **kwargs)
        self._raise_for_error(response)
        return response.text

    def _request_bytes(self, method: str, path: str, *, extra_headers: Mapping[str, str] | None = None, **kwargs: Any) -> bytes:
        response = self._request_with_retries(method, path, extra_headers=extra_headers, **kwargs)
        self._raise_for_error(response)
        return response.content

    def _request_with_retries(
        self,
        method: str,
        path: str,
        *,
        extra_headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        headers = self._headers(extra_headers)
        retryable = self._request_is_retryable(method, path, headers)
        last_error: httpx.TransportError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.request(method, self._url(path), headers=headers, **kwargs)
            except httpx.TransportError as exc:
                last_error = exc
                if not retryable or attempt >= self.max_retries:
                    raise
                self._sleep_before_retry(attempt)
                continue

            if (
                retryable
                and response.status_code in {408, 425, 429, 500, 502, 503, 504}
                and attempt < self.max_retries
            ):
                self._sleep_before_retry(attempt, response=response)
                continue
            return response
        if last_error is not None:
            raise last_error
        raise RuntimeError("HAAS request retry loop exhausted without a response")

    def _create_run_with_files(
        self,
        payload: Mapping[str, Any],
        files: list[str | Path],
        *,
        extra_headers: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        multipart_files = []
        for path in files:
            file_path = Path(path)
            content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            multipart_files.append(("files", (file_path.name, file_path.read_bytes(), content_type)))
        return self._request(
            "POST",
            "/v1/runs",
            data={"payload": json.dumps(payload, separators=(",", ":"))},
            files=multipart_files,
            extra_headers=extra_headers,
        )

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request_is_retryable(self, method: str, path: str, headers: Mapping[str, str]) -> bool:
        method = method.upper()
        if method in {"GET", "HEAD", "OPTIONS"}:
            return True
        if "Idempotency-Key" in headers:
            return True
        if method == "POST" and path.endswith("/cancel"):
            return True
        if method == "POST" and "/v1/deliveries/" in path and path.endswith("/retry"):
            return True
        return False

    def _sleep_before_retry(self, attempt: int, *, response: httpx.Response | None = None) -> None:
        retry_after = self._retry_after_seconds(response) if response is not None else None
        if retry_after is None:
            cap = self.retry_max_seconds or self.retry_base_seconds
            retry_after = min(cap, self.retry_base_seconds * (2**attempt))
            retry_after = retry_after + random.uniform(0, min(0.25, retry_after))
        if retry_after > 0:
            time.sleep(retry_after)

    def _retry_after_seconds(self, response: httpx.Response | None) -> float | None:
        if response is None:
            return None
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
        try:
            retry_at = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        return max(0.0, retry_at.timestamp() - time.time())

    def _normalize_run_many_entry(self, entry: str | Mapping[str, Any], index: int) -> dict[str, Any]:
        if isinstance(entry, str):
            return {"name": entry, "agent": entry}
        spec = dict(entry)
        if "agent" in spec:
            return spec
        if "type" in spec:
            name = spec.pop("name", spec.get("type"))
            return {"name": name, "agent": spec}
        raise ValueError(f"run_many agent spec at index {index} must be a string, agent object, or run spec")

    def _normalize_wait_many_entries(
        self,
        runs: Sequence[str | Mapping[str, Any]] | Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        if isinstance(runs, Mapping):
            items = runs.get("runs")
            if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
                raise ValueError("wait_many mapping input must contain a runs list")
        else:
            items = runs

        entries: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if isinstance(item, str):
                entries.append({"name": None, "agent": None, "run_id": item})
                continue
            entry = dict(item)
            run_id = entry.get("run_id") or entry.get("created", {}).get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise ValueError(f"wait_many run entry at index {index} is missing run_id")
            entry["run_id"] = run_id
            entries.append(entry)
        return entries

    def _created_many_result(self, runs: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "count": len(runs),
            "run_ids": [item["run_id"] for item in runs],
            "run_group_id": self._common_run_group_id(runs),
            "runs": runs,
        }

    def _cancel_created_runs_best_effort(self, runs: Sequence[Mapping[str, Any]]) -> None:
        for item in runs:
            run_id = item.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                continue
            try:
                self.cancel_run(run_id)
            except Exception:
                continue

    def _completed_many_result(
        self,
        entries: list[dict[str, Any]],
        records: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        runs: list[dict[str, Any]] = []
        succeeded: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for entry in entries:
            record = dict(records[entry["run_id"]])
            item = {
                "name": entry.get("name"),
                "agent": entry.get("agent"),
                "run_id": entry["run_id"],
                "run_group_id": entry.get("run_group_id"),
                "status": record.get("status"),
                "record": record,
            }
            runs.append(item)
            if record.get("status") == "succeeded":
                succeeded.append(item)
            else:
                failed.append(item)
        return {
            "count": len(runs),
            "run_ids": [item["run_id"] for item in runs],
            "run_group_id": self._common_run_group_id(runs),
            "runs": runs,
            "succeeded": succeeded,
            "failed": failed,
        }

    def _common_run_group_id(self, runs: Sequence[Mapping[str, Any]]) -> str | None:
        group_ids = {item.get("run_group_id") for item in runs if item.get("run_group_id") is not None}
        if len(group_ids) == 1:
            value = next(iter(group_ids))
            return value if isinstance(value, str) else None
        return None

    def _merge_mapping(
        self,
        base: Mapping[str, Any] | None,
        override: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        merged = dict(base or {})
        if override:
            merged.update(dict(override))
        return merged

    def _merge_sequence(
        self,
        base: Iterable[Any] | None,
        extra: Iterable[Any] | None,
    ) -> list[Any]:
        merged: list[Any] = []
        for value in (base, extra):
            if value is None:
                continue
            if isinstance(value, (str, bytes, Path)):
                merged.append(value)
            else:
                merged.extend(value)
        return merged

    def _normalize_document_references(self, references: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for index, reference in enumerate(references):
            context = dict(reference)
            context.setdefault("type", "document_reference")
            if context["type"] != "document_reference":
                raise ValueError(f"document reference at index {index} must have type=document_reference")
            if not context.get("url"):
                raise ValueError(f"document reference at index {index} is missing url")
            normalized.append(context)
        return normalized

    def _raise_for_error(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        message = f"HAAS request failed with HTTP {response.status_code}"
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        if detail:
            message = f"{message}: {detail}"
        raise HAASAPIError(response.status_code, message, response.text)
