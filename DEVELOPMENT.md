# Development

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Needs Python 3.10+. `LiteLLMClient` additionally needs 3.11+ and the
`llm` extra (`pip install -e ".[llm]"`) — see
`docs/adr/0002-litellm-gateway.md`. Everything else, including all
tests, runs fine on 3.10.

## Running things

```bash
# Minimal vertical slice, via CLI (prints every event)
python -m redteam.app.cli run examples/engagement.yaml

# Same thing, as a plain script
python examples/minimal_run.py
```

## Tests

```bash
pytest -q                    # everything (55 tests as of this stage)
pytest tests/unit -q         # Scope/Event/Graph/MCP-adapter/dependency-rule/ID-enum/conversion/config/port-contract tests
pytest tests/contract -q     # Pydantic schema validation
pytest tests/integration -q  # full Agent -> Control -> Capability -> Evidence -> Event -> Graph flow
```

`tests/fakes/` holds test doubles for every `core` port — reach for
those (or `tests.fakes.build_test_application()`) instead of the real
`redteam.control`/`redteam.capability`/`redteam.evidence`
implementations when a test only needs *some* response, not a
correctly-evaluated one.

`pytest-asyncio` is configured with `asyncio_mode = "auto"`
(`pyproject.toml`) — `async def test_...` functions run without any
extra marker.

## Lint / types

```bash
ruff check src tests examples
ruff check --fix src tests examples   # autofix what's fixable
mypy src/redteam tests
```

`ruff` config: `[tool.ruff]` in `pyproject.toml` (line-length 100,
`E`/`F`/`I`/`UP`/`B` rule sets). `mypy` config: `[tool.mypy]`
(`ignore_missing_imports = true` since `langgraph`/`litellm` stubs are
incomplete; `disallow_untyped_defs = false` for now — consider
tightening once the skeleton fills in).

## Local data

`data/evidence/` (gitignored) is where `FilesystemEvidenceStore`'s
default path writes `<run_id>.jsonl` files when you run the CLI/example
script. Safe to delete between runs.

## Docker / MCP servers

```bash
cd deploy/compose
docker compose up mcp-example
```

The container exposes a harmless `ping` tool at
`http://localhost:8765/mcp` over MCP Streamable HTTP. The default
vertical slice still uses `ExampleMCPAdapter` in-process; use
`WebAPIMCPAdapter` or `ReconMCPAdapter` to exercise the real endpoint.
See `deploy/mcp/README.md` for registration and deployment conventions.
