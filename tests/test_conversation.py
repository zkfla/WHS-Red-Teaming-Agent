import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, "src")

from redteaming.llm.conversation import Conversation


class ConversationTests(unittest.TestCase):
    def test_scope_is_required_before_free_form_planning(self):
        conversation = Conversation()
        self.assertIn("먼저", conversation.reply("포트 정찰해줘"))

    def test_scope_and_status_are_local_and_do_not_require_llm(self):
        conversation = Conversation()
        self.assertIn("허용 Scope", conversation.reply("/scope scan.example.test"))
        self.assertIn("scan.example.test", conversation.reply("/status"))

    def test_natural_language_scope_registration(self):
        conversation = Conversation()
        response = conversation.reply("127.0.0.1:8000을 승인된 대상으로 등록해줘")
        self.assertIn("127.0.0.1:8000", response)
        self.assertEqual(conversation.scope.allowed_hosts, ("127.0.0.1:8000",))

    def test_korean_natural_scope_registration(self):
        conversation = Conversation()
        response = conversation.reply("http://127.0.0.1:8000을 승인된 테스트 대상으로 등록해줘")
        self.assertIn("127.0.0.1:8000", response)
        self.assertEqual(conversation.scope.allowed_hosts, ("http://127.0.0.1:8000",))

    def test_korean_natural_scope_addition(self):
        conversation = Conversation()
        conversation.reply("/scope http://127.0.0.1:8000")
        conversation.reply("http://127.0.0.1:3300도 다른 승인 서버로 추가해줘")
        self.assertEqual(len(conversation.scope.allowed_hosts), 2)

    def test_natural_language_external_scope_registration(self):
        conversation = Conversation()
        response = conversation.reply("승인된 테스트베드 staging.example.com을 대상으로 등록해줘")
        self.assertIn("staging.example.com", response)

    def test_natural_language_scope_supports_ipv6_and_url_paths(self):
        conversation = Conversation()
        response = conversation.reply("https://[::1]:8443/api/health을 승인된 대상으로 등록해줘")
        self.assertIn("https://[::1]:8443/api/health", response)
        self.assertEqual(conversation.scope.allowed_hosts, ("https://[::1]:8443/api/health",))

    def test_markdown_scope_link_is_normalized(self):
        conversation = Conversation()
        response = conversation.reply("/scope [http://127.0.0.1:8000](http://127.0.0.1:8000)")
        self.assertIn("http://127.0.0.1:8000", response)
        self.assertEqual(conversation.scope.allowed_hosts, ("http://127.0.0.1:8000",))

    def test_scope_add_preserves_existing_authorized_target(self):
        conversation = Conversation()
        conversation.reply("/scope http://127.0.0.1:8000")
        response = conversation.reply("/scope-add http://127.0.0.1:3300")
        self.assertIn("추가", response)
        self.assertEqual(
            conversation.scope.allowed_hosts,
            ("http://127.0.0.1:8000", "http://127.0.0.1:3300"),
        )

    def test_natural_add_target_merges_scope(self):
        conversation = Conversation()
        conversation.reply("/scope http://127.0.0.1:8000")
        response = conversation.reply("also analyze http://127.0.0.1:3300")
        self.assertIn("127.0.0.1:3300", response)
        self.assertEqual(len(conversation.scope.allowed_hosts), 2)

    def test_natural_url_and_recon_request_registers_scope(self):
        conversation = Conversation()
        response = conversation.reply("http://127.0.0.1:8000을 대상으로 초기 정찰 실행해줘")
        self.assertIn("127.0.0.1:8000", response)
        self.assertEqual(conversation.scope.allowed_hosts, ("http://127.0.0.1:8000",))

    def test_model_selection_is_explicit_when_client_is_available(self):
        class FakeClient:
            def __init__(self):
                self.config = type("Config", (), {"model": "old/model"})()

            def set_model(self, model):
                self.config.model = model

            def chat(self, messages):
                return "ok"

        conversation = Conversation(client=FakeClient())
        self.assertIn("provider/model", conversation.reply("/model"))
        self.assertIn("new/model", conversation.reply("/model new/model"))
        self.assertIn("new/model", conversation.reply("/model"))

    def test_natural_language_oauth_request_does_not_require_scope(self):
        conversation = Conversation()
        with patch.dict("os.environ", {"REDTEAM_LLM_BACKEND": "litellm"}, clear=True):
            response = conversation.reply("OAuth로 연결해줘")
        self.assertIn("API key를 입력하지 않아도", response)

    def test_natural_language_litellm_request_does_not_require_scope(self):
        conversation = Conversation()
        with patch.dict("os.environ", {"REDTEAM_LLM_BACKEND": "litellm",
                                        "REDTEAM_LLM_MODEL": "chatgpt/gpt-test"}, clear=True):
            response = conversation.reply("LiteLLM으로 연결해줘")
        self.assertIn("LiteLLM 연결", response)

    def test_natural_language_intent_shapes_cover_paraphrases(self):
        from redteaming.cli import _is_assets_request, _is_authorization_request, _is_recon_request, _is_report_request

        self.assertTrue(_is_recon_request("서버에서 어떤 서비스가 열려 있는지 확인해줘"))
        self.assertTrue(_is_assets_request("발견된 서비스 목록을 보여줘"))
        self.assertTrue(_is_authorization_request("두 사용자 계정의 접근 권한을 비교해서 확인해줘"))
        self.assertTrue(_is_report_request("분석 결과를 문서로 정리해줘"))

    def test_scope_registration_does_not_match_recon_intent(self):
        from redteaming.cli import (_is_explicit_recon_request, _is_recon_request,
                                    _is_scope_registration_request)

        request = "http://localhost:3000을 승인된 테스트 대상으로 등록해줘"
        self.assertTrue(_is_scope_registration_request(request))
        self.assertFalse(_is_recon_request(request))
        self.assertFalse(_is_explicit_recon_request(request))

    def test_existing_plan_is_reused_until_explicit_replan(self):
        from redteaming.cli import _is_replan_request, _resume_plan_for_request
        from redteaming.models import Scope
        from redteaming.planning import EngagementPlanner

        scope = Scope(("http://localhost:3000",))
        plan = EngagementPlanner().create("초기 정찰", scope)
        self.assertIs(_resume_plan_for_request("취약점 분석을 진행해줘", plan, scope), plan)
        self.assertFalse(_is_replan_request("취약점 분석을 진행해줘"))
        self.assertIsNone(_resume_plan_for_request("새로운 계획으로 다시 구성해줘", plan, scope))
        self.assertTrue(_is_replan_request("새로운 계획으로 다시 구성해줘"))

    def test_report_request_is_not_forced_into_recon(self):
        from redteaming.cli import _is_explicit_recon_request, _is_report_request

        request = "증거 기반 리포트를 생성해줘"
        self.assertTrue(_is_report_request(request))
        self.assertFalse(_is_explicit_recon_request(request))

    def test_litellm_status_is_local_and_does_not_call_model(self):
        class FakeClient:
            def connection_summary(self):
                return "LiteLLM 준비됨: model=test/model, credential=환경변수 기반 인증"

        conversation = Conversation(client=FakeClient())
        self.assertIn("LiteLLM 준비됨", conversation.reply("LiteLLM 연결 상태 확인해줘"))


if __name__ == "__main__":
    unittest.main()
