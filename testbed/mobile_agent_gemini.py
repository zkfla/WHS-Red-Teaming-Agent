import os
import sys
import shutil
import subprocess
import time
import re
from google import genai
from google.genai import types
from google.genai import errors as genai_errors


def _run_tool(args: list) -> subprocess.CompletedProcess:
    """Windows .bat/.cmd tools (apktool, jadx) can't be run directly via
    CreateProcess and need shell=True; auto-detected here."""
    resolved = shutil.which(args[0])
    use_shell = bool(resolved) and resolved.lower().endswith((".bat", ".cmd"))
    return subprocess.run(args, capture_output=True, text=True, timeout=120, shell=use_shell)


def check_tools_installed():
    """Checks that jadx and apktool are actually on PATH before running."""
    missing = []
    for tool in ("jadx", "apktool"):
        if shutil.which(tool) is None:
            missing.append(tool)
    if missing:
        print(f"[치명적 오류] 다음 도구가 PATH에서 발견되지 않습니다: {', '.join(missing)}")
        print("설치 후 새 터미널을 열거나, 환경변수 PATH에 설치 경로를 추가하세요.")
        print("확인 명령어: where jadx  /  where apktool  (윈도우 cmd 기준)")
        sys.exit(1)
    print(f"[확인 완료] jadx: {shutil.which('jadx')}")
    print(f"[확인 완료] apktool: {shutil.which('apktool')}")


WORKDIR = "./agent_workdir"
JADX_OUT = os.path.join(WORKDIR, "jadx_out")
APKTOOL_OUT = os.path.join(WORKDIR, "apktool_out")

CURRENT_APK_PATH = None



def extract_manifest() -> str:
    """Unpacks the current APK with apktool and returns AndroidManifest.xml.
    Call this first to check exported components and deep links (intent-filters).

    Returns:
        Text content of AndroidManifest.xml (up to 8000 chars).
    """
    os.makedirs(WORKDIR, exist_ok=True)
    if not os.path.exists(APKTOOL_OUT):
        result = _run_tool(["apktool", "d", CURRENT_APK_PATH, "-o", APKTOOL_OUT, "-f"])
        if result.returncode != 0:
            return f"[ERROR] apktool 실행 실패:\n{result.stderr[:1000]}"

    manifest_path = os.path.join(APKTOOL_OUT, "AndroidManifest.xml")
    if not os.path.exists(manifest_path):
        return "[ERROR] AndroidManifest.xml을 찾을 수 없음"

    with open(manifest_path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()[:8000]


def decompile_apk() -> str:
    """Decompiles the current APK with jadx into Java-like source files
    and returns the file list. Call this when code-level analysis
    (hardcoded secrets, weak auth logic, etc.) is needed.
    Use read_source_file separately to read actual file contents.

    Returns:
        Relative paths of decompiled .java files (up to 40).
    """
    os.makedirs(JADX_OUT, exist_ok=True)
    result = _run_tool(["jadx", "-d", JADX_OUT, CURRENT_APK_PATH, "--no-res"])
    if result.returncode != 0 and not os.listdir(JADX_OUT):
        return f"[ERROR] jadx 실행 실패:\n{result.stderr[:1000]}"

    file_list = []
    for root, _, files in os.walk(JADX_OUT):
        for f in files:
            if f.endswith(".java"):
                file_list.append(os.path.relpath(os.path.join(root, f), JADX_OUT))

    preview = file_list[:40]
    more = f"\n...외 {len(file_list) - 40}개 더 있음" if len(file_list) > 40 else ""
    return "디컴파일 완료. 소스 파일 목록:\n" + "\n".join(preview) + more


def read_source_file(relative_path: str) -> str:
    """Reads the actual content of a specific file from the list returned
    by decompile_apk.

    Args:
        relative_path: Path relative to the jadx_out folder.
            Example: "com/example/CryptoClass.java"

    Returns:
        Source code content of the file (up to 6000 chars).
    """
    full_path = os.path.join(JADX_OUT, relative_path)
    if not os.path.exists(full_path):
        return f"[ERROR] 파일을 찾을 수 없음: {relative_path}"
    with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()[:6000]


SYSTEM_PROMPT = """\
You are a security analysis agent performing red teaming on an Android app.
For the given APK file, follow this process:

1. Call extract_manifest to inspect the manifest and identify exported
   components and deep links.
2. Call decompile_apk to get the list of decompiled source files.
3. Open suspicious files (e.g. classes with names related to encryption,
   login, or authentication) with read_source_file to look for hardcoded
   secrets, weak authentication logic, and similar issues.
4. Once analysis is complete, write a final report in this format:

## Findings (Unverified)
- [file path] Description of the finding, and 1-2 sentences on why it
  looks risky.

Note: This stage has no verification (verify) tool yet, so make sure the
report title says "Unverified" to make clear these are not confirmed
vulnerabilities.

**Hard rules (very important):**
- If a tool call result starts with "[ERROR]", that tool call failed.
  In this case, never fabricate analysis content based on general
  knowledge or memory of this app. Report the failure and the exact
  error message to the user as-is, and stop the analysis.
- You may only include findings that you actually confirmed by reading
  files directly via read_source_file or extract_manifest. Do not
  speculate based on "this type of app generally has this structure"
  without having read the file yourself. This is strictly forbidden.
- Ending your response without any tool calls is not allowed. The
  report must be based solely on actual tool results.
"""


def _extract_retry_delay(error: genai_errors.ClientError, default: float = 15.0) -> float:
    """Parses Google's suggested retry delay from a 429 error response.
    Falls back to `default` if not found."""
    try:
        details = error.details.get("error", {}).get("details", [])
        for d in details:
            if d.get("@type", "").endswith("RetryInfo"):
                m = re.match(r"(\d+(\.\d+)?)s", d.get("retryDelay", ""))
                if m:
                    return float(m.group(1)) + 2 
    except Exception:
        pass
    return default


def call_with_rate_limit_retry(client, **kwargs):
    """Retries automatically after waiting when hitting a 429
    (RESOURCE_EXHAUSTED) error. Needed because the free tier's RPM limit
    is easily hit by an agent loop that makes several tool calls."""
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        try:
            return client.models.generate_content(**kwargs)
        except genai_errors.ClientError as e:
            if getattr(e, "code", None) == 429 or "RESOURCE_EXHAUSTED" in str(e):
                wait = _extract_retry_delay(e)
                print(f"[속도 제한] 무료 티어 분당 요청 한도 초과. {wait:.0f}초 대기 후 재시도 "
                      f"({attempt}/{max_attempts})...")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("여러 번 재시도했지만 계속 속도 제한에 걸렸습니다. "
                        "잠시 후 다시 실행하거나 더 여유로운 모델로 바꿔보세요.")


def run_agent(apk_path: str):
    global CURRENT_APK_PATH
    CURRENT_APK_PATH = apk_path

    check_tools_installed() 

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[ERROR] 환경변수 GEMINI_API_KEY가 설정되어 있지 않습니다.")
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    response = call_with_rate_limit_retry(
        client,
        model="gemini-3.5-flash-lite",  
        contents=f"Analyze this APK from a red-teaming perspective and write a report: {apk_path}",
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[extract_manifest, decompile_apk, read_source_file],
            
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=10
            ),
        ),
    )

    
    history = getattr(response, "automatic_function_calling_history", None)
    if history:
        print("\n=== 실제 도구 호출 내역 (디버깅용) ===")
        for item in history:
            for part in getattr(item, "parts", []):
                if getattr(part, "function_call", None):
                    print(f"[호출] {part.function_call.name}({dict(part.function_call.args or {})})")
                if getattr(part, "function_response", None):
                    resp = part.function_response.response
                    preview = str(resp)[:300]
                    print(f"[결과] {preview}...")
    else:
        print("\n[경고] automatic_function_calling_history가 비어 있습니다 -> 도구가 한 번도 호출되지 않았을 수 있습니다.")

    print("\n=== 최종 보고서 ===")
    print(response.text)
    return response.text


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("사용법: python mobile_agent_gemini.py /path/to/app.apk")
        sys.exit(1)

    apk_path = sys.argv[1]
    if not os.path.exists(apk_path):
        print(f"파일을 찾을 수 없음: {apk_path}")
        sys.exit(1)

    run_agent(apk_path)
