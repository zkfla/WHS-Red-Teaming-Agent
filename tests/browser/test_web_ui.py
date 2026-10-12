"""Optional Chromium UI regression checks; all API responses are local fixtures.

Run: pip install playwright && playwright install chromium
     pytest -q tests/browser/test_web_ui.py
"""

from __future__ import annotations

import copy
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from redteam.app.web.server import STATIC_DIR

playwright = pytest.importorskip("playwright.sync_api")


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as driver:
        instance = driver.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture(scope="module")
def static_url():
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(QuietHandler, directory=str(STATIC_DIR.parent))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/static/index.html"
    server.shutdown()
    server.server_close()
    thread.join()


def fixture_data():
    state = {
        "phase": "waiting_approval",
        "activity": "계정 조회 결과를 바탕으로 다음 요청을 검토합니다.",
        "pending_approval": "APR-0231",
        "binding": None,
        "active_run": True,
    }
    items = []

    def item(kind, data):
        items.append(
            {
                "seq": len(items) + 1,
                "kind": kind,
                "data": data,
                "run_id": "run-ui",
                "ts": "2026-10-13T05:31:00Z",
            }
        )

    def event(name, payload):
        item("event", {"event_type": name, "event_id": str(len(items)), "payload": payload})

    item("host", {"state": "ready", "error": None})
    item(
        "input",
        {
            "input_id": "in-ui",
            "text": "결제 API의 계정 권한을 확인해줘. 데이터를 바꾸는 요청은 승인받고 진행해.",
            "state": "dispatched",
            "received_at": "2026-10-13T05:30:00Z",
            "decision": {"mode": "execute", "objective": "결제 API 계정 권한 점검"},
        },
    )
    event(
        "AgentTurnDecided",
        {
            "response": (
                "두 계정 세션으로 계정 조회 응답을 비교했습니다. "
                "다른 계정의 정보가 반환된 응답을 발견했습니다. "
                "영향 범위를 확인하려면 추가 검증이 필요합니다. "
                "다음 요청은 데이터를 변경하므로 승인을 기다리고 있습니다."
            )
        },
    )
    event(
        "ActionProposed",
        {"action_id": "action-read", "operation": "계정 응답 비교", "target": "GET /api/accounts"},
    )
    event(
        "ToolExecutionFinished",
        {
            "action_id": "action-read",
            "success": True,
            "summary": "두 세션의 조회 응답을 수집했습니다.",
        },
    )
    event(
        "EvidenceCreated",
        {"evidence_id": "evidence-account-comparison", "action_id": "action-read"},
    )
    event(
        "FindingCreated",
        {"finding_id": "finding-ui", "title": "계정 조회에서 소유권 검증 누락이 의심됩니다"},
    )
    event(
        "ActionProposed",
        {"action_id": "action-write", "operation": "POST", "target": "/api/transfers"},
    )
    event(
        "PolicyEvaluated",
        {"action_id": "action-write", "verdict": "REQUIRE_APPROVAL", "budget_remaining": 12},
    )
    event(
        "ApprovalRequired",
        {
            "approval_id": "APR-0231",
            "action_id": "action-write",
            "reasons": [
                "데이터를 변경하는 요청입니다.",
                "규칙 write-needs-approval에 따라 운영자 검토가 필요합니다.",
            ],
        },
    )
    item("state", state)
    usage = {"requests": 4, "in_flight": 0, "known_tokens": 800, "unknown_usage": 0}
    tasks = [
        {
            "task_id": "t1",
            "title": "이체 소유권 검증",
            "status": "blocked",
            "role": "Web Worker",
            "worker": "worker-1",
            "wait": {"reason": "approval", "approval_ref": "APR-0231"},
            "completion": ["승인된 범위에서 검증"],
            "blocking_reason": "승인 대기",
            "evidence": 0,
            "has_result": False,
        },
        {
            "task_id": "t2",
            "title": "세션·헤더 점검",
            "status": "running",
            "role": "Recon Worker",
            "worker": "worker-2",
            "wait": None,
            "completion": ["헤더 관측 저장"],
            "blocking_reason": None,
            "evidence": 1,
            "has_result": False,
        },
        {
            "task_id": "t3",
            "title": "계정 조회 응답 비교",
            "status": "completed",
            "role": "Web Worker",
            "worker": None,
            "wait": None,
            "completion": ["응답 비교"],
            "blocking_reason": None,
            "evidence": 1,
            "has_result": True,
        },
    ]
    workers = [
        {
            "instance_id": f"worker-{i}",
            "task_id": f"t{i}",
            "phase": phase,
            "model_requests": 2,
            "tool_calls": 1,
            "current_tool": None,
            "wait": None,
            "slot_released": False,
            "evidence_refs": [],
            "launch_stage": "started",
        }
        for i, phase in [(1, "waiting_approval"), (2, "tool")]
    ]
    graph = {
        "available": True,
        "run_id": "run-ui",
        "mission": {"status": "running", "termination_reason": None},
        "planner": {
            "state": "waiting",
            "inputs": {"tasks": 3, "evidence": 2, "open_questions": None},
            "last_judgment": None,
            "usage": usage,
        },
        "tasks": tasks,
        "workers": workers,
        "origin": {"evidence": 2, "findings": 1, "verified_findings": 0, "graph_updates": 2},
        "usage": usage,
        "edges": [
            {"from": "planner", "to": f"task:t{i}", "kind": "registered"} for i in range(1, 4)
        ]
        + [
            {"from": "task:t1", "to": "worker:worker-1", "kind": "held_by"},
            {"from": "task:t2", "to": "worker:worker-2", "kind": "held_by"},
            {"from": "worker:worker-2", "to": "origin", "kind": "stored"},
            {"from": "origin", "to": "planner", "kind": "wakes"},
        ],
        "events": [],
    }
    snapshot = {
        "schema_version": 1,
        "session": {
            "state": "ready",
            "engagement": "결제 API 권한 경계 점검",
            "scope": ["staging.example.test"],
        },
        "settings": {"problems": []},
        "state": state,
        "items": items,
        "last_seq": len(items),
    }
    return {"snapshot": snapshot, "graph": graph}


@pytest.fixture
def ui(browser, static_url):
    page = browser.new_page(viewport={"width": 1600, "height": 1050})
    data = fixture_data()
    writes = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def api(route):
        name = route.request.url.split("/api/v1/")[1].split("?")[0]
        if route.request.method != "GET":
            writes.append((name, route.request.post_data_json))
            route.fulfill(json={"input_id": "accepted"})
        elif name == "events":
            route.fulfill(content_type="text/event-stream", body=": offline UI fixture\n\n")
        else:
            route.fulfill(json=data.get(name, {}))

    page.route("**/api/v1/**", api)
    page.goto(static_url)
    page.locator(".card.approval").wait_for()
    page.locator(".agent-row").first.wait_for()
    yield page, data, writes
    assert errors == []
    page.close()


def screenshot(page, name):
    destination = os.environ.get("WHS_UI_SCREENSHOTS")
    if destination:
        path = Path(destination)
        path.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path / f"{name}.png"), full_page=True)


def test_cards_and_exact_approval_request(ui):
    page, _, writes = ui
    assert page.locator("#approval-count").inner_text() == "1"
    assert page.locator("#task-list .run-link").count() == 3
    assert page.locator("#agent-status .agent-row").count() == 3
    assert "검증 미확인" in page.locator(".card.finding").inner_text()
    screenshot(page, "approval-desktop")
    page.get_by_role("button", name="검토하고 결정").click()
    assert page.locator("#tab-button-approval").get_attribute("aria-selected") == "true"
    page.get_by_role("button", name="승인 · 이번 1회", exact=True).click()
    assert writes == [("approvals/APR-0231/approve", {"run_id": "run-ui"})]


def test_details_survive_stream_render_and_plain_text_is_safe(ui):
    page, _, _ = ui
    result = page.evaluate("""async () => {
      const {createStore, applyItem} = await import('/static/store.js');
      const {renderThread} = await import('/static/render_thread.js');
      const store = createStore();
      const root = document.getElementById('thread');
      const event = (seq, type, payload) => applyItem(store, {
        seq, kind:'event', data:{event_type:type, event_id:String(seq), payload}
      });
      event(1, 'ActionProposed', {
        action_id:'a', operation:'read', target:'<img src=x onerror=alert(1)>'
      });
      renderThread(root, store, {});
      root.querySelector('details').open = true;
      root.querySelector('summary').focus();
      event(2, 'ToolExecutionFinished', {action_id:'a', success:true, summary:'observed'});
      renderThread(root, store, {});
      return {open:root.querySelector('details').open, focus:document.activeElement.tagName,
              text:root.textContent, images:root.querySelectorAll('img').length};
    }""")
    assert result["open"] and result["focus"] == "SUMMARY"
    assert "<img src=x onerror=alert(1)>" in result["text"]
    assert result["images"] == 0


def test_graph_navigation_tabs_and_commands(ui):
    page, _, writes = ui
    page.locator("#task-list button").first.click()
    assert page.locator("#view-graph").is_visible()
    assert "이체 소유권 검증" in page.locator("#graph-detail").inner_text()
    page.locator(".nav-item[data-view=chat]").click()
    page.locator("#tab-button-approval").focus()
    page.keyboard.press("ArrowRight")
    assert page.locator("#tab-activity").is_visible()
    page.keyboard.press("End")
    assert page.locator("#tab-evidence").is_visible()
    page.locator("[data-command='/status']").click()
    assert page.locator("#draft").input_value() == "/status"
    assert not writes  # quick actions only prepare input; the operator submits it
    page.keyboard.press("Enter")
    assert writes[0][0] == "input" and writes[0][1]["text"] == "/status"


@pytest.mark.parametrize("width", [390, 800, 1024])
def test_narrow_layout_keeps_navigation_and_review_accessible(ui, width):
    page, _, _ = ui
    page.set_viewport_size({"width": width, "height": 900})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.locator(".card.approval .btn.primary").click()
    assert page.get_by_role("button", name="승인 · 이번 1회", exact=True).is_visible()
    assert page.locator("#open-settings").is_visible()
    screenshot(page, f"approval-{width}")


def test_missing_data_and_error_are_not_shown_as_healthy(ui):
    page, data, _ = ui
    data["graph"] = {"available": False, "reason": "state_unreadable"}
    broken = copy.deepcopy(data["snapshot"])
    broken["items"] = [
        {"seq": 1, "kind": "host", "data": {"state": "error", "error": "fixture_startup_failure"}}
    ]
    broken["state"] = None
    broken["last_seq"] = 1
    data["snapshot"] = broken
    page.reload()
    page.get_by_text("저장된 실행 상태를 읽을 수 없습니다.", exact=True).wait_for()
    assert page.locator("#agent-status .agent-row").count() == 0
    assert page.locator("#send").is_disabled()
    assert page.locator("#approval-count").is_hidden()
    page.get_by_text("오류 상세 보기", exact=True).click()
    assert page.get_by_text("fixture_startup_failure", exact=True).is_visible()
    screenshot(page, "error-desktop")


def test_readonly_schema_disables_approval(ui):
    page, data, writes = ui
    data["snapshot"]["schema_version"] = 999
    page.reload()
    assert page.get_by_role("button", name="승인 · 이번 1회", exact=True).is_disabled()
    assert page.locator("#send").is_disabled()
    assert not writes


def test_finding_verification_keeps_original_title(ui):
    page, _, _ = ui
    finding = page.evaluate("""async () => {
      const {createStore, applyItem} = await import('/static/store.js');
      const store = createStore();
      const events = [
        [1, 'FindingCreated', {finding_id:'f',title:'Original title'}],
        [2,'FindingVerified',{finding_id:'f'}]
      ];
      for (const [seq, type, payload] of events) {
        applyItem(store, {seq,kind:'event',data:{event_type:type,payload}});
      }
      return store.entries.get('fnd:f');
    }""")
    assert finding["title"] == "Original title" and finding["verified"]


def test_graph_paths_zoom_search_and_details(ui):
    page, _, writes = ui
    page.locator(".nav-item[data-view=graph]").click()
    assert page.locator("#graph-detail-panel").is_hidden()
    assert page.locator(".gnode").count() == 7
    assert page.locator(".graph-edge[marker-end]").count() == 7
    screenshot(page, "graph-overview")
    page.locator('[data-node="task:t1"]').click()
    assert page.locator("#graph-detail-panel").is_visible()
    assert page.locator('[data-node="worker:worker-1"]').get_attribute("class").find("dim") == -1
    assert "dim" in page.locator('[data-node="task:t2"]').get_attribute("class")
    assert page.locator(".graph-edge.highlighted").count() == 2
    assert "승인된 범위에서 검증" in page.locator("#graph-detail").inner_text()
    screenshot(page, "graph-selected")
    page.locator("#graph-close").click()
    assert page.locator("#graph-detail-panel").is_hidden()
    assert page.locator('[data-node="task:t1"]').evaluate("(el) => el === document.activeElement")
    page.locator("#graph-search").fill("세션")
    assert page.locator("#graph-search-count").inner_text() == "1개"
    assert page.locator(".gnode.search-match").count() == 1
    page.locator("#graph-search").fill("")
    previous = page.locator("#graph-zoom-level").inner_text()
    page.locator("#graph-zoom-out").click()
    assert page.locator("#graph-zoom-level").inner_text() != previous
    page.locator("#graph-fit").click()
    task = page.locator('[data-node="task:t2"]').bounding_box()
    worker = page.locator('[data-node="worker:worker-2"]').bounding_box()
    assert abs(task["y"] - worker["y"]) < 1
    assert not writes


def test_graph_all_tasks_multiple_workers_and_returned_connections(ui):
    page, data, _ = ui
    graph = data["graph"]
    original = copy.deepcopy(graph["tasks"][0])
    graph["tasks"] = [
        {**original, "task_id": f"t{i}", "title": f"조사 작업 {i}"} for i in range(35)
    ]
    second_worker = {
        **graph["workers"][0],
        "instance_id": "old-worker",
        "task_id": "t1",
        "launch_stage": "returned",
        "phase": "returned",
    }
    graph["workers"].append(second_worker)
    graph["edges"].append({"from": "task:t1", "to": "worker:old-worker", "kind": "returned"})
    page.reload()
    page.locator(".nav-item[data-view=graph]").click()
    assert page.locator(".gnode.task").count() == 35
    assert page.locator(".gnode.worker").count() == 3
    assert page.locator(".graph-edge.returned").count() == 1
    assert page.locator("#graph-canvas").evaluate("(el) => el.scrollHeight > el.clientHeight")


@pytest.mark.parametrize("width", [390, 1024])
def test_graph_responsive_drawer_and_no_run_state(ui, width):
    page, data, _ = ui
    page.set_viewport_size({"width": width, "height": 900})
    page.locator("#task-list button").first.click() if width > 800 else page.locator(
        ".nav-item[data-view=graph]"
    ).click()
    if width <= 800:
        page.locator('[data-node="task:t1"]').click()
    assert page.locator("#graph-detail-panel").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    screenshot(page, f"graph-{width}")
    page.locator("#graph-close").click()
    data["graph"] = {"available": False, "reason": "no_run_yet"}
    page.reload()
    page.locator(".nav-item[data-view=graph]").click()
    assert page.get_by_text("조사가 시작되면 흐름이 연결됩니다", exact=True).is_visible()
    assert page.locator(".gnode").count() == 0
    assert page.locator("#graph-fit").is_disabled()


def test_graph_poll_keeps_selection_and_manual_zoom(ui):
    page, data, _ = ui
    page.locator(".nav-item[data-view=graph]").click()
    page.locator('[data-node="worker:worker-1"]').click()
    page.locator("#graph-zoom-out").click()
    zoom = page.locator("#graph-zoom-level").inner_text()
    data["graph"]["workers"][0]["phase"] = "tool"
    page.wait_for_function(
        """() => document.querySelector(
        '[data-node="worker:worker-1"] .gchip').textContent === '도구 실행 중'""",
        timeout=8000,
    )
    assert page.locator('[data-node="worker:worker-1"]').get_attribute("aria-pressed") == "true"
    assert page.locator("#graph-detail-panel").is_visible()
    assert page.locator("#graph-zoom-level").inner_text() == zoom
