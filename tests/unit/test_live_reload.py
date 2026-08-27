"""Every module of the deposit context must be in `live_reload()`.

A missing one does not fail — it keeps running the old code. On 2026-08-27
`history_repository` was absent, so a reload rebuilt the aggregate from the
previous class and the refresh died on an attribute that had just been added.
That class of bug is invisible in review and costs a debugging session, so it is
asserted instead.

Scoped to `deposit`, where the rule is "everything reloads". The ems modules are
a deliberately curated list and garden is out entirely (restart required), so a
repo-wide assertion would be wrong.
"""

from pathlib import Path
import re

from custom_components import smart_rce
from custom_components.smart_rce import deposit

_MODULE_PATTERN = re.compile(r'"custom_components\.smart_rce\.deposit([\w.]*)"')


def _reloaded() -> set[str]:
    source = Path(smart_rce.__file__).read_text(encoding="utf-8")
    return {match.lstrip(".") for match in _MODULE_PATTERN.findall(source) if match}


def _shipped() -> set[str]:
    root = Path(deposit.__file__).parent
    names = set()
    for path in root.rglob("*.py"):
        parts = [
            p for p in path.relative_to(root).with_suffix("").parts if p != "__init__"
        ]
        if parts:
            names.add(".".join(parts))
    return names


def test_every_deposit_module_is_reloaded():
    missing = _shipped() - _reloaded()

    assert not missing, f"add to live_reload(): {sorted(missing)}"


def test_no_module_is_reloaded_twice_or_by_a_stale_name():
    """A renamed file left behind in the list would raise on the next reload."""
    assert not _reloaded() - _shipped() - {"domain", "application", "infrastructure"}
