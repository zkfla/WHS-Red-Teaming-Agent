import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, "src")

from redteaming.llm.client import LLMConfig, LiteLLMClient, OpenAICompatibleClient


class LLMClientTests(unittest.TestCase):
    def test_litellm_model_can_be_selected_without_direct_api_key(self):
        with patch.dict(os.environ, {"LITELLM_MODEL": "local/llama3", "REDTEAM_LLM_BACKEND": "litellm"}, clear=True):
            config = LLMConfig.from_env()
        self.assertIsNotNone(config)
        self.assertEqual(config.model, "local/llama3")
        self.assertIsInstance(LiteLLMClient(config), LiteLLMClient)

    def test_chatgpt_oauth_stream_is_collected(self):
        class Delta:
            def __init__(self, content):
                self.content = content

        class Choice:
            def __init__(self, content):
                self.delta = Delta(content)

        class Chunk:
            def __init__(self, content):
                self.choices = [Choice(content)]

        config = LLMConfig("https://api.openai.com/v1", "", "chatgpt/gpt-5.5")
        client = LiteLLMClient(config)
        with patch.dict("sys.modules", {"litellm": type("LiteLLM", (), {
            "drop_params": False,
            "completion": staticmethod(lambda **kwargs: iter([Chunk("hello "), Chunk("world")]))
        })()}):
            self.assertEqual(client.chat([{"role": "user", "content": "hi"}]), "hello world")

    def test_openai_compatible_reasoning_model_omits_temperature(self):
        config = LLMConfig("https://provider.invalid/v1", "test-key", "openai/gpt-5.5")
        client = OpenAICompatibleClient(config)
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"ok"}}]}'

        def fake_urlopen(request, timeout):
            captured["payload"] = request.data
            return Response()

        with patch("redteaming.llm.client.urlopen", side_effect=fake_urlopen):
            self.assertEqual(client.chat([{"role": "user", "content": "hi"}]), "ok")
        self.assertNotIn("temperature", captured["payload"].decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
