// One read-only graph projection powers the canvas and conversation's runtime overview.
import { $, h, replace } from './dom.js';
import { api } from './api.js';
import { GRAPH_UNAVAILABLE, describeError } from './labels.js';
import { renderDetail } from './graph_detail.js';
import { drawEdges } from './graph_lines.js';
import { connectedPath, COLUMNS, graphLayout } from './graph_layout.js';
import { candidateNodes, llmNode, originNode, plannerNode, taskNodes, workerNodes } from './graph_nodes.js';
import { emptyState, replaceLive } from './ui.js';

const POLL_MS = 5000;
const DEBOUNCE_MS = 700;
const MIN_ZOOM = .2;
const MAX_ZOOM = 1.5;

export function setupGraph(onView = () => {}) {
  let open = false;
  let view = null;
  let selected = null;
  let pending = null;
  let reading = false;
  let layout = null;
  let zoom = 1;
  let autoFit = true;
  let fitAll = false;
  let runId = null;
  const canvas = $('graph-canvas');

  function applyZoom() {
    if (!layout) return;
    if (autoFit) {
      const fitWidth = (canvas.clientWidth - 32) / layout.width;
      const fitHeight = (canvas.clientHeight - 28) / layout.height;
      zoom = fitAll ? Math.max(MIN_ZOOM, Math.min(1, fitWidth, fitHeight)) : Math.max(.6, Math.min(1, fitWidth, fitHeight));
    }
    $('graph-scene').style.transform = `scale(${zoom})`;
    $('graph-surface').style.width = `${layout.width * zoom}px`;
    $('graph-surface').style.height = `${layout.height * zoom}px`;
    $('graph-zoom-level').textContent = `${Math.round(zoom * 100)}%`;
    $('graph-zoom-out').disabled = zoom <= MIN_ZOOM;
    $('graph-zoom-in').disabled = zoom >= MAX_ZOOM;
  }

  function search() {
    const query = $('graph-search').value.trim().toLocaleLowerCase();
    const related = connectedPath(view?.edges || [], selected);
    let count = 0;
    for (const node of $('graph-nodes').querySelectorAll('[data-node]')) {
      const match = query && `${node.textContent} ${node.dataset.node}`.toLocaleLowerCase().includes(query);
      node.classList.toggle('search-match', Boolean(match));
      node.classList.toggle('dim', Boolean(query ? !match : related && !related.has(node.dataset.node)));
      if (match) count++;
    }
    $('graph-search-count').textContent = query ? `${count}개` : '';
  }

  function pick(id) {
    selected = selected === id ? null : id;
    paint();
  }

  function paint() {
    const note = $('graph-note');
    if (!view?.available) {
      const message = view?.error || GRAPH_UNAVAILABLE[view?.reason] || '실행 상태를 불러오고 있습니다.';
      note.textContent = '';
      replace($('graph-nodes'));
      $('graph-lines').replaceChildren();
      $('graph-surface').hidden = true;
      $('graph-empty').hidden = false;
      replace($('graph-empty'), emptyState('graph', view?.reason === 'no_run_yet' ? '조사가 시작되면 흐름이 연결됩니다' : '실행 그래프를 확인할 수 없습니다', message));
      $('graph-detail-panel').hidden = true;
      for (const id of ['graph-fit', 'graph-zoom-in', 'graph-zoom-out', 'graph-search']) $(id).disabled = true;
      return;
    }
    for (const id of ['graph-fit', 'graph-zoom-in', 'graph-zoom-out', 'graph-search']) $(id).disabled = false;
    $('graph-surface').hidden = false;
    $('graph-empty').hidden = true;
    layout = graphLayout(view);
    if (selected && !layout.positions.has(selected)) selected = null;
    $('graph-detail-panel').hidden = !selected;
    note.textContent = view.mission?.termination_reason ? `조사 종료 · ${view.mission.termination_reason}`
      : `후보 ${(view.candidates || []).length}개 · 검증 작업 ${view.tasks.length}개 · 워커 ${view.workers.length}개 · LLM 전송 ${view.llm?.provider_requests || 0}건 · 증거 ${view.origin.evidence}건`;
    const nodes = [plannerNode(view, selected, pick), ...candidateNodes(view, selected, pick), ...taskNodes(view, selected, pick), ...workerNodes(view, selected, pick), llmNode(view, selected, pick), originNode(view, selected, pick)];
    for (const node of nodes) {
      const position = layout.positions.get(node.dataset.node);
      node.style.left = `${position.x}px`;
      node.style.top = `${position.y}px`;
    }
    const headings = ['계획 · 판단', '취약점 후보', '검증 작업', '실행 워커', 'LLM 요청 · 응답', '증거 · 발견사항'].map((text, index) => {
      const label = h('span', { class: 'graph-lane-title', text });
      label.style.left = `${COLUMNS[index]}px`;
      return label;
    });
    replaceLive($('graph-nodes'), headings, nodes);
    drawEdges($('graph-lines'), view.edges, layout, connectedPath(view.edges, selected));
    renderDetail($('graph-detail'), view, selected);
    search();
    applyZoom();
  }

  async function read() {
    if (reading) return;
    reading = true;
    try {
      view = await api.graph();
      if (view.run_id !== runId) {
        runId = view.run_id;
        selected = null;
        autoFit = true;
        fitAll = false;
      }
    } catch (failure) {
      view = { available: false, error: describeError(failure) };
    } finally {
      reading = false;
    }
    onView(view);
    if (open) paint();
  }

  function hint() {
    if (pending) return;
    pending = setTimeout(() => { pending = null; read(); }, DEBOUNCE_MS);
  }

  function show(on, nodeId = null) {
    open = on;
    if (nodeId) selected = nodeId;
    $('view-graph').hidden = !on;
    $('view-chat').hidden = on;
    document.querySelector('.app').classList.toggle('graph-mode', on);
    if (on) { paint(); read(); }
  }

  function changeZoom(delta) {
    autoFit = false;
    const centerX = (canvas.scrollLeft + canvas.clientWidth / 2) / zoom;
    const centerY = (canvas.scrollTop + canvas.clientHeight / 2) / zoom;
    zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom + delta));
    applyZoom();
    canvas.scrollLeft = centerX * zoom - canvas.clientWidth / 2;
    canvas.scrollTop = centerY * zoom - canvas.clientHeight / 2;
  }

  $('graph-search').addEventListener('input', search);
  $('graph-zoom-in').addEventListener('click', () => changeZoom(.1));
  $('graph-zoom-out').addEventListener('click', () => changeZoom(-.1));
  $('graph-fit').addEventListener('click', () => { autoFit = true; fitAll = true; applyZoom(); canvas.scrollTo(0, 0); });
  $('graph-close').addEventListener('click', () => {
    const id = selected;
    selected = null;
    paint();
    [...$('graph-nodes').querySelectorAll('[data-node]')].find((node) => node.dataset.node === id)?.focus({ preventScroll: true });
  });
  $('view-graph').addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && event.target !== $('graph-search')) {
      event.preventDefault();
      $('graph-close').click();
    }
  });
  let drag = null;
  canvas.addEventListener('pointerdown', (event) => {
    if (event.pointerType === 'touch' || event.button !== 0 || event.target.closest('button, input')) return;
    drag = { x: event.clientX, y: event.clientY, left: canvas.scrollLeft, top: canvas.scrollTop };
    canvas.setPointerCapture(event.pointerId);
    canvas.classList.add('dragging');
  });
  canvas.addEventListener('pointermove', (event) => {
    if (!drag) return;
    canvas.scrollLeft = drag.left - event.clientX + drag.x;
    canvas.scrollTop = drag.top - event.clientY + drag.y;
  });
  const endDrag = () => { drag = null; canvas.classList.remove('dragging'); };
  canvas.addEventListener('pointerup', endDrag);
  canvas.addEventListener('pointercancel', endDrag);
  window.addEventListener('resize', () => { if (open) applyZoom(); });
  const timer = setInterval(() => { if (!document.hidden) read(); }, POLL_MS);
  window.addEventListener('pagehide', () => { clearInterval(timer); clearTimeout(pending); });
  read();
  return { show, hint };
}
