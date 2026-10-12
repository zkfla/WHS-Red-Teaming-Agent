import { $ } from "./dom.js";
import { ApiError, api, openStream, requestId } from "./api.js";
import { describeError } from "./labels.js";
import { renderComposer, renderInspector, renderSide, renderTop } from "./render_panels.js";
import { renderThread } from "./render_thread.js";
import { setupEngagement } from "./engagement.js";
import { setupGraph } from "./graph.js";
import { setupSettings } from "./settings.js";
import { applyItem, createStore, loadSnapshot } from "./store.js";

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
  renderSide(store, streamLabel);
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
}

const handlers = {
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
  graph = setupGraph();
  for (const button of document.querySelectorAll(".nav-item[data-view]")) {
    button.addEventListener("click", () => {
      for (const other of document.querySelectorAll(".nav-item[data-view]")) other.classList.toggle("on", other === button);
      graph.show(button.dataset.view === "graph");
    });
  }
  $("composer").addEventListener("submit", send);
  $("draft").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) $("composer").requestSubmit();
  });
  $("btn-pause").addEventListener("click", () => control("pause"));
  $("btn-resume").addEventListener("click", () => control("resume"));
  $("btn-stop").addEventListener("click", () => control("stop"));
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => {
      for (const other of document.querySelectorAll(".tab")) {
        const on = other === tab;
        other.classList.toggle("on", on);
        other.setAttribute("aria-selected", String(on));
        $(`tab-${other.dataset.tab}`).hidden = !on;
      }
    });
  }
}

setup();
sync().catch((failure) => {
  streamLabel = failure instanceof ApiError ? "서버 거부" : "연결 실패";
  schedule();
});
