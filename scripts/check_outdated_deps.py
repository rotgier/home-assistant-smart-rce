#!/usr/bin/env python3
"""Check outdated direct pins in requirements-test.txt.

Pip-managed equivalent of dependabot dla repo gdzie pip ecosystem jest
disabled (per CLAUDE.md — `pytest-homeassistant-custom-component`
constraint blokuje większość auto-bumpów).

Usage:
    .venv/bin/python scripts/check_outdated_deps.py            # full table, exit 1 = bump
    .venv/bin/python scripts/check_outdated_deps.py --commit   # pre-commit reminder

`--commit` (the pre-commit hook on every commit, user's request 03.11.2026):
never blocks — exit 0 also when PyPI is unreachable. PyPI is asked at most
once a day (`.git/check-outdated-deps.json`); other commits that day repeat
the cached verdict in one line. The weekly `deps-check.yml` keeps the full
table and its exit code.

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

from concurrent.futures import ThreadPoolExecutor
import contextlib
from datetime import date
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.request import urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS_FILE = REPO_ROOT / "requirements-test.txt"
PIN_PATTERN = re.compile(
    r"^(?P<name>[\w-]+)==(?P<version>[\d.]+(?:[a-z]\d*)?)\s*(?:#.*)?$"
)
CACHE_NAME = "check-outdated-deps.json"
PYPI_TIMEOUT_SEC = 5

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


def get_outdated(pins: dict[str, str]) -> dict[str, tuple[str, str]]:
    """Ask PyPI for the latest release of each pin → {name: (current, latest)}.

    Only the direct pins, in parallel (~1 s) — not `pip list --outdated`,
    which asks for every package of the venv (~40 s).
    """

    def latest(name: str) -> str:
        url = f"https://pypi.org/pypi/{name}/json"
        with urlopen(url, timeout=PYPI_TIMEOUT_SEC) as response:  # noqa: S310
            return str(json.load(response)["info"]["version"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        found = dict(zip(pins, pool.map(latest, pins), strict=True))
    return {
        name: (pins[name], version)
        for name, version in found.items()
        if version != pins[name]
    }


def commit_reminder() -> int:
    """Pre-commit: one line on what to bump; PyPI once a day; never blocks."""
    today = date.today().isoformat()
    cache = _cache_file()
    try:
        cached = json.loads(cache.read_text())
    except (OSError, ValueError):
        cached = {}
    if cached.get("date") != today:
        pins = parse_pins(REQUIREMENTS_FILE)
        try:
            outdated = get_outdated(pins)
        except OSError as err:
            print(f"deps: PyPI unreachable ({err}) — not checked, commit goes on")
            return 0
        cached = {
            "date": today,
            "bumps": {n: list(v) for n, v in sorted(outdated.items())},
        }
        # No cache: PyPI is asked again on the next commit.
        with contextlib.suppress(OSError):
            cache.write_text(json.dumps(cached))
    bumps = cached.get("bumps") or {}

    def rows(names: list[str]) -> str:
        return ", ".join(f"{n} {bumps[n][0]} → {bumps[n][1]}" for n in names)

    safe = [n for n in bumps if n not in PYTEST_HA_CONSTRAINED]
    blocked = [n for n in bumps if n in PYTEST_HA_CONSTRAINED]
    if not bumps:
        print("deps: wszystkie piny aktualne")
        return 0
    if safe:
        print(f"deps: ⚠️  do podbicia ({len(safe)}): {rows(safe)}")
    if blocked:
        print(f"deps: czekają na pytest-ha ({len(blocked)}): {rows(blocked)}")
    print("      pełna tabela: .venv/bin/python scripts/check_outdated_deps.py")
    return 0


def _cache_file() -> Path:
    """Return the cache path in the git dir (shared by worktrees, outside the tree)."""
    common = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    ).stdout.strip()
    return (REPO_ROOT / common).resolve() / CACHE_NAME if common else Path(CACHE_NAME)


def main() -> int:
    if "--commit" in sys.argv[1:]:
        return commit_reminder()
    pins = parse_pins(REQUIREMENTS_FILE)
    outdated = get_outdated(pins)

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
