"""Read-only load of one Run's stored state for the graph view.

Uses the existing read-only inspection (``CentralStateStore.inspect_run``): it never
creates or upgrades a database, takes no Run ownership, opens no checkpoint, provider or
actor. Failures are a coded ``available: false`` result, not an empty graph.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from redteam.app.web.graph_model import GraphRaw, build_graph
from redteam.core.contracts.execution import TaskExecution
from redteam.orchestration.central_state import CentralStateStore, CentralTransaction
from redteam.orchestration.coordination import CoordinationError, StateConflict

EVENT_WINDOW = 300


def _request_rows(tx: CentralTransaction, kind: str) -> list[dict[str, Any]]:
    rows = tx.db.execute(
        "SELECT payload FROM state_requests WHERE run_id=? AND kind=?", (tx.run_id, kind),
    ).fetchall()
    return [json.loads(row[0]) for row in rows]


def _collect(tx: CentralTransaction) -> GraphRaw:
    mission_id, _ = tx.binding()
    db, run = tx.db, tx.run_id
    activations = db.execute(
        "SELECT instance_id,task_id,owner,launch_stage FROM state_activations "
        "WHERE run_id=? ORDER BY instance_id", (run,),
    ).fetchall()
    judgment = db.execute(
        "SELECT payload FROM state_requests WHERE run_id=? AND kind='planner_termination' "
        "AND identity=?", (run, run),
    ).fetchone()
    counts = db.execute(
        "SELECT json_extract(payload,'$.envelope.event_type'), COUNT(*) FROM state_outbox "
        "WHERE run_id=? GROUP BY 1", (run,),
    ).fetchall()
    events = db.execute(
        "SELECT payload FROM state_outbox WHERE run_id=? ORDER BY sequence DESC LIMIT ?",
        (run, EVENT_WINDOW),
    ).fetchall()
    return GraphRaw(
        run_id=run,
        mission=tx.get_mission(mission_id),
        tasks=tx.list_tasks(),
        executions=[TaskExecution.model_validate_json(row[0]) for row in db.execute(
            "SELECT payload FROM state_executions WHERE run_id=? ORDER BY invocation_id", (run,),
        )],
        activations=[dict(zip(("instance_id", "task_id", "owner", "launch_stage"), row,
                              strict=True)) for row in activations],
        llm_requests=_request_rows(tx, "llm_request"),
        planner_judgment=json.loads(judgment[0]) if judgment else None,
        evidence_count=db.execute(
            "SELECT COUNT(*) FROM state_evidence_refs WHERE run_id=?", (run,),
        ).fetchone()[0],
        event_counts={name: count for name, count in counts if name},
        events=[json.loads(row[0]) for row in events],
        budget_remaining=tx.policy_budget_remaining(),
    )


async def read_graph(path: str | Path, run_id: str) -> dict[str, Any]:
    try:
        state = await CentralStateStore.inspect_run(path, run_id)
        raw = await state.transaction(_collect, write=False)
        return build_graph(raw)
    except (StateConflict, CoordinationError) as exc:
        return {"available": False, "reason": str(exc)[:80], "run_id": run_id}
    except (sqlite3.Error, OSError):
        return {"available": False, "reason": "state_unreadable", "run_id": run_id}
