"""Split-stage v3 golden regression tests without models or transport calls."""

import hashlib

import yaml

from redteam.app.agent_profile import SpecialistProfile, load_specialist_profile
from tests.integration.test_central_development import PROFILE

PROBES = PROFILE.with_name("native-probes.yaml")
LEGACY = PROFILE.with_name("native-probes-legacy.yaml")

# Golden constants for the version-18 single-stage v3 compatibility profile,
# intentionally versioned for the shared worker's Korean methodology and contract instructions.
# This does not assert compatibility with older prompt/profile versions;
# integration tests separately require matching selection fingerprints on resume.
# Runtime fingerprints hash raw prompt bytes, so both LF and CRLF prompt variants
# are pinned while the YAML checksum is newline-canonicalized.
LEGACY_YAML_LF_SHA256 = "f5a16d67d0e7478e04b0722760ea52e7cdff58c558939ebf2718548a797cb949"
LEGACY_FINGERPRINT_CRLF = "1bc830a178857d90ea5170353cf756d4c979e595fa0dfa97024ca89234919b3c"
LEGACY_FINGERPRINT_LF = "73b15e911cf5d06ccf02c00b150dc0851bf574aa279e33507840d3e2d121fa4a"


def test_legacy_golden_fingerprint_and_bytes():
    canonical = LEGACY.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(canonical).hexdigest() == LEGACY_YAML_LF_SHA256
    loaded = load_specialist_profile(LEGACY)
    assert loaded.profile.version == "18"
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
