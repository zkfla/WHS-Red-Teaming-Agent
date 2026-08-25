"""Engagement (Scope / Rules of Engagement) 레이어.

사용자가 정의한 참여 범위·금지 행위·제한을 사람이 읽는 `engagement.md` +
코드가 강제하는 `engagement.yaml` 로 분리해서 저장하고(`store.py`), 승인된
것만 `CompiledPolicy`(`policy.py`)로 컴파일해 `probing/session.py` 의 네트워크
경계에서 강제한다. `agent/react.py` 의 판정 로직(Oracle/Finding)은 이 레이어와
무관하다 — Engagement 는 "무엇을 시도할 수 있는가"만 결정하고, "시도한 게
취약점인가"는 여전히 오라클이 결정한다.
"""
