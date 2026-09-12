from __future__ import annotations

import sys
from pathlib import Path

import yaml

from spec.schema.attack_graph import KNOWN_ATTRS, NodeKind
from spec.schema.registry import ToolRegistry
from spec.schema.tool_manifest import FORBIDDEN_KEYS, ToolManifest

_READ_ONLY = ("GET", "HEAD", "OPTIONS")
_VALID_KINDS = set(getattr(NodeKind, "__args__", ()))

# ㉑ 이런 이름·타입의 파라미터는 값을 기록에 남기면 안 된다.
#    Transition.params 는 그대로 JSONL 로 나가 evidence store 에 오래 남는다.
_SECRET_NAME_HINTS = (
    "password", "passwd", "pwd", "secret", "token", "credential", "cvv", "pin",
    "api_key", "apikey", "ssn", "extra_fields", "claims", "body",
)


# ══════════════════════════════════════════════════════════════════════
#  파일 단위 검사
# ══════════════════════════════════════════════════════════════════════


def check_file(path: Path) -> tuple[list[str], ToolManifest | None]:
    errs: list[str] = []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return [f"{path.name}: YAML 파싱 실패 — {exc}"], None

    # ① 금지 필드 — 매니페스트는 판정 범위를 좁힐 수 없다
    for key in FORBIDDEN_KEYS:
        if key in raw:
            errs.append(
                f"{path.name}: 금지 필드 {key!r} — tool 이 오라클 범위를 정하면 "
                f"'registry 전체가 매번 돈다' 불변식이 깨진다"
            )

    # ② 스키마 (extra=forbid 라 오타 필드도 여기서 걸린다)
    try:
        m = ToolManifest.model_validate(raw)
    except Exception as exc:
        lines = [l.strip() for l in str(exc).splitlines()]
        detail = " / ".join(lines[1:4]) or lines[0]
        return errs + [f"{path.name}: 스키마 위반 — {detail}"], None

    n = path.name
    errs += check_manifest_rules(m, filename=n, stem=path.stem)
    return errs, m


def check_manifest_rules(m: ToolManifest, *, filename: str = "", stem: str | None = None) -> list[str]:
    errs: list[str] = []
    n = filename or f"{m.id}.yaml"

    # ★ "네트워크를 타는가" 는 이제 사실(offline)로 판단한다.
    #   예전에는 `layer != "meta"` 를 대용으로 썼는데, probe_path 가 meta 라서
    #   정찰 tool 이 표면 선언·failure_modes 의무에서 통째로 빠져 있었다.
    net = not m.offline

    # ③ 파일명 = id
    if stem is not None and stem != m.id:
        errs.append(f"{n}: 파일명과 id({m.id!r}) 불일치")

    # ④ attrs 오타 — 오타 하나면 전제조건이 영원히 안 맞는다
    for p in m.preconditions + m.postconditions:
        if p.kind not in _VALID_KINDS:
            errs.append(f"{n}: 알 수 없는 노드 종류 {p.kind!r} (허용: {sorted(_VALID_KINDS)})")
            continue
        for k in p.attrs:
            if k not in KNOWN_ATTRS.get(p.kind, ()):
                errs.append(
                    f"{n}: {p.kind}.attrs 의 미등록 키 {k!r} "
                    f"(허용: {KNOWN_ATTRS.get(p.kind, ())})"
                )

    # ⑤ 상태변경 tool 은 state_change 를 선언해야 한다
    if m.effects.mutates_balance and "state_change" not in m.capabilities.required:
        errs.append(f"{n}: mutates_balance=true 인데 state_change capability 미선언")

    # ⑥ 금액을 건드리면 상한을 선언해야 정책이 사전 검사할 수 있다
    if m.effects.mutates_balance and m.capabilities.max_amount_touched is None:
        if not m.amount_params():
            errs.append(
                f"{n}: 잔액을 바꾸는데 max_amount_touched 도 policy_checked 파라미터도 없다 "
                f"— 금액 검사가 아예 안 걸린다"
            )

    # ⑦ 동시 요청은 race_testing 을 요구한다
    if m.capabilities.concurrency > 1 and "race_testing" not in m.capabilities.required:
        errs.append(f"{n}: concurrency>1 인데 race_testing capability 미선언")

    # ⑧ 리소스를 만들면 권한이 필요하다
    if m.effects.creates_resource_types and not m.capabilities.required:
        errs.append(f"{n}: 리소스를 만드는데 필요한 capability 가 하나도 없다")

    # ⑨ 요청을 보내는 tool 은 표면을 선언해야 한다 (정적 / 바인딩 / 접두어)
    if net and not m.declares_surface():
        errs.append(
            f"{n}: surface 미선언 — control plane 이 스코프 이탈을 대조할 수 없다 "
            f"(동적 대상이면 surface_binding, 정찰이면 surface_prefixes, "
            f"네트워크를 안 타면 offline: true)"
        )

    # ⑩ 잔액을 바꾼다면서 읽기 전용 메서드만 선언
    if m.effects.mutates_balance and m.surface and all(d.method in _READ_ONLY for d in m.surface):
        errs.append(f"{n}: 잔액을 바꾼다면서 surface 에 상태변경 메서드가 하나도 없다")

    # ⑪ surface_binding 이 가리키는 파라미터가 실제로 있어야 한다
    if m.surface_binding:
        if m.surface_binding not in m.params:
            errs.append(f"{n}: surface_binding={m.surface_binding!r} 인데 params 에 그 키가 없다")
        elif m.params[m.surface_binding].binds_to != "endpoint":
            errs.append(
                f"{n}: surface_binding 파라미터는 binds_to: endpoint 여야 한다 "
                f"(현재 {m.params[m.surface_binding].binds_to!r})"
            )

    # ⑫-a may_produce 가 실존하는 노드 종류여야 한다
    for k in m.may_produce:
        if k not in _VALID_KINDS:
            errs.append(f"{n}: may_produce 의 {k!r} 는 알 수 없는 노드 종류")

    # ⑫ binds_to 가 실존하는 노드 종류여야 한다
    for pname, kind in m.node_bound_params().items():
        if kind not in _VALID_KINDS:
            errs.append(f"{n}: {pname}.binds_to={kind!r} 는 알 수 없는 노드 종류")

    # ⑬ ★ 자원을 만들면 되돌릴 방법을 선언해야 한다 (Engagement 의 cleanup 의무)
    if m.effects.creates_resource_types and not m.cleanup_with and not m.cleanup_impossible_reason:
        errs.append(
            f"{n}: {m.effects.creates_resource_types} 를 만드는데 cleanup_with 도 "
            f"cleanup_impossible_reason 도 없다 — 못 치우면 못 치운다고 적어야 한다"
        )

    # ⑭ ★ MCP 어노테이션과 실제 동작이 어긋나면 안 된다
    ro = str(m.mcp.annotations.get("readOnlyHint", "")).lower() == "true"
    if ro and (m.effects.mutates_balance or m.effects.creates_resource_types):
        errs.append(
            f"{n}: readOnlyHint=true 인데 실제로는 상태를 바꾼다 "
            f"— 모델이 안전한 tool 로 오해한다"
        )
    if net and not m.failure_modes:
        errs.append(
            f"{n}: 네트워크를 타는데 failure_modes 미선언 "
            f"— '전제 미충족'과 '대상이 죽어간다'를 구분할 수 없다"
        )

    # ── 이하 ⑲~㉓ : 이번에 추가된 검사 ──────────────────────────────

    binds = {p.bind or p.kind for p in m.preconditions}

    # ⑲ ★ from_param 이 실존 파라미터를 가리키고, 종류가 맞아야 한다
    for p in m.preconditions:
        if not p.from_param:
            continue
        spec = m.params.get(p.from_param)
        if spec is None:
            errs.append(
                f"{n}: 전제 {p.bind or p.kind!r} 의 from_param={p.from_param!r} 가 "
                f"params 에 없다 — 무엇으로 특정하라는 건지 알 수 없다"
            )
        elif spec.binds_to != p.kind:
            errs.append(
                f"{n}: from_param={p.from_param!r} 는 binds_to: {p.kind} 여야 한다 "
                f"(현재 {spec.binds_to!r}) — 종류가 어긋나면 바인딩이 영원히 안 맞는다"
            )

    # ⑳ ★★ 동적 대상 tool 은 반드시 params ↔ 전제를 from_param 으로 묶어야 한다.
    #     안 묶으면 resolve() 가 패턴에 맞는 아무 endpoint 나 집고, ingest() 가
    #     그 엉뚱한 바인딩을 허용 표면으로 삼는다. 모델은 A 를 골랐는데 대조 기준은
    #     B 가 되는 식이라, 스코프 이탈 탐지 전체가 무의미해진다.
    if m.surface_binding:
        linked = [p for p in m.preconditions if p.from_param == m.surface_binding]
        if not linked:
            errs.append(
                f"{n}: surface_binding={m.surface_binding!r} 인데 이 파라미터를 "
                f"from_param 으로 묶은 전제가 없다 — 실행 대상과 대조 기준이 어긋난다"
            )
        elif any(p.kind != "endpoint" for p in linked):
            errs.append(f"{n}: surface_binding 과 묶인 전제는 kind: endpoint 여야 한다")

    # ㉑ ★ 자격증명·자유 입력 파라미터는 sensitive 를 선언해야 한다
    for pname, spec in m.params.items():
        looks_secret = any(h in pname.lower() for h in _SECRET_NAME_HINTS)
        if (looks_secret or spec.type == "json") and not spec.sensitive:
            errs.append(
                f"{n}: params.{pname} 는 sensitive: true 여야 한다 "
                f"— 기록(Transition.params)이 그대로 evidence store 로 나간다"
            )

    # ㉒ ★ 관계 조건의 related_to 가 실존하는 bind 여야 한다
    for p in m.preconditions:
        if p.related_to and p.related_to not in binds:
            errs.append(
                f"{n}: 전제 {p.bind or p.kind!r} 의 related_to={p.related_to!r} 가 "
                f"다른 전제의 bind 이름이 아니다 (있는 것: {sorted(binds)})"
            )
        for other in p.distinct_from:
            if other not in binds:
                errs.append(
                    f"{n}: distinct_from 의 {other!r} 가 다른 전제의 bind 이름이 아니다 "
                    f"— 오타면 배타 제약이 조용히 사라진다 (있는 것: {sorted(binds)})"
                )

    # ㉓ ★ 접두어 표면 선언은 읽기 전용 tool 만 쓸 수 있다
    if m.surface_prefixes:
        writes = m.effects.mutates_balance or m.effects.creates_resource_types
        if writes:
            errs.append(
                f"{n}: 상태를 바꾸는 tool 은 surface_prefixes 로 광범위 선언을 할 수 없다 "
                f"— 접두어는 '모르는 곳을 읽어보는' 정찰 전용 탈출구다"
            )
        for pre in m.surface_prefixes:
            bad = [x for x in pre.methods if x not in _READ_ONLY]
            if bad:
                errs.append(f"{n}: surface_prefixes 에 상태변경 메서드 {bad} — 읽기 전용만 허용")

    return errs


# ══════════════════════════════════════════════════════════════════════
#  교차 참조 검사
# ══════════════════════════════════════════════════════════════════════


def check_cross(manifests: dict[str, ToolManifest] | ToolRegistry) -> list[str]:
    if isinstance(manifests, ToolRegistry):
        manifests = manifests.as_dict()
    errs: list[str] = []
    ids = set(manifests)

    # ⑮ cleanup_with 가 실존하는 tool 을 가리켜야 한다
    for mid, m in manifests.items():
        for c in m.cleanup_with:
            if c not in ids:
                errs.append(
                    f"{mid}: cleanup_with 의 {c!r} 가 tool 목록에 없다 "
                    f"(정리 tool 이 없으면 선언이 거짓말이 된다)"
                )

    # ⑯ 폐기된 tool 의 대체재가 실존해야 한다
    for mid, m in manifests.items():
        if m.deprecated and m.replaced_by and m.replaced_by not in ids:
            errs.append(f"{mid}: deprecated 인데 replaced_by={m.replaced_by!r} 가 없다")

    # ⑰ MCP 로 나가는 tool 이름이 겹치면 안 된다
    seen: dict[str, str] = {}
    for mid, m in manifests.items():
        if not m.mcp.expose:
            continue
        name = m.mcp.tool_name or m.id
        if name in seen:
            errs.append(f"{mid}: MCP tool 이름 {name!r} 이 {seen[name]} 와 충돌")
        seen[name] = mid

    # ⑱ ★ 전제조건이 요구하는 노드를 **아무 tool 도 만들지 않으면** 그 tool 은
    #    영원히 frontier 에 못 오른다. 부트스트랩으로 생기는 종류는 예외.
    BOOTSTRAP = {"principal", "account", "endpoint"}
    produced = {p.kind for m in manifests.values() for p in m.postconditions}
    produced |= {k for m in manifests.values() for k in m.may_produce}
    for mid, m in manifests.items():
        for pre in m.preconditions:
            if pre.optional or pre.kind in BOOTSTRAP or pre.kind in produced:
                continue
            errs.append(
                f"{mid}: {pre.kind} 노드를 전제로 하는데 이걸 만드는 tool 이 없다 "
                f"— 이 tool 은 영원히 실행 불가"
            )
    return errs


def main(*roots: str) -> int:
    files = sorted(f for r in roots for f in Path(r).glob("*.yaml"))
    if not files:
        print(f"매니페스트 없음: {roots}")
        return 1

    file_errs: list[str] = []
    bad_files: set[str] = set()
    for f in files:
        errs, _ = check_file(f)
        file_errs += errs
        if errs:
            bad_files.add(f.name)

    # ★ 적재는 레지스트리가 한다 — id 중복을 조용히 덮어쓰지 않는 유일한 경로다.
    registry, load_errs = ToolRegistry.load(*roots)
    cross_errs = load_errs + check_cross(registry)

    for e in file_errs + cross_errs:
        print("✗", e)
    # ★ 통과 개수는 **파일 단위 오류가 없는 파일 수**다. 예전에는 파일 오류(파일명으로
    #   시작)와 교차 오류(tool id 로 시작)를 같은 방식으로 세서 숫자가 틀렸다.
    print(f"\n{len(files)}개 파일 검사 · 통과 {len(files) - len(bad_files)} · "
          f"파일오류 {len(file_errs)}건 · 교차오류 {len(cross_errs)}건")
    return 1 if (file_errs or cross_errs) else 0


if __name__ == "__main__":
    sys.exit(main(*(sys.argv[1:] or ["spec/tools", "spec/primitives"])))
