// View model built only from server items (snapshot replay and live stream share one reducer).
const ACTIVITY_LIMIT = 200;
const RUN_LIMIT = 8;
export const SUPPORTED_SCHEMA = 1;

export function createStore() {
  return {
    incompatible: false,
    lastSeq: 0,
    host: { state: "needs_settings", error: null, problems: [] },
    session: { engagement: "", scope: [] },
    state: null,
    settings: null,
    entries: new Map(),
    actions: new Map(),
    evidence: [],
    activity: [],
    budget: null,
    actionCount: 0,
  };
}

function upsert(store, key, patch) {
  const current = store.entries.get(key);
  const next = { ...(current || { key }), ...patch };
  store.entries.set(key, next);
  return next;
}

function shortTime(iso) {
  // Server timestamps are UTC; show the viewer's local time.
  const date = iso ? new Date(iso) : null;
  return date && !Number.isNaN(date.getTime())
    ? date.toLocaleTimeString("en-GB")
    : "";
}

function logActivity(store, item, text) {
  store.activity.push({ ts: shortTime(item.ts), text, kind: item.kind, key: String(item.seq) });
  if (store.activity.length > ACTIVITY_LIMIT) store.activity.shift();
}

function applyEvent(store, item) {
  const type = item.data.event_type;
  const p = item.data.payload || {};
  const runId = item.run_id;
  logActivity(store, item, type);
  if (type === "AgentTurnDecided") {
    const text = p.response || p.decision_summary;
    if (text) {
      upsert(store, `turn:${item.data.event_id}`, {
        type: "agent", text, ts: item.ts, summary: p.decision_summary && p.decision_summary !== text ? p.decision_summary : null,
      });
    }
  } else if (type === "ActionProposed") {
    store.actionCount += 1;
    const action = upsert(store, `act:${p.action_id}`, {
      type: "action", action_id: p.action_id, operation: p.operation || p.tool_id || "action",
      target: p.target || "", verdict: null, result: null, rules: [], ts: item.ts,
    });
    store.actions.set(p.action_id, action);
  } else if (type === "PolicyEvaluated" || type === "ActionDenied") {
    const action = store.actions.get(p.action_id);
    if (action) {
      action.verdict = type === "ActionDenied" ? "DENY" : String(p.verdict || "").toUpperCase();
      action.rules = p.matched_rules || p.reasons || [];
    }
    if (type === "PolicyEvaluated" && typeof p.budget_remaining === "number") store.budget = p.budget_remaining;
  } else if (type === "ToolExecutionFinished" || type === "ToolExecutionFailed") {
    const action = store.actions.get(p.action_id);
    if (action) {
      action.result = {
        ok: type === "ToolExecutionFinished" && p.success !== false,
        text: p.summary || p.error_message || p.error_code || "",
      };
    }
  } else if (type === "ApprovalRequired") {
    upsert(store, `apr:${p.approval_id}`, {
      type: "approval", approval_id: p.approval_id, run_id: runId, action_id: p.action_id, reasons: p.reasons || [], ts: item.ts,
    });
  } else if (type === "FindingCreated" || type === "FindingVerified") {
    const key = `fnd:${p.finding_id || item.data.event_id}`;
    const previous = store.entries.get(key);
    upsert(store, key, {
      type: "finding", title: p.title || p.summary || previous?.title || "발견사항",
      verified: type === "FindingVerified" || Boolean(previous?.verified), ts: item.ts,
    });
  } else if (type === "EvidenceCreated") {
    store.evidence.push({ id: p.evidence_id, action_id: p.action_id, ts: shortTime(item.ts) });
  } else if (type === "RunStarted") {
    upsert(store, `note:start:${runId}`, { type: "note", text: "조사를 시작했습니다." });
  } else if (type === "RunFinished") {
    upsert(store, `note:end:${runId}`, { type: "note", text: `조사 종료 · ${p.termination_reason || "알 수 없음"}` });
  }
}

export function applyItem(store, item) {
  if (item.seq <= store.lastSeq) return false;
  store.lastSeq = item.seq;
  if (item.kind === "host") store.host = item.data;
  else if (item.kind === "state") {
    store.state = item.data;
    if (item.data.last_summary) {
      upsert(store, `sum:${item.data.binding ? item.data.binding.attachment_id : "run"}:${item.data.last_summary.length}`, {
        type: "note", text: item.data.last_summary,
      });
    }
  } else if (item.kind === "input") upsert(store, `in:${item.data.input_id}`, { type: "input", ...item.data });
  else if (item.kind === "console") logActivity(store, item, item.data.text);
  else if (item.kind === "event") applyEvent(store, item);
  return true;
}

export function loadSnapshot(store, snapshot) {
  const fresh = createStore();
  Object.assign(store, fresh);
  store.incompatible = snapshot.schema_version !== SUPPORTED_SCHEMA;
  store.session = snapshot.session;
  store.settings = snapshot.settings;
  store.state = snapshot.state;
  for (const item of snapshot.items) applyItem(store, item);
  store.lastSeq = Math.max(store.lastSeq, snapshot.last_seq);
}

export function runList(store) {
  return [...store.entries.values()]
    .filter((e) => e.type === "input" && e.decision && e.decision.mode === "execute")
    .slice(-RUN_LIMIT)
    .map((e) => ({ key: e.key, text: e.decision.objective || e.text, state: e.state }));
}
