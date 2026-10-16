"""Split-stage v3 golden regression tests without models or transport calls."""

import hashlib

import yaml

from redteam.app.agent_profile import SpecialistProfile, load_specialist_profile
from tests.integration.test_central_development import PROFILE

PROBES = PROFILE.with_name("native-probes.yaml")
LEGACY = PROFILE.with_name("native-probes-legacy.yaml")

# Golden constants for the version-18 single-stage v3 compatibility profile,
# whose shared Worker prompt binds restrained execution and v2 promotion guidance.
# This does not assert compatibility with older prompt/profile versions;
# integration tests separately require matching selection fingerprints on resume.
# Runtime fingerprints hash raw prompt bytes, so both LF and CRLF prompt variants
# are pinned while the YAML checksum is newline-canonicalized.
LEGACY_YAML_LF_SHA256 = "c31b04f283f2d528528484ab3b24db864c9300a01734426e8957f7dbd565dc79"
LEGACY_FINGERPRINT_LF = "c6bdbda91a944aacb38e6cadbf5bfb6a72245e6c49bb3c0828e0081ac9fc5148"
LEGACY_FINGERPRINT_CRLF = "8be2502f6e689d34cd520dba7783ab5d7f230d6e40606a146caba8e048f17c5f"


def test_legacy_golden_fingerprint_and_bytes():
    canonical = LEGACY.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(canonical).hexdigest() == LEGACY_YAML_LF_SHA256
    loaded = load_specialist_profile(LEGACY)
    assert loaded.profile.version == "18"
    assert loaded.fingerprint in {LEGACY_FINGERPRINT_CRLF, LEGACY_FINGERPRINT_LF}
    assert loaded.run_policy().probe_atomization is False
    assert loaded.run_policy().fingerprint == loaded.fingerprint
    assert loaded.fingerprint not in {
        "18b04d45c2207efe133788f52f6c1baeec58e8ffd803b1d5b3216a89e3a275ab",
        "50126322fedf6851f1a4791e6c816aab748d256317d889a5e0e3f9f3dadd2fa7",
        "292fe25da9babd70c3e7704bc91f2843dbe0a6a69163cd27be37dc76efc30f17",
        "b176b4317993b1483666b9dcfa330d0f413a2ca616435a931b21ca0af0046ffe",
        "92dbea51ba10fd7a019a312a1cfd85dc9db4cba2bcef39dce77ebeb9c3524cf5",
        "b0dfb2534e37b4418138dc0ab54ddd924a6f975b156c51526b58c909b224a2c5",
        "20a1f2d00c8e315653e38c0b15aae1d8201c9f18f080ea22cbc4587a96ffb041",
    }  # Historical selections must not silently resume with the new contract.


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
