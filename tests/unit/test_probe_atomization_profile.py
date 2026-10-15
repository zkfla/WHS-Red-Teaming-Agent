"""Split-stage v3 golden regression tests without models or transport calls."""

import hashlib

import yaml

from redteam.app.agent_profile import SpecialistProfile, load_specialist_profile
from tests.integration.test_central_development import PROFILE

PROBES = PROFILE.with_name("native-probes.yaml")
LEGACY = PROFILE.with_name("native-probes-legacy.yaml")

# Golden constants for the version-16 single-stage v3 compatibility profile,
# intentionally versioned for bounded planner batch lookups and retained read windows.
# This does not assert compatibility with older prompt/profile versions;
# integration tests separately require matching selection fingerprints on resume.
# Runtime fingerprints hash raw prompt bytes, so both LF and CRLF prompt variants
# are pinned while the YAML checksum is newline-canonicalized.
LEGACY_YAML_LF_SHA256 = "b2494e2d69a754c6cd3b35a529d8fc4d96cc9bff3048cdbe7a76bf62d63b0247"
LEGACY_FINGERPRINT_CRLF = "604bfcaa4981f4097dad54451dda7c9f5cef4da1f002ab1058410cf71c5492c1"
LEGACY_FINGERPRINT_LF = "6bc51d0b42281ebe2a016fa1ffe6d6c01ad422b8aac6943b9677d9ef8b8e5834"


def test_legacy_golden_fingerprint_and_bytes():
    canonical = LEGACY.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(canonical).hexdigest() == LEGACY_YAML_LF_SHA256
    loaded = load_specialist_profile(LEGACY)
    assert loaded.profile.version == "16"
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
