"""Small independent models. Deliberately do not import redteam_core helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ObservedValue:
    identifier: str
    payload_hash: str
    acquired_at: datetime
    inferred: bool = False
    snapshot: str | None = None
    expires_at: datetime | None = None


def knowledge_oracle(
    rows: tuple[ObservedValue, ...],
    *,
    now: datetime,
    max_age: int | None = None,
    snapshot: str | None = None,
) -> dict[str, object]:
    """Direct set classification, without the production freshness/slot helpers."""
    dates = [now, *(r.acquired_at for r in rows)]
    dates.extend(r.expires_at for r in rows if r.expires_at is not None)
    if any(d.tzinfo is None or d.utcoffset() is None for d in dates):
        raise ValueError("aware dates required")
    if any(r.acquired_at > now for r in rows):
        raise ValueError("future observation")
    if len({r.identifier for r in rows}) != len(rows):
        raise ValueError("oracle input must have unique observation IDs")
    stale = {
        r.identifier
        for r in rows
        if (max_age is not None and (now - r.acquired_at).total_seconds() > max_age)
        or (snapshot is not None and r.snapshot != snapshot)
        or (r.expires_at is not None and now > r.expires_at)
    }
    fresh = [r for r in rows if r.identifier not in stale]
    authoritative = [r for r in fresh if not r.inferred]
    hashes = sorted({r.payload_hash for r in authoritative})
    return {
        "status": "UNKNOWN" if not hashes else "CONSISTENT" if len(hashes) == 1 else "CONFLICTING",
        "effective_payload_hash": hashes[0] if len(hashes) == 1 else None,
        "authoritative_payload_hashes": hashes,
        "observation_ids": sorted(r.identifier for r in fresh),
        "authoritative_observation_ids": sorted(r.identifier for r in authoritative),
        "inferred_observation_ids": sorted(r.identifier for r in fresh if r.inferred),
        "stale_observation_ids": sorted(stale),
    }


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    role: str = "flow"
    verdict: str = "TRUE"
    contexts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Guard:
    node: str
    effect: str = "BLOCKS"
    verdict: str = "TRUE"
    contexts: tuple[str, ...] = ()


# Compare reachability by principal, sink, context and claim ceiling, not path count.
# Different routes for the SAME principal may be represented by one frontier.
Reachability = tuple[str, str, tuple[str, ...], str]


def graph_oracle(
    edges: tuple[Edge, ...], guards: tuple[Guard, ...] = (), *, max_depth: int = 8
) -> frozenset[Reachability]:
    """Exhaustive simple paths; no dominance pruning, ranking, or core imports.

    The bounded vocabulary supports source/flow/bridge/sink/unregistered edges,
    terminating sinks, and target-scoped guards. Other production features are
    intentionally not claimed by this oracle.
    """
    nodes = {n for e in edges for n in (e.source, e.target)}
    if len(nodes) > 8 or len(edges) > 12 or not 1 <= max_depth <= 8:
        raise ValueError("small graph oracle bounds exceeded")
    if any(e.role not in {"source", "flow", "bridge", "sink", "unregistered"} for e in edges):
        raise ValueError("unsupported oracle edge role")
    if any(x.verdict not in {"TRUE", "FALSE", "UNKNOWN"} for x in (*edges, *guards)):
        raise ValueError("invalid oracle verdict")
    if any(g.effect not in {"BLOCKS", "MAY_BLOCK", "INFORMATIONAL"} for g in guards):
        raise ValueError("unsupported guard effect")
    reached: set[Reachability] = set()

    def visit(
        edge: Edge,
        path: tuple[str, ...],
        contexts: frozenset[str],
        uncertain: bool,
        influence: bool,
        depth: int,
    ) -> None:
        if edge.role == "unregistered" or edge.verdict == "FALSE" or edge.target in path:
            return
        extra = frozenset(edge.contexts)
        if contexts and extra and contexts.isdisjoint(extra) and edge.role != "bridge":
            return
        current_context = contexts | extra
        uncertain = uncertain or edge.verdict == "UNKNOWN"
        influence = influence or (edge.role == "source" and edge.verdict == "TRUE")
        for guard in guards:
            if guard.node != edge.target or guard.verdict == "FALSE":
                continue
            if guard.contexts and current_context and current_context.isdisjoint(guard.contexts):
                continue
            if guard.effect == "BLOCKS" and guard.verdict == "TRUE":
                return
            if guard.effect == "MAY_BLOCK" or guard.verdict == "UNKNOWN":
                uncertain = True
        if edge.role == "sink":
            reached.add(
                (
                    path[0],
                    edge.target,
                    tuple(sorted(current_context)),
                    "SUPPORTED" if influence and not uncertain else "CANDIDATE",
                )
            )
            return
        if depth < max_depth:
            for following in edges:
                if following.source == edge.target:
                    visit(
                        following,
                        (*path, edge.target),
                        current_context,
                        uncertain,
                        influence,
                        depth + 1,
                    )

    for entry in edges:
        if entry.role == "source":
            visit(entry, (entry.source,), frozenset(), False, False, 1)
    return frozenset(reached)


def transition_oracle(
    baseline: tuple[int, int] | None,
    observed: tuple[int, int] | None,
    *,
    authoritative: bool = True,
) -> str:
    if baseline is None or observed is None or not authoritative:
        return "UNKNOWN"
    if observed[0] != baseline[0]:
        return "ANOMALY"
    return "PASS" if observed[1] == baseline[1] else "VIOLATION"


def completion_oracle(
    *,
    scope_complete: bool,
    search_complete: bool,
    truncated: bool,
    unresolved: int,
    blocked: int,
    uncertain: int,
    cleanup_gaps: int,
) -> str:
    """Specification for G7, not a claim that a production G7 checker exists."""
    if min(unresolved, blocked, uncertain, cleanup_gaps) < 0:
        raise ValueError("counts cannot be negative")
    complete = scope_complete and search_complete and not truncated
    return (
        "COMPLETE"
        if complete and not any((unresolved, blocked, uncertain, cleanup_gaps))
        else "PARTIAL"
    )


def scheduler_oracle(events: tuple[tuple[str, str], ...], limit: int) -> dict:
    """Independent durable single-request specification for G3/G4.

    DISPATCH denotes durable intent, CALL is external invocation. Restart can
    release only proven-undispatched reservations; a receipt survives restart.
    Used/reserved/uncertain all consume the original request allowance.
    """
    if limit < 0:
        raise ValueError("invalid request limit")
    states: dict[str, str] = {}
    calls = 0
    visible: set[str] = set()
    for action, attempt in events:
        state = states.get(attempt)
        if action == "RESTART":
            states = {
                key: ("UNCERTAIN" if value in {"DISPATCHED", "CALLED"} else value)
                for key, value in states.items()
                if value != "RESERVED"
            }
        elif action == "RESERVE":
            if state is not None or len(states) >= limit:
                raise ValueError("reservation rejected")
            states[attempt] = "RESERVED"
        elif action == "DISPATCH" and state == "RESERVED":
            states[attempt] = "DISPATCHED"
        elif action == "CALL" and state == "DISPATCHED":
            calls += 1
            states[attempt] = "CALLED"
        elif action == "RESULT" and state == "CALLED":
            states[attempt] = "RESULT"
        elif action == "FAILURE" and state == "CALLED":
            states[attempt] = "FAILED"
        elif action == "COMMIT" and state in {"RESULT", "COMMITTED"}:
            states[attempt] = "COMMITTED"
            visible.add(attempt)
        else:
            raise ValueError("invalid scheduler transition")
    return {
        "states": states,
        "calls": calls,
        "remaining": limit - len(states),
        "visible": frozenset(visible),
    }
