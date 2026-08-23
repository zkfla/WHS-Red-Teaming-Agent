"""Juice Shop 자동 채점 오라클.

Juice Shop은 /api/Challenges 에 116개 챌린지와 solved 플래그를 그대로 노출한다.
→ 정답셋을 사람이 라벨링할 필요가 없다. 탐지율/성공률이 자동으로 나온다.
   (D안에서 가장 검증하기 까다로웠던 '성능 유지'가 여기서 객관적 숫자가 된다.)
"""
import json
import subprocess
import time
import urllib.request

import config

# 스코프: 웹 취약점 위주 + 난이도 1~3. 분모를 고정해야 실행 간 비교가 성립한다.
DEFAULT_SCOPE = {"max_difficulty": 3}


def fetch_challenges(url: str = None) -> list:
    url = (url or config.TARGET_HOST_URL).rstrip("/") + "/api/Challenges"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)["data"]


def in_scope(ch: dict, scope: dict) -> bool:
    if ch.get("disabledEnv"):
        return False
    if ch["difficulty"] > scope.get("max_difficulty", 6):
        return False
    cats = scope.get("categories")
    return not cats or ch["category"] in cats


def snapshot(scope: dict = None) -> dict:
    """현재 스코어보드 상태. 실행 전/후 두 번 찍어서 차분을 낸다."""
    scope = scope or DEFAULT_SCOPE
    chs = [c for c in fetch_challenges() if in_scope(c, scope)]
    return {
        "denominator": len(chs),
        "solved": sorted(c["key"] for c in chs if c.get("solved")),
        "scope": scope,
    }


def reset() -> bool:
    """스코어보드 초기화. 매 실행을 같은 출발선에 세운다.

    주의 — `docker restart`로는 초기화되지 않는다. Juice Shop은 챌린지 상태를
    컨테이너 파일시스템의 SQLite에 쓰기 때문에 재시작해도 그대로 남는다.
    컨테이너 생성은 bench/lab.py가 담당한다 (포트/네트워크 정의가 한 곳에 있어야 한다).
    """
    from bench import lab
    lab.create_target()
    verify_clean()
    return True


def container_started_at() -> str:
    """타깃 컨테이너의 기동 시각. 실행 전후로 달라지면 중간에 재시작된 것이고,
    그러면 스코어보드가 초기화되어 '탐지 0건'처럼 보인다."""
    return subprocess.run(
        ["docker", "inspect", config.JUICESHOP_CONTAINER, "--format", "{{.State.StartedAt}}"],
        capture_output=True, text=True).stdout.strip()


class ContaminatedError(RuntimeError):
    """새 인스턴스인데 이미 풀린 챌린지가 있다 = 측정이 오염됐다."""


def verify_clean(scope: dict = None):
    """리셋 직후 스코어보드가 0인지 확인한다.

    실제로 겪은 오염원: 브라우저에 열려 있는 Juice Shop 탭.
    프론트엔드가 localStorage의 continueCode를 새 인스턴스에 자동 적용해서
    이전 진행상황을 되살린다 (컨테이너 로그에 'Restored ... Challenge'로 찍힌다).

    지금은 타깃을 127.0.0.1:3100에 바인드해서 localhost:3000을 가리키던
    탭이 닿지 못하게 했지만, 그건 '알려진 경로 하나'를 막은 것이다.
    검사는 그대로 둔다 — 방어가 뚫렸는지 알려주는 게 이 함수의 일이다.
    """
    snap = snapshot(scope)
    if snap["solved"]:
        raise ContaminatedError(
            f"리셋했는데 이미 {len(snap['solved'])}개가 풀려 있습니다: {snap['solved']}\n"
            "  원인 1: 브라우저에 Juice Shop 탭이 열려 있음 "
            "(continueCode가 진행상황을 자동 복원). 탭을 닫으세요.\n"
            "  원인 2: 다른 에이전트/스캐너가 같은 타깃을 두드리는 중.\n"
            f"  컨테이너 로그로 확인: docker logs {config.JUICESHOP_CONTAINER} | grep Restored"
        )
    return snap


def score(before: dict, after: dict) -> dict:
    new = sorted(set(after["solved"]) - set(before["solved"]))
    denom = after["denominator"]
    return {
        "denominator": denom,
        "solved_before": len(before["solved"]),
        "solved_after": len(after["solved"]),
        "newly_solved": len(new),
        "solved_keys": new,
        "detection_rate": round(len(new) / denom, 4) if denom else 0.0,
    }


def catalog(scope: dict = None) -> str:
    """스코프 내 챌린지 목록 (사람이 실험 설계 검토할 때 쓰는 용도. 에이전트엔 주지 않는다)."""
    scope = scope or DEFAULT_SCOPE
    rows = [c for c in fetch_challenges() if in_scope(c, scope)]
    rows.sort(key=lambda c: (c["category"], c["difficulty"]))
    return "\n".join(f"[{c['difficulty']}] {c['category']:<28} {c['key']}" for c in rows)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "catalog":
        print(catalog())
    elif len(sys.argv) > 1 and sys.argv[1] == "reset":
        reset(); print("reset ok (스코어보드 0 확인됨)")
    elif len(sys.argv) > 1 and sys.argv[1] == "verify":
        print(json.dumps(verify_clean(), indent=2))
    else:
        print(json.dumps(snapshot(), indent=2))
