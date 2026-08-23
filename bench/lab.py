"""실험 환경을 재현 가능하게 구성한다.

수동 docker 명령으로 만든 환경은 팀원이 재현할 수 없고, 조용히 달라진다.
여기가 환경의 단일 정의다.

구성:
    whs-bench (--internal, 외부 경로 없음)
      ├── juiceshop-bench   ← bridge에도 연결 (호스트 127.0.0.1:3100 노출, 채점용)
      └── whs-sandbox       ← whs-bench 전용 = 인터넷 접근 불가

이 격리가 스코프 강제의 진짜 경계다. tools/shell.py의 정규식은 보조 수단.
"""
import json
import re
import subprocess
import time
import urllib.request

import config

NEEDED_TOOLS = ["curl", "sqlmap", "nmap", "jq", "python3"]


def _run(*argv, check=True):
    return subprocess.run(argv, capture_output=True, text=True, check=check)


def _exists(name: str) -> bool:
    out = _run("docker", "ps", "-aq", "-f", f"name=^{name}$", check=False).stdout
    return bool(out.strip())


def ensure_network():
    out = _run("docker", "network", "ls", "--format", "{{.Name}}", check=False).stdout
    if config.BENCH_NETWORK not in out.split():
        _run("docker", "network", "create", "--internal", config.BENCH_NETWORK)
        print(f"  · 네트워크 {config.BENCH_NETWORK} 생성 (--internal)")


def create_target():
    """타깃을 새 컨테이너로 생성. 이게 스코어보드 초기화이기도 하다."""
    _run("docker", "rm", "-f", config.JUICESHOP_CONTAINER, check=False)
    _run("docker", "run", "-d", "--name", config.JUICESHOP_CONTAINER,
         "-p", f"{config.HOST_BIND}:{config.HOST_PORT}:{config.JUICESHOP_PORT}",
         config.JUICESHOP_IMAGE)
    _run("docker", "network", "connect", config.BENCH_NETWORK, config.JUICESHOP_CONTAINER)
    _wait_target()


def _wait_target(wait_sec: int = 120):
    url = config.TARGET_HOST_URL.rstrip("/") + "/api/Challenges"
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if json.load(r)["data"]:
                    time.sleep(2)
                    return
        except Exception:
            time.sleep(3)
    raise RuntimeError(f"타깃이 {wait_sec}초 안에 뜨지 않았습니다: {url}")


def create_sandbox():
    """샌드박스는 도구 설치 후에 격리 네트워크로 '이사'시킨다.

    --internal 네트워크에서는 apt가 안 되므로 순서가 중요하다:
      bridge에서 생성 → 도구 설치 → bridge 분리 → whs-bench 연결
    """
    _run("docker", "rm", "-f", config.SANDBOX_CONTAINER, check=False)
    _run("docker", "run", "-d", "--name", config.SANDBOX_CONTAINER,
         config.SANDBOX_IMAGE, "tail", "-f", "/dev/null")

    missing = _missing_tools()
    if missing:
        print(f"  · 도구 설치 중: {', '.join(missing)}")
        _run("docker", "exec", config.SANDBOX_CONTAINER, "bash", "-lc",
             "apt-get update -qq && apt-get install -y -qq " + " ".join(missing),
             check=False)

    _run("docker", "network", "disconnect", "bridge", config.SANDBOX_CONTAINER, check=False)
    _run("docker", "network", "connect", config.BENCH_NETWORK, config.SANDBOX_CONTAINER)


def _missing_tools() -> list:
    probe = "; ".join(f"command -v {t} >/dev/null || echo {t}" for t in NEEDED_TOOLS)
    out = _run("docker", "exec", config.SANDBOX_CONTAINER, "bash", "-lc", probe,
               check=False).stdout
    return [l.strip() for l in out.splitlines() if l.strip()]


def verify() -> dict:
    """격리가 실제로 동작하는지 확인한다. 주장하지 말고 측정한다."""
    def sb(cmd):
        return _run("docker", "exec", config.SANDBOX_CONTAINER, "bash", "-lc", cmd,
                    check=False).stdout.strip()

    target = sb(f'curl -s -o /dev/null -m 8 -w "%{{http_code}}" '
                f'{config.TARGET_SANDBOX_URL}/')
    # curl 실패 시 http_code는 000이고 exit도 0이 아니다 → 둘 다 출력될 수 있으므로
    # '2xx/3xx가 아니면 차단'으로 판정한다 (문자열 일치는 취약)
    egress_raw = sb('curl -s -o /dev/null -m 8 -w "%{http_code}" http://example.com/ '
                    '2>/dev/null; echo " rc=$?"')
    codes = re.findall(r"\b([1-5]\d\d)\b", egress_raw.split("rc=")[0])
    egress_blocked = not codes
    missing = _missing_tools()

    ok = (target == "200" and egress_blocked and not missing)
    return {"target_reachable": target,
            "internet_egress": "blocked" if egress_blocked else codes[0],
            "missing_tools": missing, "ok": ok}


def provision(recreate_sandbox: bool = False):
    print("· 실험 환경 구성")
    ensure_network()
    if recreate_sandbox or not _exists(config.SANDBOX_CONTAINER):
        create_sandbox()
        print(f"  · 샌드박스 {config.SANDBOX_CONTAINER} 생성")
    create_target()
    print(f"  · 타깃 {config.JUICESHOP_CONTAINER} 생성 "
          f"({config.TARGET_HOST_URL})")
    v = verify()
    print(f"  · 격리 검증: 타깃={v['target_reachable']} "
          f"외부={v['internet_egress']} 누락도구={v['missing_tools'] or '없음'} "
          f"→ {'OK ✅' if v['ok'] else '실패 ❌'}")
    if not v["ok"]:
        raise RuntimeError(f"환경 검증 실패: {v}")
    return v


if __name__ == "__main__":
    import sys
    if "verify" in sys.argv:
        print(json.dumps(verify(), indent=2, ensure_ascii=False))
    else:
        provision(recreate_sandbox="--sandbox" in sys.argv)
