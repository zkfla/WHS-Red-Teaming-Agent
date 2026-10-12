"""Actual local stdio initialization/discovery only: no LLM or target calls."""

from redteam.app.web.host import RealLauncher
from redteam.capability.bootstrap import open_mcp_tool_gateway
from redteam.core.environment import env_snapshot, environment_context
from tests.unit.test_web_policy import save


async def test_web_generated_policy_starts_the_real_builtin_mcp_servers(tmp_path):
    store = save(tmp_path / "lab.yaml")
    environment = {
        "REDTEAM_LLM_PROVIDER": "litellm",
        "REDTEAM_LLM_MODEL": "fixture/not-called",
        "REDTEAM_SPECIALIST_PROFILE": "config/agents/native-specialists-web.yaml",
        "REDTEAM_LLM_REQUEST_POLICY_JSON":
            '{"mode":"bounded","max_requests":5,"max_concurrent_requests":1}',
    }
    original = env_snapshot()
    launcher = RealLauncher(store.path)
    with environment_context(environment):
        await launcher.prepare()
    config = launcher.build_config()
    assert config.capability.mcp_servers == ["web-http", "recon-passive"]
    assert config.engagement.scope.allowed_hosts == ["127.0.0.1:8443"]
    assert config.engagement.metadata["web_policy"]["mode"] == "site"
    async with open_mcp_tool_gateway(
        config.capability.mcp_servers, env=launcher.environment, health_refresh_enabled=False,
    ) as gateway:
        descriptors = gateway.all_descriptors()
        assert {tool.server_id for tool in descriptors} == {"web-http", "recon-passive"}
        assert {tool.tool_name for tool in descriptors} >= {"http_request", "http_probe"}
    assert env_snapshot() == original
