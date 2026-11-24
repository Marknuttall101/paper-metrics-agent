"""Tests for the deterministic tools - no network, no model."""

import copy
import json
import unittest
from pathlib import Path

from paper_metrics import compare, numparse, pubmed, score, validate
from paper_metrics.cli import sample_file

ROOT = Path(__file__).resolve().parents[1]
PAPERS = json.loads(sample_file("papers.json").read_text(encoding="utf-8"))
GOLD = json.loads(sample_file("gold.json").read_text(encoding="utf-8"))


class TestNumbers(unittest.TestCase):
    def test_number_styles(self):
        self.assertTrue(numparse.number_appears(1204, "We enrolled 1,204 adults"))
        self.assertTrue(numparse.number_appears(2870, "We followed 2 870 patients"))
        self.assertTrue(numparse.number_appears(1.02, "hazard ratio 1·02"))
        self.assertTrue(numparse.number_appears(0.94, "95% CI 0.70-0.94"))
        self.assertTrue(numparse.number_appears(1.18, "0·88–1·18"))
        self.assertFalse(numparse.number_appears(0.75, "hazard ratio 0.74"))

    def test_clean_p_value(self):
        self.assertEqual(numparse.clean_p_value("P=0.002"), "0.002")
        self.assertEqual(numparse.clean_p_value("p < .001"), "<.001")
        self.assertEqual(numparse.clean_p_value("0\u00b779"), "0.79")
        self.assertIsNone(numparse.clean_p_value(None))

    def test_p_values(self):
        self.assertEqual(numparse.p_value_number("<0.001"), 0.001)
        self.assertEqual(numparse.p_value_number("P = .03"), 0.03)
        self.assertIsNone(numparse.p_value_number(None))


class TestValidate(unittest.TestCase):
    def test_hand_checked_answers_pass(self):
        errors, warnings = validate.validate(GOLD, PAPERS)
        self.assertEqual(errors, [])
        # SAMPLE-003 writes n in words ("Sixty-two"), so a human check is requested
        self.assertEqual(len(warnings), 1)
        self.assertIn("SAMPLE-003", warnings[0])

    def test_made_up_value_is_caught(self):
        bad = copy.deepcopy(GOLD[:1])
        bad[0]["value"] = 0.72  # the paper says 0.74
        errors, _ = validate.validate(bad, PAPERS)
        self.assertTrue(any("value=0.72" in e for e in errors))

    def test_made_up_quote_is_caught(self):
        bad = copy.deepcopy(GOLD[:1])
        bad[0]["evidence"] = "Examplozumab cut hospitalisation (HR 0.74; 95% CI 0.61 to 0.90)."
        errors, _ = validate.validate(bad, PAPERS)
        self.assertTrue(any("not word-for-word" in e for e in errors))

    def test_interval_must_contain_value(self):
        bad = copy.deepcopy(GOLD[:1])
        bad[0]["ci_low"], bad[0]["ci_high"] = 0.90, 0.61  # swapped
        errors, _ = validate.validate(bad, PAPERS)
        self.assertTrue(any("outside its interval" in e for e in errors))

    def test_unknown_paper_and_metric(self):
        bad = copy.deepcopy(GOLD[:1])
        bad[0]["pmid"], bad[0]["metric"] = "NOPE", "hr"
        errors, _ = validate.validate(bad, PAPERS)
        self.assertTrue(any("metric 'hr'" in e for e in errors))
        self.assertTrue(any("pmid not found" in e for e in errors))


class TestScore(unittest.TestCase):
    def test_perfect(self):
        s = score.score(GOLD, GOLD)
        self.assertEqual((s["precision"], s["recall"]), (1.0, 1.0))

    def test_missed_and_extra(self):
        pred = copy.deepcopy(GOLD[1:])
        pred.append(dict(GOLD[0], value=0.5))
        s = score.score(pred, GOLD)
        self.assertEqual(s["found"], len(GOLD) - 1)
        self.assertEqual(len(s["missed"]), 1)
        self.assertEqual(len(s["extra"]), 1)


class TestCompare(unittest.TestCase):
    def test_report(self):
        report = compare.build_report(GOLD, PAPERS)
        self.assertIn("## Hazard ratio (3)", report)
        self.assertIn("0.74 (95% CI 0.61 to 0.90)", report)  # trailing zero kept
        self.assertIn("18.5 m (90% CI 4.2 to 32.8)", report)
        self.assertIn("0 error(s)", report)


class TestPubmedParser(unittest.TestCase):
    def test_parse_fixture(self):
        xml = (ROOT / "tests" / "fixtures" / "efetch_sample.xml").read_bytes()
        papers = pubmed.parse_pubmed_xml(xml)
        self.assertEqual([p["pmid"] for p in papers], ["99000001", "99000002"])
        first, second = papers
        self.assertEqual(first["title"], "A fictional trial of examplozumab")
        self.assertEqual(first["year"], "2024")
        self.assertIn("RESULTS: The hazard ratio was 0.80", first["abstract"])
        self.assertEqual(first["authors"], ["Novak L", "Example Trial Group"])
        self.assertEqual(second["year"], "2019")
        self.assertEqual(second["abstract"], "")


if __name__ == "__main__":
    unittest.main()


class TestValidatorEdgeCases(unittest.TestCase):
    """Cases from code review: each would let a wrong number through, or reject a right one."""

    def _check(self, abstract, **rec):
        base = {"pmid": "X", "metric": "mean_difference", "outcome": "o"}
        return validate.check_record({**base, **rec}, {"pmid": "X", "abstract": abstract})

    def test_quote_cut_from_inside_a_number_is_rejected(self):
        abstract = "The adjusted OR was 0.815 (95% CI 0.70 to 0.94) in the cohort."
        e, _ = self._check(abstract, metric="odds_ratio", value=0.81, evidence="The adjusted OR was 0.81")
        self.assertTrue(any("not word-for-word" in m for m in e))
        e, _ = self._check(abstract, metric="odds_ratio", value=0.815, ci_low=0.70, ci_high=0.94,
                           evidence="The adjusted OR was 0.815 (95% CI 0.70 to 0.94)")
        self.assertEqual(e, [])

    def test_one_word_quote_is_rejected(self):
        e, _ = self._check("We enrolled 1204 adults.", value=120, evidence="120")
        self.assertTrue(any("too short" in m for m in e))

    def test_sign_flip_is_rejected(self):
        abstract = "Weight changed by −0.3 kg (95% CI −0.5 to −0.1) with treatment."
        good = dict(value=-0.3, ci_low=-0.5, ci_high=-0.1, evidence="changed by −0.3 kg (95% CI −0.5 to −0.1)")
        self.assertEqual(self._check(abstract, **good)[0], [])
        flipped = dict(good, value=0.3, ci_low=0.1, ci_high=0.5)
        self.assertTrue(any("check the sign" in m for m in self._check(abstract, **flipped)[0]))

    def test_en_dash_as_minus_sign(self):
        abstract = "Weight changed by \u20132.1 kg (95% CI \u20133.0 to \u20131.2) over a year."
        good = dict(value=-2.1, ci_low=-3.0, ci_high=-1.2, evidence="Weight changed by \u20132.1 kg (95% CI \u20133.0 to \u20131.2)")
        self.assertEqual(self._check(abstract, **good)[0], [])
        self.assertTrue(self._check(abstract, **dict(good, value=2.1, ci_low=1.2, ci_high=3.0))[0])

    def test_quote_starting_after_a_minus_is_rejected(self):
        abstract = "Walk distance changed by \u22120.3 m (95% CI \u22120.5 to \u22120.1) in the trial."
        e, _ = self._check(abstract, value=0.3, evidence="0.3 m (95% CI")
        self.assertTrue(any("not word-for-word" in m for m in e))

    def test_leading_dot_numbers(self):
        abstract = "Scores were correlated (r = .45, P = .003) in the sample."
        ok = dict(metric="correlation", value=0.45, p_value=".003", evidence="Scores were correlated (r = .45, P = .003)")
        self.assertEqual(self._check(abstract, **ok)[0], [])
        self.assertTrue(self._check(abstract, **dict(ok, value=45))[0])

    def test_borrowed_interval_is_flagged(self):
        abstract = "HR 0.74 (95% CI 0.61 to 0.90) for admission and HR 0.93 (95% CI 0.79 to 1.10) for death."
        e, w = self._check(abstract, metric="hazard_ratio", value=0.93, ci_low=0.61, ci_high=1.10,
                           evidence=abstract)
        self.assertEqual(e, [])
        self.assertTrue(any("interval is not written right after" in m for m in w))

    def test_non_numeric_fields_are_errors_not_crashes(self):
        e, _ = self._check("Effect 1.2 (95% CI 1.0 to 1.4) seen.", value=1.2, ci_low="one",
                           ci_high=1.4, evidence="Effect 1.2 (95% CI 1.0 to 1.4) seen.")
        self.assertTrue(any("ci_low='one' is not a number" in m for m in e))


class TestPapersAndScore(unittest.TestCase):
    def test_papers_file_is_checked_and_tidied(self):
        from paper_metrics.papers import normalise_papers
        tidy = normalise_papers([{"pmid": 123, "year": 2024, "authors": "Smith J, Jones K", "abstract": None}])
        self.assertEqual(tidy[0]["pmid"], "123")
        self.assertEqual(tidy[0]["year"], "2024")
        self.assertEqual(tidy[0]["authors"], ["Smith J", "Jones K"])
        self.assertEqual(tidy[0]["abstract"], "")
        with self.assertRaises(ValueError):
            normalise_papers([{"title": "no id"}])
        with self.assertRaises(ValueError):
            normalise_papers([{"pmid": "1"}, {"pmid": "1"}])

    def test_score_matches_one_to_one(self):
        gold = [dict(GOLD[0], outcome="a"), dict(GOLD[0], outcome="b")]  # same paper, metric and value twice
        s = score.score(gold[:1], gold)
        self.assertEqual((s["found"], len(s["missed"])), (1, 1))

    def test_report_handles_mixed_year_types(self):
        papers = [dict(p, year=2021) if i == 0 else dict(p, year=None) for i, p in enumerate(PAPERS)]
        self.assertIn("## Hazard ratio", compare.build_report(GOLD, papers))
