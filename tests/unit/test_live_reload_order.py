"""`live_reload()` must reload battery_schedule before anything holding SlotKind.

`reload()` builds a NEW enum class. Enum members hash by identity, so a module
reloaded *before* `battery_schedule` keeps members of the outgoing class while
the aggregate is rebuilt with the incoming one — and every dict lookup keyed by
SlotKind then raises KeyError.

That is not theoretical: `domain.evening_plan` sat before it and silently broke
the evening planner across two days (02-03.10.2026), costing the 21:05 and
22:05 runs and three hours of the afternoon sweep. The failure is invisible in
tests and in a dev restart, because a cold start has only one enum class — it
only appears after a config-entry reload on a live system.
"""

from __future__ import annotations

from pathlib import Path
import re

_ROOT = Path(__file__).resolve().parents[2] / "custom_components" / "smart_rce"
_PACKAGE = "domain.battery_schedule"

_RELOADED = re.compile(
    r'import_module\(\s*"custom_components\.smart_rce\.([a-z_.]+)"\s*\)'
)
_IMPORTS_SLOT_KIND = re.compile(r"from [\w.]*battery_schedule(?:\.\w+)* import")


def _reload_order() -> dict[str, int]:
    text = (_ROOT / "__init__.py").read_text(encoding="utf-8")
    order: dict[str, int] = {}
    for match in _RELOADED.finditer(text):
        order.setdefault(match.group(1), match.start())
    return order


def _modules_importing_slot_kind() -> set[str]:
    found = set()
    for path in _ROOT.rglob("*.py"):
        dotted = path.relative_to(_ROOT).with_suffix("").as_posix().replace("/", ".")
        if dotted.startswith(_PACKAGE) or dotted == "__init__":
            continue  # the package itself, and the reload list's own module
        if _IMPORTS_SLOT_KIND.search(path.read_text(encoding="utf-8")):
            found.add(dotted.removesuffix(".__init__"))
    return found


def test_battery_schedule_is_reloaded_before_every_module_that_imports_it():
    order = _reload_order()
    package_at = order[_PACKAGE]

    too_early = sorted(
        module
        for module in _modules_importing_slot_kind()
        if module in order and order[module] < package_at
    )

    assert not too_early, (
        "reloaded before domain.battery_schedule, so they keep the outgoing "
        f"SlotKind class and every lookup keyed by it raises KeyError: {too_early}"
    )


def test_the_guard_above_is_actually_watching_something():
    """A typo in either regex would make the test vacuously pass."""
    importers = _modules_importing_slot_kind()

    assert "domain.evening_plan" in importers
    assert "application.battery_schedule_service" in importers
    assert _PACKAGE in _reload_order()
