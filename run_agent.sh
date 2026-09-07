#!/bin/bash
# 사용법: ./run_agent.sh /절대경로/app.apk
set -e

if [ -z "$1" ]; then
    echo "사용법: ./run_agent.sh /절대경로/app.apk"
    exit 1
fi

APK_PATH="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Codex 실행 중... ==="
codex -a never -s danger-full-access exec --model gpt-5.6-sol \
  -o "$SCRIPT_DIR/codex_report.txt" \
  "$(cat "$SCRIPT_DIR/SYSTEM_PROMPT.txt")

이 APK를 레드티밍 관점에서 분석하고 위 형식대로 최종 리포트를 작성해줘: $APK_PATH"

echo ""
echo "=== 자동 재검수 (audit_report) 실행 중... ==="
python3 "$SCRIPT_DIR/postprocess_report.py" "$SCRIPT_DIR/codex_report.txt"
