# 모바일 앱 특화 레드티밍 에이전트 - 프로토타입

APK 경로 하나만 주면, LLM 에이전트가 스스로 apktool/jadx로 디컴파일하고,
정적 분석으로 의심 지점을 찾은 뒤, 실제 Android 에뮬레이터에서 동적으로
실행해 검증하고, 그 결과를 다시 코드로 재검수하는 프로토타입입니다.


## 메인 버전 사용법 (Codex CLI + MCP)

### 사전 준비물

- Python 3.10+, Node.js/npm
- [jadx](https://github.com/skylot/jadx/releases), [apktool](https://apktool.org/docs/install) — PATH 등록
- Android 에뮬레이터 (Android Studio AVD)
- 실제 Codex CLI (`npm install -g @openai/codex`), ChatGPT 계정으로 로그인
- `pip install mcp --break-system-packages`

### 설치

```bash
npm install -g @openai/codex
codex   # 최초 1회 실행, 브라우저에서 ChatGPT 계정 로그인

pip install mcp --break-system-packages

mkdir -p ~/.codex
cat >> ~/.codex/config.toml << 'EOF'

[mcp_servers.mobile_redteam]
command = "python3"
args = ["/절대경로/mcp_server.py"]
EOF
```

### 실행

```bash
chmod +x run_agent.sh
./run_agent.sh /절대경로/InsecureBankv2.apk
```

`run_agent.sh`가 하는 일:
1. `codex -a never -s danger-full-access exec`로 분석 실행 (승인 프롬프트 없이 자동 진행)
2. 결과를 `codex_report.txt`에 저장
3. **자동으로 이어서** `postprocess_report.py`를 실행해 사후 재검수

## 도구 10개

| 구분 | 도구 | 역할 |
|---|---|---|
| 정적 | `extract_manifest` | 매니페스트 확인 (exported 컴포넌트, 딥링크) |
| 정적 | `decompile_apk` | jadx로 소스코드 목록 확보 |
| 정적 | `read_source_file` | 소스 파일 내용 읽기 |
| 동적 | `launch_activity` | 화면 직접 실행, `MATCH`/`MISMATCH` 코드 판정 |
| 동적 | `push_file` | 위조 파일 심기 |
| 동적 | `read_recent_logcat` | 로그 필터링 확인 |
| 동적 | `send_broadcast` | 위조 신호 전송 |
| 동적 | `query_content_provider` | 무단 데이터 조회, `DATA RETURNED`/`INCONCLUSIVE`/`BLOCKED` 판정 |
| 동적 | `insert_content_provider` | 무단 데이터 쓰기 시도 (읽기가 애매할 때 대체 증명) |
| 동적 | `list_device_files` | 외부 저장소 파일 목록 (심볼릭 링크 자동 추적) |

각 도구는 `@mcp.tool()` 데코레이터만 붙이면 함수 시그니처·docstring에서
JSON 스키마가 자동 생성됩니다 (수동 스키마 작성 불필요).

## 검증 시나리오 5개 (시스템 프롬프트에서 카테고리만 제시, 대상/방법은 LLM이 설계)

1. Vulnerable Activity Components — 인증 없는 화면 접근
2. Insecure Webview — 위조 파일 로드 → JS 실행
3. Flawed Broadcast Receivers — 발신자 미검증 신호 처리
4. Insecure Content Provider — 무단 데이터 노출/쓰기
5. Insecure SDCard Storage — 공용 저장소 민감정보 노출


## 사후 재검수 (`audit_report` / `postprocess_report.py`)

LLM이 쓴 리포트의 `[Verified]` 표시가 실제 도구 실행 기록(`MATCH`,
`DATA RETURNED` 등)과 일치하는지 순수 문자열 대조로 확인합니다. LLM을
다시 호출하지 않는 결정론적 코드이며, 다음 두 가지를 탐지합니다:
- 실제로는 `MISMATCH`/`INCONCLUSIVE`인데 `[Verified]`로 잘못 쓴 경우
- 매니페스트에 있는 exported 컴포넌트인데 한 번도 시도되지 않은 경우

