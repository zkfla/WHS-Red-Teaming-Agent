#!/usr/bin/env bash
# redteam(우리 도구) vs ARTEX 비교용 — testbed vulnbank 공격 실행.
# Control Plane: testbed-policy.json 의 allowlist 로 공격면을 통제(파괴적 admin 제외).
# LLM: ChatGPT 구독(OAuth) via litellm. 첫 LLM 호출 때 터미널에 device-flow URL 이 뜸 → 브라우저 인증(1회).
set -euo pipefail
ROOT=/root/projects/WHS-Red-Teaming-Agent
VENV="$ROOT/.venv/bin"
POLICY="$ROOT/testbed-policy.json"
ENG="$ROOT/testbed-engagement.yaml"
LOGDIR=/tmp/redteam-testbed
mkdir -p "$LOGDIR"

cd "$ROOT"

echo "[1/3] capability 서버 기동 (web-http:18001, recon-passive:18002)"
"$VENV/python" -m redteam.capability.servers web-http \
  --policy "$POLICY" --transport streamable-http --host 127.0.0.1 --port 18001 \
  > "$LOGDIR/web-http.log" 2>&1 &
WEBHTTP_PID=$!
"$VENV/python" -m redteam.capability.servers recon-passive \
  --policy "$POLICY" --transport streamable-http --host 127.0.0.1 --port 18002 \
  > "$LOGDIR/recon-passive.log" 2>&1 &
RECON_PID=$!
trap 'kill $WEBHTTP_PID $RECON_PID 2>/dev/null || true' EXIT
sleep 4
echo "   web-http PID=$WEBHTTP_PID / recon-passive PID=$RECON_PID (로그: $LOGDIR/)"

echo "[2/3] MCP/LLM 환경 변수 설정"
export REDTEAM_MCP_SERVERS=web-http,recon-passive

export REDTEAM_MCP_WEB_HTTP_KIND=web_api
export REDTEAM_MCP_WEB_HTTP_TRANSPORT=streamable_http
export REDTEAM_MCP_WEB_HTTP_ENDPOINT=http://127.0.0.1:18001/mcp
export REDTEAM_MCP_WEB_HTTP_ALLOW_INSECURE_HTTP=true
export REDTEAM_MCP_WEB_HTTP_RISK_LEVEL=high
export REDTEAM_MCP_WEB_HTTP_MAX_CONCURRENCY=1
export REDTEAM_MCP_WEB_HTTP_MAX_OUTPUT_BYTES=262144
export REDTEAM_MCP_WEB_HTTP_ALLOWED_TOOLS_JSON='["http_request","http_security_posture"]'
export REDTEAM_MCP_WEB_HTTP_TOOL_OPERATIONS_JSON='{"http_request":["http_request"],"http_security_posture":["response_analysis"]}'

export REDTEAM_MCP_RECON_PASSIVE_KIND=recon
export REDTEAM_MCP_RECON_PASSIVE_TRANSPORT=streamable_http
export REDTEAM_MCP_RECON_PASSIVE_ENDPOINT=http://127.0.0.1:18002/mcp
export REDTEAM_MCP_RECON_PASSIVE_ALLOW_INSECURE_HTTP=true
export REDTEAM_MCP_RECON_PASSIVE_RISK_LEVEL=medium
export REDTEAM_MCP_RECON_PASSIVE_MAX_CONCURRENCY=1
export REDTEAM_MCP_RECON_PASSIVE_MAX_OUTPUT_BYTES=262144
export REDTEAM_MCP_RECON_PASSIVE_ALLOWED_TOOLS_JSON='["http_probe"]'
export REDTEAM_MCP_RECON_PASSIVE_TOOL_OPERATIONS_JSON='{"http_probe":["service_discovery"]}'

export REDTEAM_LLM_PROVIDER=litellm
export REDTEAM_LLM_MODEL=chatgpt/gpt-5.5

echo "[3/3] redteam 실행 (첫 LLM 호출 때 브라우저 OAuth 1회)"
"$VENV/redteam" run "$ENG"
