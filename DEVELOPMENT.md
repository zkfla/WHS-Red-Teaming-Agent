# Development

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Needs Python 3.10+. A real LLM provider (`LiteLLMClient`) needs the `llm`
extra, or `agent` for that plus the SQLite checkpoint/work-ledger backend
`redteam run` always uses (`pip install -e ".[dev,agent]"` for a full local
dev setup in one command) — kept optional for its dependency footprint, not
a Python-version requirement; `litellm` installs and imports fine on 3.10
too (verified directly) — see `docs/adr/0002-litellm-gateway.md`.
Everything else, including all tests, runs fine on 3.10 with just `.[dev]`.

`redteam` loads a `.env` in the current directory into its own process
environment automatically at startup, best-effort
(`redteam.app.cli._shared.load_dotenv_if_present`) — a real shell/CI-exported
variable always wins over a `.env` value, so a manual `source .env` is
optional, not required. See `.env.example`.

## Running things

```bash
# Interactive: natural-language setup -> Preflight -> run
redteam

# Explicit/scripted, via CLI (prints every event)
python -m redteam.app.cli run examples/engagement.yaml

# Same thing, as a plain script
python examples/minimal_run.py
```

## Tests

```bash
pytest -q                    # everything
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

### Optional browser UI checks

The UI uses local HTML/CSS/JavaScript; there is no frontend build step. Run
`redteam web --engagements-dir data/ui-dev/engagements` and open the printed
loopback URL. Static changes appear on reload; restart for Python changes.

For Chromium interaction and responsive-layout checks:

```bash
python -m pip install playwright
python -m playwright install chromium
pytest -q tests/browser/test_web_ui.py
```

These tests serve static files on a temporary local port and intercept every API
request with explicit fixtures. No LLM, MCP server, or target is called. The
module is skipped when Playwright is not installed. To save preview screenshots,
set `WHS_UI_SCREENSHOTS=data/ui-dev` when running the tests. These screenshots
show synthetic test data, not investigation results.

### Python checks

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

The central storage and runtime are documented in
[Central state](docs/contracts/central-state.md). After plan2d verification,
the CLI and application default to central state with two workers. Select a
specialist profile with `REDTEAM_SPECIALIST_PROFILE` and an explicit shared LLM
request policy with `REDTEAM_LLM_REQUEST_POLICY_JSON`. Missing configuration
fails before execution. `REDTEAM_STATE_DB` selects the central database;
`REDTEAM_MAX_WORKERS` or `run --workers` selects the worker limit. Limits remain
bounded by the existing global, target, resource, ROE and Control admission.
The explicit development assembly uses local LLM/tool/job doubles only.
`open_central_run` selects a separate directory through the same application
assembly. No default production path selects those doubles.

After plan2e cleanup, central assembly also constructs no empty compatibility
Task/Path/Work stores. Its SDK graph registers only MODEL/TOOLS/SETTLE; legacy
recovery nodes and separate domain DB selectors remain confined to explicit
compatibility consumers. Unused public graph visualization/traversal/snapshot
APIs are removed; the internal SQLite/migration record codec and bounded
neighbourhood reads remain. See the current agent guides for these boundaries.

`data/` (gitignored, the whole directory) is where the CLI's filesystem-backed
stores write runtime state when you run it: `data/evidence/<run_id>.jsonl`
(`FilesystemEvidenceStore`), `data/audit/<run_id>.jsonl`
(`FilesystemAuditStore`), `data/approvals/` (`FilesystemApprovalStore`),
`data/orchestration/state.sqlite3` (Mission, investigations, executions, graph,
LLM/network ledgers and original storage references), and separate SDK
checkpoints. Reports under `data/reports/` are exports; `status`, `findings` and
`report` read the current central originals, including suspended/running Runs.
Pass `--state-db` for another database. Execution and resume use central state
only. Old databases are retained for explicit offline migration, never an
alternative runtime. Unspecified resource effects prevent goal success until
cleanup is verified.

## Docker / MCP servers

```bash
cd deploy/compose
docker compose up mcp-example
```

The container exposes a harmless `ping` tool at
`http://localhost:8765/mcp` over MCP Streamable HTTP. Use one of the
Web/API, Mobile, Recon, or Analysis adapters to exercise a real server.
See `deploy/mcp/README.md` for registration and deployment conventions.

Which servers are active comes from `REDTEAM_MCP_SERVERS` (a
comma-separated list of server ids), never from a config file; each
listed id's own settings are resolved from `REDTEAM_MCP_<SERVER_ID>_KIND`,
`_TRANSPORT`, and the corresponding HTTP `_ENDPOINT` or stdio `_COMMAND`.
Bootstrap fails fast when a configured server is missing settings, is
unreachable, or discovers no tools. `REDTEAM_MCP_SERVERS` unset or empty
is not an error: `run`/`tools list`/`tools health` get an honest empty
tool catalog and a `WARNING` on stderr, never a silent
`ExampleMCPAdapter` substitute. The literal value
`REDTEAM_MCP_SERVERS=example` is the one explicit, deliberate way to opt
into that in-process fixture instead.
