"""Tests do not provision host Docker implicitly when composing a CLI run."""

import pytest


@pytest.fixture(autouse=True)
def manual_shell_setup_for_tests(monkeypatch):
    # Lifecycle tests explicitly opt in and replace process boundaries. Existing
    # graph/CLI fixtures use manual policies and must stay independent of Docker.
    monkeypatch.setenv("REDTEAM_SHELL_AUTO_SETUP", "false")
