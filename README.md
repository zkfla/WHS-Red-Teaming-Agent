# 모바일 앱 특화 레드티밍 에이전트 - 프로토타입

APK 경로 하나만 주면, LLM 에이전트가 스스로 apktool/jadx를 호출해 디컴파일하고,
정적 분석으로 의심 지점을 찾은 뒤, 에뮬레이터에서 실제로 실행해 검증까지
자동으로 수행하는 프로토타입입니다.

## 현재 범위

- **정적 분석**: 매니페스트(exported 컴포넌트, 딥링크) + 소스코드(하드코딩 시크릿,
  인증 로직 등)를 LLM이 직접 읽고 판단
- **동적 검증**: 에뮬레이터에서 실제로 실행해서, 정적 분석이 의심한 내용이
  진짜인지 확인. 검증된 것만 리포트에 `[Verified]`로 표시, 나머지는
  `[Unverified]`로 구분
- **사후 기계 검수 (audit_report)**: LLM이 리포트에 적은 `[Verified]` 판정이
  실제 도구 실행 결과(MATCH/MISMATCH, DATA RETURNED/INCONCLUSIVE)와
  모순되면, 코드가 그 사실을 계산해서 리포트 하단에 자동으로 경고를 붙임.
  "아예 호출조차 안 했는데 검증했다고 서술하는" 경우도 매니페스트의
  exported 컴포넌트 목록과 실제 호출 목록을 대조해서 잡아냄
- LLM에게 특정 앱/컴포넌트 이름을 지정해주지 않음 — 매니페스트/소스코드를
  스스로 읽고 무엇을 테스트할지 직접 판단하는 구조

## 사용 가능한 도구 9개

| 구분 | 도구 | 역할 |
|---|---|---|
| 정적 분석 | `extract_manifest` | apktool로 매니페스트 확인 (exported 컴포넌트, 딥링크) |
| 정적 분석 | `decompile_apk` | jadx로 소스코드 목록 확보 |
| 정적 분석 | `read_source_file` | 특정 소스 파일 내용 읽기 |
| 동적 검증 | `launch_activity` | 특정 액티비티를 직접 실행 (인증 우회 등 확인). 결과에 `MATCH`/`MISMATCH`를 코드가 직접 계산해서 명시 |
| 동적 검증 | `push_file` | 임의 파일을 기기에 심기 (위조 파일 공격 재현용) |
| 동적 검증 | `read_recent_logcat` | 실행 직후 logcat 확인, 클래스명 등으로 필터링 가능 |
| 동적 검증 | `send_broadcast` | BroadcastReceiver 스푸핑 테스트 |
| 동적 검증 | `query_content_provider` | ContentProvider 무단 접근 테스트. `DATA RETURNED`/`INCONCLUSIVE`/`BLOCKED`를 코드가 직접 계산해서 명시 |
| 동적 검증 | `list_device_files` | 외부 저장소 파일 노출 확인 |

## 검증 시나리오 2개 

1. **Vulnerable Activity Components** — exported 액티비티(`PostLogin`,
   `DoTransfer`, `ViewStatement`, `ChangePassword` 등)를 로그인 없이 직접
   실행 가능한지 `launch_activity`로 검증.
2. **Insecure Webview** — `ViewStatement`가 외부 저장소 파일 존재 여부만
   확인하고 출처 검증 없이 WebView에 로드하는 문제를, `push_file`로 위조
   HTML(+JS `alert`)을 심고 `launch_activity` + `read_recent_logcat`으로
   실제 코드 실행을 검증.

## 사전 준비물

- Python 3.10+
- [jadx](https://github.com/skylot/jadx/releases) — PATH에 등록
- [apktool](https://apktool.org/docs/install) — PATH에 등록
- **Android 에뮬레이터** (Android Studio + AVD) — 동적 검증에 필요.
  WSL 환경이면 에뮬레이터는 Windows 쪽에서 실행하고, WSL에서는 `adb`/`adb.exe`로
  원격 제어

## 설치

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 실행

```bash
# 에뮬레이터를 먼저 켜고 (Android Studio Device Manager)
adb devices          # 또는 adb.exe devices (WSL)

# 테스트 대상 apk 설치
adb install testbed/Android-InsecureBankv2/InsecureBankv2.apk

# API 키 설정
export GEMINI_API_KEY=<API키>

# 실행
python testbed/mobile_agent_gemini.py testbed/Android-InsecureBankv2/InsecureBankv2.apk
```

