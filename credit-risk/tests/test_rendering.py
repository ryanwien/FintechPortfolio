"""Renderers reproduce the committed documents from the committed metrics."""

import json
from pathlib import Path

import pytest

from credit.markdown import pct, table
from credit.model_card import render_model_card
from credit.report import render_report
from credit.site import site_json

RESULTS = Path(__file__).resolve().parents[1] / "results"


@pytest.fixture(scope="module")
def metrics() -> dict:
    return json.loads((RESULTS / "metrics.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("filename", "render"),
    [("REPORT.md", render_report), ("MODEL_CARD.md", render_model_card)],
    ids=["report", "model_card"],
)
def test_markdown_documents_match_committed_results(metrics, filename, render):
    assert render(metrics) == (RESULTS / filename).read_text(encoding="utf-8")


def test_site_json_matches_committed_results(metrics):
    committed = json.loads((RESULTS / "site.json").read_text(encoding="utf-8"))
    assert site_json(metrics) == committed


def test_markdown_table_shape():
    assert table(["a", "b"], [[1, 2]]) == "| a | b |\n|---|---|\n| 1 | 2 |"


@pytest.mark.parametrize(("x", "nd", "text"), [(0.1234, 1, "12.3%"), (0.05, 2, "5.00%")])
def test_pct(x, nd, text):
    assert pct(x, nd) == text
