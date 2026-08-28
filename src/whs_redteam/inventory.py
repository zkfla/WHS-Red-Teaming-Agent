"""SQLite persistence for deterministic crawl inventory."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from whs_redteam.api_schema import merge_query_schemas, merge_schema_nodes
from whs_redteam.models import (
    ActionResult,
    AgentRun,
    AgentRunStatus,
    ApiRequestSchemaRecord,
    DeferredLinkRecord,
    EndpointRecord,
    Finding,
    FindingStatus,
    FormRecord,
    Observation,
    ObservedSchemaNode,
    PageRecord,
    ResourceIdentifier,
    ResourceOwnershipRecord,
    ResourceRecord,
)


class InventoryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._create_schema()

    def __enter__(self) -> InventoryStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS pages (
                run_id TEXT NOT NULL, url TEXT NOT NULL, final_url TEXT NOT NULL,
                depth INTEGER NOT NULL, title TEXT NOT NULL, status_code INTEGER,
                screenshot_path TEXT, discovered_from TEXT, error TEXT,
                observed_at TEXT NOT NULL, PRIMARY KEY (run_id, url)
            );
            CREATE TABLE IF NOT EXISTS forms (
                run_id TEXT NOT NULL, page_url TEXT NOT NULL, action_url TEXT NOT NULL,
                method TEXT NOT NULL, input_names TEXT NOT NULL, input_types TEXT NOT NULL,
                in_scope INTEGER NOT NULL DEFAULT 0, policy_reason TEXT NOT NULL DEFAULT '',
                observed_at TEXT NOT NULL,
                UNIQUE (run_id, page_url, action_url, method, input_names)
            );
            CREATE TABLE IF NOT EXISTS endpoints (
                run_id TEXT NOT NULL, url TEXT NOT NULL, method TEXT NOT NULL,
                resource_type TEXT NOT NULL, source_page TEXT, status_code INTEGER,
                content_type TEXT, observed_at TEXT NOT NULL,
                UNIQUE (run_id, url, method, resource_type)
            );
            CREATE TABLE IF NOT EXISTS endpoint_sources (
                run_id TEXT NOT NULL, url TEXT NOT NULL, method TEXT NOT NULL,
                resource_type TEXT NOT NULL, source_page TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                UNIQUE (run_id, url, method, resource_type, source_page)
            );
            CREATE TABLE IF NOT EXISTS api_request_schemas (
                run_id TEXT NOT NULL, endpoint_url TEXT NOT NULL,
                method TEXT NOT NULL, resource_type TEXT NOT NULL,
                source_page TEXT, query_schema TEXT NOT NULL,
                body_schema TEXT, request_content_type TEXT,
                observed_at TEXT NOT NULL,
                UNIQUE (run_id, endpoint_url, method, resource_type)
            );
            CREATE TABLE IF NOT EXISTS resources (
                run_id TEXT NOT NULL, endpoint_template TEXT NOT NULL,
                method TEXT NOT NULL, resource_type TEXT NOT NULL,
                identifiers TEXT NOT NULL, observed_endpoint_count INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                UNIQUE (run_id, endpoint_template, method, resource_type)
            );
            CREATE TABLE IF NOT EXISTS resource_ownership (
                run_id TEXT NOT NULL, fixture_id TEXT NOT NULL,
                endpoint_template TEXT NOT NULL, target_fingerprint TEXT NOT NULL,
                owner_actor TEXT NOT NULL, non_owner_actors TEXT NOT NULL,
                source TEXT NOT NULL, observed_at TEXT NOT NULL,
                PRIMARY KEY (run_id, fixture_id)
            );
            CREATE TABLE IF NOT EXISTS deferred_links (
                run_id TEXT NOT NULL, source_page TEXT NOT NULL, target_url TEXT NOT NULL,
                reason TEXT NOT NULL, observed_at TEXT NOT NULL,
                UNIQUE (run_id, source_page, target_url)
            );
            CREATE TABLE IF NOT EXISTS observations (
                run_id TEXT NOT NULL, test_id TEXT NOT NULL, category TEXT NOT NULL,
                target_url TEXT NOT NULL, verdict TEXT NOT NULL, severity TEXT NOT NULL,
                title TEXT NOT NULL, summary TEXT NOT NULL, evidence TEXT NOT NULL,
                remediation TEXT, experiment_id TEXT, evidence_hash TEXT,
                catalog_version TEXT NOT NULL DEFAULT 'day3-v1', observed_at TEXT NOT NULL,
                UNIQUE (run_id, test_id, target_url)
            );
            CREATE TABLE IF NOT EXISTS findings (
                finding_id TEXT PRIMARY KEY, dedup_key TEXT NOT NULL UNIQUE,
                validation_id TEXT NOT NULL, category TEXT NOT NULL, test_id TEXT NOT NULL,
                target_url TEXT NOT NULL, status TEXT NOT NULL, severity TEXT NOT NULL,
                title TEXT NOT NULL, summary TEXT NOT NULL, remediation TEXT,
                reproduction_count INTEGER NOT NULL, required_reproductions INTEGER NOT NULL,
                negative_control_passed INTEGER NOT NULL, experiment_ids TEXT NOT NULL,
                evidence_hashes TEXT NOT NULL, first_observed_at TEXT NOT NULL,
                validated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_runs (
                run_id TEXT PRIMARY KEY, engagement_name TEXT NOT NULL,
                config_hash TEXT NOT NULL, status TEXT NOT NULL,
                resume_status TEXT, exploration_run_id TEXT,
                attack_surface_json TEXT, plan_json TEXT,
                validation_id TEXT, exploitability_decision_json TEXT,
                findings_path TEXT, last_error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_actions (
                action_id TEXT PRIMARY KEY, queue_id TEXT, run_id TEXT NOT NULL,
                capability TEXT NOT NULL, target_url TEXT NOT NULL,
                status TEXT NOT NULL, output TEXT NOT NULL,
                error_type TEXT, error_message TEXT,
                started_at TEXT NOT NULL, finished_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS action_queue (
                queue_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, proposal_json TEXT NOT NULL,
                status TEXT NOT NULL, priority INTEGER NOT NULL,
                attempt_count INTEGER NOT NULL, max_attempts INTEGER NOT NULL,
                worker_id TEXT, lease_expires_at TEXT, action_id TEXT,
                approval_verified INTEGER NOT NULL DEFAULT 0,
                approval_digest TEXT,
                last_error TEXT, available_at TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(run_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_action_queue_claim
                ON action_queue(run_id, status, available_at, priority);
            CREATE TABLE IF NOT EXISTS action_dependencies (
                queue_id TEXT NOT NULL,
                depends_on_queue_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(queue_id, depends_on_queue_id),
                FOREIGN KEY(queue_id) REFERENCES action_queue(queue_id),
                FOREIGN KEY(depends_on_queue_id) REFERENCES action_queue(queue_id),
                CHECK(queue_id <> depends_on_queue_id)
            );
            CREATE TABLE IF NOT EXISTS cleanup_tasks (
                cleanup_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                parent_action_id TEXT NOT NULL UNIQUE, parent_queue_id TEXT,
                capability TEXT NOT NULL, backend TEXT NOT NULL,
                tool_name TEXT NOT NULL, arguments_json TEXT NOT NULL,
                status TEXT NOT NULL, attempt_count INTEGER NOT NULL,
                max_attempts INTEGER NOT NULL, worker_id TEXT,
                lease_expires_at TEXT,
                last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cleanup_tasks_run_status
                ON cleanup_tasks(run_id, status, created_at);
            CREATE TABLE IF NOT EXISTS approval_grants (
                token_digest TEXT PRIMARY KEY, approval_id TEXT NOT NULL,
                engagement_name TEXT NOT NULL, issuer TEXT NOT NULL,
                expires_at TEXT NOT NULL, max_uses INTEGER NOT NULL,
                use_count INTEGER NOT NULL DEFAULT 0,
                revoked INTEGER NOT NULL DEFAULT 0, last_used_at TEXT
            );
            CREATE TABLE IF NOT EXISTS run_budgets (
                run_id TEXT PRIMARY KEY, max_requests INTEGER NOT NULL,
                used_requests INTEGER NOT NULL DEFAULT 0,
                requests_per_second INTEGER NOT NULL,
                started_at REAL NOT NULL, deadline REAL NOT NULL,
                recent_requests TEXT NOT NULL DEFAULT '[]', paused_at REAL
            );
            """
        )
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(forms)").fetchall()
        }
        if "in_scope" not in columns:
            self.connection.execute(
                "ALTER TABLE forms ADD COLUMN in_scope INTEGER NOT NULL DEFAULT 0"
            )
        if "policy_reason" not in columns:
            self.connection.execute(
                "ALTER TABLE forms ADD COLUMN policy_reason TEXT NOT NULL DEFAULT ''"
            )
        observation_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(observations)").fetchall()
        }
        if "experiment_id" not in observation_columns:
            self.connection.execute("ALTER TABLE observations ADD COLUMN experiment_id TEXT")
        if "evidence_hash" not in observation_columns:
            self.connection.execute("ALTER TABLE observations ADD COLUMN evidence_hash TEXT")
        if "catalog_version" not in observation_columns:
            self.connection.execute(
                "ALTER TABLE observations ADD COLUMN catalog_version "
                "TEXT NOT NULL DEFAULT 'day3-v1'"
            )
        action_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(agent_actions)").fetchall()
        }
        if "error_type" not in action_columns:
            self.connection.execute("ALTER TABLE agent_actions ADD COLUMN error_type TEXT")
        if "error_message" not in action_columns:
            self.connection.execute("ALTER TABLE agent_actions ADD COLUMN error_message TEXT")
        if "queue_id" not in action_columns:
            self.connection.execute("ALTER TABLE agent_actions ADD COLUMN queue_id TEXT")
        queue_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(action_queue)").fetchall()
        }
        if "approval_verified" not in queue_columns:
            self.connection.execute(
                "ALTER TABLE action_queue ADD COLUMN approval_verified "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "approval_digest" not in queue_columns:
            self.connection.execute("ALTER TABLE action_queue ADD COLUMN approval_digest TEXT")
        cleanup_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(cleanup_tasks)").fetchall()
        }
        if "lease_expires_at" not in cleanup_columns:
            self.connection.execute(
                "ALTER TABLE cleanup_tasks ADD COLUMN lease_expires_at TEXT"
            )
        run_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(agent_runs)").fetchall()
        }
        if "attack_surface_json" not in run_columns:
            self.connection.execute(
                "ALTER TABLE agent_runs ADD COLUMN attack_surface_json TEXT"
            )
        if "exploitability_decision_json" not in run_columns:
            self.connection.execute(
                "ALTER TABLE agent_runs ADD COLUMN exploitability_decision_json TEXT"
            )
        budget_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(run_budgets)").fetchall()
        }
        if "paused_at" not in budget_columns:
            self.connection.execute("ALTER TABLE run_budgets ADD COLUMN paused_at REAL")
        self.connection.commit()

    def save_agent_run(self, run: AgentRun) -> None:
        self.connection.execute(
            """
            INSERT INTO agent_runs (
                run_id, engagement_name, config_hash, status, resume_status,
                exploration_run_id, attack_surface_json, plan_json, validation_id,
                exploitability_decision_json, findings_path, last_error,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                engagement_name=excluded.engagement_name,
                config_hash=excluded.config_hash,
                status=excluded.status,
                resume_status=excluded.resume_status,
                exploration_run_id=excluded.exploration_run_id,
                attack_surface_json=excluded.attack_surface_json,
                plan_json=excluded.plan_json,
                validation_id=excluded.validation_id,
                exploitability_decision_json=excluded.exploitability_decision_json,
                findings_path=excluded.findings_path,
                last_error=excluded.last_error,
                updated_at=excluded.updated_at
            """,
            (
                run.run_id, run.engagement_name, run.config_hash, run.status.value,
                run.resume_status.value if run.resume_status else None,
                run.exploration_run_id, run.attack_surface_json, run.plan_json,
                run.validation_id, run.exploitability_decision_json,
                run.findings_path, run.last_error,
                run.created_at.isoformat(), run.updated_at.isoformat(),
            ),
        )
        self.connection.commit()

    def get_agent_run(self, run_id: str) -> AgentRun | None:
        row = self.connection.execute(
            "SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["status"] = AgentRunStatus(data["status"])
        if data["resume_status"]:
            data["resume_status"] = AgentRunStatus(data["resume_status"])
        return AgentRun.model_validate(data)

    def save_action_result(self, result: ActionResult) -> None:
        self.connection.execute(
            """
            INSERT INTO agent_actions (
                action_id, queue_id, run_id, capability, target_url, status, output,
                error_type, error_message, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(action_id) DO NOTHING
            """,
            (
                result.action_id,
                result.queue_id,
                result.run_id,
                result.capability.value,
                result.target_url,
                result.status.value,
                json.dumps(result.output, ensure_ascii=False, sort_keys=True),
                result.error_type,
                result.error_message,
                result.started_at.isoformat(),
                result.finished_at.isoformat(),
            ),
        )
        self.connection.commit()

    def list_action_results(self, run_id: str) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT * FROM agent_actions WHERE run_id = ?
            ORDER BY started_at, action_id
            """,
            (run_id,),
        ).fetchall()
        return [{**dict(row), "output": json.loads(row["output"])} for row in rows]

    def queue_counts(self, run_id: str) -> dict[str, int]:
        rows = self.connection.execute(
            """
            SELECT status, COUNT(*) AS count FROM action_queue
            WHERE run_id = ? GROUP BY status ORDER BY status
            """,
            (run_id,),
        ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def save_page(self, run_id: str, page: PageRecord) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, page.url, page.final_url, page.depth, page.title,
                page.status_code, page.screenshot_path, page.discovered_from,
                page.error, page.observed_at.isoformat(),
            ),
        )
        self.connection.commit()

    def save_form(self, run_id: str, form: FormRecord) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO forms (
                run_id, page_url, action_url, method, input_names, input_types,
                in_scope, policy_reason, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, form.page_url, form.action_url, form.method,
                json.dumps(form.input_names), json.dumps(form.input_types),
                int(form.in_scope), form.policy_reason,
                form.observed_at.isoformat(),
            ),
        )
        self.connection.commit()

    def save_deferred_link(self, run_id: str, link: DeferredLinkRecord) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO deferred_links VALUES (?, ?, ?, ?, ?)",
            (
                run_id,
                link.source_page,
                link.target_url,
                link.reason,
                link.observed_at.isoformat(),
            ),
        )
        self.connection.commit()

    def save_endpoint(self, run_id: str, endpoint: EndpointRecord) -> None:
        self.connection.execute(
            """
            INSERT INTO endpoints VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, url, method, resource_type) DO UPDATE SET
                status_code=excluded.status_code,
                content_type=excluded.content_type,
                observed_at=excluded.observed_at
            """,
            (
                run_id, endpoint.url, endpoint.method, endpoint.resource_type,
                endpoint.source_page, endpoint.status_code, endpoint.content_type,
                endpoint.observed_at.isoformat(),
            ),
        )
        if endpoint.source_page:
            self.connection.execute(
                "INSERT OR IGNORE INTO endpoint_sources VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    endpoint.url,
                    endpoint.method,
                    endpoint.resource_type,
                    endpoint.source_page,
                    endpoint.observed_at.isoformat(),
                ),
            )
        self.connection.commit()

    def list_endpoints(self, run_id: str) -> list[EndpointRecord]:
        rows = self.connection.execute(
            """
            SELECT * FROM endpoints WHERE run_id = ?
            ORDER BY url, method, resource_type
            """,
            (run_id,),
        ).fetchall()
        return [
            EndpointRecord(
                url=row["url"],
                method=row["method"],
                resource_type=row["resource_type"],
                source_page=row["source_page"],
                status_code=row["status_code"],
                content_type=row["content_type"],
                observed_at=datetime.fromisoformat(row["observed_at"]),
            )
            for row in rows
        ]

    def save_api_request_schema(
        self, run_id: str, record: ApiRequestSchemaRecord
    ) -> None:
        existing = self.connection.execute(
            """
            SELECT * FROM api_request_schemas
            WHERE run_id = ? AND endpoint_url = ? AND method = ?
              AND resource_type = ?
            """,
            (
                run_id,
                record.endpoint_url,
                record.method,
                record.resource_type,
            ),
        ).fetchone()
        query_schema = record.query_schema
        body_schema = record.body_schema
        source_page = record.source_page
        request_content_type = record.request_content_type
        if existing is not None:
            previous_query = {
                name: self._schema_node(value)
                for name, value in json.loads(existing["query_schema"]).items()
            }
            query_schema = merge_query_schemas(previous_query, query_schema)
            if existing["body_schema"]:
                previous_body = self._schema_node(
                    json.loads(existing["body_schema"])
                )
                body_schema = (
                    merge_schema_nodes(previous_body, body_schema)
                    if body_schema is not None
                    else previous_body
                )
            source_page = source_page or existing["source_page"]
            request_content_type = (
                request_content_type or existing["request_content_type"]
            )
        self.connection.execute(
            """
            INSERT INTO api_request_schemas (
                run_id, endpoint_url, method, resource_type, source_page,
                query_schema, body_schema, request_content_type, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, endpoint_url, method, resource_type) DO UPDATE SET
                source_page=excluded.source_page,
                query_schema=excluded.query_schema,
                body_schema=excluded.body_schema,
                request_content_type=excluded.request_content_type,
                observed_at=excluded.observed_at
            """,
            (
                run_id,
                record.endpoint_url,
                record.method,
                record.resource_type,
                source_page,
                json.dumps(
                    {
                        name: node.model_dump(mode="json")
                        for name, node in query_schema.items()
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                (
                    json.dumps(
                        body_schema.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    if body_schema is not None
                    else None
                ),
                request_content_type,
                record.observed_at.isoformat(),
            ),
        )
        self.connection.commit()

    def list_api_request_schemas(
        self, run_id: str
    ) -> list[ApiRequestSchemaRecord]:
        rows = self.connection.execute(
            """
            SELECT * FROM api_request_schemas WHERE run_id = ?
            ORDER BY endpoint_url, method, resource_type
            """,
            (run_id,),
        ).fetchall()
        return [
            ApiRequestSchemaRecord(
                endpoint_url=row["endpoint_url"],
                method=row["method"],
                resource_type=row["resource_type"],
                source_page=row["source_page"],
                query_schema={
                    name: self._schema_node(value)
                    for name, value in json.loads(row["query_schema"]).items()
                },
                body_schema=(
                    self._schema_node(json.loads(row["body_schema"]))
                    if row["body_schema"]
                    else None
                ),
                request_content_type=row["request_content_type"],
                observed_at=datetime.fromisoformat(row["observed_at"]),
            )
            for row in rows
        ]

    def api_request_schema_count(self, run_id: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM api_request_schemas WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["count"])

    def replace_resources(
        self, run_id: str, resources: list[ResourceRecord]
    ) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM resources WHERE run_id = ?", (run_id,)
            )
            self.connection.executemany(
                """
                INSERT INTO resources (
                    run_id, endpoint_template, method, resource_type,
                    identifiers, observed_endpoint_count, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        resource.endpoint_template,
                        resource.method,
                        resource.resource_type,
                        json.dumps(
                            [
                                item.model_dump(mode="json")
                                for item in resource.identifiers
                            ],
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        resource.observed_endpoint_count,
                        resource.observed_at.isoformat(),
                    )
                    for resource in resources
                ],
            )

    def list_resources(self, run_id: str) -> list[ResourceRecord]:
        rows = self.connection.execute(
            """
            SELECT * FROM resources WHERE run_id = ?
            ORDER BY endpoint_template, method, resource_type
            """,
            (run_id,),
        ).fetchall()
        return [
            ResourceRecord(
                endpoint_template=row["endpoint_template"],
                method=row["method"],
                resource_type=row["resource_type"],
                identifiers=tuple(
                    ResourceIdentifier.model_validate(item)
                    for item in json.loads(row["identifiers"])
                ),
                observed_endpoint_count=row["observed_endpoint_count"],
                observed_at=datetime.fromisoformat(row["observed_at"]),
            )
            for row in rows
        ]

    def resource_count(self, run_id: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM resources WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["count"])

    def replace_resource_ownership(
        self,
        run_id: str,
        records: list[ResourceOwnershipRecord],
    ) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM resource_ownership WHERE run_id = ?", (run_id,)
            )
            self.connection.executemany(
                """
                INSERT INTO resource_ownership (
                    run_id, fixture_id, endpoint_template, target_fingerprint,
                    owner_actor, non_owner_actors, source, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        record.fixture_id,
                        record.endpoint_template,
                        record.target_fingerprint,
                        record.owner_actor,
                        json.dumps(record.non_owner_actors),
                        record.source,
                        record.observed_at.isoformat(),
                    )
                    for record in records
                ],
            )

    def list_resource_ownership(
        self, run_id: str
    ) -> list[ResourceOwnershipRecord]:
        rows = self.connection.execute(
            """
            SELECT * FROM resource_ownership WHERE run_id = ?
            ORDER BY fixture_id
            """,
            (run_id,),
        ).fetchall()
        return [
            ResourceOwnershipRecord(
                fixture_id=row["fixture_id"],
                endpoint_template=row["endpoint_template"],
                target_fingerprint=row["target_fingerprint"],
                owner_actor=row["owner_actor"],
                non_owner_actors=tuple(json.loads(row["non_owner_actors"])),
                source=row["source"],
                observed_at=datetime.fromisoformat(row["observed_at"]),
            )
            for row in rows
        ]

    def resource_ownership_count(self, run_id: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM resource_ownership WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["count"])

    @staticmethod
    def _schema_node(value: dict[str, object]) -> ObservedSchemaNode:
        return ObservedSchemaNode.model_validate(value)

    def counts(self, run_id: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for table in ("pages", "forms", "endpoints", "deferred_links"):
            row = self.connection.execute(
                f"SELECT COUNT(*) AS count FROM {table} WHERE run_id = ?",  # noqa: S608
                (run_id,),
            ).fetchone()
            result[table] = int(row["count"])
        return result

    def save_observation(self, run_id: str, observation: Observation) -> None:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO observations (
                run_id, test_id, category, target_url, verdict, severity, title,
                summary, evidence, remediation, experiment_id, evidence_hash,
                catalog_version, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                observation.test_id,
                observation.category,
                observation.target_url,
                observation.verdict.value,
                observation.severity.value,
                observation.title,
                observation.summary,
                json.dumps(observation.evidence, ensure_ascii=False, sort_keys=True),
                observation.remediation,
                observation.experiment_id,
                observation.evidence_hash,
                observation.catalog_version,
                observation.observed_at.isoformat(),
            ),
        )
        self.connection.commit()

    def observation_count(self, run_id: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM observations WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["count"])

    def list_observations(self, run_id: str) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT test_id, category, target_url, verdict, severity, title, summary,
                   evidence, remediation, experiment_id, evidence_hash,
                   catalog_version, observed_at
            FROM observations WHERE run_id = ? ORDER BY target_url, test_id
            """,
            (run_id,),
        ).fetchall()
        return [
            {
                **dict(row),
                "evidence": json.loads(row["evidence"]),
            }
            for row in rows
        ]

    def latest_page_urls(self, limit: int) -> list[str]:
        latest = self.connection.execute(
            "SELECT run_id FROM pages GROUP BY run_id ORDER BY MAX(observed_at) DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            return []
        rows = self.connection.execute(
            """
            SELECT final_url FROM pages
            WHERE run_id = ? AND error IS NULL
            ORDER BY depth, final_url LIMIT ?
            """,
            (latest["run_id"], limit),
        ).fetchall()
        return [str(row["final_url"]) for row in rows if "[REDACTED]" not in row["final_url"]]

    def page_urls(self, run_id: str, limit: int) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT final_url FROM pages
            WHERE run_id = ? AND error IS NULL
            ORDER BY depth, final_url LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()
        return [
            str(row["final_url"])
            for row in rows
            if "[REDACTED]" not in row["final_url"]
        ]

    def save_finding(self, validation_id: str, finding: Finding) -> None:
        existing = self.connection.execute(
            "SELECT * FROM findings WHERE dedup_key = ?", (finding.dedup_key,)
        ).fetchone()
        experiment_ids = set(finding.experiment_ids)
        evidence_hashes = set(finding.evidence_hashes)
        first_observed_at = finding.first_observed_at
        status = finding.status
        severity = finding.severity.value
        reproduction_count = finding.reproduction_count
        negative_control_passed = finding.negative_control_passed
        if existing is not None:
            experiment_ids.update(json.loads(existing["experiment_ids"]))
            evidence_hashes.update(json.loads(existing["evidence_hashes"]))
            first_observed_at = min(
                first_observed_at,
                datetime.fromisoformat(existing["first_observed_at"]),
            )
            if existing["status"] == FindingStatus.CONFIRMED.value:
                status = FindingStatus.CONFIRMED
            severity_order = {"informational": 0, "low": 1, "medium": 2, "high": 3}
            if severity_order[existing["severity"]] > severity_order[severity]:
                severity = existing["severity"]
            reproduction_count = max(
                reproduction_count, int(existing["reproduction_count"])
            )
            negative_control_passed = (
                negative_control_passed or bool(existing["negative_control_passed"])
            )
        self.connection.execute(
            """
            INSERT INTO findings VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(dedup_key) DO UPDATE SET
                validation_id=excluded.validation_id,
                status=excluded.status,
                severity=excluded.severity,
                title=excluded.title,
                summary=excluded.summary,
                remediation=excluded.remediation,
                reproduction_count=excluded.reproduction_count,
                required_reproductions=excluded.required_reproductions,
                negative_control_passed=excluded.negative_control_passed,
                experiment_ids=excluded.experiment_ids,
                evidence_hashes=excluded.evidence_hashes,
                first_observed_at=excluded.first_observed_at,
                validated_at=excluded.validated_at
            """,
            (
                finding.finding_id,
                finding.dedup_key,
                validation_id,
                finding.category,
                finding.test_id,
                finding.target_url,
                status.value,
                severity,
                finding.title,
                finding.summary,
                finding.remediation,
                reproduction_count,
                finding.required_reproductions,
                int(negative_control_passed),
                json.dumps(sorted(experiment_ids)),
                json.dumps(sorted(evidence_hashes)),
                first_observed_at.isoformat(),
                finding.validated_at.isoformat(),
            ),
        )
        self.connection.commit()

    def list_findings(self, validation_id: str | None = None) -> list[dict[str, object]]:
        if validation_id is None:
            rows = self.connection.execute(
                "SELECT * FROM findings ORDER BY target_url, test_id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM findings WHERE validation_id = ? ORDER BY target_url, test_id",
                (validation_id,),
            ).fetchall()
        return [
            {
                **dict(row),
                "negative_control_passed": bool(row["negative_control_passed"]),
                "experiment_ids": json.loads(row["experiment_ids"]),
                "evidence_hashes": json.loads(row["evidence_hashes"]),
            }
            for row in rows
        ]
