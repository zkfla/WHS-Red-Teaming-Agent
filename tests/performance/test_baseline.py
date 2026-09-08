from pathlib import Path

from tests.performance.baseline import (
    ROOT,
    current_core_hash,
    historical_core_hash,
    release_baseline,
    tree_hash,
)


def test_historical_batch_f_source_is_preserved_in_git_objects():
    baseline = release_baseline()
    assert baseline["historical_commit"] == "a01417cb241fc437ce0556ab0d9d5afb0381e8a2"
    assert baseline["historical_core_sha256"] == (
        "53e814dcb5152fa178765201a9afa22c22eab928b3ba3b2101645b738cd1c21c"
    )
    assert historical_core_hash() == baseline["historical_core_sha256"]


def test_current_reference_stage_matches_explicit_approved_release():
    baseline = release_baseline()
    assert current_core_hash() == baseline["approved_core_sha256"]
    receipt = Path(str(baseline["approval_receipt"]))
    assert not receipt.is_absolute() and ".." not in receipt.parts
    assert (ROOT / receipt).is_file()


def test_hash_binds_path_content_and_line_endings_not_enumeration_order():
    rows = [("b.py", b"b\n"), ("a.py", b"a\n")]
    assert tree_hash(rows) == tree_hash(list(reversed(rows)))
    assert tree_hash(rows) != tree_hash([("b.py", b"b\n"), ("a.py", b"a\r\n")])
    assert tree_hash(rows) != tree_hash([("c.py", b"b\n"), ("a.py", b"a\n")])
