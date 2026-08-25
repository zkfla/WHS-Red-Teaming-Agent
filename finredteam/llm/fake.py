"""API 키 없이 ReAct 루프 배관을 시험해보기 위한 결정적 가짜 LLM.

★ 실제 추론을 하지 않는다 — 정해진 순서(observe_graph → F1 시도 → F2 음수/양수
대조 → finish)만 밟는다. "루프가 실제로 도는가"를 확인하는 용도지, 실제
취약점 탐색 지능을 보는 용도가 아니다. 진짜 시험은 `llm/factory.py` 로 만든
Anthropic/OpenAI 클라이언트를 써야 한다.
"""
from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel

from finredteam.llm.base import Usage

SchemaT = TypeVar("SchemaT", bound=BaseModel)

_PRINCIPALS_RE = re.compile(r'"principals":\s*(\[[^\]]*\])')
_ENTITLED_OWN_RE = re.compile(r'"entitled_own":\s*(\{[^}]*\})')


def _extract_json_field(text: str, pattern: re.Pattern) -> Any | None:
    match = pattern.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


class FakeLLMClient:
    provider = "fake"
    model = "fake-react"

    def __init__(self) -> None:
        self._usage = Usage()
        self._call = 0

    def complete_json(
        self, system: str, user: str, schema: type[SchemaT], temperature: float = 0.3
    ) -> list[SchemaT]:
        self._call += 1
        self._usage = self._usage + Usage(tokens_in=30, tokens_out=30, requests=1)

        principals = _extract_json_field(user, _PRINCIPALS_RE) or []
        entitled_own = _extract_json_field(user, _ENTITLED_OWN_RE) or {}

        step = self._next_step(principals, entitled_own)
        return [schema.model_validate(step)]

    def _next_step(self, principals: list[str], entitled_own: dict[str, str]) -> dict:
        if self._call == 1 or len(entitled_own) < 2:
            # 스크래치패드에 이전 관측이 누적되므로(agent/react.py 참고) 한 번만
            # 확인하면 이후 스텝에서도 계속 보인다 — 그런데도 부족하면 다시 본다.
            return {
                "thought": "먼저(또는 다시) 그래프에 뭐가 있는지 확인한다.",
                "action": "observe_graph",
                "action_input": {},
            }

        attacker = principals[0] if principals else next(iter(entitled_own))
        others = [(pid, acct) for pid, acct in entitled_own.items() if pid != attacker]
        target_account = others[0][1] if others else next(iter(entitled_own.values()))
        raw_account_number = target_account.split(":")[-1]

        if self._call == 2:
            return {
                "thought": f"{attacker} 로 다른 주체({target_account})의 계좌를 인증 없이 조회해본다.",
                "action": "propose_candidate",
                "action_input": {"attack_class": "F1", "principal_id": attacker, "target": target_account},
            }
        if self._call == 3:
            return {
                "thought": "F2: 음수 금액 송금이 부호를 반전시키는지 시험한다.",
                "action": "propose_candidate",
                "action_input": {
                    "attack_class": "F2",
                    "principal_id": attacker,
                    "target": target_account,
                    "params": {"amount": "-500.00", "to_account": raw_account_number},
                },
            }
        if self._call == 4:
            return {
                "thought": "대조군으로 정상 양수 송금도 시험한다.",
                "action": "propose_candidate",
                "action_input": {
                    "attack_class": "F2",
                    "principal_id": attacker,
                    "target": target_account,
                    "params": {"amount": "7.00", "to_account": raw_account_number},
                },
            }
        return {"thought": "충분히 탐색했다.", "action": "finish", "action_input": {"reason": "fake demo done"}}

    @property
    def usage(self) -> Usage:
        return self._usage
