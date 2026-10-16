"""Split-stage v3 golden regression tests without models or transport calls."""

import hashlib

import yaml

from redteam.app.agent_profile import SpecialistProfile, load_specialist_profile
from tests.integration.test_central_development import PROFILE

PROBES = PROFILE.with_name("native-probes.yaml")
LEGACY = PROFILE.with_name("native-probes-legacy.yaml")

# Golden constants for the version-26 single-stage v3 compatibility profile
# after the staged role-contract rewrite through plan8c.
# This does not assert compatibility with older prompt/profile versions;
# integration tests separately require matching selection fingerprints on resume.
# Runtime fingerprints hash raw prompt bytes, so both LF and CRLF prompt variants
# are pinned while the YAML checksum is newline-canonicalized.
LEGACY_YAML_LF_SHA256 = "a1eb2fb0b89824a584fb3b7faad9235dc2badf6c2e15556c1ba72be53174537f"
LEGACY_FINGERPRINT_LF = "e97070271757303f01236751a10c9fe770a2fae3cd0bc47ff49c63a5a513bc0a"
LEGACY_FINGERPRINT_CRLF = "f79d4a7ec9064769142de58787e6c9020e76623c99f5bb8c5f6eb3a29d7fd7ad"


def test_legacy_golden_fingerprint_and_bytes():
    canonical = LEGACY.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(canonical).hexdigest() == LEGACY_YAML_LF_SHA256
    loaded = load_specialist_profile(LEGACY)
    assert loaded.profile.version == "26"
    assert loaded.fingerprint in {
        LEGACY_FINGERPRINT_CRLF, LEGACY_FINGERPRINT_LF,
    }
    assert loaded.run_policy().probe_atomization is False
    assert loaded.run_policy().fingerprint == loaded.fingerprint
    assert loaded.fingerprint not in {
        "50f299e5b8c70a469ed475e519bfcc1deda3860e586125c3374b7bb4e18891b0",
        "c7a3fea60051e195d1a8f24ba0ac646311fda9734139ac6e77322f117be6b4a8",
        "d7161eb762cb276d10d0dcc66298e12721bc640680960586739a449815903c45",
        "a2526a4fc165d7008bb0b3195c1e27c2e5773a9ba8b290183f7acf74d11d4b1e",
        "21ef378c2f2af9b1b8d2fb83e30bfb078157de7de8ee260c244ff97035e1e3dc",
        "18b04d45c2207efe133788f52f6c1baeec58e8ffd803b1d5b3216a89e3a275ab",
        "50126322fedf6851f1a4791e6c816aab748d256317d889a5e0e3f9f3dadd2fa7",
        "292fe25da9babd70c3e7704bc91f2843dbe0a6a69163cd27be37dc76efc30f17",
        "b176b4317993b1483666b9dcfa330d0f413a2ca616435a931b21ca0af0046ffe",
        "92dbea51ba10fd7a019a312a1cfd85dc9db4cba2bcef39dce77ebeb9c3524cf5",
        "b0dfb2534e37b4418138dc0ab54ddd924a6f975b156c51526b58c909b224a2c5",
        "20a1f2d00c8e315653e38c0b15aae1d8201c9f18f080ea22cbc4587a96ffb041",
        "ec47a5a7b602b156b15dd466b5b422f437340efb91150eb1ed52a6d6964616a4",
        "81032f10261cb3d4d9396f12126d935688409a72e6ea295ea8beb9ce8ef31b38",
        "1d981f0941651698c48aed8776e28045c83d3f29eca6bd138d3194cc6084283d",
        "1d79e247e541239cc7328837dd2ae332a69d7382de47f32dbd5587567b09d04e",
        "69681b38e366d6747012ac64fb1de5381e1f070029ec8948611e22248a945338",
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
