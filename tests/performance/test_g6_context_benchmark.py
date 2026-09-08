from tests.performance.g6_context_benchmark import run


def test_g6_benchmark_fixture_preserves_complete_bundle_and_reduces_context():
    result = run(repetitions=2, warmup=0, unrelated_relations=25)
    assert result["correctness"] == {
        "complete_active_bundles": 1,
        "incomplete_active_bundles": 0,
        "required_context_omissions": 0,
        "raw_payloads_included": False,
    }
    assert result["token_metric"]["target_met"] is True
    assert result["output_bytes"]["focused"] < result["output_bytes"]["legacy"]
