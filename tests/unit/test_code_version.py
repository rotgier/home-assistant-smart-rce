"""read_code_version: commit SHA of the enclosing checkout, robust to ref layouts."""

from __future__ import annotations

from pathlib import Path

from custom_components.smart_rce.infrastructure.code_version import read_code_version

_SHA = "0123456789abcdef0123456789abcdef01234567"


def _checkout(tmp_path: Path) -> Path:
    """Build a fake checkout; return a file nested like the integration's modules."""
    (tmp_path / ".git" / "refs" / "heads").mkdir(parents=True)
    module = tmp_path / "custom_components" / "smart_rce" / "infrastructure" / "x.py"
    module.parent.mkdir(parents=True)
    module.touch()
    return module


def test_loose_branch_ref(tmp_path: Path) -> None:
    module = _checkout(tmp_path)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / ".git" / "refs" / "heads" / "main").write_text(_SHA + "\n")
    assert read_code_version(module) == _SHA[:12]


def test_packed_ref(tmp_path: Path) -> None:
    module = _checkout(tmp_path)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/deploy\n")
    (tmp_path / ".git" / "packed-refs").write_text(
        f"# pack-refs with: peeled\n{_SHA} refs/heads/deploy\n"
    )
    assert read_code_version(module) == _SHA[:12]


def test_detached_head(tmp_path: Path) -> None:
    module = _checkout(tmp_path)
    (tmp_path / ".git" / "HEAD").write_text(_SHA + "\n")
    assert read_code_version(module) == _SHA[:12]


def test_no_checkout_is_unknown(tmp_path: Path) -> None:
    module = tmp_path / "a" / "b.py"
    module.parent.mkdir()
    module.touch()
    assert read_code_version(module) == "unknown"
