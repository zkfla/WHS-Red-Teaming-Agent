"""Offline G6 focused-context benchmark with deterministic synthetic token counts."""

import argparse
import json
import math
import platform
import time
import tracemalloc
from pathlib import Path

from redteam_core.models import Relation
from redteam_core.models.base import canonical_json, sha256_text
from redteam_core.reasoning import (
    ContextProjectionPolicy,
    FocusedContextPolicy,
    project_decision_context,
    project_focused_decision_context,
)
from tests.performance.baseline import ROOT, current_core_hash, tree_hash
from tests.performance.statistics import paired_ratio_interval, samples_summary
from tests.unit.test_focused_context import RELATION_TYPE, fixture


def synthetic_token_count(content: str) -> int:
    """Stable UTF-8 quad estimate; explicitly not a provider tokenizer."""
    return math.ceil(len(content.encode("utf-8")) / 4)


def workload(unrelated_relations: int):
    session, candidate, requirement, _secret = fixture()
    for index in range(unrelated_relations):
        relation = Relation.create(
            source_ref=f"unrelated-source:{index}",
            relation_type_id=RELATION_TYPE,
            target_ref=f"unrelated-target:{index}",
        )
        session.semantic.relations[relation.relation_id] = relation
    return session, candidate, requirement


def verify_focused(result, candidate, requirement):
    parsed = json.loads(result.content)
    bundles = parsed["channels"]["CANONICAL_STATE"]["candidate_bundles"]
    if result.complete_bundles != 1 or result.incomplete_bundles != 0 or len(bundles) != 1:
        raise AssertionError("focused context did not preserve one complete active bundle")
    bundle = bundles[0]
    if bundle["candidate"]["candidate_id"] != candidate.candidate_id:
        raise AssertionError("focused context selected the wrong candidate")
    if {row["relation_id"] for row in bundle["relations"]} != {
        *candidate.supporting_relation_ids,
        *candidate.contradicting_relation_ids,
        *candidate.guard_relation_ids,
    }:
        raise AssertionError("focused context omitted a required relation role")
    if {row["requirement_id"] for row in bundle["requirements"]} != {requirement.requirement_id}:
        raise AssertionError("focused context omitted the requirement")
    if {row["expectation_id"] for row in bundle["expectations"]} != set(candidate.expectation_ids):
        raise AssertionError("focused context omitted the normative baseline")


def run(*, repetitions: int, warmup: int, unrelated_relations: int):
    if not 2 <= repetitions <= 1_000 or not 0 <= warmup <= 100:
        raise ValueError("invalid sample bounds")
    if type(unrelated_relations) is not int or not 0 <= unrelated_relations <= 4_000:
        raise ValueError("fixture exceeds bounded relation capacity")
    source = current_core_hash()
    paths = (
        "tests/performance/g6_context_benchmark.py",
        "tests/unit/test_focused_context.py",
        "tests/helpers.py",
        "uv.lock",
    )

    def harness_hash():
        return tree_hash([(name, (ROOT / name).read_bytes()) for name in paths])

    harness = harness_hash()
    session, candidate, requirement = workload(unrelated_relations)
    legacy_policy = ContextProjectionPolicy(
        max_characters=1_000_000,
        max_candidates=500,
        max_relations=5_000,
        max_requirements=1_000,
        max_observations=1_000,
    )
    focused_policy = FocusedContextPolicy(
        max_characters=1_000_000,
        max_utf8_bytes=4_000_000,
    )
    samples = ([], [])
    order_log = []
    legacy = focused = None
    for pair in range(warmup + repetitions):
        order = (0, 1) if pair % 2 == 0 else (1, 0)
        for arm in order:
            started = time.perf_counter_ns()
            if arm == 0:
                result = project_decision_context(
                    session,
                    policy=legacy_policy,
                    active_portfolio_candidate_ids=(candidate.candidate_id,),
                )
                legacy = result
            else:
                result = project_focused_decision_context(
                    session,
                    active_candidate_ids=(candidate.candidate_id,),
                    policy=focused_policy,
                )
                verify_focused(result, candidate, requirement)
                focused = result
            elapsed = time.perf_counter_ns() - started
            if elapsed <= 0:
                raise ValueError("nonpositive benchmark duration")
            if pair >= warmup:
                samples[arm].append(elapsed)
        if pair >= warmup:
            order_log.append(list(order))
    if legacy is None or focused is None:
        raise AssertionError("benchmark did not execute both arms")
    legacy_tokens = synthetic_token_count(legacy.content)
    focused_tokens = synthetic_token_count(focused.content)
    token_ratio = focused_tokens / legacy_tokens
    timing = paired_ratio_interval(*samples)
    timing["approval"] = "NOT_A_RELEASE_SPEED_GATE"
    memory = {}
    for arm in ("legacy", "focused"):
        tracemalloc.start()
        try:
            memory_session, memory_candidate, memory_requirement = workload(unrelated_relations)
            if arm == "legacy":
                memory_result = project_decision_context(
                    memory_session,
                    policy=legacy_policy,
                    active_portfolio_candidate_ids=(memory_candidate.candidate_id,),
                )
            else:
                memory_result = project_focused_decision_context(
                    memory_session,
                    policy=focused_policy,
                    active_candidate_ids=(memory_candidate.candidate_id,),
                )
                verify_focused(memory_result, memory_candidate, memory_requirement)
            memory[arm] = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    if source != current_core_hash() or harness != harness_hash():
        raise RuntimeError("source or harness changed during context benchmark")
    config = {
        "repetitions": repetitions,
        "warmup": warmup,
        "legacy_policy": legacy_policy.model_dump(mode="json"),
        "focused_policy": focused_policy.model_dump(mode="json"),
    }
    return {
        "schema_version": 1,
        "source_hash": source,
        "harness_hash": harness,
        "source_integrity": "PASS",
        "fixture_hash": sha256_text(canonical_json(session.model_dump(mode="json"))),
        "config": config,
        "config_hash": sha256_text(canonical_json(config)),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "fixture": {
            "name": "focused-active-candidate-with-unrelated-global-relations",
            "unrelated_relations": unrelated_relations,
            "active_candidates": 1,
            "required_bundle_status": "COMPLETE",
        },
        "token_metric": {
            "counter": "synthetic_utf8_quad_estimate_not_provider_tokenizer",
            "legacy_input_tokens": legacy_tokens,
            "focused_input_tokens": focused_tokens,
            "focused_to_legacy_ratio": token_ratio,
            "reduction_fraction": 1 - token_ratio,
            "target_reduction_fraction": 0.2,
            "target_met": token_ratio <= 0.8,
        },
        "output_bytes": {
            "legacy": len(legacy.content.encode("utf-8")),
            "focused": len(focused.content.encode("utf-8")),
        },
        "timing_ns": {
            "legacy": samples_summary(samples[0]),
            "focused": samples_summary(samples[1]),
            "comparison": timing,
            "execution_order": order_log,
        },
        "correctness": {
            "complete_active_bundles": focused.complete_bundles,
            "incomplete_active_bundles": focused.incomplete_bundles,
            "required_context_omissions": 0,
            "raw_payloads_included": False,
        },
        "memory": "NOT_MEASURED_IN_TIMING_SAMPLES",
        "separate_python_peak_bytes": memory,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--unrelated-relations", type=int, default=1_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.exists():
        parser.error("refusing to overwrite an existing receipt")
    result = run(
        repetitions=args.repetitions,
        warmup=args.warmup,
        unrelated_relations=args.unrelated_relations,
    )
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
