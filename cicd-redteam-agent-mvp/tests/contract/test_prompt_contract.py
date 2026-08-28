from pathlib import Path

PROMPT = (
    Path(__file__).parents[2]
    / "src"
    / "cicd_redteam_agent"
    / "prompts"
    / "cicd_security.md"
)


def test_security_prompt_keeps_required_workflow_and_grounding_contracts() -> None:
    text = PROMPT.read_text(encoding="utf-8")

    headings = [
        "## 1. Role and trust boundary",
        "## 2. Required capability order",
        "## 3. Attack-path construction rules",
        "## 4. Status decision table",
        "## 5. SubmitAnalysis output contract",
    ]
    assert all(heading in text for heading in headings)
    assert text.index("`ListWorkflows`") < text.index("`ListAttackFrontiers`")
    assert text.index("`ListAttackFrontiers`") < text.index("`ListSecurityFacts`")
    assert text.index("`ListSecurityFacts`") < text.index("`SubmitAnalysis`")
    assert '"schema_version": "1.0"' in text
    assert '"paths": []' in text
    assert "never `attack_paths`" in text


def test_security_prompt_keeps_static_verdict_safety_rules() -> None:
    text = PROMPT.read_text(encoding="utf-8")

    assert "relation_id" in text
    assert "missing_evidence" in text
    assert "Any relation is `FALSE`" in text
    assert "Never `CONFIRMED`" in text
    assert "Do not duplicate an existing Finding, path, or identity" in text
    assert "STRUCTURED_GOAL_HINTS" in text
    assert "never Evidence" in text
    assert "investigation hint" in text
