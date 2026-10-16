"""Web settings: write-only key, validated candidate, saved non-secret choices."""

from __future__ import annotations

import json

import pytest

from redteam.app.web.settings import (
    ENV_CYBER,
    ENV_EFFORT,
    ENV_KEY_VAR,
    ENV_MODEL,
    ENV_POLICY,
    ENV_PROFILE,
    ENV_PROVIDER,
    REMOVED_MAX_RETRIES_ENV,
    SettingsError,
    SettingsStore,
)

SECRET = "sk-test-secret-value-1234567890"


@pytest.fixture
def store(tmp_path):
    profiles = tmp_path / "config" / "agents"
    profiles.mkdir(parents=True)
    (profiles / "alpha.yaml").write_text("profile_id: alpha-profile\n", encoding="utf-8")
    (profiles / "beta.yaml").write_text("profile_id: beta-profile\n", encoding="utf-8")
    (profiles / "broken.yaml").write_text("a: [unclosed\n", encoding="utf-8")
    env: dict[str, str] = {}
    return SettingsStore(
        env=env,
        state_path=tmp_path / "data" / "web" / "settings.json",
        profile_dir=profiles,
        env_path=tmp_path / ".env",
    )


def complete(store):
    return store.apply(
        {
            "model": "provider/model-1",
            "api_key": SECRET,
            "profile_id": "alpha",
            "max_requests": 50,
            "max_concurrent_requests": 2,
        }
    )


def test_missing_fields_are_listed_by_name(store):
    fields = {item["field"] for item in store.problems()}
    assert fields == {"provider", "model", "api_key", "profile_id", "llm_request_policy"}
    assert store.summary()["ready"] is False


def test_repository_web_settings_select_candidate_profile_by_default(tmp_path):
    from pathlib import Path

    env = {
        ENV_PROVIDER: "litellm",
        ENV_MODEL: "chatgpt/test-model",
        ENV_POLICY: '{"mode":"bounded","max_requests":5,"max_concurrent_requests":1}',
    }
    current = SettingsStore(
        env=env,
        state_path=tmp_path / "settings.json",
        profile_dir=Path(__file__).parents[2] / "config" / "agents",
        env_path=tmp_path / ".env",
    )
    assert current.problems() == []
    assert current.summary()["profile_id"] == "native-probes"
    assert ENV_PROFILE not in env


def test_profiles_list_ids_and_labels_and_skip_broken_files(store):
    assert store.profiles() == [
        {"id": "alpha", "label": "alpha-profile (alpha)"},
        {"id": "beta", "label": "beta-profile (beta)"},
    ]


def test_complete_apply_makes_settings_ready_and_sets_process_environment(store):
    result = complete(store)
    assert store.problems() == []
    assert store.env[ENV_PROVIDER] == "litellm"
    assert store.env[ENV_MODEL] == "provider/model-1"
    assert json.loads(store.env[ENV_POLICY]) == {
        "mode": "bounded", "max_requests": 50, "max_concurrent_requests": 2,
    }
    assert result["key_persisted"] is False
    assert not store.env_path.exists()


def test_summary_never_contains_the_key_or_a_profile_path(store):
    complete(store)
    text = json.dumps(store.summary())
    assert SECRET not in text
    assert "config/agents" not in text and str(store.profile_dir) not in text
    summary = store.summary()
    assert summary["api_key_set"] is True
    assert summary["profile_id"] == "alpha"
    assert summary["max_requests"] == 50


def test_saved_state_file_has_no_secret_and_is_private(store):
    complete(store)
    saved = store.state_path.read_text(encoding="utf-8")
    assert SECRET not in saved
    assert json.loads(saved)["profile_id"] == "alpha"
    assert (store.state_path.stat().st_mode & 0o777) == 0o600


def test_key_reaches_env_file_only_when_explicitly_persisted(store):
    store.apply({"api_key": SECRET, "persist_key": True})
    assert f"LLM_API_KEY={SECRET}" in store.env_path.read_text(encoding="utf-8")


def test_persist_without_a_key_is_rejected(store):
    with pytest.raises(SettingsError) as caught:
        store.apply({"persist_key": True})
    assert (caught.value.field, caught.value.code) == ("persist_key", "requires_api_key")


@pytest.mark.parametrize("profile", ["../alpha", "/etc/passwd", "alpha.yaml", "missing", 7])
def test_profile_must_be_one_of_the_listed_ids(store, profile):
    with pytest.raises(SettingsError) as caught:
        store.apply({"profile_id": profile})
    assert caught.value.field == "profile_id"
    assert ENV_PROFILE not in store.env


@pytest.mark.parametrize(
    ("payload", "field", "code"),
    [
        ({"model": ""}, "model", "invalid"),
        ({"model": "has space"}, "model", "invalid"),
        ({"model": "x" * 300}, "model", "invalid"),
        ({"api_key": "has space"}, "api_key", "invalid"),
        ({"api_key": ""}, "api_key", "invalid"),
        ({"max_requests": 0, "max_concurrent_requests": 1}, "max_requests", "out_of_range"),
        ({"max_requests": True, "max_concurrent_requests": 1}, "max_requests", "invalid_type"),
        ({"max_requests": 5, "max_concurrent_requests": 999}, "max_concurrent_requests",
         "out_of_range"),
        ({"max_requests": 5}, "max_concurrent_requests", "missing"),
        ({"surprise": 1}, "body", "unknown_field"),
    ],
)
def test_invalid_input_is_rejected_by_field_and_code_without_changes(store, payload, field, code):
    with pytest.raises(SettingsError) as caught:
        store.apply(payload)
    assert (caught.value.field, caught.value.code) == (field, code)
    assert store.env == {}
    assert not store.state_path.exists()


def test_error_text_never_contains_submitted_values(store):
    with pytest.raises(SettingsError) as caught:
        store.apply({"api_key": "bad key " + SECRET})
    assert SECRET not in str(caught.value)


def test_partial_update_keeps_existing_values(store):
    complete(store)
    store.apply({"max_requests": 80})
    policy = json.loads(store.env[ENV_POLICY])
    assert policy["max_requests"] == 80 and policy["max_concurrent_requests"] == 2
    assert store.env[ENV_MODEL] == "provider/model-1"


def test_chatgpt_models_do_not_require_an_api_key(store):
    store.apply({"model": "chatgpt/some-model", "profile_id": "alpha",
                 "max_requests": 5, "max_concurrent_requests": 1})
    assert store.problems() == []


def test_removed_retry_environment_setting_has_an_actionable_problem(store):
    complete(store)
    store.env[REMOVED_MAX_RETRIES_ENV] = "3"

    assert store.problems() == [{"field": "max_retries", "code": "removed"}]
    with pytest.raises(SettingsError) as caught:
        store.apply({"max_requests": 10})
    assert (caught.value.field, caught.value.code) == ("max_retries", "removed")


def test_apply_reports_values_it_overrode_from_the_environment(store):
    store.env[ENV_MODEL] = "old/model"
    result = store.apply({"model": "new/model"})
    assert result["overrode_environment"] == [ENV_MODEL]


def test_saved_choices_fill_only_unset_environment_keys(store):
    complete(store)
    fresh = SettingsStore(
        env={ENV_MODEL: "shell/wins"},
        state_path=store.state_path,
        profile_dir=store.profile_dir,
        env_path=store.env_path,
    )
    fresh.load_saved()
    assert fresh.env[ENV_MODEL] == "shell/wins"
    assert fresh.env[ENV_PROFILE].endswith("alpha.yaml")
    assert json.loads(fresh.env[ENV_POLICY])["max_requests"] == 50


def test_saved_profile_that_no_longer_exists_is_ignored(store, tmp_path):
    complete(store)
    (store.profile_dir / "alpha.yaml").unlink()
    fresh = SettingsStore(
        env={}, state_path=store.state_path, profile_dir=store.profile_dir,
        env_path=store.env_path,
    )
    fresh.load_saved()
    assert ENV_PROFILE not in fresh.env


def test_storage_failure_leaves_the_process_environment_unchanged(store, tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    store.state_path = blocker / "settings.json"
    with pytest.raises(SettingsError) as caught:
        complete(store)
    assert (caught.value.field, caught.value.code) == ("storage", "write_failed")
    assert store.env == {}


def test_model_only_apply_preserves_litellm_default(store):
    store.apply({"model": "provider/model-1"})
    assert store.env[ENV_PROVIDER] == "litellm"
    assert store.summary()["provider"] == "litellm"


def test_model_only_apply_preserves_existing_opencode_provider(store):
    store.env[ENV_PROVIDER] = "opencode-go"
    store.apply({"model": "openai/gpt-x"})
    assert store.env[ENV_PROVIDER] == "opencode-go"


def test_opencode_go_selectable_with_effort(store):
    store.apply(
        {
            "provider": "opencode-go",
            "model": "openai/gpt-x",
            "api_key": SECRET,
            "reasoning_effort": "high",
            "profile_id": "alpha",
            "max_requests": 50,
            "max_concurrent_requests": 2,
        }
    )
    assert store.env[ENV_PROVIDER] == "opencode-go"
    assert store.env[ENV_EFFORT] == "high"
    assert store.summary()["provider"] == "opencode-go"
    assert store.summary()["reasoning_effort"] == "high"
    assert store.problems() == []


def test_unknown_provider_is_rejected_atomically(store):
    with pytest.raises(SettingsError) as caught:
        store.apply({"provider": "openai"})
    assert (caught.value.field, caught.value.code) == ("provider", "unsupported")
    assert store.env == {}
    assert not store.state_path.exists()


@pytest.mark.parametrize("effort", ["turbo", "ultra", 123, True, ["high"]])
def test_invalid_effort_is_rejected_atomically(store, effort):
    with pytest.raises(SettingsError) as caught:
        store.apply({"reasoning_effort": effort})
    assert (caught.value.field, caught.value.code) == ("reasoning_effort", "invalid")
    assert store.env == {}
    assert not store.state_path.exists()


@pytest.mark.parametrize("program", ["blue", "red", 123, True, ["daybreak_blue"]])
def test_invalid_cyber_program_is_rejected_atomically(store, program):
    with pytest.raises(SettingsError) as caught:
        store.apply({"cyber_access_program": program})
    assert (caught.value.field, caught.value.code) == ("cyber_access_program", "invalid")
    assert store.env == {}
    assert not store.state_path.exists()


def test_cyber_program_is_saved_reloaded_and_cleared(store):
    complete(store)
    store.apply({"cyber_access_program": "DAYBREAK_BLUE"})
    assert store.env[ENV_CYBER] == "daybreak_blue"
    assert store.summary()["cyber_access_program"] == "daybreak_blue"
    fresh = SettingsStore(
        env={}, state_path=store.state_path, profile_dir=store.profile_dir,
        env_path=store.env_path,
    )
    fresh.load_saved()
    assert fresh.env[ENV_CYBER] == "daybreak_blue"
    fresh.apply({"cyber_access_program": ""})
    assert ENV_CYBER not in fresh.env
    assert "cyber_access_program" not in json.loads(
        fresh.state_path.read_text(encoding="utf-8")
    )


def test_cyber_program_rejects_opencode_provider(store):
    with pytest.raises(SettingsError) as caught:
        store.apply({
            "provider": "opencode-go",
            "model": "openai/gpt-x",
            "cyber_access_program": "daybreak_blue",
        })
    assert (caught.value.field, caught.value.code) == (
        "cyber_access_program", "unsupported"
    )


@pytest.mark.parametrize("clear", ["", "   ", None])
def test_effort_clearing_removes_env_and_saved_key(store, clear):
    store.apply(
        {
            "provider": "opencode-go",
            "model": "openai/gpt-x",
            "api_key": SECRET,
            "reasoning_effort": "medium",
            "profile_id": "alpha",
            "max_requests": 50,
            "max_concurrent_requests": 2,
        }
    )
    assert store.env[ENV_EFFORT] == "medium"
    store.apply({"reasoning_effort": clear})
    assert ENV_EFFORT not in store.env
    assert store.summary()["reasoning_effort"] is None
    assert "reasoning_effort" not in json.loads(store.state_path.read_text(encoding="utf-8"))
    assert store.env[ENV_PROVIDER] == "opencode-go"


def test_effort_reload_reflects_persisted_value(store):
    store.apply(
        {
            "provider": "opencode-go",
            "model": "openai/gpt-x",
            "api_key": SECRET,
            "reasoning_effort": "xhigh",
            "profile_id": "alpha",
            "max_requests": 50,
            "max_concurrent_requests": 2,
        }
    )
    fresh = SettingsStore(
        env={},
        state_path=store.state_path,
        profile_dir=store.profile_dir,
        env_path=store.env_path,
    )
    fresh.load_saved()
    assert fresh.env[ENV_PROVIDER] == "opencode-go"
    assert fresh.env[ENV_EFFORT] == "xhigh"
    assert fresh.summary()["reasoning_effort"] == "xhigh"
    assert fresh.env[ENV_KEY_VAR] == "OPENCODE_API_KEY"
    assert fresh.summary()["api_key_env_var"] == "OPENCODE_API_KEY"


def test_effort_value_is_normalized(store):
    store.apply({"reasoning_effort": "HIGH"})
    assert store.env[ENV_EFFORT] == "high"
    assert store.summary()["reasoning_effort"] == "high"


def test_invalid_persisted_effort_does_not_break_reload(store):
    complete(store)
    saved = json.loads(store.state_path.read_text(encoding="utf-8"))
    saved["reasoning_effort"] = "turbo"
    store.state_path.write_text(json.dumps(saved), encoding="utf-8")
    fresh = SettingsStore(
        env={},
        state_path=store.state_path,
        profile_dir=store.profile_dir,
        env_path=store.env_path,
    )
    fresh.load_saved()
    assert ENV_EFFORT not in fresh.env


def test_summary_reports_unset_effort_as_none(store):
    complete(store)
    assert store.summary()["reasoning_effort"] is None


def test_profiles_disambiguate_when_id_differs_from_filename(store, tmp_path):
    (store.profile_dir / "same.yaml").write_text("profile_id: same\n", encoding="utf-8")
    (store.profile_dir / "legacy.yaml").write_text("profile_id: same\n", encoding="utf-8")
    by_id = {item["id"]: item["label"] for item in store.profiles()}
    assert by_id["same"] == "same"
    assert by_id["legacy"] == "same (legacy)"
    assert by_id["alpha"] == "alpha-profile (alpha)"


def test_summary_exposes_effort_options_from_core(store):
    from redteam.core.config import REASONING_EFFORTS

    assert store.summary()["reasoning_effort_options"] == sorted(REASONING_EFFORTS)
    assert set(store.summary()["reasoning_effort_options"]) == {
        "none", "minimal", "low", "medium", "high", "xhigh", "max",
    }


def test_go_save_resolves_opencode_key_default(store):
    from redteam.core.config import LLMConfig
    from redteam.llm.opencode_go_config import OPENCODE_GO_API_KEY_ENV_VAR

    store.apply(
        {
            "provider": "opencode-go",
            "model": "openai/gpt-x",
            "api_key": SECRET,
            "profile_id": "alpha",
            "max_requests": 50,
            "max_concurrent_requests": 2,
        }
    )
    assert store.env[ENV_PROVIDER] == "opencode-go"
    assert store.env[OPENCODE_GO_API_KEY_ENV_VAR] == SECRET
    assert "LLM_API_KEY" not in store.env
    summary = store.summary()
    assert summary["api_key_env_var"] == OPENCODE_GO_API_KEY_ENV_VAR
    assert summary["api_key_set"] is True
    assert store.problems() == []

    consumer = LLMConfig.from_environment(store.env)
    assert consumer.api_key_env_var == OPENCODE_GO_API_KEY_ENV_VAR
    assert consumer.api_key_env_var == summary["api_key_env_var"]
    assert store.env[consumer.api_key_env_var] == SECRET


def test_go_save_factory_receives_opencode_key_reference(store, monkeypatch):
    import redteam.app.bootstrap as bootstrap
    from redteam.core.config import LLMConfig
    from redteam.llm.opencode_go_config import OPENCODE_GO_API_KEY_ENV_VAR

    store.apply(
        {
            "provider": "opencode-go",
            "model": "openai/gpt-x",
            "api_key": SECRET,
            "profile_id": "alpha",
            "max_requests": 50,
            "max_concurrent_requests": 2,
        }
    )
    seen = {}

    def fake_factory(llm_config):
        seen["api_key_env_var"] = llm_config.api_key_env_var
        return object()

    monkeypatch.setattr(bootstrap, "build_llm_client", fake_factory)
    bootstrap.build_llm_client(LLMConfig.from_environment(store.env))
    assert seen["api_key_env_var"] == OPENCODE_GO_API_KEY_ENV_VAR


def test_go_to_litellm_switch_drops_generated_go_reference(store):
    import json as _json

    from redteam.core.config import LLMConfig

    store.apply(
        {
            "provider": "opencode-go",
            "model": "openai/gpt-x",
            "api_key": SECRET,
            "profile_id": "alpha",
            "max_requests": 50,
            "max_concurrent_requests": 2,
        }
    )
    store.apply({"provider": "litellm", "model": "provider/model-1"})
    assert store.env[ENV_KEY_VAR] == "LLM_API_KEY"
    saved = _json.loads(store.state_path.read_text(encoding="utf-8"))
    assert "api_key_env_var" not in saved
    consumer = LLMConfig.from_environment(store.env)
    assert consumer.api_key_env_var == "LLM_API_KEY"
    assert consumer.api_key_env_var == store.summary()["api_key_env_var"]


def test_explicit_key_reference_survives_go_to_litellm_switch(store):
    explicit = SettingsStore(
        env={ENV_KEY_VAR: "CUSTOM_KEY_VAR"},
        state_path=store.state_path,
        profile_dir=store.profile_dir,
        env_path=store.env_path,
    )
    explicit.apply(
        {
            "provider": "opencode-go",
            "model": "openai/gpt-x",
            "api_key": SECRET,
            "profile_id": "alpha",
            "max_requests": 50,
            "max_concurrent_requests": 2,
        }
    )
    assert explicit.env["CUSTOM_KEY_VAR"] == SECRET
    explicit.apply({"provider": "litellm", "model": "provider/model-1"})
    assert explicit.env[ENV_KEY_VAR] == "CUSTOM_KEY_VAR"
    assert explicit.summary()["api_key_env_var"] == "CUSTOM_KEY_VAR"


def test_explicit_default_matching_ref_retained_on_litellm_to_go_switch(store):
    import json as _json

    from redteam.core.config import LLMConfig

    explicit = SettingsStore(
        env={ENV_KEY_VAR: "LLM_API_KEY"},
        state_path=store.state_path,
        profile_dir=store.profile_dir,
        env_path=store.env_path,
    )
    explicit.apply(
        {
            "model": "provider/model-1",
            "api_key": SECRET,
            "profile_id": "alpha",
            "max_requests": 50,
            "max_concurrent_requests": 2,
        }
    )
    assert explicit.env[ENV_KEY_VAR] == "LLM_API_KEY"
    explicit.apply({"provider": "opencode-go", "model": "openai/gpt-x"})
    assert explicit.env[ENV_KEY_VAR] == "LLM_API_KEY"
    saved = _json.loads(store.state_path.read_text(encoding="utf-8"))
    assert saved.get("api_key_env_var") == "LLM_API_KEY"
    consumer = LLMConfig.from_environment(explicit.env)
    assert consumer.api_key_env_var == "LLM_API_KEY"
    assert consumer.api_key_env_var == explicit.summary()["api_key_env_var"]


def test_go_save_preserves_explicit_key_var(store):
    explicit = SettingsStore(
        env={ENV_KEY_VAR: "CUSTOM_KEY_VAR"},
        state_path=store.state_path,
        profile_dir=store.profile_dir,
        env_path=store.env_path,
    )
    explicit.apply(
        {
            "provider": "opencode-go",
            "model": "anthropic/model-y",
            "api_key": SECRET,
            "profile_id": "alpha",
            "max_requests": 50,
            "max_concurrent_requests": 2,
        }
    )
    assert explicit.env["CUSTOM_KEY_VAR"] == SECRET
    assert explicit.summary()["api_key_env_var"] == "CUSTOM_KEY_VAR"
    assert explicit.problems() == []


def test_candidate_provider_key_uses_candidate_not_old_provider(store):
    from redteam.llm.opencode_go_config import OPENCODE_GO_API_KEY_ENV_VAR

    store.apply({"model": "provider/model-1", "api_key": SECRET})
    assert store.env["LLM_API_KEY"] == SECRET
    store.apply({"provider": "opencode-go", "model": "openai/gpt-x", "api_key": SECRET})
    assert store.env[OPENCODE_GO_API_KEY_ENV_VAR] == SECRET
    assert store.summary()["api_key_env_var"] == OPENCODE_GO_API_KEY_ENV_VAR


@pytest.mark.parametrize("model", ["bad-model", "openai/", "openai/a/b", "gpt-4o-mini", ""])
def test_malformed_go_model_rejected_atomically(store, model):
    with pytest.raises(SettingsError) as caught:
        store.apply({"provider": "opencode-go", "model": model})
    assert (caught.value.field, caught.value.code) == ("model", "invalid")
    assert store.env == {}
    assert not store.state_path.exists()


def test_malformed_go_model_surfaces_problem_entry(store):
    store.env[ENV_PROVIDER] = "opencode-go"
    store.env[ENV_MODEL] = "not-a-qualified-model"
    assert {"field": "model", "code": "invalid"} in store.problems()
    assert store.summary()["ready"] is False


def test_switch_to_go_with_incompatible_existing_model_rejected(store):
    store.env[ENV_MODEL] = "provider/model-1"
    with pytest.raises(SettingsError) as caught:
        store.apply({"provider": "opencode-go"})
    assert (caught.value.field, caught.value.code) == ("model", "invalid")
    assert store.env[ENV_MODEL] == "provider/model-1"
    assert ENV_PROVIDER not in store.env


def test_litellm_model_shape_not_go_validated(store):
    store.apply({"model": "provider/model-1", "api_key": SECRET})
    assert store.env[ENV_PROVIDER] == "litellm"
    assert store.env[ENV_MODEL] == "provider/model-1"


def test_go_first_startup_publishes_derived_key_reference(store):
    from redteam.core.config import LLMConfig

    initial = SettingsStore(
        env={ENV_PROVIDER: "opencode-go", ENV_MODEL: "openai/gpt-x", "OPENCODE_API_KEY": SECRET},
        state_path=store.state_path, profile_dir=store.profile_dir, env_path=store.env_path,
    )
    initial.load_saved()
    assert LLMConfig.from_environment(initial.env).api_key_env_var == "OPENCODE_API_KEY"
    initial.apply({"provider": "litellm", "model": "provider/model-1"})
    assert LLMConfig.from_environment(initial.env).api_key_env_var == "LLM_API_KEY"
