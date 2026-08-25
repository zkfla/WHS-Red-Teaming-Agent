"""engagement.yaml → Engagement, 그리고 `--engagement <ref>` 하나로 실행 대상을
찾는 규칙(하위 `--target` 플래그는 없다 — 어떤 어댑터 설정을 쓸지는
`Engagement.target.name` 안에 있다, engagement/schema.py 참고)."""
from __future__ import annotations

from pathlib import Path

import yaml

from finredteam.engagement.schema import Engagement

_ENGAGEMENTS_ROOT = Path("engagements")
_EXAMPLES_ROOT = _ENGAGEMENTS_ROOT / "examples"


def load_engagement(path: Path) -> Engagement:
    yaml_path = path / "engagement.yaml" if path.is_dir() else path
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    return Engagement.model_validate(raw)


def resolve_engagement_ref(ref: str) -> Path | None:
    """`--engagement <ref>` 하나로 engagement.yaml 을 찾는다. 순서대로 시도:

    1. `ref` 자체가 디렉터리(또는 engagement.yaml 파일) 경로
    2. `engagements/examples/<ref>/` — 저장소에 커밋된 예시(vuln-bank 등)
    3. `engagements/<ref>/` — `finredteam setup` 이 만든 참여
    """
    direct = Path(ref)
    if direct.is_dir() and (direct / "engagement.yaml").exists():
        return direct / "engagement.yaml"
    if direct.is_file():
        return direct

    example = _EXAMPLES_ROOT / ref / "engagement.yaml"
    if example.exists():
        return example

    own = _ENGAGEMENTS_ROOT / ref / "engagement.yaml"
    if own.exists():
        return own

    return None


def list_available_engagements() -> list[Path]:
    """`--engagement` 없이 그냥 실행했을 때 대화형으로 고를 목록 —
    `engagements/examples/*/`(커밋된 예시)와 `engagements/*/`(`finredteam
    setup` 이 만든 것, examples 자기 자신은 제외)를 합쳐서 반환한다."""
    paths: list[Path] = []
    if _EXAMPLES_ROOT.is_dir():
        paths += sorted(_EXAMPLES_ROOT.glob("*/engagement.yaml"))
    if _ENGAGEMENTS_ROOT.is_dir():
        paths += sorted(
            p for p in _ENGAGEMENTS_ROOT.glob("*/engagement.yaml") if p.parent.name != "examples"
        )
    return paths
