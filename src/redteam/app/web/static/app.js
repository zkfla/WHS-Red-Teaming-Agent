import { $ } from "./dom.js";
import { ApiError, api, openStream, requestId } from "./api.js";
import { describeError } from "./labels.js";
import { renderComposer, renderInspector, renderSide, renderTop } from "./render_panels.js";
import { renderThread } from "./render_thread.js";
import { setupEngagement } from "./engagement.js";
import { setupGraph } from "./graph.js";
import { setupSettings } from "./settings.js";
import { applyItem, createStore, loadSnapshot } from "./store.js";
import { renderRuntime } from "./render_runtime.js";
import { hydrateIcons } from "./ui.js";

const store = createStore();
let streamLabel = "연결 중";
let source = null;
let scheduled = false;
let settingsDialog = null;
let engagementDialog = null;
let graph = null;
let autoOpened = false;

function render() {
  scheduled = false;
  renderTop(store);
  renderSide(store, streamLabel, handlers);
  renderComposer(store);
  renderThread($("thread"), store, handlers);
  renderInspector(store, handlers);
  const dialogs = { needs_engagement: engagementDialog, needs_settings: settingsDialog };
  const wanted = dialogs[store.host.state];
  if (wanted && !autoOpened) {
    autoOpened = true;
    wanted.open();
  }
}

function schedule() {
  if (!scheduled) {
    scheduled = true;
    requestAnimationFrame(render);
  }
}

function say(text) {
  $("composer-note").textContent = text || "";
  delete $("composer-note").dataset.statusNote;
}

function showView(name, nodeId = null) {
  for (const button of document.querySelectorAll("[data-view]")) {
    const on = button.dataset.view === name;
    button.classList.toggle("on", on);
    button.setAttribute("aria-pressed", String(on));
  }
  graph.show(name === "graph", nodeId);
}

function selectTab(name, focus = false) {
  for (const tab of document.querySelectorAll(".tab")) {
    const on = tab.dataset.tab === name;
    tab.classList.toggle("on", on);
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
    $(`tab-${tab.dataset.tab}`).hidden = !on;
    if (on && focus) tab.focus({ preventScroll: true });
  }
}

const handlers = {
  inspect(name) {
    showView("chat");
    selectTab(name, true);
    $(`tab-${name}`).scrollIntoView({ block: "nearest", behavior: "smooth" });
  },
  jump(key) {
    showView("chat");
    const node = [...document.querySelectorAll("[data-entry]")].find((entry) => entry.dataset.entry === key);
    node?.scrollIntoView({ block: "center", behavior: "smooth" });
  },
  async decide(entry, decision) {
    if (store.incompatible) return;
    if (!entry.run_id) return say("승인 대상 Run 정보를 받지 못했습니다.");
    if (decision === "deny" && !window.confirm("이 요청을 거부할까요?")) return;
    try {
      await api.decide(entry.approval_id, decision, entry.run_id);
      say("");
    } catch (failure) {
      say(describeError(failure));
    }
  },
};

async function sync() {
  if (source) source.close();
  const snapshot = await api.snapshot();
  loadSnapshot(store, snapshot);
  graph?.hint();
  schedule();
  source = openStream(store.lastSeq, {
    onItem(item) {
      if (applyItem(store, item)) {
        schedule();
        if (graph && item.kind !== "console") graph.hint();
      }
    },
    onOpen() {
      streamLabel = "연결됨";
      schedule();
    },
    onError() {
      streamLabel = "재연결 중";
      schedule();
    },
    onReset() {
      sync().catch(() => {});
    },
  });
}

async function send(event) {
  event.preventDefault();
  const draft = $("draft");
  const text = draft.value.trim();
  if (!text || store.incompatible) return;
  $("send").disabled = true;
  try {
    await api.sendInput(text, requestId());
    draft.value = ""; // the draft is kept unless the server accepted it
    say("");
  } catch (failure) {
    say(describeError(failure));
  } finally {
    schedule();
  }
}

async function control(action) {
  if (store.incompatible) return;
  if (action === "stop" && !window.confirm("현재 조사를 중단할까요?")) return;
  try {
    await api.control(action);
    say("");
  } catch (failure) {
    say(describeError(failure));
  }
}

function setup() {
  hydrateIcons();
  for (const button of document.querySelectorAll('.side .nav-item, .new-investigation')) {
    const name = button.textContent.trim();
    button.setAttribute('aria-label', name);
    button.title = name;
  }
  settingsDialog = setupSettings(() => store, (note) => {
    say(note);
    autoOpened = true;
    sync().catch(() => {});
  });
  engagementDialog = setupEngagement(() => {
    say("");
    autoOpened = false; // the next missing piece (settings) opens on its own
    sync().catch(() => {});
  });
  graph = setupGraph((view) => renderRuntime(view, (id) => showView("graph", id)));
  for (const button of document.querySelectorAll("[data-view]")) {
    button.addEventListener("click", () => showView(button.dataset.view));
  }
  for (const button of document.querySelectorAll("[data-inspector]")) {
    button.addEventListener("click", () => handlers.inspect(button.dataset.inspector));
  }
  for (const button of document.querySelectorAll("[data-command]")) {
    button.addEventListener("click", () => {
      $("draft").value = button.dataset.command;
      $("draft").focus();
    });
  }
  $("composer").addEventListener("submit", send);
  $("draft").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!$("send").disabled) $("composer").requestSubmit();
    }
  });
  $("btn-pause").addEventListener("click", () => control("pause"));
  $("btn-resume").addEventListener("click", () => control("resume"));
  $("btn-stop").addEventListener("click", () => control("stop"));
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => selectTab(tab.dataset.tab));
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const tabs = [...document.querySelectorAll(".tab")];
      const index = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (tabs.indexOf(tab) + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      selectTab(tabs[index].dataset.tab, true);
    });
  }
}

setup();
sync().catch((failure) => {
  streamLabel = failure instanceof ApiError ? "서버 거부" : "연결 실패";
  schedule();
});
