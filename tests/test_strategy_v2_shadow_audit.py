import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "strategy_v2_shadow_audit",
    ROOT / "scripts" / "audit_strategy_v2_shadow_candidate.py",
)
AUDIT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(AUDIT)


def test_porcelain_parser_preserves_first_path_character_and_leading_status_space():
    payload = b" M core/config.py\0?? new file.py\0"
    assert AUDIT._parse_porcelain_z(payload) == [
        "core/config.py",
        "new file.py",
    ]


def test_generated_verification_artifacts_are_classified_separately():
    assert AUDIT._classify_path(".shadow_stress/") == "GENERATED_ARTIFACT"
    assert AUDIT._classify_path(
        ".shadow_stress/strategy_v2_shadow.sqlite3"
    ) == "GENERATED_ARTIFACT"
    assert AUDIT._classify_path(
        "SHADOW_STORAGE_STRESS.json"
    ) == "GENERATED_ARTIFACT"
    assert AUDIT._classify_path(
        "SHADOW_DIFF_AUDIT.json"
    ) == "GENERATED_ARTIFACT"
    assert AUDIT._classify_path(
        "strategy_v2_shadow/strategy_v2_shadow.sqlite3"
    ) == "GENERATED_ARTIFACT"


def test_lf_normalization_only_changes_crlf_bytes():
    lf = b"first\nsecond\n"
    crlf = b"first\r\nsecond\r\n"
    assert lf != crlf
    assert AUDIT._normalize_lf(lf) == AUDIT._normalize_lf(crlf)
