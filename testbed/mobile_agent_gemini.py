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
                # 코드가 직접 일치 여부를 계산해서 명시적으로 알려줌 --
                # LLM이 직접 문자열을 비교하다가 실수하는 것을 방지
                short_name = component.split("/")[-1].lstrip(".")
                if short_name in line:
                    verdict = f"MATCH: the requested component ({component}) IS the foreground activity."
                else:
                    verdict = (f"MISMATCH: the requested component was {component}, but the "
                               f"actual foreground activity below is different. This does NOT "
                               f"confirm the launch succeeded.")
                return (f"{verdict}\n"
                        f"am start output: {start.stdout.strip()}\n"
                        f"Foreground activity line: {line.strip()}")
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


def read_recent_logcat(lines: int = 50, filter_text: str = "") -> str:
    """Reads the most recent lines of logcat. Use this after triggering
    something dynamically (e.g. after launch_activity loads a page in a
    WebView, or after send_broadcast) to check whether injected code or
    a target component actually executed, by looking for output it
    produced.

    Args:
        lines: How many recent logcat lines to return (before
            filtering, if filter_text is used).
        filter_text: Optional substring to filter for, e.g. the app's
            package name ("com.android.insecurebankv2") or a specific
            class name ("MyBroadCastReceiver"). System logcat is very
            noisy, so filtering to the relevant app/class makes it much
            easier to tell whether something you triggered actually
            produced output, instead of unrelated system noise. Leave
            empty to get the raw unfiltered tail.

    Returns:
        The requested number of recent logcat lines, or only the lines
        containing filter_text if it was given. If filter_text was
        given and nothing matched, this returns a message saying so --
        that itself is useful evidence (absence of expected output).
    """
    try:
        result = _adb(["logcat", "-d", "-t", str(lines)])
        if result.returncode != 0:
            return f"[ERROR] adb logcat failed:\n{result.stderr[:1000]}"
        output = result.stdout
        if filter_text:
            matched = [line for line in output.splitlines() if filter_text in line]
            if not matched:
                return f"[INFO] No logcat lines contained '{filter_text}' in the last {lines} lines."
            return "\n".join(matched)[-4000:]
        return output[-4000:]
    except FileNotFoundError as e:
        return f"[ERROR] {e}"
    except subprocess.TimeoutExpired:
        return "[ERROR] adb command timed out. Is the emulator running and connected (adb devices)?"


def send_broadcast(action: str, extras: str = "") -> str:
    """Sends a broadcast Intent via adb to test an exported
    BroadcastReceiver for spoofing (e.g. triggering app logic without
    proper authorization). Use this when the manifest shows a
    <receiver> with android:exported="true".

    Args:
        action: Intent action string to broadcast, matching an
            <action> found inside that receiver's intent-filter in the
            manifest.
        extras: Optional extra "am broadcast" arguments as a single
            string, e.g. '--es phonenumber 1234567890'. Leave empty if
            the receiver takes no extras (check the receiver's source
            code with read_source_file to see what extras it reads).

    Returns:
        Raw "am broadcast" output only. This does NOT include logcat --
        "result=0" here only means the OS delivered the broadcast, it
        does NOT prove the receiver's code ran. You MUST separately
        call read_recent_logcat with filter_text set to the receiver's
        class name to check for actual evidence before claiming this
        is Verified.
    """
    try:
        cmd = ["shell", "am", "broadcast", "-a", action]
        if extras:
            cmd += extras.split()
        result = _adb(cmd)
        if result.returncode != 0:
            return f"[ERROR] adb am broadcast failed:\n{result.stderr[:1000]}"

        return (f"am broadcast output: {result.stdout.strip()}\n"
                f"NOTE: this only confirms delivery, not that the receiver's code ran. "
                f"Call read_recent_logcat(filter_text='<ReceiverClassName>') next to check "
                f"for actual evidence before reporting this as Verified.")
    except FileNotFoundError as e:
        return f"[ERROR] {e}"
    except subprocess.TimeoutExpired:
        return "[ERROR] adb command timed out. Is the emulator running and connected (adb devices)?"


def query_content_provider(uri: str) -> str:
    """Queries an exported ContentProvider directly via adb, without
    going through the app's UI. Use this when the manifest shows a
    <provider> with android:exported="true", to check whether it
    returns data without requiring authentication or a permission.

    Args:
        uri: Full content:// URI to query. Build it from the provider's
            android:authorities value found in the manifest, e.g.
            "content://com.example.app.SomeProvider/some_path".

    Returns:
        The rows returned by the provider (if any), or an [ERROR] if
        the query failed (a permission error here would actually mean
        the provider is NOT vulnerable).
    """
    try:
        result = _adb(["shell", "content", "query", "--uri", uri])
        if result.returncode != 0:
            return f"[ERROR] adb content query failed:\n{result.stderr[:1000]}"
        output = result.stdout.strip()
        # 코드가 직접 판정해서 명시적으로 알려줌 -- "No result found"를
        # LLM이 긍정적으로 오해석하는 것을 방지
        if not output or "No result found" in output:
            return ("INCONCLUSIVE: the query returned no rows. This does NOT confirm "
                    "unauthorized data exposure -- it may mean the URI/table name was "
                    "wrong, or the table is legitimately empty. Do not report this as "
                    "Verified.")
        if "Exception" in output or "SecurityException" in output or "Permission" in output:
            return (f"BLOCKED: the query appears to have been rejected "
                    f"(permission/security exception). This means the provider is "
                    f"NOT vulnerable to unauthenticated access.\nRaw output: {output}")
        return f"DATA RETURNED: the provider returned actual row data below.\n{output}"
    except FileNotFoundError as e:
        return f"[ERROR] {e}"
    except subprocess.TimeoutExpired:
        return "[ERROR] adb command timed out. Is the emulator running and connected (adb devices)?"


def list_device_files(device_path: str) -> str:
    """Lists files at a path on the device via adb, e.g. to check
    whether the app writes sensitive data to world-readable external
    storage (/sdcard) instead of app-private storage.

    Args:
        device_path: Directory to list, e.g. "/sdcard" or
            "/sdcard/Android/data/<package_name>/files".

    Returns:
        Directory listing, or an [ERROR] if the path doesn't exist or
        isn't accessible.
    """
    try:
        result = _adb(["shell", "ls", "-la", device_path])
        if result.returncode != 0:
            return f"[ERROR] adb shell ls failed:\n{result.stderr[:1000]}"
        return result.stdout.strip() or "[INFO] Directory is empty."
    except FileNotFoundError as e:
        return f"[ERROR] {e}"
    except subprocess.TimeoutExpired:
        return "[ERROR] adb command timed out. Is the emulator running and connected (adb devices)?"


SYSTEM_PROMPT = """\
You are a security analysis agent performing red teaming on an Android app.
For the given APK file, follow this process:

1. Call extract_manifest to inspect the manifest. List every exported
   component you find (activities, receivers, providers -- look for
   android:exported="true" on <activity>, <receiver>, and <provider>
   tags), not just the first one you notice. Also note any deep links
   (intent-filters with a scheme/host).
2. Call decompile_apk to get the list of decompiled source files.
3. For EACH exported component listed in step 1 (not just one), read
   its source file with read_source_file to understand what it does
   and whether it trusts external input without validation.
4. Test EVERY exported component from step 1 dynamically -- do not
   stop after the first successful finding, keep going through the
   rest of the list. For each component, use your own judgment to
   decide which available tool (if any) would test whether it behaves
   insecurely, based on what you observed in its source code and its
   manifest declaration. You are not limited to the mapping below --
   it is a set of examples, not an exhaustive or mandatory recipe:
   - launch_activity: useful for an exported Activity that might be
     reachable without going through expected screens (e.g. login).
   - push_file + launch_activity + read_recent_logcat: useful when an
     Activity trusts a file's existence on external storage without
     verifying who created it (e.g. loads it into a WebView). Prefer
     staging JavaScript with a visible effect (e.g. window.alert) over
     a silent one (e.g. console.log), so the impact can also be
     confirmed visually.
   - send_broadcast: useful for an exported BroadcastReceiver, using
     whatever action you find in its intent-filter. After calling it,
     you MUST call read_recent_logcat with filter_text set to the
     receiver's class name to check for actual evidence -- do not
     conclude success from delivery alone.
   - query_content_provider: useful for an exported ContentProvider,
     using a URI built from its authorities value. It returns an
     explicit "INCONCLUSIVE", "BLOCKED", or "DATA RETURNED" verdict --
     trust that verdict as-is rather than re-judging the raw output
     yourself.
   - list_device_files: useful when source code shows the app writes
     to external storage (e.g. Environment.getExternalStorageDirectory()),
     to check whether sensitive files end up in a world-readable
     location.
   If none of the available tools fit a given component well, say so
   explicitly in the report instead of forcing an irrelevant tool call.
   Only mark a finding as "Verified" if a dynamic tool actually
   confirmed the behavior. If a dynamic check fails or the tool errors
   out (e.g. no emulator connected), leave that finding as "Unverified"
   and say why.
   launch_activity returns an explicit "MATCH" or "MISMATCH" verdict at
   the start of its result -- trust that verdict as-is; do not
   independently re-decide based on other parts of the output (e.g.
   the "am start output" line always contains the component name you
   requested regardless of what actually happened, so it is never
   evidence by itself). On a MISMATCH, either retry launch_activity for
   that component or report it as Unverified -- do not silently ignore
   a failed attempt and treat the component as verified anyway.
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
  dynamic check (launch_activity, push_file + read_recent_logcat,
  send_broadcast, query_content_provider, list_device_files). Do not
  speculate based on "this type of app generally has this structure"
  without having checked it yourself. This is strictly forbidden.
- Do not stop after your first successful finding. Go through every
  exported component identified in step 1 before writing the report.
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


def extract_exported_components(manifest_xml: str) -> dict:
    """매니페스트 텍스트에서 exported="true"인 컴포넌트를 태그 종류별로
    나눠서 짧은 이름(예: "PostLogin")을 추출한다.
    ".ClassName" 축약형과 "com.pkg.ClassName" 풀네임 둘 다 마지막
    점(.) 뒤의 클래스명만 정확히 뽑도록 처리한다.

    Returns:
        {"activity": [...], "receiver": [...], "provider": [...]}
    """
    result = {"activity": [], "receiver": [], "provider": []}
    for tag in result.keys():
        for block in re.findall(rf"<{tag}\b[^>]*>", manifest_xml):
            if not re.search(r'exported\s*=\s*["\']true["\']', block):
                continue
            m = re.search(r'name\s*=\s*["\']([^"\']+)["\']', block)
            if m:
                full = m.group(1)
                short = full.rsplit(".", 1)[-1] if "." in full else full
                if short:
                    result[tag].append(short)
    return result


def audit_report(report_text: str, tool_calls: list, exported: dict = None) -> str:
    """LLM이 쓴 최종 리포트가 실제 도구 실행 결과와 모순되지 않는지 코드로
    기계적으로 재검수한다. launch_activity의 MATCH/MISMATCH,
    query_content_provider의 DATA RETURNED/INCONCLUSIVE 판정은 이미 코드가
    계산해서 도구 결과에 박아둔 객관적 사실이므로, 리포트가 그 사실과
    모순되면(예: MISMATCH였는데 [Verified]라고 적음) 자동으로 경고를 붙인다.
    exported가 주어지면, 아예 시도조차 안 됐는데 [Verified]로 적힌
    컴포넌트가 있는지도 잡아낸다 (커버리지 사각지대 방지 -- '틀린 판정'뿐
    아니라 '아예 호출 안 하고 지어낸 것'까지 커버).
    LLM에게 정확히 써달라고 부탁하는 것의 한계를 코드로 보완하는 마지막
    방어선이다."""
    activity_matched = {}   # short_name -> 그 컴포넌트에 대해 한 번이라도 MATCH가 있었는가
    activity_attempted = set()  # launch_activity로 시도된 짧은 이름들
    provider_has_data = {}  # uri -> 한 번이라도 DATA RETURNED가 있었는가

    for name, args, result in tool_calls:
        result_str = str(result).strip()
        if name == "launch_activity":
            component = args.get("component", "")
            short_name = component.split("/")[-1].lstrip(".")
            if short_name:
                activity_attempted.add(short_name)
                is_match = result_str.startswith("MATCH:")
                activity_matched[short_name] = activity_matched.get(short_name, False) or is_match
        elif name == "query_content_provider":
            uri = args.get("uri", "")
            is_data = result_str.startswith("DATA RETURNED:")
            provider_has_data[uri] = provider_has_data.get(uri, False) or is_data

    warnings = []
    for line in report_text.splitlines():
        if "[Verified]" not in line:
            continue
        for short_name, matched in activity_matched.items():
            if short_name and short_name in line and not matched:
                warnings.append(
                    f"'{short_name}'가 [Verified]로 적혀 있지만, 실제 launch_activity "
                    f"결과는 MATCH가 아니었습니다 (MISMATCH만 있었음). 해당 줄: {line.strip()}"
                )
        # 아예 launch_activity를 호출조차 안 한 exported activity가
        # [Verified]로 적혀 있으면 잡아낸다 (activity_matched엔 애초에
        # 키가 없으니 위 검사만으로는 못 잡는 사각지대)
        if exported:
            for exp_name in exported.get("activity", []):
                if exp_name and exp_name in line and exp_name not in activity_attempted:
                    warnings.append(
                        f"'{exp_name}'가 [Verified]로 적혀 있지만, launch_activity가 "
                        f"이 컴포넌트에 대해 단 한 번도 호출되지 않았습니다 (아예 시도 "
                        f"안 한 것을 검증된 것처럼 서술함). 해당 줄: {line.strip()}"
                    )
        for uri, has_data in provider_has_data.items():
            uri_tail = uri.split("/")[-2] if "/" in uri else uri
            if uri_tail and uri_tail in line and not has_data:
                warnings.append(
                    f"ContentProvider 관련 줄이 [Verified]로 적혀 있지만, 실제 "
                    f"query_content_provider 결과는 DATA RETURNED가 아니었습니다 "
                    f"(INCONCLUSIVE/BLOCKED였음). 해당 줄: {line.strip()}"
                )

    coverage_notes = []
    if exported:
        untested_activities = [
            n for n in exported.get("activity", []) if n and n not in activity_attempted
        ]
        if untested_activities:
            coverage_notes.append(
                f"매니페스트에서 exported activity로 확인됐지만 launch_activity로 "
                f"한 번도 시도되지 않은 것: {', '.join(sorted(set(untested_activities)))}"
            )

    if warnings:
        report_text += "\n\n---\n### ⚠️ 자동 재검수 경고 (기계적 사실 대조 결과)\n"
        report_text += "위 리포트의 일부 [Verified] 표시가 실제 도구 실행 결과와 모순됩니다:\n"
        report_text += "\n".join(f"- {w}" for w in warnings)
    if coverage_notes:
        report_text += "\n\n---\n### 📋 커버리지 점검 (기계적 계산)\n"
        report_text += "\n".join(f"- {c}" for c in coverage_notes)
    return report_text


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
                   launch_activity, push_file, read_recent_logcat,
                   send_broadcast, query_content_provider, list_device_files],
            
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=40
            ),
        ),
    )

    
    history = getattr(response, "automatic_function_calling_history", None)
    tool_calls = []  # audit_report에 넘길 (도구명, 인자, 결과) 구조화 리스트
    if history:
        print("\n=== 실제 도구 호출 내역 (디버깅용) ===")
        pending_call = None
        for item in history:
            for part in getattr(item, "parts", []):
                if getattr(part, "function_call", None):
                    pending_call = (part.function_call.name, dict(part.function_call.args or {}))
                    print(f"[호출] {pending_call[0]}({pending_call[1]})")
                if getattr(part, "function_response", None):
                    resp = part.function_response.response
                    preview = str(resp)[:300]
                    print(f"[결과] {preview}...")
                    if pending_call:
                        result_value = resp.get("result") if isinstance(resp, dict) else resp
                        tool_calls.append((pending_call[0], pending_call[1], result_value))
                        pending_call = None
    else:
        print("\n[경고] automatic_function_calling_history가 비어 있습니다 -> 도구가 한 번도 호출되지 않았을 수 있습니다.")

    print("\n=== 최종 보고서 ===")
    if response.text:
        manifest_text = next(
            (str(result) for name, args, result in tool_calls if name == "extract_manifest"),
            ""
        )
        exported = extract_exported_components(manifest_text) if manifest_text else {}
        final_report = audit_report(response.text, tool_calls, exported)
        print(final_report)
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
