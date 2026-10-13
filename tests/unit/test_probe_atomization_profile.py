"""Split-stage v3 golden regression tests without models or transport calls."""

import hashlib

import yaml

from redteam.app.agent_profile import SpecialistProfile, load_specialist_profile
from tests.integration.test_central_development import PROFILE

PROBES = PROFILE.with_name("native-probes.yaml")
LEGACY = PROFILE.with_name("native-probes-legacy.yaml")

# Golden constants for the frozen single-stage v3 compat profile. The working tree
# may carry CRLF while CI/Linux checkouts normalize to LF, and the runtime
# fingerprint intentionally hashes raw prompt bytes -- so the yaml golden is pinned
# newline-canonicalized while both platform fingerprint variants are pinned. The LF
# yaml bytes reproduce the committed blob b640bbe7f9f821606fcc94a7c7bf3b2f0b07408c
# (HEAD:config/agents/native-probes.yaml).
LEGACY_YAML_LF_SHA256 = "55c5a084ed59538a15dff416dc347e0d2ea04a6495cc5e84c56440df5377139e"
LEGACY_FINGERPRINT_CRLF = "742729fc847d7ce57f79ea0f03d774e4fb5b43b632da4b81460c3cf410eaf581"
LEGACY_FINGERPRINT_LF = "6ce76f856a100190b29e80c2d2e05542d47f6114bc248f2fa1fb67cae21a9190"


def test_legacy_golden_fingerprint_and_bytes():
    canonical = LEGACY.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(canonical).hexdigest() == LEGACY_YAML_LF_SHA256
    loaded = load_specialist_profile(LEGACY)
    assert loaded.fingerprint in {LEGACY_FINGERPRINT_CRLF, LEGACY_FINGERPRINT_LF}
    assert loaded.run_policy().probe_atomization is False
    assert loaded.run_policy().fingerprint == loaded.fingerprint


def test_split_profile_fingerprint_roundtrip():
    first = load_specialist_profile(PROBES)
    assert first.run_policy().probe_atomization is True
    assert load_specialist_profile(PROBES).fingerprint == first.fingerprint
    assert SpecialistProfile.model_validate(
        first.profile.model_dump(mode="json"),
    ).model_dump(mode="json") == first.profile.model_dump(mode="json")


def test_split_profile_accepts_arbitrary_stage_bound_role_ids():
    raw = yaml.safe_load(PROBES.read_text())
    renames = {
        raw["stages"]["hypothesis_planning"]: "custom-strategy-planner",
        raw["stages"]["task_atomization"]: "custom-probe-atomizer",
    }
    for role in raw["roles"]:
        if role["definition"]["agent_id"] in renames:
            role["definition"]["agent_id"] = renames[role["definition"]["agent_id"]]
    for stage, identity in raw["stages"].items():
        if identity in renames:
            raw["stages"][stage] = renames[identity]
    profile = SpecialistProfile.model_validate(raw)
    assert profile.stages["hypothesis_planning"] == "custom-strategy-planner"
    assert profile.stages["task_atomization"] == "custom-probe-atomizer"
