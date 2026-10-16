// HTTP/SSE client for /api/v1. Writes carry the proofs the server's guard requires.
const BASE = "/api/v1";
const WRITE_HEADERS = { "Content-Type": "application/json", "X-Redteam-Client": "web" };

export class ApiError extends Error {
  constructor(status, body) {
    super(body && body.error ? body.error : `http_${status}`);
    this.status = status;
    this.body = body || {};
  }
}

async function call(method, path, body) {
  const options = { method, cache: "no-store" };
  if (method !== "GET") {
    options.headers = WRITE_HEADERS;
    options.body = JSON.stringify(body || {});
  }
  let response;
  try {
    response = await fetch(BASE + path, options);
  } catch (error) {
    throw new ApiError(0, { error: "network" });
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = null;
  }
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}

export const api = {
  snapshot: () => call("GET", "/snapshot"),
  graph: () => call("GET", "/graph"),
  profiles: () => call("GET", "/settings/profiles"),
  sendInput: (text, requestId) => call("POST", "/input", { text, request_id: requestId }),
  decide: (approvalId, decision, runId) =>
    call("POST", `/approvals/${encodeURIComponent(approvalId)}/${decision}`, { run_id: runId }),
  control: (action) => call("POST", `/run/${action}`, {}),
  saveSettings: (values) => call("PUT", "/settings", values),
  reauthenticateChatGPT: () => call("POST", "/settings/chatgpt/reauth", {}),
  engagements: () => call("GET", "/engagements"),
  engagement: (id) => call("GET", id ? `/engagement?id=${encodeURIComponent(id)}` : "/engagement"),
  previewEngagement: (id, draft) => call("POST", "/engagement/preview", { id, draft }),
  saveEngagement: (id, draft, confirmScope, confirmExpansion) =>
    call("PUT", "/engagement", { id, draft, confirm_scope: confirmScope, confirm_expansion: confirmExpansion }),
  interview: (payload) => call("POST", "/engagement/interview", payload),
  activateEngagement: (id) => call("POST", "/engagement/activate", { id }),
  restart: () => call("POST", "/session/restart", {}),
};

const KINDS = ["host", "state", "input", "event", "console"];

// Opens the event stream after `lastSeq`. The browser resends Last-Event-ID on its own
// reconnects; `onReset` fires when the server can no longer replay the gap.
export function openStream(lastSeq, { onItem, onOpen, onError, onReset }) {
  const source = new EventSource(`${BASE}/events?after=${lastSeq}`);
  source.onopen = () => onOpen();
  source.onerror = () => onError();
  for (const kind of KINDS) {
    source.addEventListener(kind, (message) => {
      try {
        onItem(JSON.parse(message.data));
      } catch (error) {
        /* a malformed item is skipped; the next snapshot repairs the view */
      }
    });
  }
  source.addEventListener("reset", () => {
    source.close();
    onReset();
  });
  return source;
}

export function requestId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID().replaceAll("-", "");
  return `r${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}
