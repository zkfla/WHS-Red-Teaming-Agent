import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, "src")

from redteaming.llm.client import LLMConfig


class OnboardingTests(unittest.TestCase):
    def test_api_key_is_supported_without_being_required(self):
        with patch.dict(
            os.environ,
            {
                "REDTEAM_LLM_BACKEND": "litellm",
                "REDTEAM_LLM_MODEL": "chatgpt/gpt-5.5",
                "REDTEAM_LLM_API_KEY": "temporary-test-key",
            },
            clear=True,
        ):
            config = LLMConfig.from_env()
        self.assertEqual(config.model, "chatgpt/gpt-5.5")
        self.assertEqual(config.api_key, "temporary-test-key")


if __name__ == "__main__":
    unittest.main()
