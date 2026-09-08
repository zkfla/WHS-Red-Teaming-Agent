from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "src" / "redteam_core"


def test_core_has_no_donor_package_imports():
    forbidden_prefixes = (
        "cicd_redteam_agent",
        "finredteam",
        "tools.agent_tools",
        "tools.dynamic_tool",
        "google.genai",
        "boto3",
    )
    violations = []
    for path in CORE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.startswith(forbidden_prefixes):
                    violations.append(f"{path.relative_to(ROOT)}: {name}")
    assert not violations, violations


def test_semantic_code_has_no_hidden_wall_clock_calls():
    violations = []
    for path in CORE.rglob("*.py"):
        if path.name == "clock.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "datetime.now(" in text or "datetime.utcnow(" in text:
            violations.append(str(path.relative_to(ROOT)))
    assert not violations, violations


def test_project_dependencies_are_domain_neutral():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8").casefold()
    forbidden = ["boto3", "github", "google-genai", "httpx", "anthropic", "networkx"]
    assert not [item for item in forbidden if item in pyproject]


def test_core_source_has_no_concrete_donor_domain_vocabulary():
    forbidden_terms = (
        "github",
        "github_token",
        "workflow_run",
        "aws_oidc",
        "androidmanifest",
        "jadx",
        "adb_path",
        "finredteam",
        "cicd_redteam_agent",
    )
    violations = []
    for path in CORE.rglob("*.py"):
        text = path.read_text(encoding="utf-8").casefold()
        for term in forbidden_terms:
            if term in text:
                violations.append(f"{path.relative_to(ROOT)}: {term}")
    assert not violations, violations


def test_core_source_contains_no_known_donor_semantic_vocabulary():
    pattern = re.compile(
        r"github|workflow|\bgha\b|\baws\b|oidc|android|\bapk\b|\badb\b|jadx|"
        r"\baccount\b|transaction|finredteam|cicd_redteam_agent",
        re.IGNORECASE,
    )
    violations = []
    for root in (CORE, ROOT / "src" / "redteam_app"):
        for path in root.rglob("*.py"):
            matches = sorted(
                set(match.group(0) for match in pattern.finditer(path.read_text("utf-8")))
            )
            if matches:
                violations.append(f"{path.relative_to(ROOT)}: {matches}")
    assert not violations, violations


def test_frontier_engine_contains_no_namespaced_relation_type_literals():
    frontier = CORE / "graph" / "frontier.py"
    pattern = re.compile(r"[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)+@[1-9][0-9]*")
    assert not pattern.findall(frontier.read_text(encoding="utf-8"))


def test_candidate_derivation_does_not_depend_on_finding_materialization():
    path = CORE / "investigation" / "candidates.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported_names: set[str] = set()
    referenced_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(
                alias.asname or alias.name.rsplit(".", 1)[-1] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            imported_names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            referenced_names.add(node.id)
    assert "Finding" not in imported_names
    assert "Finding" not in referenced_names


def test_acquisition_scope_cannot_be_narrowed_by_analysis_hints():
    operations = (CORE / "acquisition" / "operations.py").read_text(encoding="utf-8")
    assert "AnalysisHint" not in operations
    assert "hint" not in operations.casefold()


def test_working_memory_cannot_assign_authoritative_session_state():
    source = (CORE / "reasoning" / "working_memory.py").read_text(encoding="utf-8")
    forbidden_assignment = re.compile(r"session\.(knowledge|semantic|candidate_state)\s*=")
    assert not forbidden_assignment.search(source)


def test_validation_core_has_no_llm_network_subprocess_or_external_io_imports():
    forbidden_prefixes = (
        "httpx",
        "requests",
        "urllib",
        "socket",
        "subprocess",
        "openai",
        "anthropic",
        "google.genai",
    )
    violations: list[str] = []
    validation_root = CORE / "validation"
    for path in validation_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.startswith(forbidden_prefixes):
                    violations.append(f"{path.relative_to(ROOT)}: {name}")
    assert not violations, violations


def test_candidate_contract_has_no_validator_allowlist_escape_hatch():
    security = (CORE / "models" / "security.py").read_text(encoding="utf-8")
    tree = ast.parse(security)
    candidate = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SecurityCandidate"
    )
    field_names = {
        node.target.id
        for node in candidate.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert "allowed_validators" not in field_names
    assert "validator_ids" not in field_names


def test_only_execution_runtime_invokes_experiment_executor_effect_method():
    violations: list[str] = []
    for path in CORE.rglob("*.py"):
        if path == CORE / "execution" / "runtime.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "execute":
                violations.append(str(path.relative_to(ROOT)))
    assert not violations, violations


def test_non_execution_core_layers_do_not_import_execution_runtime():
    protected = ("validation", "graph", "investigation", "knowledge", "models", "reasoning")
    violations: list[str] = []
    for directory in protected:
        for path in (CORE / directory).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                module = node.module if isinstance(node, ast.ImportFrom) else None
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
                if module == "redteam_core.execution.runtime" or any(
                    name.startswith("redteam_core.execution.runtime") for name in names
                ):
                    violations.append(str(path.relative_to(ROOT)))
    assert not violations, violations


def test_runtime_persistence_layers_have_no_network_llm_or_subprocess_imports():
    forbidden_prefixes = (
        "httpx",
        "requests",
        "urllib",
        "socket",
        "subprocess",
        "openai",
        "anthropic",
        "google.genai",
    )
    violations: list[str] = []
    for filename in ("checkpoint.py", "journal.py", "finalizer.py", "termination.py"):
        path = CORE / "runtime" / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.startswith(forbidden_prefixes):
                    violations.append(f"{path.relative_to(ROOT)}: {name}")
    assert not violations, violations


def test_checkpoint_persistence_does_not_interpret_domain_state_payload_keys():
    source = (CORE / "runtime" / "checkpoint.py").read_text(encoding="utf-8")
    assert "canonical_payload_json" not in source
    assert "json.loads(" not in source
