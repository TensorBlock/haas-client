# HAAS Python SDK

Install directly from the public client repository:

```bash
pip install 'git+https://github.com/TensorBlock/haas-client.git#subdirectory=python'
```

Usage:

```python
from haas_client import HAASClient

client = HAASClient(
    base_url="https://haas.example.com",
    token="YOUR_HAAS_API_TOKEN",
)

run = client.run_and_wait(
    agent="codex",
    prompt="Hello world. Reply with a short greeting.",
    worklog=True,
    timeout_seconds=120,
    idempotency_key="pipeline-run-123:step-1",
    raise_on_failure=True,
)

print(run["result"]["final_message"])
print([artifact for artifact in run["result"]["artifacts"] if artifact["type"] == "worklog"])
```

For polling systems that only need the final message and artifact list, use the
lighter summary endpoint:

```python
summary = client.get_run_summary(run["run_id"])
print(summary["final_message"])
```

Attach reusable sandbox skills:

```python
skills = client.list_skills()
assert any(skill["ref"] == "spreadsheet-analysis" for skill in skills)
client.get_skill("spreadsheet-analysis", version="0.1.0")

run = client.run_and_wait(
    agent="codex",
    prompt="Analyze the attached workbook.",
    files=["input.xlsx"],
    extensions=[
        {"type": "skill", "ref": "spreadsheet-analysis", "version": "0.1.0"},
    ],
    raise_on_failure=True,
)
```

Fan out one task to a dynamic set of agents:

```python
batch = client.run_many_and_wait(
    agents=[
        "codex",
        {"name": "claude", "agent": "claude-code"},
        {
            "name": "grok-deep",
            "agent": {"type": "grok", "options": {"model": "grok-4.5"}},
            "metadata": {"lane": "deep"},
            "extensions": [{"type": "skill", "ref": "grok-reporting", "version": "0.1.0"}],
        },
    ],
    prompt="Run this pipeline task and return the final deliverables.",
    extensions=[{"type": "skill", "ref": "common-pipeline", "version": "1.0.0"}],
    project_id="pipeline",
    worklog=True,
    idempotency_key_prefix="pipeline-run-123:fanout",
    timeout_seconds=1800,
)

for item in batch["runs"]:
    print(item["name"], item["status"], item["run_id"])
    print((item["record"].get("result") or {}).get("final_message"))
```

`run_many()` submits the runs and returns run ids immediately. `wait_many()`
accepts either that return value or a list of run ids. Each agent entry can
override shared fields such as `prompt`, `metadata`, `timeout_seconds`,
`tools`, `context`, `extensions`, `files`, `options`, `worklog`, and
`agent_options`; HAAS itself still only sees ordinary single-run API calls.

Set `worklog=True` when a run should return a generated `worklog.md` artifact.
The worklog records the run metadata, lifecycle timeline, final message, error,
and artifact list. You can also read the dynamic worklog view directly:

```python
worklog = client.get_worklog(run["run_id"])
```

The client automatically sends an idempotency key for `create_run()` and
`run_many()` calls when one is not provided, so retryable HTTP failures can be
retried without duplicating runs. For pipeline replay across process restarts,
pass a stable `idempotency_key` or `idempotency_key_prefix`.

If fanout submission fails after some runs were already created, the client
raises `HAASPartialBatchError` with the created run ids. Set
`cancel_on_submit_failure=True` when a partial fanout should be cancelled rather
than allowed to continue.

Use `metadata.run_group_id` as the pipeline observability contract when a group
of single-run requests belong to one fanout. HAAS can list that group directly:

```python
runs = client.list_run_group("pipeline-run-123", project_id="pipeline")
print([run["status"] for run in runs])
```

Run the production fanout acceptance script before wiring HAAS into a pipeline
release:

```bash
export HAAS_API_TOKEN="..."
haas-fanout-acceptance \
  --base-url https://haas-api-production.up.railway.app \
  --agents codex,claude-code,grok \
  --groups 1 \
  --worklog
```

Increase `--groups` to submit multiple fanout groups in one acceptance run.
When running from a source checkout instead of an installed package, use
`python python/examples/fanout_acceptance.py` from the repository root.

Pass local files as run input:

```python
run = client.run_and_wait(
    agent="claude-code",
    prompt="Read the attached spreadsheet and summarize it.",
    files=["input.xlsx"],
    raise_on_failure=True,
)
```

`upload_file()` remains available when a pipeline wants to upload once and reuse
the returned context object across multiple runs.

Chain a previous run artifact into another run:

```python
artifact = run["result"]["artifacts"][0]

next_run = client.run_and_wait(
    agent="claude-code",
    prompt="Use the attached previous output and produce a concise summary.",
    context=[
        {
            "type": "run_artifact",
            "run_id": run["run_id"],
            "artifact_id": artifact["artifact_id"],
            "name": artifact["name"],
        }
    ],
    raise_on_failure=True,
)
```

Webhook completion:

```python
client.create_run(
    agent="claude-code",
    prompt="Run a long analysis task.",
    delivery={
        "mode": "webhook",
        "callback_url": "https://pipeline.example.com/haas/callback",
    },
    idempotency_key="pipeline-run-123:step-2",
)
```

Events:

```python
events = client.list_events("run_...")
```

Delivery state:

```python
deliveries = client.list_deliveries(run_id="run_...")

if deliveries and deliveries[0]["status"] == "exhausted":
    client.retry_delivery(deliveries[0]["id"])
```

Internal operations:

```python
health = client.list_agent_health()
status = client.get_system_status()
cleanup_plan = client.cleanup_completed_runs(older_than_days=14, dry_run=True)
```

The client is intentionally small. It wraps the HAAS run, polling, delivery,
event, artifact, and internal operations endpoints and returns raw response
dictionaries so pipeline code can choose its own data model.
