import re
from pathlib import Path


def test_domain_layer_does_not_import_framework() -> None:
    domain = Path(__file__).parents[2] / "src" / "cicd_redteam_agent" / "domain"
    for source in domain.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert not re.search(r"^(?:from|import)\s+hackingBuddyGPT", text, re.MULTILINE)
        assert not re.search(
            r"^(?:from|import)\s+cicd_redteam_agent\.framework", text, re.MULTILINE
        )
