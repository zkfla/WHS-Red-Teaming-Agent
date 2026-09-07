"""
모바일 레드티밍 도구 9개를 MCP 서버로 노출한다.
진짜 Codex CLI가 이 서버를 자식 프로세스로 띄워서, stdio로 통신하며
우리 도구들을 호출한다.

실행법(직접 실행 안 함, Codex가 알아서 띄움):
    python3 mcp_server.py
"""

import os
import sys
import shutil
import subprocess
import time
import json
import functools
import inspect

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("mobile-redteam")


def _run_tool(args: list) -> subprocess.CompletedProcess:
    resolved = shutil.which(args[0])
    use_shell = bool(resolved) and resolved.lower().endswith((".bat", ".cmd"))
    return subprocess.run(args, capture_output=True, text=True, timeout=120, shell=use_shell)


def _adb(args: list, timeout: int = 20) -> subprocess.CompletedProcess:
    # WSL 환경에서는 리눅스용 adb(/usr/bin/adb)가 별도로 설치되어 있을 수
    # 있는데, 이건 윈도우 쪽 에뮬레이터와 별개의 서버라 명령이 실패한다.
    # 실제 에뮬레이터가 떠 있는 adb.exe(윈도우 SDK)를 우선 사용한다.
    adb_bin = shutil.which("adb.exe") or shutil.which("adb")
    if adb_bin is None:
        raise FileNotFoundError("adb not found on PATH (checked 'adb.exe' and 'adb')")
    return subprocess.run([adb_bin] + args, capture_output=True, text=True, timeout=timeout)


WORKDIR = "./agent_workdir"
JADX_OUT = os.path.join(WORKDIR, "jadx_out")
APKTOOL_OUT = os.path.join(WORKDIR, "apktool_out")
LOG_PATH = os.path.join(WORKDIR, "mcp_tool_log.jsonl")


def _log_call(name: str, args: dict, result: str):
    """나중에 audit_report로 재검수할 수 있도록 모든 도구 호출을
    파일에 순서대로 기록해둔다 (Codex 자체 로그와는 별개)."""
    os.makedirs(WORKDIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"name": name, "args": args, "result": str(result)}, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 도구 9개 -- 기존 API 키 버전과 완전히 동일한 순수 로직
# ---------------------------------------------------------------------------

def extract_manifest(apk_path: str) -> str:
    """Unpacks the given APK with apktool and returns AndroidManifest.xml.
    Call this first to check exported components and deep links.

    Args:
        apk_path: Absolute path to the APK file being analyzed (the
            same path given in the task).
    """
    os.makedirs(WORKDIR, exist_ok=True)
    if not os.path.exists(APKTOOL_OUT):
        result = _run_tool(["apktool", "d", apk_path, "-o", APKTOOL_OUT, "-f"])
        if result.returncode != 0:
            return f"[ERROR] apktool 실행 실패:\n{result.stderr[:1000]}"
    manifest_path = os.path.join(APKTOOL_OUT, "AndroidManifest.xml")
    if not os.path.exists(manifest_path):
        return "[ERROR] AndroidManifest.xml을 찾을 수 없음"
    with open(manifest_path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()[:15000]


def decompile_apk(apk_path: str) -> str:
    """Decompiles the given APK with jadx into Java-like source files
    and returns the file list. Use read_source_file separately to read
    actual file contents. Useful for finding hardcoded secrets, weak
    authentication logic, insecure storage, insecure cryptography, and
    other issues -- illustrative, not exhaustive.

    Args:
        apk_path: Absolute path to the APK file being analyzed (the
            same path given in the task).
    """
    os.makedirs(JADX_OUT, exist_ok=True)
    result = _run_tool(["jadx", "-d", JADX_OUT, apk_path, "--no-res"])
    if result.returncode != 0 and not os.listdir(JADX_OUT):
        return f"[ERROR] jadx 실행 실패:\n{result.stderr[:1000]}"
    file_list = []
    for root, _, files in os.walk(JADX_OUT):
        for f in files:
            if f.endswith(".java"):
                file_list.append(os.path.relpath(os.path.join(root, f), JADX_OUT))
    preview = file_list[:150]
    more = f"\n...외 {len(file_list) - 150}개 더 있음" if len(file_list) > 150 else ""
    return "디컴파일 완료. 소스 파일 목록:\n" + "\n".join(preview) + more


def read_source_file(relative_path: str) -> str:
    """Reads the actual content of a specific file from the list
    returned by decompile_apk.

    Args:
        relative_path: Path relative to the jadx_out folder, e.g.
            'com/example/CryptoClass.java'
    """
    full_path = os.path.join(JADX_OUT, relative_path)
    if not os.path.exists(full_path):
        return f"[ERROR] 파일을 찾을 수 없음: {relative_path}"
    with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()[:20000]


def launch_activity(component: str, extras: str = "") -> str:
    """Directly launches a specific Activity via adb, bypassing normal
    app navigation. Use this to check whether an exported activity can
    be reached without authentication. Returns an explicit MATCH/MISMATCH
    verdict computed by code -- trust it as-is.

    Args:
        component: Full component name, e.g. 'com.example.someapp/.SomeActivity'
        extras: Optional extra 'am start' arguments, e.g. '--es uname attacker'
    """
    try:
        cmd = ["shell", "am", "start", "-n", component]
        if extras:
            cmd += extras.split()
        start = _adb(cmd)
        if start.returncode != 0:
            return f"[ERROR] adb am start failed:\n{start.stderr[:1000]}"
        time.sleep(1.5)
        dump = _adb(["shell", "dumpsys", "activity", "activities"])
        if dump.returncode != 0:
            return f"[ERROR] adb dumpsys failed:\n{dump.stderr[:1000]}"
        for line in dump.stdout.splitlines():
            if "mResumedActivity" in line or "topResumedActivity" in line:
                short_name = component.split("/")[-1].lstrip(".")
                if short_name in line:
                    verdict = f"MATCH: the requested component ({component}) IS the foreground activity."
                else:
                    verdict = (f"MISMATCH: the requested component was {component}, but the "
                               f"actual foreground activity below is different. This does NOT "
                               f"confirm the launch succeeded.")
                return (f"{verdict}\nam start output: {start.stdout.strip()}\n"
                        f"Foreground activity line: {line.strip()}")
        return f"am start output: {start.stdout.strip()}\n[WARN] Could not find foreground activity line."
    except FileNotFoundError as e:
        return f"[ERROR] {e}"
    except subprocess.TimeoutExpired:
        return "[ERROR] adb command timed out. Is the emulator running and connected?"


def push_file(device_path: str, content: str) -> str:
    """Writes arbitrary text/HTML content to a local temp file and
    pushes it to the device via adb push. Use this to stage an
    attacker-controlled file when static analysis shows the app trusts
    a file's existence without verifying its origin.

    Args:
        device_path: Full path on the device, e.g. '/sdcard/some_file.html'
        content: The file content (e.g. HTML with a <script> tag calling
            both window.alert(...) for visible confirmation AND
            console.log('<unique_marker>') for logcat-based verification).
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
        return "[ERROR] adb command timed out. Is the emulator running and connected?"


def read_recent_logcat(lines: int = 50, filter_text: str = "") -> str:
    """Reads the most recent lines of logcat. Use this after triggering
    something dynamically to check whether injected code actually executed.
    If filter_text is given and nothing matches on the first try, this
    automatically waits and retries once -- WebView console logs (e.g.
    from console.log in staged JS) can take a moment to actually reach
    logcat after the page loads, so an immediate check can miss them
    even when the script genuinely ran.

    Args:
        lines: How many recent logcat lines to return.
        filter_text: Optional substring to filter for. Leave empty for
            the raw unfiltered tail.
    """
    try:
        def _grab_and_filter():
            result = _adb(["logcat", "-d", "-t", str(lines)])
            if result.returncode != 0:
                return None, f"[ERROR] adb logcat failed:\n{result.stderr[:1000]}"
            output = result.stdout
            if filter_text:
                matched = [line for line in output.splitlines() if filter_text in line]
                return matched, output
            return None, output

        matched, output = _grab_and_filter()
        if matched is None and not filter_text:
            return output[-4000:]
        if output.startswith("[ERROR]"):
            return output

        if filter_text and not matched:
            # WebView 콘솔 로그 등은 페이지 로드 직후 곧바로 logcat에
            # 안 찍힐 수 있어서, 잠깐 더 기다렸다가 한 번 더 시도한다
            time.sleep(1.5)
            matched, output = _grab_and_filter()
            if output.startswith("[ERROR]"):
                return output

        if filter_text:
            if not matched:
                return (f"[INFO] No logcat lines contained '{filter_text}' in the "
                        f"last {lines} lines (checked twice, 1.5s apart).")
            return "\n".join(matched)[-4000:]
        return output[-4000:]
    except FileNotFoundError as e:
        return f"[ERROR] {e}"
    except subprocess.TimeoutExpired:
        return "[ERROR] adb command timed out. Is the emulator running and connected?"


def send_broadcast(action: str, extras: str = "") -> str:
    """Sends a broadcast Intent via adb to test an exported
    BroadcastReceiver for spoofing. Returns only delivery confirmation
    -- you MUST separately call read_recent_logcat with filter_text set
    to the receiver's class name to check for actual evidence.

    Args:
        action: Intent action string matching an <action> in the
            receiver's intent-filter.
        extras: Optional extra 'am broadcast' arguments, e.g.
            '--es phonenumber 1234567890'
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
                f"Call read_recent_logcat(filter_text='<ReceiverClassName>') next.")
    except FileNotFoundError as e:
        return f"[ERROR] {e}"
    except subprocess.TimeoutExpired:
        return "[ERROR] adb command timed out. Is the emulator running and connected?"


def query_content_provider(uri: str) -> str:
    """Queries an exported ContentProvider directly via adb. Returns an
    explicit DATA RETURNED / INCONCLUSIVE / BLOCKED verdict computed by
    code -- trust it as-is. If INCONCLUSIVE due to an empty table, try
    insert_content_provider instead.

    Args:
        uri: Full content:// URI, built from the provider's authorities value.
    """
    try:
        result = _adb(["shell", "content", "query", "--uri", uri])
        if result.returncode != 0:
            return f"[ERROR] adb content query failed:\n{result.stderr[:1000]}"
        output = result.stdout.strip()
        if not output or "No result found" in output:
            return ("INCONCLUSIVE: the query returned no rows. This does NOT confirm "
                    "unauthorized data exposure. Consider trying insert_content_provider instead.")
        if "Exception" in output or "SecurityException" in output or "Permission" in output:
            return (f"BLOCKED: the query was rejected (permission/security exception). "
                    f"NOT vulnerable to unauthenticated access.\nRaw output: {output}")
        return f"DATA RETURNED: the provider returned actual row data below.\n{output}"
    except FileNotFoundError as e:
        return f"[ERROR] {e}"
    except subprocess.TimeoutExpired:
        return "[ERROR] adb command timed out. Is the emulator running and connected?"


def insert_content_provider(uri: str, values: str) -> str:
    """Attempts to insert a row into an exported ContentProvider via
    adb, without authentication. Use this when query_content_provider
    returned INCONCLUSIVE -- an unauthenticated write succeeding is
    independent proof of the same vulnerability.

    Args:
        uri: Same content:// URI as used with query_content_provider.
        values: One or more 'content insert' --bind arguments, e.g.
            '--bind username:s:attacker'
    """
    try:
        cmd = ["shell", "content", "insert", "--uri", uri] + values.split()
        result = _adb(cmd)
        output = (result.stdout + result.stderr).strip()
        if (result.returncode != 0 or "Exception" in output
                or "SecurityException" in output or "Permission" in output):
            return f"BLOCKED: the insert was rejected.\nRaw output: {output[:1000]}"
        return (f"INSERT_SUCCEEDED: the row was written without any permission check.\n"
                f"Raw output: {output[:500]}")
    except FileNotFoundError as e:
        return f"[ERROR] {e}"
    except subprocess.TimeoutExpired:
        return "[ERROR] adb command timed out. Is the emulator running and connected?"


def list_device_files(device_path: str) -> str:
    """Lists files at a path via adb. Automatically resolves and lists
    one level of symlink target in the same call, so a single call is
    enough even for symlinked paths like /sdcard.

    Args:
        device_path: Directory to list, e.g. '/sdcard'
    """
    try:
        path_for_listing = device_path if device_path.endswith("/") else device_path + "/"
        result = _adb(["shell", "ls", "-la", path_for_listing])
        if result.returncode != 0:
            return f"[ERROR] adb shell ls failed:\n{result.stderr[:1000]}"
        parts = [f"[{device_path}]\n" + (result.stdout.strip() or "[INFO] Directory is empty.")]
        resolved = _adb(["shell", "readlink", "-f", device_path])
        target = resolved.stdout.strip() if resolved.returncode == 0 else ""
        if target and target.rstrip("/") != device_path.rstrip("/"):
            target_listing = _adb(["shell", "ls", "-la", target + "/"])
            if target_listing.returncode == 0:
                parts.append(f"\n[symlink target: {target}]\n" +
                             (target_listing.stdout.strip() or "[INFO] Directory is empty."))
        return "\n".join(parts)
    except FileNotFoundError as e:
        return f"[ERROR] {e}"
    except subprocess.TimeoutExpired:
        return "[ERROR] adb command timed out. Is the emulator running and connected?"


TOOL_FUNCTIONS = {
    "extract_manifest": extract_manifest,
    "decompile_apk": decompile_apk,
    "read_source_file": read_source_file,
    "launch_activity": launch_activity,
    "push_file": push_file,
    "read_recent_logcat": read_recent_logcat,
    "send_broadcast": send_broadcast,
    "query_content_provider": query_content_provider,
    "insert_content_provider": insert_content_provider,
    "list_device_files": list_device_files,
}


def _make_logged_tool(fn):
    """원본 함수의 시그니처/설명은 그대로 유지하면서, 호출 결과를 파일에
    기록하는 래퍼를 만든다. FastMCP가 스키마를 만들 때 원본 시그니처를
    그대로 읽도록 __signature__를 명시적으로 복사해준다."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        result = fn(*args, **kwargs)
        _log_call(fn.__name__, kwargs, result)
        return result
    wrapper.__signature__ = inspect.signature(fn)
    return wrapper


for _name, _fn in TOOL_FUNCTIONS.items():
    mcp.tool()(_make_logged_tool(_fn))


if __name__ == "__main__":
    mcp.run()
