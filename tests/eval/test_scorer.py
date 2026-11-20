"""Offline tests for the M0 evaluation harness: answer key v2, matcher, metrics."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from eval.adapters import ScoredDoc, ScoredLine, from_contract, from_legacy
from eval.matcher import match_lines
from eval.metrics import aggregate, score_document

KEY_ROOT = Path(__file__).resolve().parents[3] / "TRAINING_SAMPLES_extracted"
KEY_V2 = KEY_ROOT / "v2"
needs_key = pytest.mark.skipif(not KEY_V2.exists(), reason="answer key v2 not present")


def line(page, name, amount, role="billable", rate=None, qty=None):
    d = lambda x: None if x is None else Decimal(str(x)).quantize(Decimal("0.01"))  # noqa: E731
    return ScoredLine(page_no=page, item_name=name, amount=d(amount), rate=d(rate), quantity=d(qty), role=role)


class TestMatcher:
    def test_exact_amount_and_similar_name_match(self):
        m = match_lines([line(1, "CBC", 240)], [line(1, "CBC ", 240)])
        assert len(m.matched) == 1 and not m.missed and not m.extra

    def test_code_glued_to_name_still_matches(self):
        """Sample 10 prints 'Equipment(999311)'; punctuation must not hide the name."""
        m = match_lines([line(1, "Equipment", 213250)], [line(1, "Equipment(999311)", 213250)])
        assert len(m.matched) == 1

    def test_wrong_page_is_missed_and_extra(self):
        m = match_lines([line(1, "CBC", 240)], [line(2, "CBC", 240)])
        assert len(m.missed) == 1 and len(m.extra) == 1
        assert len(match_lines([line(1, "CBC", 240)], [line(2, "CBC", 240)], ignore_page=True).matched) == 1

    def test_same_amount_different_item_does_not_match(self):
        m = match_lines([line(1, "SERUM AMYLASE", 240)], [line(1, "HBsAg", 240)])
        assert not m.matched

    def test_duplicates_match_one_to_one(self):
        key = [line(1, "IP CONSULTATION CHARGES", 1000)] * 3
        m = match_lines(key, [line(1, "IP CONSULTATION CHARGES", 1000)] * 2)
        assert len(m.matched) == 2 and len(m.missed) == 1

    def test_misread_amount_is_reported_but_still_missed(self):
        m = match_lines([line(1, "PROCALCITONIN", 2070)], [line(1, "PROCALCITONIN", 2010)])
        assert len(m.misread) == 1 and len(m.missed) == 1 and len(m.extra) == 1

    def test_best_name_wins_when_amounts_tie(self):
        key = [line(1, "CBC", 240), line(1, "HBsAg", 240)]
        m = match_lines(key, [line(1, "HBSAG", 240), line(1, "CBC", 240)])
        assert {(k.item_name, s.item_name.upper()) for k, s in m.matched} == {("CBC", "CBC"), ("HBsAg", "HBSAG")}


class TestMetrics:
    def test_double_counting_is_a_role_error(self):
        key = ScoredDoc("d", "passed", [line(1, "Drugs", 100), line(2, "Tab X", 100, role="breakdown")])
        legacy = ScoredDoc("d", "passed", [line(1, "Drugs", 100), line(2, "Tab X", 100)])
        s = score_document(key, legacy, "dev", legacy=True)
        assert s.matched == 2 and s.role_ok == 1 and not s.correct
        assert s.total_ok is False  # 200 counted vs 100 billable

    def test_trusted_precision_and_flag_recall(self):
        good = ScoredDoc("a", "passed", [line(1, "CBC", 240)])
        bad_passed = ScoredDoc("b", "passed", [])
        bad_flagged = ScoredDoc("c", "flagged", [])
        key = ScoredDoc("k", "passed", [line(1, "CBC", 240)])
        scores = [score_document(key, d, "dev", legacy=False) for d in (good, bad_passed, bad_flagged)]
        agg = aggregate(scores)
        assert agg["trusted_precision"] == 0.5  # 1 of 2 passed documents is correct
        assert agg["flag_recall"] == 0.5  # 1 of 2 wrong documents was flagged

    def test_failed_response_scores_zero(self):
        doc = from_legacy("x", {"is_success": False, "error": "quota"})
        s = score_document(ScoredDoc("k", "passed", [line(1, "CBC", 240)]), doc, "dev", legacy=True)
        assert s.status == "failed" and s.missed == 1 and not s.correct


@needs_key
class TestAnswerKey:
    @pytest.mark.parametrize("n", range(1, 16))
    def test_key_scores_perfectly_against_itself(self, n):
        doc = from_contract(json.loads((KEY_V2 / f"train_sample_{n}.json").read_text()))
        s = score_document(doc, doc, "dev", legacy=False)
        assert s.correct and s.missed == 0 and s.extra == 0 and s.role_ok == s.matched == s.key_lines

    def test_v1_key_as_legacy_output_shows_only_the_role_and_coverage_gaps(self):
        """v1 dropped summary/breakdown lines; scored as legacy output it must miss exactly those."""
        expected_missed = {4: 6, 10: 12, 13: 174, 14: 1}
        # Sample 13: v1 named 5 parents "BLOOD BANK CHARGES - Packed Cell (NAT Tested)" etc., merging in the
        # child's text. With equal amounts, the matcher pairs them with the child (token-set ratio 100), a
        # documented limitation for merged parent+child names (lld.md §11.2).
        merged_parent_names = {13: 5}
        for n in range(1, 16):
            v1 = json.loads((KEY_ROOT / f"train_sample_{n}.json").read_text())
            key = from_contract(json.loads((KEY_V2 / f"train_sample_{n}.json").read_text()))
            s = score_document(key, from_legacy("x", {"is_success": True, "data": v1["data"]}), "dev", legacy=True)
            assert s.extra == 0, n
            assert s.missed == expected_missed.get(n, 0), n
            assert s.matched - s.role_ok == merged_parent_names.get(n, 0), n  # v1 kept only billable lines
            if not key.is_partial:
                assert s.total_ok, n

    def test_every_line_has_a_role_and_non_billable_lines_say_what_covers_them(self):
        for path in KEY_V2.glob("train_sample_*.json"):
            for bill in json.loads(path.read_text())["bills"]:
                for ln in bill["lines"]:
                    assert ln["role"] in ("billable", "summary", "breakdown"), path.name
                    assert ln["role"] == "billable" or ln["covered_by"], (path.name, ln["line_id"])

    def test_sample_14_reports_the_bills_own_error(self):
        doc = json.loads((KEY_V2 / "train_sample_14.json").read_text())
        recon = doc["bills"][0]["reconciliation"]
        assert recon["status"] == "mismatch" and recon["difference"] == pytest.approx(262.69)
