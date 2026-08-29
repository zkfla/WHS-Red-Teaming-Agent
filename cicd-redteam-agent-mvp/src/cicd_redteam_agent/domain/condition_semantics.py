from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from cicd_redteam_agent.domain.event_provenance import (
    event_principal_qualifier,
    event_value_from_linkage,
    principal_role_for_reference,
)
from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.investigation import (
    EventPrincipalEvidence,
    WorkflowRunEventLinkageEvidence,
)
from cicd_redteam_agent.domain.models import (
    AttackerProfile,
    ConditionEvaluation,
    ConditionSecurityEffect,
    EventPrincipalRole,
    EvidenceProvider,
    EvidenceRequirementKind,
    EvidenceRequirementRef,
    ExecutionResult,
    PrincipalAuthorizationAssessment,
    TriState,
)


class ConditionSyntaxError(ValueError):
    pass


@dataclass(frozen=True)
class ConditionRuntimeContext:
    event_name: str
    profile: AttackerProfile
    repository_full_name: str | None = None
    repository_owner: str | None = None
    default_branch: str | None = None
    trigger_config: dict[str, Any] = field(default_factory=dict)
    repository_id: str | None = None
    github_run_id: int | None = None
    actor_login: str | None = None
    actor_id: int | None = None
    triggering_actor_login: str | None = None
    triggering_actor_id: int | None = None
    run_event_name: str | None = None
    actor_permissions: dict[str, tuple[str, int | None]] = field(default_factory=dict)
    event_linkage: WorkflowRunEventLinkageEvidence | None = None
    event_principals: dict[EventPrincipalRole, EventPrincipalEvidence] = field(default_factory=dict)
    implicit_success_gate: TriState = TriState.TRUE
    status_success: TriState = TriState.UNKNOWN
    status_failure: TriState = TriState.UNKNOWN
    status_cancelled: TriState = TriState.UNKNOWN
    needs_results: dict[str, ExecutionResult] = field(default_factory=dict)
    step_results: dict[str, tuple[ExecutionResult, ExecutionResult]] = field(default_factory=dict)


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str


@dataclass(frozen=True)
class _Node:
    kind: Literal["literal", "reference", "status", "function", "not", "and", "or", "compare"]
    value: object | None = None
    left: _Node | None = None
    right: _Node | None = None
    args: tuple[_Node, ...] = ()


@dataclass
class _Value:
    kind: Literal["boolean", "string", "number", "list", "stringset", "pattern", "unknown"]
    value: bool | str | int | tuple[str, ...] | tuple[str, str] | None = None
    missing: set[str] = field(default_factory=set)


def _normalize(condition: str | None) -> str:
    if condition is None:
        return "true"
    expression = condition.strip()
    if expression.startswith("${{") and expression.endswith("}}"):
        expression = expression[3:-2].strip()
    return expression


def _tokenize(expression: str) -> list[_Token]:
    tokens: list[_Token] = []
    index = 0
    while index < len(expression):
        char = expression[index]
        if char.isspace():
            index += 1
            continue
        pair = expression[index : index + 2]
        if pair in {"&&", "||", "==", "!="}:
            tokens.append(_Token("operator", pair))
            index += 2
            continue
        if char in "!(),*[]":
            tokens.append(_Token("operator", char))
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            value: list[str] = []
            while index < len(expression):
                current = expression[index]
                if current == "\\" and index + 1 < len(expression):
                    value.append(expression[index + 1])
                    index += 2
                    continue
                if current == quote:
                    index += 1
                    break
                value.append(current)
                index += 1
            else:
                raise ConditionSyntaxError("unterminated string literal")
            tokens.append(_Token("string", "".join(value)))
            continue
        if char.isdigit():
            start = index
            while index < len(expression) and expression[index].isdigit():
                index += 1
            tokens.append(_Token("number", expression[start:index]))
            continue
        if char.isalpha() or char == "_":
            start = index
            while index < len(expression) and (
                expression[index].isalnum() or expression[index] in "._-*"
            ):
                index += 1
            tokens.append(_Token("identifier", expression[start:index]))
            continue
        raise ConditionSyntaxError(f"unsupported token {char!r}")
    tokens.append(_Token("eof", ""))
    return tokens


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.index = 0

    def _peek(self, value: str | None = None) -> bool:
        token = self.tokens[self.index]
        return token.value == value if value is not None else token.kind != "eof"

    def _take(self) -> _Token:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def _accept(self, value: str) -> bool:
        if self._peek(value):
            self._take()
            return True
        return False

    def parse(self) -> _Node:
        node = self._or()
        if self.tokens[self.index].kind != "eof":
            raise ConditionSyntaxError(f"unexpected token {self.tokens[self.index].value!r}")
        return node

    def _or(self) -> _Node:
        node = self._and()
        while self._accept("||"):
            node = _Node("or", left=node, right=self._and())
        return node

    def _and(self) -> _Node:
        node = self._comparison()
        while self._accept("&&"):
            node = _Node("and", left=node, right=self._comparison())
        return node

    def _comparison(self) -> _Node:
        node = self._unary()
        if self._peek("==") or self._peek("!="):
            operator = self._take().value
            node = _Node("compare", value=operator, left=node, right=self._unary())
        return node

    def _unary(self) -> _Node:
        if self._accept("!"):
            return _Node("not", left=self._unary())
        return self._primary()

    def _primary(self) -> _Node:
        if self._accept("("):
            node = self._or()
            if not self._accept(")"):
                raise ConditionSyntaxError("missing closing parenthesis")
            return node
        token = self._take()
        if token.kind == "string":
            return _Node("literal", value=token.value)
        if token.kind == "number":
            return _Node("literal", value=int(token.value))
        if token.kind != "identifier":
            raise ConditionSyntaxError(f"expected expression, got {token.value!r}")
        lowered = token.value.casefold()
        if lowered in {"true", "false"}:
            return _Node("literal", value=lowered == "true")
        if self._accept("("):
            if lowered in {"success", "failure", "cancelled", "always"}:
                if not self._accept(")"):
                    raise ConditionSyntaxError("status functions do not accept arguments")
                return _Node("status", value=lowered)
            args: list[_Node] = []
            if not self._accept(")"):
                while True:
                    args.append(self._or())
                    if self._accept(")"):
                        break
                    if not self._accept(","):
                        raise ConditionSyntaxError("missing function argument separator")
            if lowered in {"contains", "startswith", "endswith"} and len(args) != 2:
                raise ConditionSyntaxError(f"{token.value} requires two arguments")
            return _Node("function", value=lowered, args=tuple(args))
        return _Node("reference", value=lowered)


def _tri_not(value: TriState) -> TriState:
    if value is TriState.TRUE:
        return TriState.FALSE
    if value is TriState.FALSE:
        return TriState.TRUE
    return TriState.UNKNOWN


def _tri_and(left: TriState, right: TriState) -> TriState:
    if TriState.FALSE in {left, right}:
        return TriState.FALSE
    if left is TriState.TRUE and right is TriState.TRUE:
        return TriState.TRUE
    return TriState.UNKNOWN


def _tri_or(left: TriState, right: TriState) -> TriState:
    if TriState.TRUE in {left, right}:
        return TriState.TRUE
    if left is TriState.FALSE and right is TriState.FALSE:
        return TriState.FALSE
    return TriState.UNKNOWN


def _boolean(value: _Value) -> tuple[TriState, set[str]]:
    if value.kind == "boolean":
        return (TriState.TRUE if value.value is True else TriState.FALSE), value.missing
    return TriState.UNKNOWN, value.missing or {"condition did not produce a boolean value"}


def _exact_filters(config: object, key: str) -> tuple[str, ...]:
    if not isinstance(config, dict):
        return ()
    raw = config.get(key)
    values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    if not values or any(
        not isinstance(item, str)
        or not item
        or item.startswith("!")
        or any(c in item for c in "*?[")
        for item in values
    ):
        return ()
    return tuple(dict.fromkeys(str(item) for item in values))


def _event_filter(context: ConditionRuntimeContext) -> object:
    return context.trigger_config.get(context.event_name, {})


def _ref_value(name: str, context: ConditionRuntimeContext) -> _Value | None:
    event = context.event_name
    config = _event_filter(context)
    branches = _exact_filters(config, "branches")
    tags = _exact_filters(config, "tags")
    if name == "github.head_ref":
        if event in {"pull_request", "pull_request_target"}:
            return _Value("unknown", missing={"pull-request head branch value"})
        return _Value("string", "")
    if name == "github.base_ref":
        if event not in {"pull_request", "pull_request_target"}:
            return _Value("string", "")
        if branches:
            return _Value(
                "string" if len(branches) == 1 else "stringset",
                branches[0] if len(branches) == 1 else branches,
            )
        if context.default_branch:
            return _Value("string", context.default_branch)
        return _Value("unknown", missing={"pull-request base branch value"})
    if name not in {"github.ref", "github.ref_name"}:
        return None
    if event == "pull_request":
        return _Value(
            "pattern", ("refs/pull/", "/merge") if name == "github.ref" else ("", "/merge")
        )
    if event == "pull_request_target":
        candidates = branches or ((context.default_branch,) if context.default_branch else ())
        if not candidates:
            return _Value("unknown", missing={"pull-request target base branch value"})
        rendered = tuple(
            f"refs/heads/{item}" if name == "github.ref" else item for item in candidates
        )
        return _Value(
            "string" if len(rendered) == 1 else "stringset",
            rendered[0] if len(rendered) == 1 else rendered,
        )
    if event == "push":
        rendered = tuple(
            [f"refs/heads/{item}" if name == "github.ref" else item for item in branches]
            + [f"refs/tags/{item}" if name == "github.ref" else item for item in tags]
        )
        if rendered:
            return _Value(
                "string" if len(rendered) == 1 else "stringset",
                rendered[0] if len(rendered) == 1 else rendered,
            )
        return _Value("unknown", missing={"push ref value or exact trigger filter"})
    return _Value("unknown", missing={f"runtime ref value for {event}"})


def _reference_value(name: str, context: ConditionRuntimeContext) -> _Value:
    if name.startswith("needs.*"):
        return _Value("unknown", missing={"needs wildcard runtime semantics"})
    if name.startswith("needs.") and name.endswith(".result"):
        parts = name.split(".")
        if len(parts) == 3:
            result = context.needs_results.get(parts[1].casefold())
            if result is None or result is ExecutionResult.UNKNOWN:
                return _Value("unknown", missing={f"dependency result: {parts[1]}"})
            return _Value("string", result.value)
    if name.startswith("steps.") and (name.endswith(".outcome") or name.endswith(".conclusion")):
        parts = name.split(".")
        if len(parts) == 3:
            results = context.step_results.get(parts[1].casefold())
            if results is None:
                return _Value("unknown", missing={f"prior step result: {parts[1]}"})
            result = results[0] if parts[2] == "outcome" else results[1]
            if result is ExecutionResult.UNKNOWN:
                return _Value("unknown", missing={f"prior step {parts[2]}: {parts[1]}"})
            return _Value("string", result.value)
    if name == "github.event_name":
        return _Value("string", context.event_name)
    ref_value = _ref_value(name, context)
    if ref_value is not None:
        return ref_value
    if name == "github.repository":
        if context.repository_full_name:
            return _Value("string", context.repository_full_name)
        return _Value("string", "__base_repository__")
    if name == "github.repository_owner":
        if context.repository_owner:
            return _Value("string", context.repository_owner)
        return _Value("unknown", missing={"GitHub repository owner metadata"})
    if name == "github.event.pull_request.head.repo.fork":
        if context.event_name not in {"pull_request", "pull_request_target"}:
            return _Value("boolean", False)
        if context.profile is AttackerProfile.FORK_PR:
            return _Value("boolean", True)
        return _Value("unknown", missing={"pull-request head repository identity"})
    if name == "github.event.pull_request.head.repo.full_name":
        if context.event_name not in {"pull_request", "pull_request_target"}:
            return _Value("unknown", missing={"pull-request event context"})
        if context.profile is AttackerProfile.FORK_PR:
            return _Value("string", "__fork_repository__")
        return _Value("unknown", missing={"pull-request head repository identity"})
    if name == "github.event.pull_request.base.repo.full_name":
        if context.event_name not in {"pull_request", "pull_request_target"}:
            return _Value("unknown", missing={"pull-request event context"})
        return _Value("string", context.repository_full_name or "__base_repository__")
    if name == "github.event.pull_request.labels.*.name":
        return _Value("unknown", missing={"pull-request label set at evaluated revision"})
    if name == "github.actor":
        if context.actor_login is None:
            return _Value("unknown", missing={"workflow run actor identity"})
        return _Value("string", context.actor_login)
    if name == "github.actor_id":
        if context.actor_id is None:
            return _Value("unknown", missing={"workflow run actor identity"})
        return _Value("number", context.actor_id)
    if name == "github.triggering_actor":
        if context.triggering_actor_login is None:
            return _Value("unknown", missing={"workflow run triggering actor identity"})
        return _Value("string", context.triggering_actor_login)

    linkage_value = event_value_from_linkage(name, context.event_linkage)
    if linkage_value is not None:
        return _Value("number" if isinstance(linkage_value, int) else "string", linkage_value)

    role = principal_role_for_reference(name, context.event_name)
    if role is not None:
        principal = context.event_principals.get(role)
        if principal is None:
            return _Value("unknown", missing={f"event principal identity: {role.value}"})
        if name.endswith(".login"):
            if principal.login is None:
                return _Value("unknown", missing={f"event principal login: {role.value}"})
            return _Value("string", principal.login)
        if name.endswith(".id"):
            if principal.actor_id is None:
                return _Value("unknown", missing={f"event principal id: {role.value}"})
            return _Value("number", principal.actor_id)
        if name.endswith(".type"):
            if principal.account_type is None:
                return _Value("unknown", missing={f"event principal account type: {role.value}"})
            return _Value("string", principal.account_type)
        if name.endswith(".author_association"):
            if principal.author_association is None:
                return _Value("unknown", missing={f"event author association: {role.value}"})
            return _Value("string", principal.author_association)
        return _Value("unknown", missing={f"event payload content value: {name}"})
    return _Value("unknown", missing={f"unsupported condition context: {name}"})


def _string_function(name: str, subject: _Value, needle: _Value) -> _Value:
    missing = subject.missing | needle.missing
    if subject.kind == "unknown" or needle.kind == "unknown":
        return _Value("unknown", missing=missing)
    if needle.kind != "string":
        return _Value("unknown", missing={"string function needle must be a string"})
    expected = str(needle.value).casefold()
    if subject.kind == "list":
        values = tuple(str(item).casefold() for item in subject.value or ())
        return _Value("boolean", expected in values)
    if subject.kind == "stringset":
        values = tuple(str(item) for item in subject.value or ())
        outcomes = {
            expected in value.casefold()
            if name == "contains"
            else value.casefold().startswith(expected)
            if name == "startswith"
            else value.casefold().endswith(expected)
            for value in values
        }
        if len(outcomes) == 1:
            return _Value("boolean", outcomes.pop())
        return _Value("unknown", missing={"workflow trigger ref alternatives"})
    if subject.kind == "pattern":
        prefix, suffix = subject.value if isinstance(subject.value, tuple) else ("", "")
        prefix = prefix.casefold()
        suffix = suffix.casefold()
        if name == "startswith" and prefix.startswith(expected):
            return _Value("boolean", True)
        if name == "endswith" and suffix.endswith(expected):
            return _Value("boolean", True)
        if name == "contains" and (expected in prefix or expected in suffix):
            return _Value("boolean", True)
        return _Value("unknown", missing={"symbolic GitHub ref value"})
    if subject.kind != "string":
        return _Value("unknown", missing={"string function subject must be a string or list"})
    actual = str(subject.value).casefold()
    result = (
        expected in actual
        if name == "contains"
        else actual.startswith(expected)
        if name == "startswith"
        else actual.endswith(expected)
    )
    return _Value("boolean", result)


def _compare_values(left: _Value, right: _Value, operator: object) -> _Value:
    missing = left.missing | right.missing
    if left.kind == "unknown" or right.kind == "unknown":
        return _Value("unknown", missing=missing)
    if left.kind == "pattern" or right.kind == "pattern":
        return _Value("unknown", missing={"symbolic GitHub ref equality"})
    if left.kind == "stringset" and right.kind == "string":
        outcomes = {
            str(value).casefold() == str(right.value).casefold() for value in left.value or ()
        }
    elif right.kind == "stringset" and left.kind == "string":
        outcomes = {
            str(value).casefold() == str(left.value).casefold() for value in right.value or ()
        }
    elif left.kind != right.kind:
        return _Value("unknown", missing={"unsupported GitHub loose equality coercion"})
    elif left.kind == "string":
        outcomes = {str(left.value).casefold() == str(right.value).casefold()}
    else:
        outcomes = {left.value == right.value}
    if operator == "!=":
        outcomes = {not item for item in outcomes}
    if len(outcomes) != 1:
        return _Value("unknown", missing={"workflow trigger ref alternatives"})
    return _Value("boolean", outcomes.pop())


def _evaluate(node: _Node, context: ConditionRuntimeContext) -> _Value:
    if node.kind == "literal":
        if isinstance(node.value, bool):
            return _Value("boolean", node.value)
        if isinstance(node.value, int):
            return _Value("number", node.value)
        return _Value("string", str(node.value))
    if node.kind == "reference":
        return _reference_value(str(node.value), context)
    if node.kind == "status":
        if node.value == "always":
            return _Value("boolean", True)
        result = {
            "success": context.status_success,
            "failure": context.status_failure,
            "cancelled": context.status_cancelled,
        }[str(node.value)]
        if result is TriState.UNKNOWN:
            return _Value("unknown", missing={f"runtime status result for {node.value}()"})
        return _Value("boolean", result is TriState.TRUE)
    if node.kind == "function":
        if node.value not in {"contains", "startswith", "endswith"}:
            return _Value("unknown", missing={f"unsupported function semantics: {node.value}"})
        return _string_function(
            str(node.value),
            _evaluate(node.args[0], context),
            _evaluate(node.args[1], context),
        )
    if node.kind == "not":
        verdict, missing = _boolean(_evaluate(node.left, context))  # type: ignore[arg-type]
        return _Value(
            "boolean" if verdict is not TriState.UNKNOWN else "unknown",
            verdict is TriState.FALSE,
            missing,
        )
    left = _evaluate(node.left, context)  # type: ignore[arg-type]
    right = _evaluate(node.right, context)  # type: ignore[arg-type]
    if node.kind in {"and", "or"}:
        left_verdict, left_missing = _boolean(left)
        right_verdict, right_missing = _boolean(right)
        verdict = (
            _tri_and(left_verdict, right_verdict)
            if node.kind == "and"
            else _tri_or(left_verdict, right_verdict)
        )
        missing = left_missing | right_missing if verdict is TriState.UNKNOWN else set()
        return _Value(
            "boolean" if verdict is not TriState.UNKNOWN else "unknown",
            verdict is TriState.TRUE,
            missing,
        )
    if node.kind == "compare":
        return _compare_values(left, right, node.value)
    raise AssertionError(f"unhandled condition node: {node.kind}")


def _references(node: _Node) -> set[str]:
    result = {str(node.value)} if node.kind == "reference" else set()
    if node.left is not None:
        result.update(_references(node.left))
    if node.right is not None:
        result.update(_references(node.right))
    for arg in node.args:
        result.update(_references(arg))
    return result


def _status_functions(node: _Node) -> set[str]:
    result = {str(node.value)} if node.kind == "status" else set()
    if node.left is not None:
        result.update(_status_functions(node.left))
    if node.right is not None:
        result.update(_status_functions(node.right))
    for arg in node.args:
        result.update(_status_functions(arg))
    return result


@dataclass(frozen=True)
class _SecurityAssessment:
    effect: ConditionSecurityEffect
    missing: frozenset[str] = frozenset()
    revision_bound: TriState = TriState.UNKNOWN


_ATTACKER_CONTROLLED_REFERENCES = {
    "github.head_ref",
    "github.event.comment.body",
    "github.event.issue.body",
    "github.event.issue.title",
    "github.event.pull_request.body",
    "github.event.pull_request.title",
    "github.event.review.body",
    "github.event.review_comment.body",
}
_LABEL_REFERENCES = {
    "github.event.label.name",
    "github.event.pull_request.labels.*.name",
}


def _effect_from_value(node: _Node, context: ConditionRuntimeContext) -> _SecurityAssessment:
    verdict, missing = _boolean(_evaluate(node, context))
    if verdict is TriState.TRUE:
        return _SecurityAssessment(
            ConditionSecurityEffect.ALWAYS_TRUE, revision_bound=TriState.TRUE
        )
    if verdict is TriState.FALSE:
        return _SecurityAssessment(ConditionSecurityEffect.BLOCKS, revision_bound=TriState.TRUE)
    return _SecurityAssessment(ConditionSecurityEffect.UNKNOWN, frozenset(missing))


def _predicate_security(node: _Node, context: ConditionRuntimeContext) -> _SecurityAssessment:
    references = _references(node)
    if references & _LABEL_REFERENCES:
        return _SecurityAssessment(
            ConditionSecurityEffect.TRUST_REQUIRED,
            frozenset(
                {
                    "trusted label mutation provenance",
                    "condition approval is not bound to the exact workflow revision",
                }
            ),
            TriState.UNKNOWN,
        )
    association_refs = {item for item in references if item.endswith(".author_association")}
    if association_refs:
        return _SecurityAssessment(
            ConditionSecurityEffect.TRUST_REQUIRED,
            frozenset({"event-time trusted association semantics"}),
            TriState.TRUE,
        )
    if "github.triggering_actor" in references:
        return _SecurityAssessment(
            ConditionSecurityEffect.UNKNOWN,
            frozenset({"github.triggering_actor is not workflow privilege authority"}),
        )
    identity_refs = {
        item
        for item in references
        if item in {"github.actor", "github.actor_id"}
        or item.endswith((".user.login", ".user.id", ".sender.login", ".sender.id"))
    }
    if identity_refs and "github.repository_owner" in references:
        if context.profile is AttackerProfile.FORK_PR:
            return _SecurityAssessment(
                ConditionSecurityEffect.TRUST_REQUIRED,
                revision_bound=TriState.TRUE,
            )
        return _SecurityAssessment(
            ConditionSecurityEffect.UNKNOWN,
            frozenset({"repository writer may also be the repository owner"}),
        )
    if identity_refs:
        if (
            context.profile is AttackerProfile.FORK_PR
            and node.kind == "compare"
            and node.value == "!="
        ):
            return _SecurityAssessment(
                ConditionSecurityEffect.ATTACKER_SATISFIABLE,
                revision_bound=TriState.TRUE,
            )
        return _SecurityAssessment(
            ConditionSecurityEffect.UNKNOWN,
            frozenset({"literal actor identity does not prove trusted authorization"}),
        )
    if references & _ATTACKER_CONTROLLED_REFERENCES:
        return _SecurityAssessment(
            ConditionSecurityEffect.ATTACKER_SATISFIABLE,
            revision_bound=TriState.TRUE,
        )
    return _effect_from_value(node, context)


def _and_security(left: _SecurityAssessment, right: _SecurityAssessment) -> _SecurityAssessment:
    missing = left.missing | right.missing
    effects = {left.effect, right.effect}
    if ConditionSecurityEffect.BLOCKS in effects:
        return _SecurityAssessment(ConditionSecurityEffect.BLOCKS, revision_bound=TriState.TRUE)
    if effects == {ConditionSecurityEffect.ALWAYS_TRUE}:
        return _SecurityAssessment(
            ConditionSecurityEffect.ALWAYS_TRUE, revision_bound=TriState.TRUE
        )
    if ConditionSecurityEffect.TRUST_REQUIRED in effects:
        revision = (
            TriState.TRUE
            if left.revision_bound is TriState.TRUE and right.revision_bound is TriState.TRUE
            else TriState.UNKNOWN
        )
        return _SecurityAssessment(ConditionSecurityEffect.TRUST_REQUIRED, missing, revision)
    if ConditionSecurityEffect.UNKNOWN in effects:
        return _SecurityAssessment(ConditionSecurityEffect.UNKNOWN, missing)
    if effects <= {
        ConditionSecurityEffect.ALWAYS_TRUE,
        ConditionSecurityEffect.ATTACKER_SATISFIABLE,
    }:
        if left.effect is right.effect is ConditionSecurityEffect.ATTACKER_SATISFIABLE:
            return _SecurityAssessment(
                ConditionSecurityEffect.UNKNOWN,
                frozenset({"correlated attacker-controlled predicates"}),
            )
        return _SecurityAssessment(
            ConditionSecurityEffect.ATTACKER_SATISFIABLE,
            revision_bound=TriState.TRUE,
        )
    return _SecurityAssessment(ConditionSecurityEffect.UNKNOWN, missing)


def _or_security(left: _SecurityAssessment, right: _SecurityAssessment) -> _SecurityAssessment:
    missing = left.missing | right.missing
    effects = {left.effect, right.effect}
    if ConditionSecurityEffect.ALWAYS_TRUE in effects:
        return _SecurityAssessment(
            ConditionSecurityEffect.ALWAYS_TRUE, revision_bound=TriState.TRUE
        )
    if effects == {ConditionSecurityEffect.BLOCKS}:
        return _SecurityAssessment(ConditionSecurityEffect.BLOCKS, revision_bound=TriState.TRUE)
    if ConditionSecurityEffect.ATTACKER_SATISFIABLE in effects:
        return _SecurityAssessment(
            ConditionSecurityEffect.ATTACKER_SATISFIABLE,
            revision_bound=TriState.TRUE,
        )
    if effects <= {ConditionSecurityEffect.BLOCKS, ConditionSecurityEffect.TRUST_REQUIRED}:
        revision = (
            TriState.TRUE
            if left.revision_bound is TriState.TRUE and right.revision_bound is TriState.TRUE
            else TriState.UNKNOWN
        )
        return _SecurityAssessment(ConditionSecurityEffect.TRUST_REQUIRED, missing, revision)
    return _SecurityAssessment(ConditionSecurityEffect.UNKNOWN, missing)


def _security_assessment(node: _Node, context: ConditionRuntimeContext) -> _SecurityAssessment:
    if node.kind in {"literal", "status", "reference", "compare", "function"}:
        return _predicate_security(node, context)
    if node.kind == "not":
        child = _security_assessment(node.left, context)  # type: ignore[arg-type]
        if child.effect is ConditionSecurityEffect.ALWAYS_TRUE:
            return _SecurityAssessment(ConditionSecurityEffect.BLOCKS, revision_bound=TriState.TRUE)
        if child.effect is ConditionSecurityEffect.BLOCKS:
            return _SecurityAssessment(
                ConditionSecurityEffect.ALWAYS_TRUE, revision_bound=TriState.TRUE
            )
        if child.effect is ConditionSecurityEffect.ATTACKER_SATISFIABLE:
            return child
        return _SecurityAssessment(
            ConditionSecurityEffect.UNKNOWN,
            child.missing or frozenset({"negated trust-mediated predicate"}),
        )
    left = _security_assessment(node.left, context)  # type: ignore[arg-type]
    right = _security_assessment(node.right, context)  # type: ignore[arg-type]
    if (
        node.kind == "and"
        and left.effect is ConditionSecurityEffect.ATTACKER_SATISFIABLE
        and right.effect is ConditionSecurityEffect.ATTACKER_SATISFIABLE
        and not (_references(node.left) & _references(node.right))  # type: ignore[arg-type]
    ):
        return _SecurityAssessment(
            ConditionSecurityEffect.ATTACKER_SATISFIABLE,
            revision_bound=TriState.TRUE,
        )
    return _and_security(left, right) if node.kind == "and" else _or_security(left, right)


def _effect_flags(effect: ConditionSecurityEffect) -> tuple[TriState, TriState]:
    attacker = (
        TriState.TRUE
        if effect
        in {ConditionSecurityEffect.ALWAYS_TRUE, ConditionSecurityEffect.ATTACKER_SATISFIABLE}
        else TriState.FALSE
        if effect in {ConditionSecurityEffect.TRUST_REQUIRED, ConditionSecurityEffect.BLOCKS}
        else TriState.UNKNOWN
    )
    trusted = (
        TriState.TRUE
        if effect is ConditionSecurityEffect.TRUST_REQUIRED
        else TriState.FALSE
        if effect
        in {
            ConditionSecurityEffect.ALWAYS_TRUE,
            ConditionSecurityEffect.ATTACKER_SATISFIABLE,
            ConditionSecurityEffect.BLOCKS,
        }
        else TriState.UNKNOWN
    )
    return attacker, trusted


def evaluate_condition(
    condition: str | None,
    *,
    target_id: str,
    target_kind: Literal["job", "step"],
    trigger_context_id: str,
    context: ConditionRuntimeContext,
) -> ConditionEvaluation:
    expression = condition or "true"
    normalized = _normalize(condition)
    missing: set[str] = set()
    references: set[str] = set()
    status_functions: set[str] = set()
    try:
        tokens = _tokenize(normalized)
        references = {
            token.value.casefold()
            for token in tokens
            if token.kind == "identifier" and token.value.casefold().startswith("github.")
        }
        tree = _Parser(tokens).parse()
        references.update(_references(tree))
        status_functions = _status_functions(tree)
        value = _evaluate(tree, context)
        security = _security_assessment(tree, context)
        raw_verdict, raw_missing = _boolean(value)
        implicit_success = not status_functions
        status_gate = context.implicit_success_gate if implicit_success else TriState.TRUE
        verdict = _tri_and(status_gate, raw_verdict)
        missing = set(raw_missing) if verdict is TriState.UNKNOWN else set()
        if implicit_success and status_gate is TriState.UNKNOWN and verdict is TriState.UNKNOWN:
            missing.add("runtime status result for implicit success()")
    except ConditionSyntaxError as exc:
        raw_verdict = TriState.UNKNOWN
        status_gate = TriState.UNKNOWN
        implicit_success = False
        verdict = TriState.UNKNOWN
        missing = {f"unsupported condition syntax: {exc}"}
        security = _SecurityAssessment(
            ConditionSecurityEffect.UNKNOWN,
            frozenset(missing),
        )

    refs: list[EvidenceRequirementRef] = []
    if context.github_run_id is not None and context.repository_id:
        status_missing = any(
            item.startswith("runtime status result for")
            or item.startswith("dependency result:")
            or item.startswith("prior step result:")
            or item.startswith("prior step outcome:")
            or item.startswith("prior step conclusion:")
            for item in missing
        )
        status_references = {
            reference for reference in references if reference.startswith(("needs.", "steps."))
        }
        if status_missing:
            refs.append(
                EvidenceRequirementRef(
                    kind=EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_IDENTITY,
                    provider=EvidenceProvider.GITHUB,
                    subject_id=f"{context.repository_id}:run:{context.github_run_id}",
                    qualifier=str(context.github_run_id),
                )
            )
        if status_missing and (
            status_references
            or status_functions & {"success", "failure", "cancelled"}
            or "runtime status result for implicit success()" in missing
        ):
            refs.append(
                EvidenceRequirementRef(
                    kind=EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_JOBS,
                    provider=EvidenceProvider.GITHUB,
                    subject_id=f"{context.repository_id}:run:{context.github_run_id}",
                    qualifier=str(context.github_run_id),
                )
            )

    repository_refs = references & {"github.repository", "github.repository_owner"}
    if (
        repository_refs
        and (context.repository_full_name is None or context.repository_owner is None)
        and context.repository_id
    ):
        refs.append(
            EvidenceRequirementRef(
                kind=EvidenceRequirementKind.GITHUB_REPOSITORY_METADATA,
                provider=EvidenceProvider.GITHUB,
                subject_id=context.repository_id,
            )
        )

    actor_refs = references & {
        "github.actor",
        "github.actor_id",
        "github.triggering_actor",
    }
    identity_matches_event = (
        context.run_event_name is None
        or context.run_event_name.casefold() == context.event_name.casefold()
    )
    if actor_refs and context.github_run_id is not None and context.actor_login is None:
        refs.append(
            EvidenceRequirementRef(
                kind=EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_IDENTITY,
                provider=EvidenceProvider.GITHUB,
                subject_id=f"{context.repository_id}:run:{context.github_run_id}",
                qualifier=str(context.github_run_id),
            )
        )
    if actor_refs and not identity_matches_event:
        verdict = TriState.UNKNOWN
        missing.add("workflow run event does not match trigger context")

    current_authorization: TriState | None = None
    authorization_missing: set[str] = set()
    authorization_scope: Literal["none", "current"] = "none"
    authoritative_actor_ref = bool(actor_refs & {"github.actor", "github.actor_id"})
    if authoritative_actor_ref:
        authorization_scope = "current"
        if context.actor_login is None or not identity_matches_event:
            current_authorization = TriState.UNKNOWN
            authorization_missing.add("workflow run actor identity")
        else:
            permission_evidence = context.actor_permissions.get(context.actor_login.casefold())
            if permission_evidence is None:
                current_authorization = TriState.UNKNOWN
                authorization_missing.add("current repository actor authorization")
                if context.repository_id:
                    refs.append(
                        EvidenceRequirementRef(
                            kind=EvidenceRequirementKind.GITHUB_REPOSITORY_ACTOR_PERMISSION,
                            provider=EvidenceProvider.GITHUB,
                            subject_id=f"{context.repository_id}:actor:{context.actor_login}",
                            qualifier=context.actor_login,
                        )
                    )
            else:
                permission, permission_actor_id = permission_evidence
                if (
                    permission_actor_id is not None
                    and context.actor_id is not None
                    and permission_actor_id != context.actor_id
                ):
                    current_authorization = TriState.UNKNOWN
                    authorization_missing.add("actor permission identity mismatch")
                elif permission in {"admin", "write"}:
                    current_authorization = TriState.TRUE
                elif permission in {"read", "none"}:
                    current_authorization = TriState.FALSE
                else:
                    current_authorization = TriState.UNKNOWN
                    authorization_missing.add("supported repository actor permission")
    elif "github.triggering_actor" in actor_refs:
        current_authorization = TriState.UNKNOWN
        authorization_missing.add("github.triggering_actor is not workflow privilege authority")

    event_refs_by_role: dict[EventPrincipalRole, list[str]] = {}
    for reference in references:
        role = principal_role_for_reference(reference, context.event_name)
        if role is not None:
            event_refs_by_role.setdefault(role, []).append(reference)

    principal_assessments: list[PrincipalAuthorizationAssessment] = []
    linkage = context.event_linkage
    linkage_matches_event = (
        linkage is not None and linkage.event_name.casefold() == context.event_name.casefold()
    )
    if event_refs_by_role and not linkage_matches_event:
        if context.github_run_id is not None and context.repository_id:
            refs.append(
                EvidenceRequirementRef(
                    kind=EvidenceRequirementKind.GITHUB_WORKFLOW_RUN_EVENT_LINKAGE,
                    provider=EvidenceProvider.GITHUB,
                    subject_id=f"{context.repository_id}:run:{context.github_run_id}",
                    qualifier=str(context.github_run_id),
                )
            )
        missing.add("workflow run event resource linkage")

    for role, role_references in sorted(event_refs_by_role.items(), key=lambda item: item[0].value):
        assessment_missing: set[str] = set()
        principal = context.event_principals.get(role) if linkage_matches_event else None
        if principal is None:
            qualifier = event_principal_qualifier(role, linkage) if linkage_matches_event else None
            if (
                qualifier is not None
                and context.repository_id
                and context.github_run_id is not None
            ):
                refs.append(
                    EvidenceRequirementRef(
                        kind=EvidenceRequirementKind.GITHUB_EVENT_PRINCIPAL,
                        provider=EvidenceProvider.GITHUB,
                        subject_id=(
                            f"{context.repository_id}:run:{context.github_run_id}:principal:{role.value}"
                        ),
                        qualifier=qualifier,
                    )
                )
                assessment_missing.add(f"event principal identity: {role.value}")
            elif linkage_matches_event:
                assessment_missing.add(f"exact event principal locator: {role.value}")
            else:
                assessment_missing.add("workflow run event resource linkage")
            principal_login = None
            principal_id = None
            current_principal_authorization = TriState.UNKNOWN
            principal_scope: Literal["none", "current"] = "none"
            event_action_observed = TriState.UNKNOWN
        else:
            principal_login = principal.login
            principal_id = principal.actor_id
            event_action_observed = TriState.TRUE
            principal_scope = "current"
            if principal_login is None:
                current_principal_authorization = TriState.UNKNOWN
                assessment_missing.add(f"event principal login: {role.value}")
            else:
                permission_evidence = context.actor_permissions.get(principal_login.casefold())
                if permission_evidence is None:
                    current_principal_authorization = TriState.UNKNOWN
                    assessment_missing.add("current repository event principal authorization")
                    if context.repository_id:
                        refs.append(
                            EvidenceRequirementRef(
                                kind=EvidenceRequirementKind.GITHUB_REPOSITORY_ACTOR_PERMISSION,
                                provider=EvidenceProvider.GITHUB,
                                subject_id=f"{context.repository_id}:actor:{principal_login}",
                                qualifier=principal_login,
                            )
                        )
                else:
                    permission, permission_actor_id = permission_evidence
                    if (
                        permission_actor_id is not None
                        and principal_id is not None
                        and permission_actor_id != principal_id
                    ):
                        current_principal_authorization = TriState.UNKNOWN
                        assessment_missing.add("event principal permission identity mismatch")
                    elif permission in {"admin", "write"}:
                        current_principal_authorization = TriState.TRUE
                    elif permission in {"read", "none"}:
                        current_principal_authorization = TriState.FALSE
                    else:
                        current_principal_authorization = TriState.UNKNOWN
                        assessment_missing.add("supported repository event principal permission")
            assessment_missing.add("historical event principal authorization")

        principal_assessments.append(
            PrincipalAuthorizationAssessment(
                principal_role=role,
                reference=sorted(role_references)[0],
                principal_login=principal_login,
                principal_id=principal_id,
                current_repository_authorization=current_principal_authorization,
                authorization_scope=principal_scope,
                event_action_observed=event_action_observed,
                historical_repository_authorization=TriState.UNKNOWN,
                missing_evidence=sorted(assessment_missing),
            )
        )

    unique_refs = {(ref.kind, ref.provider, ref.subject_id, ref.qualifier): ref for ref in refs}
    attacker_satisfiable, trusted_required = _effect_flags(security.effect)
    return ConditionEvaluation(
        evaluation_id=stable_id("condition-evaluation", trigger_context_id, target_id),
        target_id=target_id,
        target_kind=target_kind,
        trigger_context_id=trigger_context_id,
        expression=expression,
        normalized_expression=normalized,
        verdict=verdict,
        raw_expression_verdict=raw_verdict,
        status_gate_verdict=status_gate,
        implicit_success_applied=implicit_success,
        referenced_contexts=sorted(references),
        missing_evidence=sorted(missing),
        evidence_requirements=sorted(
            unique_refs.values(),
            key=lambda item: (item.kind.value, item.subject_id, item.qualifier or ""),
        ),
        current_actor_authorization=current_authorization,
        authorization_missing_evidence=sorted(authorization_missing),
        authorization_scope=authorization_scope,
        principal_assessments=principal_assessments,
        security_effect=security.effect,
        attacker_satisfiable=attacker_satisfiable,
        trusted_principal_required=trusted_required,
        revision_bound=security.revision_bound,
        security_missing_evidence=sorted(security.missing),
    )
