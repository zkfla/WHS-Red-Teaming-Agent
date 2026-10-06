# Evaluation case format

Ground truth belongs to the testbed owner. The agent never asks the LLM to
invent labels. Store one or more cases in a JSON file and pass it to the CLI:

```json
{
  "cases": [
    {
      "name": "approved-lab-baseline",
      "expected_categories": [
        "http_security_headers",
        "server_information_disclosure"
      ]
    }
  ]
}
```

```text
/evaluate cases.json
```

The output reports category coverage, precision, verification rate, and
reproducibility rate for the Findings already produced in the current
engagement. Run the same case file against repeated runs to compare changes in
detector behavior. Library callers can additionally use
`EvaluationHarness.compare_runs(first, second)`; it compares stable
`category + affected asset` signatures rather than random Finding IDs. Do not
mix labels from different testbeds or scopes.

For repeated runs, library callers can use
`EvaluationHarness.benchmark(case, runs)`. It returns per-run coverage,
precision, verification, and reproducibility plus adjacent-run Finding
stability, so detector changes can be compared without trusting random Finding
IDs or LLM output.

Each `EvaluationResult` also records `missing_categories` (owner-expected
categories not detected) and `unexpected_categories` (detected categories not
in that case). These fields make false negatives and false positives visible
without asking the LLM to infer ground truth.
