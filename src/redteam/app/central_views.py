"""Read-only central consumers. No checkpoint, provider or recovery is opened."""

import json

from redteam.app.bootstrap import build_central_graph
from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import AgentPhase, MissionStatus
from redteam.core.ids import EngagementId, RunId
from redteam.evidence.stores.filesystem import EvidenceReadSnapshot, FilesystemEvidenceStore
from redteam.orchestration.central_state import CentralStateStore
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.reporting import ReportingAgent
from redteam.orchestration.state import RuntimeState


async def inspect_central_run(path, run_id):
    state = await CentralStateStore.inspect_run(path, run_id)
    def read(tx):
        mission = tx.get_mission(tx.binding()[0])
        row = tx.db.execute(
            "SELECT payload FROM state_requests WHERE run_id=? AND kind='run_configuration' "
            "AND identity=?", (tx.run_id, tx.run_id),
        ).fetchone()
        if row is None:
            raise StateConflict(
                "central_original_configuration_missing_requires_explicit_preparation"
            )
        return mission, json.loads(row[0]), tx.active_activation_count(), tx.planning_diagnostics()
    mission, config, active_actors, (diagnostic, diagnostic_history) = await state.transaction(
        read, write=False,
    )
    evidence = await FilesystemEvidenceStore.inspect_run(config["storage_refs"]["evidence"],
                                                         RunId(str(run_id)))
    if evidence is None:
        has_refs = await state.transaction(lambda tx: tx.db.execute(
            "SELECT 1 FROM state_evidence_refs WHERE run_id=? LIMIT 1", (tx.run_id,),
        ).fetchone() is not None, write=False)
        if has_refs:
            raise StateConflict("central_original_evidence_inventory_missing")
        evidence = EvidenceReadSnapshot([])
    runtime = RuntimeState(
        run_id=RunId(str(run_id)), engagement_id=EngagementId(state.engagement_id),
        mission_id=mission.mission_id, current_goal=mission.objective,
        started_at=mission.created_at,
        phase=(AgentPhase.OBSERVE if active_actors else AgentPhase.FAILED
               if mission.termination_reason == "planning_error" else AgentPhase.FINISHED
               if mission.termination_reason or mission.status in {
                   MissionStatus.COMPLETED, MissionStatus.CANCELLED, MissionStatus.FAILED,
               } else AgentPhase.OBSERVE),
        termination_reason=None if active_actors else mission.termination_reason,
        mission_status=mission.status,
        planning_diagnostic=diagnostic, planning_diagnostic_history=diagnostic_history,
    )
    tasks = await state.list_tasks()
    report = await ReportingAgent().project(
        runtime, graph=AttackGraphService(build_central_graph(state)), evidence_store=evidence,
        work_store=None, task_store=state, central_state=state,
        required_hypothesis_ids={str(h) for task in tasks if task.required
                                 for h in task.related_hypothesis_ids},
    )
    report.effective_limits = config["effective_limits"]
    report.feature_availability = config.get("code_features", {
        "code_artifacts": "disabled", "code_fixtures": "disabled",
    })
    report.previous_termination_reason = mission.termination_reason if active_actors else None
    return state, config, report
