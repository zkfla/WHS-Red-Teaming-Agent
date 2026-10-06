import os
import sys
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "src")

from redteaming.llm.client import LLMConfig
from redteaming.doctor import run_diagnostics
from redteaming.policies import load_detector_names
from redteaming.cli import _load_local_env


class LocalConfigTests(unittest.TestCase):
    def test_local_litellm_mode_can_run_without_an_api_key(self):
        with patch.dict(os.environ, {"REDTEAM_LLM_BACKEND": "litellm", "REDTEAM_LLM_MODEL": "gpt-4o-mini"}, clear=True):
            config = LLMConfig.from_env()
        self.assertIsNotNone(config)
        self.assertEqual(config.model, "gpt-4o-mini")

    def test_doctor_is_local_and_reports_optional_components(self):
        checks = {item.name: item for item in run_diagnostics()}
        self.assertIn("python", checks)
        self.assertIn("litellm", checks)
        self.assertIn(checks["litellm"].status, {"ok", "optional"})
        self.assertIn("tool:httpx", checks)

    def test_detector_subset_is_loaded_from_operator_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps({"detectors": ["http-security-headers", "http-security-headers"]}),
                            encoding="utf-8")
            self.assertEqual(load_detector_names(path), ("http-security-headers",))

    def test_local_env_loader_is_allowlisted_and_does_not_override_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                'REDTEAM_LLM_MODEL="chatgpt/test"\n'
                'REDTEAM_LLM_REASONING_EFFORT=low\n'
                'UNSAFE_OTHER_SETTING=ignored\n', encoding="utf-8"
            )
            with patch.dict(os.environ, {"REDTEAM_LLM_MODEL": "already-set"}, clear=True):
                _load_local_env(path)
                self.assertEqual(os.environ["REDTEAM_LLM_MODEL"], "already-set")
                self.assertEqual(os.environ["REDTEAM_LLM_REASONING_EFFORT"], "low")
                self.assertNotIn("UNSAFE_OTHER_SETTING", os.environ)


if __name__ == "__main__":
    unittest.main()
