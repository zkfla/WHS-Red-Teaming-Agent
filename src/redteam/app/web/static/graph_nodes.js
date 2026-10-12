// Compact nodes retain status and one key fact; selection reveals the full read model.
import { h } from "./dom.js";
import { PLANNER, TASK, UNKNOWN, WAIT, WORKER } from "./labels.js";
import { icon } from "./ui.js";

function node(id, selected, onPick, kind, title, status, summary, tone = '') {
  return h('button', {
    type: 'button', 'data-node': id, 'data-focus': id, title: `${title} · ${status}`,
    class: `gnode ${kind} ${tone}${selected === id ? ' on' : ''}`,
    'aria-pressed': String(selected === id), onclick: () => onPick(id),
  }, h('span', { class: 'gnode-icon' }, icon(kind === 'planner' ? 'target' : kind === 'task' ? 'file' : kind === 'worker' ? 'terminal' : 'graph')),
    h('span', { class: 'gnode-copy' }, h('span', { class: 'gnode-kind', text: {planner:'계획 · 판단', task:'조사 작업', worker:'실행 워커', origin:'결과 저장'}[kind] }),
      h('strong', { text: title })),
    h('span', { class: 'gmeta', text: summary }),
    h('span', { class: 'gchip', text: status }));
}

export function plannerNode(view, selected, onPick) {
  const p = view.planner;
  return node('planner', selected, onPick, 'planner', 'Planner', PLANNER[p.state] || UNKNOWN,
    `작업 ${p.inputs.tasks}개 · 증거 ${p.inputs.evidence}건`, p.state === 'deciding' ? 'active' : '');
}

export function taskNodes(view, selected, onPick) {
  return view.tasks.map((task) => node(`task:${task.task_id}`, selected, onPick, 'task', task.title,
    task.wait ? `${WAIT[task.wait.reason] || task.wait.reason} 대기` : TASK[task.status] || UNKNOWN,
    `${task.role || '역할 미지정'} · 증거 ${task.evidence}건`,
    task.wait || task.status === 'blocked' ? 'waiting' : task.status === 'failed' ? 'failed' : task.status === 'running' ? 'active' : task.status === 'completed' ? 'done' : ''));
}

export function workerNodes(view, selected, onPick) {
  return view.workers.map((worker) => node(`worker:${worker.instance_id}`, selected, onPick, 'worker', worker.instance_id,
    WORKER[worker.phase] || UNKNOWN,
    worker.current_tool || `모델 ${worker.model_requests}회 · 도구 ${worker.tool_calls}회`,
    worker.phase.startsWith('waiting_') ? 'waiting' : worker.phase === 'outcome_unknown' ? 'failed' : ['model', 'tool'].includes(worker.phase) ? 'active' : ''));
}

export function originNode(view, selected, onPick) {
  const o = view.origin;
  return node('origin', selected, onPick, 'origin', '증거와 발견사항', `증거 ${o.evidence}건`,
    `발견 ${o.findings}건 · 검증 ${o.verified_findings}건`);
}
