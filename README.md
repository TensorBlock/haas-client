# HAAS Client SDKs

Public client SDKs for TensorBlock HAAS.

HAAS exposes agent harness runtimes through an HTTP API. This repository contains
language-specific SDKs that wrap the run, polling, artifact, event, delivery,
and extension APIs for application backends and pipelines.

## SDKs

- Python: [`python/`](python/)

Future SDKs can live beside it, for example:

```text
python/
typescript/
go/
java/
```

## Python Install

```bash
pip install 'git+https://github.com/TensorBlock/haas-client.git#subdirectory=python'
```

## Python Quick Start

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
    raise_on_failure=True,
)

print(run["result"]["final_message"])
print([artifact for artifact in run["result"]["artifacts"] if artifact["type"] == "worklog"])
```

## Production Acceptance

The Python SDK includes a fanout acceptance script for pipeline releases:

```bash
export HAAS_API_TOKEN="..."
haas-fanout-acceptance \
  --base-url https://haas-api-production.up.railway.app \
  --agents codex,claude-code,grok \
  --groups 1 \
  --worklog
```
