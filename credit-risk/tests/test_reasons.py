import pandas as pd

from credit.reasons import REASON_CODES, STATEMENT_BY_CODE, reason_codes


def test_reason_codes_ordered_by_points_lost():
    max_pts = {
        "dti": 40.0,
        "annual_income": 30.0,
        "delinq_2y": 25.0,
        "purpose": 20.0,
        "inquiries_6m": 15.0,
    }
    pts = pd.DataFrame(
        [
            {
                "dti": 10.0,
                "annual_income": 28.0,
                "delinq_2y": 5.0,
                "purpose": 12.0,
                "inquiries_6m": 15.0,
            }
        ]
    )
    (reasons,) = reason_codes(pts, max_pts, top_n=4)
    # lost: dti 30, delinq 20, purpose 8, income 2, inquiries 0
    assert [r.feature for r in reasons] == ["dti", "delinq_2y", "purpose", "annual_income"]
    assert [r.points_lost for r in reasons] == [30.0, 20.0, 8.0, 2.0]
    assert reasons[0].code == REASON_CODES["dti"][0]
    assert all(r.statement for r in reasons)


def test_reason_codes_skip_attributes_with_no_points_lost():
    max_pts = {"dti": 40.0, "annual_income": 30.0}
    pts = pd.DataFrame([{"dti": 40.0, "annual_income": 25.0}, {"dti": 40.0, "annual_income": 30.0}])
    first, second = reason_codes(pts, max_pts)
    assert [r.feature for r in first] == ["annual_income"]
    assert second == []


def test_reason_code_ties_break_by_column_order():
    max_pts = {"dti": 10.0, "annual_income": 10.0}
    pts = pd.DataFrame([{"dti": 5.0, "annual_income": 5.0}])
    (reasons,) = reason_codes(pts, max_pts)
    assert [r.feature for r in reasons] == ["dti", "annual_income"]


def test_reason_codes_are_unique_and_invertible():
    assert len(STATEMENT_BY_CODE) == len(REASON_CODES)
    for code, statement in REASON_CODES.values():
        assert STATEMENT_BY_CODE[code] == statement
