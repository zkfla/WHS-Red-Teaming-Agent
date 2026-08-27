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


def _adb(args: list, timeout: int = 20) -> subprocess.CompletedProcess:
    """Runs an adb command. Tries 'adb' first, falls back to 'adb.exe'
    (useful in WSL where the emulator runs on the Windows side)."""
    adb_bin = shutil.which("adb") or shutil.which("adb.exe")
    if adb_bin is None:
        raise FileNotFoundError("adb not found on PATH (checked 'adb' and 'adb.exe')")
    return subprocess.run([adb_bin] + args, capture_output=True, text=True, timeout=timeout)


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


def launch_activity(component: str, extras: str = "") -> str:
    """Directly launches a specific Activity via adb, bypassing normal
    app navigation (e.g. skipping the login screen). Use this to check
    whether an exported activity found in the manifest can be reached
    without authentication.

    Args:
        component: Full component name, e.g.
            "com.android.insecurebankv2/.PostLogin"
        extras: Optional extra "am start" arguments as a single string,
            e.g. '--es uname attacker'. Leave empty if none needed.

    Returns:
        The resulting foreground activity as reported by adb, so you can
        check whether launching directly actually worked (i.e. whether
        the target activity became the foreground activity instead of
        being redirected to a login screen).
    """
    try:
        cmd = ["shell", "am", "start", "-n", component]
        if extras:
            cmd += extras.split()
        start = _adb(cmd)
        if start.returncode != 0:
            return f"[ERROR] adb am start failed:\n{start.stderr[:1000]}"

        time.sleep(1.5)  # give the activity a moment to come to the foreground
        dump = _adb(["shell", "dumpsys", "activity", "activities"])
        if dump.returncode != 0:
            return f"[ERROR] adb dumpsys failed:\n{dump.stderr[:1000]}"

        for line in dump.stdout.splitlines():
            if "mResumedActivity" in line or "topResumedActivity" in line:
                return f"am start output: {start.stdout.strip()}\nForeground activity line: {line.strip()}"
        return f"am start output: {start.stdout.strip()}\n[WARN] Could not find foreground activity line in dumpsys output."
    except FileNotFoundError as e:
        return f"[ERROR] {e}"
    except subprocess.TimeoutExpired:
        return "[ERROR] adb command timed out. Is the emulator running and connected (adb devices)?"


def push_file(device_path: str, content: str) -> str:
    """Writes arbitrary text/HTML content to a local temp file and pushes
    it to the device at the given path via adb push. Use this to stage
    an attacker-controlled file (e.g. a spoofed HTML page) before
    triggering an activity that reads it, when static analysis shows
    the app trusts a file's existence without verifying its origin.

    Args:
        device_path: Full path on the device, e.g.
            "/sdcard/Statements_test.html"
        content: The file content to write (e.g. an HTML page with a
            <script> tag that calls window.alert(...) so the effect is
            visible on screen, not just in logcat).

    Returns:
        adb push output, or an [ERROR] if it failed.
    """
    try:
        local_tmp = os.path.join(WORKDIR, "_push_tmp")
        os.makedirs(WORKDIR, exist_ok=True)
        with open(local_tmp, "w", encoding="utf-8") as f:
            f.write(content)
        result = _adb(["push", local_tmp, device_path])
        if result.returncode != 0:
            return f"[ERROR] adb push failed:\n{result.stderr[:1000]}"
        return f"Pushed to {device_path}: {result.stdout.strip()}"
    except FileNotFoundError as e:
        return f"[ERROR] {e}"
    except subprocess.TimeoutExpired:
        return "[ERROR] adb command timed out. Is the emulator running and connected (adb devices)?"


def read_recent_logcat(lines: int = 50) -> str:
    """Reads the most recent lines of logcat. Use this after triggering
    something dynamically (e.g. after launch_activity loads a page in a
    WebView) to check whether injected JavaScript or other code actually
    executed, by looking for output it produced (e.g. console.log calls
    show up here for WebView JS).

    Args:
        lines: How many recent logcat lines to return.

    Returns:
        The requested number of recent logcat lines.
    """
    try:
        result = _adb(["logcat", "-d", "-t", str(lines)])
        if result.returncode != 0:
            return f"[ERROR] adb logcat failed:\n{result.stderr[:1000]}"
        return result.stdout[-4000:]
    except FileNotFoundError as e:
        return f"[ERROR] {e}"
    except subprocess.TimeoutExpired:
        return "[ERROR] adb command timed out. Is the emulator running and connected (adb devices)?"


SYSTEM_PROMPT = """\
You are a security analysis agent performing red teaming on an Android app.
For the given APK file, follow this process:

1. Call extract_manifest to inspect the manifest and identify exported
   components and deep links.
2. Call decompile_apk to get the list of decompiled source files.
3. Open suspicious files (e.g. classes with names related to encryption,
   login, or authentication) with read_source_file to look for hardcoded
   secrets, weak authentication logic, and similar issues.
4. For any exported Activity you identified as suspicious in the
   manifest (e.g. an activity that should only be reachable after
   login), use launch_activity to try launching it directly and check
   whether it bypasses expected screens.
   Additionally, if source code shows an Activity trusts a file's
   existence on external storage without verifying who created it
   (e.g. it checks a file exists on the sdcard and then loads it into a
   WebView), use push_file to stage an attacker-controlled file
   matching the expected naming pattern, then launch_activity with the
   matching intent extra, then read_recent_logcat to check whether
   injected content (e.g. JavaScript) actually executed. Prefer
   staging JavaScript with a visible effect (e.g. window.alert) over a
   silent one (e.g. console.log), so the impact can also be confirmed
   visually and not only via logcat.
   Only mark a finding as "Verified" if a dynamic tool actually
   confirmed the behavior. If a dynamic check fails or the tool errors
   out (e.g. no emulator connected), leave that finding as "Unverified"
   and say why.
   When using launch_activity, only count it as confirmed if the
   "Foreground activity line" in the result actually names the
   component you tried to launch. If it names a different activity
   (e.g. the app redirected elsewhere, or a stale foreground activity
   from a previous call was reported), do not claim that launch
   succeeded, even if the component name appears elsewhere in the
   output (e.g. in the "am start output" line).
5. Once analysis is complete, write a final report in this format:

## Findings
- [Verified/Unverified] [file path or component] Description of the
  finding, 1-2 sentences on why it looks risky, and (if verified) what
  the dynamic check showed.

Note: Only mark something "Verified" if a dynamic tool (launch_activity,
push_file + read_recent_logcat, etc.) actually confirmed it. Everything
else stays "Unverified".

**Hard rules (very important):**
- If a tool call result starts with "[ERROR]", that tool call failed.
  In this case, never fabricate analysis content based on general
  knowledge or memory of this app. Report the failure and the exact
  error message to the user as-is, and stop the analysis.
- You may only include findings that you actually confirmed by reading
  files directly (read_source_file, extract_manifest) or by running a
  dynamic check (launch_activity, push_file + read_recent_logcat). Do
  not speculate based on "this type of app generally has this
  structure" without having checked it yourself. This is strictly
  forbidden.
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
            tools=[extract_manifest, decompile_apk, read_source_file,
                   launch_activity, push_file, read_recent_logcat],
            
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=20
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
    if response.text:
        print(response.text)
    else:
        print("[경고] 최종 텍스트 응답이 없습니다.")
        print("가능한 원인: maximum_remote_calls 한도에 도달해서 LLM이 도구를 "
              "한 번 더 부르려다 응답이 중간에 끊겼을 수 있습니다.")
        print("해결: run_agent 안의 maximum_remote_calls 값을 늘려서 다시 시도해보세요.")
        # 혹시 마지막 응답에 텍스트 조각이 일부라도 있으면 참고용으로 출력
        for cand in getattr(response, "candidates", []) or []:
            for part in getattr(cand.content, "parts", []) or []:
                if getattr(part, "text", None):
                    print(f"[참고: 마지막 후보의 텍스트 조각] {part.text}")
                if getattr(part, "function_call", None):
                    print(f"[참고: 끊긴 시점에 시도하려던 도구 호출] {part.function_call.name}"
                          f"({dict(part.function_call.args or {})})")
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
