#!/usr/bin/env python3
"""Check outdated direct pins in requirements-test.txt.

Pip-managed equivalent of dependabot dla repo gdzie pip ecosystem jest
disabled (per CLAUDE.md — `pytest-homeassistant-custom-component`
constraint blokuje większość auto-bumpów).

Usage:
    .venv/bin/python scripts/check_outdated_deps.py

Output:
    Tabela direct pins z bumpem + flag czy bumpa blokuje pytest-ha
    constraint. Wyniki sortowane: safe-to-bump na górze, blocked na dole.

Workflow:
    1. Uruchom raz na miesiąc
    2. Bump safe-to-bump pinów ręcznie w requirements-test.txt
    3. Run pytest + mypy lokalnie
    4. Commit ze sztandarem "deps: bump X 1.2 → 1.3"
    5. Push, CI gates
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS_FILE = REPO_ROOT / "requirements-test.txt"
PIN_PATTERN = re.compile(r"^(?P<name>[\w-]+)==(?P<version>[\d.]+(?:[a-z]\d*)?)\s*$")

# Pakiety pinowane jako transitive constraints przez
# pytest-homeassistant-custom-component. Bumpy tych są blocked dopóki nie
# bumpniemy pytest-ha jako całość (zwykle wymaga update HA Core wersji).
PYTEST_HA_CONSTRAINED = {
    "pytest",
    "pytest-asyncio",
    "pytest-cov",
    "pytest-timeout",
    "syrupy",
    "coverage",
    "aioresponses",
    "aiofiles",
}


def parse_pins(path: Path) -> dict[str, str]:
    """Read requirements-test.txt → {name: current_version}."""
    pins: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = PIN_PATTERN.match(line)
        if m:
            pins[m["name"].lower()] = m["version"]
    return pins


def get_outdated() -> dict[str, tuple[str, str]]:
    """Query pip for outdated packages → {name: (current, latest)}."""
    venv_pip = REPO_ROOT / ".venv" / "bin" / "pip"
    if not venv_pip.exists():
        sys.exit(
            f"venv pip not found at {venv_pip} — run from repo root with .venv setup"
        )
    # pip JSON output includes "[notice]" lines; isolate JSON via prefix detection.
    result = subprocess.run(
        [str(venv_pip), "list", "--outdated", "--format=json"],
        capture_output=True,
        text=True,
        check=False,
    )
    # Find JSON start (skip pip notice lines).
    json_start = result.stdout.find("[")
    if json_start == -1:
        return {}
    data = json.loads(result.stdout[json_start:])
    return {
        item["name"].lower(): (item["version"], item["latest_version"]) for item in data
    }


def main() -> int:
    pins = parse_pins(REQUIREMENTS_FILE)
    outdated = get_outdated()

    safe: list[tuple[str, str, str]] = []
    blocked: list[tuple[str, str, str]] = []
    up_to_date: list[str] = []

    for name in sorted(pins):
        current = pins[name]
        if name in outdated:
            _, latest = outdated[name]
            row = (name, current, latest)
            if name in PYTEST_HA_CONSTRAINED:
                blocked.append(row)
            else:
                safe.append(row)
        else:
            up_to_date.append(name)

    width = max(len(n) for n in pins) if pins else 0

    # Filter "false positive" entries gdzie current == latest_ver (venv stale —
    # requirements-test.txt bumped ale .venv nie reinstalled jeszcze).
    safe = [(n, c, latest_ver) for n, c, latest_ver in safe if c != latest_ver]
    blocked = [(n, c, latest_ver) for n, c, latest_ver in blocked if c != latest_ver]

    pytest_ha_bump = next(
        (
            (c, latest_ver)
            for n, c, latest_ver in safe
            if n == "pytest-homeassistant-custom-component"
        ),
        None,
    )

    if safe:
        print(f"\n=== SAFE TO BUMP ({len(safe)}) ===")
        for name, current, latest in safe:
            marker = (
                "  ⭐ GATEWAY"
                if name == "pytest-homeassistant-custom-component"
                else ""
            )
            print(f"  {name:<{width}}  {current:>10}  →  {latest}{marker}")

    if blocked:
        print(
            f"\n=== BLOCKED by pytest-homeassistant-custom-component ({len(blocked)}) ==="
        )
        print("  (transitive constraints — bump razem z pytest-ha)")
        for name, current, latest in blocked:
            print(f"  {name:<{width}}  {current:>10}  →  {latest}")
        if pytest_ha_bump:
            print(
                f"\n  💡 pytest-ha {pytest_ha_bump[0]} → {pytest_ha_bump[1]} dostępna —\n"
                f"     bumpa ją PIERWSZĄ, potem retry blocked w tym samym commit."
            )

    if up_to_date:
        print(f"\n=== UP-TO-DATE ({len(up_to_date)}) ===")
        for name in up_to_date:
            print(f"  {name:<{width}}  {pins[name]}")

    print(
        f"\nTotal pinned: {len(pins)}  |  Safe: {len(safe)}  |  "
        f"Blocked: {len(blocked)}  |  Up-to-date: {len(up_to_date)}"
    )
    # Exit non-zero TYLKO gdy są actionable bumpy (SAFE TO BUMP).
    # BLOCKED-only (bez pytest-ha gateway w SAFE) = czekamy na pytest-ha
    # release — nic nie możemy zrobić sami, więc nie spamujemy notyfikacji.
    if not safe:
        if blocked:
            print(
                "\n  ℹ️  Tylko BLOCKED — czekamy na pytest-homeassistant-custom-component\n"
                "     release. Nothing actionable. Exit 0."
            )
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
