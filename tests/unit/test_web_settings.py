"""Web settings: write-only key, validated candidate, saved non-secret choices."""

from __future__ import annotations

import json

import pytest

from redteam.app.web.settings import (
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
        {"id": "alpha", "label": "alpha-profile"},
        {"id": "beta", "label": "beta-profile"},
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
