import json
import subprocess
import time
from pathlib import Path


ADB_PATH = None
PACKAGE_NAME = None


def configure_dynamic_target(adb_path: str, package_name: str):
    """에이전트가 조작할 adb 경로와 대상 앱 패키지명을 설정한다."""
    global ADB_PATH, PACKAGE_NAME
    ADB_PATH = adb_path
    PACKAGE_NAME = package_name


def _adb(*args, timeout=15):
    if ADB_PATH is None:
        raise RuntimeError("동적분석 대상이 설정되지 않았습니다.")
    result = subprocess.run(
        [ADB_PATH, *args],
        capture_output=True, text=True, timeout=timeout
    )
    return result


def check_device_ready() -> str:
    """
    에뮬레이터/기기가 연결되어 있고 대상 앱이 설치되어 있는지 확인한다.
    동적분석을 시작하기 전에 반드시 먼저 호출한다.
    """
    print("[AGENT TOOL] check_device_ready()")

    devices = _adb("devices").stdout
    device_connected = "\tdevice" in devices

    packages = _adb("shell", "pm", "list", "packages", PACKAGE_NAME).stdout
    app_installed = PACKAGE_NAME in packages

    return json.dumps({
        "device_connected": device_connected,
        "app_installed": app_installed,
        "raw_devices": devices.strip()
    }, ensure_ascii=False, indent=2)


def get_current_screen() -> str:
    """
    현재 에뮬레이터 화면에 어떤 Activity가 떠 있는지 확인한다.
    launch_activity 실행 후, 실제로 해당 화면으로 전환됐는지
    (즉 인증을 우회하고 화면 진입에 성공했는지) 검증할 때 사용한다.
    """
    print("[AGENT TOOL] get_current_screen()")

    result = _adb("shell", "dumpsys", "activity", "activities")
    lines = result.stdout.splitlines()

    focused = [l.strip() for l in lines if "mResumedActivity" in l or "mFocusedActivity" in l]

    return json.dumps({
        "focused_activity_lines": focused
    }, ensure_ascii=False, indent=2)


def launch_activity(activity_name: str) -> str:
    """
    지정한 activity를 인증 절차 없이 직접 실행한다.
    exported activity가 로그인 등 인증을 우회하여 접근 가능한지 검증할 때 사용한다.

    Args:
        activity_name: manifest에서 얻은 activity 클래스명.
                        전체 경로(com.xxx.yyy.ZZZActivity) 또는
                        패키지명 기준 상대 경로(.ZZZActivity) 모두 가능.
    """
    print(f"[AGENT TOOL] launch_activity({activity_name})")

    if activity_name.startswith("."):
        component = f"{PACKAGE_NAME}/{activity_name}"
    elif "/" in activity_name:
        component = activity_name
    else:
        component = f"{PACKAGE_NAME}/{activity_name}"

    result = _adb("shell", "am", "start", "-n", component)

    time.sleep(1)
    current = json.loads(get_current_screen())

    return json.dumps({
        "component": component,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "command_succeeded": "Error" not in result.stderr and result.returncode == 0,
        "current_screen_after_launch": current["focused_activity_lines"]
    }, ensure_ascii=False, indent=2)


def inspect_app_storage() -> str:
    """
    앱의 내부 저장소(shared_prefs, databases, files 디렉터리)를 조사한다.
    평문 저장된 비밀번호, 토큰 등 민감정보 흔적을 찾을 때 사용한다.
    (대상 앱이 디버거블 빌드여야 run-as 접근이 가능하다)
    """
    print("[AGENT TOOL] inspect_app_storage()")

    result = {}

    for subdir in ["shared_prefs", "databases", "files"]:
        listing = _adb("shell", "run-as", PACKAGE_NAME, "ls", subdir)
        files = [f for f in listing.stdout.splitlines() if f.strip()]
        result[subdir] = files

    prefs_content = {}
    for fname in result.get("shared_prefs", []):
        content = _adb("shell", "run-as", PACKAGE_NAME, "cat", f"shared_prefs/{fname}")
        prefs_content[fname] = content.stdout[:2000]

    result["shared_prefs_content"] = prefs_content

    return json.dumps(result, ensure_ascii=False, indent=2)


def capture_logcat(seconds: int = 5, filter_tag: str = "") -> str:
    """
    최근 logcat 버퍼를 캡처한다. WebView의 JavaScript 인터페이스 호출,
    SSL 오류 무시 로그, 민감정보 노출 등을 확인할 때 사용한다.

    Args:
        seconds: 참고용 캡처 구간(초). 실제로는 현재 버퍼를 즉시 덤프한다.
        filter_tag: 특정 태그/패키지/키워드로 필터링할 문자열 (비우면 전체).
    """
    print(f"[AGENT TOOL] capture_logcat(filter_tag={filter_tag})")

    proc = _adb("logcat", "-d", timeout=20)
    lines = proc.stdout.splitlines()

    if filter_tag:
        lines = [l for l in lines if filter_tag.lower() in l.lower()]

    return json.dumps({
        "line_count": len(lines),
        "lines": lines[-200:]
    }, ensure_ascii=False, indent=2)


def take_screenshot(save_name: str = "screenshot.png") -> str:
    """
    현재 에뮬레이터 화면을 캡처해서 output 폴더에 저장한다.
    취약점이 실제로 트리거된 화면을 증거로 남길 때 사용한다.

    Args:
        save_name: 저장할 파일 이름 (예: activity_bypass.png)
    """
    print(f"[AGENT TOOL] take_screenshot({save_name})")

    remote_path = "/sdcard/claude_capture.png"
    _adb("shell", "screencap", "-p", remote_path)

    local_path = Path("output") / "evidence" / save_name
    local_path.parent.mkdir(parents=True, exist_ok=True)

    _adb("pull", remote_path, str(local_path))

    return json.dumps({
        "saved_to": str(local_path),
        "exists": local_path.exists()
    }, ensure_ascii=False, indent=2)