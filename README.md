# 모바일 앱 특화 레드티밍 에이전트 - 프로토타입 (1단계)

APK 경로만 넘기면, LLM 에이전트가 스스로 apktool/jadx를 호출해서
디컴파일하고, 정적 분석까지 자동으로 수행하는 컨셉 검증용 프로토타입.

## 현재 범위 (1단계)
- 정적 분석만 다룸 (동적 분석/에뮬레이터 연동은 다음 단계)
- 재검증(verify) 단계는 아직 없음 (다음 단계에서 추가 예정)
- LLM이 실제로 읽는 파일: `.java` 소스코드 + `AndroidManifest.xml`

## 사전 준비물

- Python 3.10+
- [jadx](https://github.com/skylot/jadx/releases) — PATH에 등록되어 있어야 함
- [apktool](https://apktool.org/docs/install) — PATH에 등록되어 있어야 함
- Gemini API 키


## 설치

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 실행

```bash
export GEMINI_API_KEY=<API키>
python mobile_agent_gemini.py android-insecurebankv2/InsecureBankv2.apk
```


