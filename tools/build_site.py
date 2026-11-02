"""Bundle every project's results into assets/data.js for the dashboard.

The page reads `window.PORTFOLIO` / `window.PORTFOLIO_META` instead of fetching JSON,
so it also works when opened straight from disk.

Run from the repo root after the projects have run: `python tools/build_site.py`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

ROOT: Final = Path(__file__).resolve().parent.parent
PROJECTS: Final = (
    "ledger",
    "fraud-scoring",
    "credit-risk",
    "matching-engine",
    "matching-engine-cpp",
    "aml-monitoring",
)
COMMIT_LIMIT: Final = 10
_COLLECTED: Final = re.compile(r"(\d+) tests? collected")


def _run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
        cmd, cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def count_tests(folder: Path) -> int:
    """Test cases pytest collects, so parametrized cases count the same way CI counts them.

    A project without a pyproject.toml is not a Python project; its runner writes its own
    test count into results/site.json and this returns -1 to signal "use that".
    """
    if not (folder / "pyproject.toml").exists():
        return -1
    # -o addopts= neutralizes per-project verbosity flags so the summary line is always printed.
    out = _run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts="], folder)
    found = _COLLECTED.search(out)
    if not found:
        raise SystemExit(f"could not read pytest collection summary for {folder.name}")
    return int(found.group(1))


def count_loc(folder: Path) -> int:
    """Non-blank lines of source (Python or C++) under src/ and tests/."""
    return sum(
        sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        for sub in ("src", "tests")
        for pattern in ("*.py", "*.cpp", "*.hpp", "*.h")
        for path in (folder / sub).rglob(pattern)
    )


def recent_commits(limit: int = COMMIT_LIMIT) -> list[dict[str, str]]:
    """Latest commits for the activity feed; empty when git is unavailable."""
    git = shutil.which("git")
    if git is None:
        return []
    try:
        out = _run([git, "log", f"-{limit}", "--date=short", "--format=%h%x09%ad%x09%s"], ROOT)
    except subprocess.CalledProcessError:
        return []
    commits = []
    for line in out.splitlines():
        sha, date, msg = line.split("\t", 2)
        commits.append({"sha": sha, "date": date, "msg": msg})
    return commits


def load_project(name: str) -> dict[str, Any]:
    """One project's site feed, enriched with its folder, counts and run report."""
    folder = ROOT / name
    results = folder / "results"
    site = results / "site.json"
    if not site.exists():
        raise SystemExit(f"missing {site.relative_to(ROOT)}: run the project first")
    data = json.loads(site.read_text(encoding="utf-8"))
    report = results / "REPORT.md"
    pytest_count = count_tests(folder)
    data |= {
        "folder": name,
        # Python projects name their entry module in .run; others ship a run_cmd in site.json.
        "run": (folder / ".run").read_text(encoding="utf-8").strip()
        if (folder / ".run").exists()
        else "",
        # -1 means "not a pytest project": keep the count its own test runner wrote.
        "tests": pytest_count if pytest_count >= 0 else int(data.get("tests", 0)),
        "loc": count_loc(folder),
        "report": report.read_text(encoding="utf-8") if report.exists() else "",
    }
    return data


def merge_cpp_twin(py: dict[str, Any], cpp: dict[str, Any]) -> dict[str, Any]:
    """Fold the C++ twin into the Python engine's entry: one system, two implementations.

    The dashboard shows a single Matching engine with both languages' numbers; the repo
    keeps the two folders separate.
    """

    def metric(src: dict[str, Any], i: int, label: str) -> dict[str, Any]:
        return {**src["metrics"][i], "label": label}

    merged = dict(py)
    merged["title"] = "Limit order book matching engine, in Python and C++"
    merged["tagline"] = (
        "One price-time priority book, implemented twice: a Python reference with an "
        "event-driven backtester, and a C++20 twin with identical rules and ported tests "
        "running twenty times faster on the same machine."
    )
    merged["stack"] = py["stack"] + [s for s in cpp["stack"] if s not in py["stack"]]
    merged["tests"] = py["tests"] + cpp["tests"]
    merged["loc"] = py["loc"] + cpp["loc"]
    merged["metrics"] = [
        metric(cpp, 0, "Engine throughput (C++ twin)"),
        metric(cpp, 2, "C++ speedup vs Python"),
        metric(py, 0, "Engine throughput (Python)"),
        metric(cpp, 1, "p99 per-order latency (C++)"),
        py["metrics"][3],
    ]
    merged["extra_series"] = [{**cpp["series"], "short": "C++"}, *py.get("extra_series", [])]
    merged["series"] = {**py["series"], "short": "Python"}
    twin_heading = "\n\n---\n\n# The C++ twin (matching-engine-cpp/)\n\n"
    merged["report"] = py["report"] + twin_heading + cpp["report"]
    return merged


def stamp_assets() -> str:
    """Version-stamp index.html's local asset URLs with a hash of their content.

    Browsers cache assets per URL, so without this a visitor can mix a fresh page with
    a stale cached data.js (or vice versa). A content hash in the query string makes
    every asset change a new URL; unchanged assets keep their cached copy.
    """
    import hashlib
    import re as _re

    html = ROOT / "index.html"
    text = html.read_text(encoding="utf-8")
    pattern = _re.compile(r'"(assets/[\w/.-]+\.(?:css|js))\?v=\w*"')
    refs = pattern.findall(text)
    if not refs:
        raise SystemExit("index.html has no versioned asset references to stamp")
    digest = hashlib.sha1(usedforsecurity=False)
    for ref in sorted(set(refs)):
        digest.update((ROOT / ref).read_bytes())
    stamp = digest.hexdigest()[:10]
    text = pattern.sub(lambda m: f'"{m.group(1)}?v={stamp}"', text)
    html.write_text(text, encoding="utf-8")
    return stamp


def main() -> None:
    by_name = {name: load_project(name) for name in PROJECTS}
    by_name["matching-engine"] = merge_cpp_twin(
        by_name["matching-engine"], by_name.pop("matching-engine-cpp")
    )
    projects = list(by_name.values())
    meta = {
        "tests": sum(p["tests"] for p in projects),
        "loc": sum(p["loc"] for p in projects),
        "commits": recent_commits(),
    }
    out = ROOT / "assets" / "data.js"
    out.write_text(
        f"window.PORTFOLIO = {json.dumps(projects, indent=1, ensure_ascii=False)};\n"
        f"window.PORTFOLIO_META = {json.dumps(meta)};\n",
        encoding="utf-8",
    )
    stamp = stamp_assets()
    print(
        f"wrote {out.relative_to(ROOT)}: {len(projects)} projects, "
        f"{meta['tests']} tests, {meta['loc']} lines, asset stamp v={stamp}"
    )


if __name__ == "__main__":
    main()
