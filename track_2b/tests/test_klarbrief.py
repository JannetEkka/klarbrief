"""Unit tests (standard library only): python -m unittest discover -s tests"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from klarbrief import evaluate as E  # noqa: E402
from klarbrief import langcheck as LC  # noqa: E402
from klarbrief.explain import explain  # noqa: E402
from klarbrief.llm import Reply  # noqa: E402
from klarbrief import parse  # noqa: E402
from klarbrief import verify as V  # noqa: E402
from klarbrief.explain import from_model_text, parse_json  # noqa: E402

LETTER = ("Gemäss unseren Unterlagen ist die Hundesteuer 2026 von CHF 120.00 für Ihren Hund «Luna» noch offen.\n"
          "Bitte begleichen Sie den Betrag bis zum 30. November 2026. Andernfalls wird eine\n"
          "Mahngebühr von CHF 20.00 erhoben.")


class ParseTests(unittest.TestCase):
    def test_dates_all_languages(self):
        text = "bis zum 30. November 2026, le 1er décembre 2026, il 1° gennaio 2027, 15.01.2027, 2027-02-03"
        self.assertEqual([d.isoformat() for d in parse.find_dates(text)],
                         ["2026-11-30", "2026-12-01", "2027-01-01", "2027-01-15", "2027-02-03"])

    def test_invalid_date_ignored(self):
        self.assertEqual(parse.find_dates("31.02.2026"), [])

    def test_amounts_swiss_formats(self):
        text = "CHF 1'250.50, Fr. 85.–, CHF 1’680.00, 120.00 Franken, CHF 40, 4,5 %"
        self.assertEqual(parse.find_amounts(text), [1250.5, 85.0, 1680.0, 120.0, 40.0])

    def test_letter_date_only_from_dateline(self):
        self.assertEqual(parse.letter_date("Gemeinde X\n\nBeispielwil, 2. Oktober 2026\n\nFrau Y").isoformat(), "2026-10-02")
        self.assertEqual(parse.letter_date("Rechnungsdatum: 1. Oktober 2026\nBetrag").isoformat(), "2026-10-01")
        self.assertEqual(parse.letter_date("Lacfictif, le 9 octobre 2026\n").isoformat(), "2026-10-09")
        # undated header: a date in the body must not be taken as the letter date
        self.assertIsNone(parse.letter_date("Beispielwil, im Oktober 2026\n\nAb dem 6. Januar 2027 neu."))

    def test_to_amount(self):
        self.assertEqual(parse.to_amount("CHF 1'320.00"), 1320.0)
        self.assertEqual(parse.to_amount(95), 95.0)
        self.assertIsNone(parse.to_amount(True))


class VerifyTests(unittest.TestCase):
    def _one(self, **item):
        kind = item.pop("_kind")
        return V.verify(V.points_from_model({kind: [item]}), LETTER)[0]

    def test_exact_quote_is_shown_with_letter_words(self):
        p = self._one(_kind="actions", text="pay", quote="Bitte  begleichen Sie den Betrag")
        self.assertTrue(p.verified)
        self.assertEqual(p.match, "exact")
        self.assertEqual(p.evidence, "Bitte begleichen Sie den Betrag")

    def test_quote_across_line_break_and_case(self):
        p = self._one(_kind="consequences", text="fee", quote="andernfalls wird eine Mahngebühr")
        self.assertTrue(p.verified)

    def test_invented_quote_rejected(self):
        p = self._one(_kind="actions", text="pay now", quote="Bitte überweisen Sie sofort")
        self.assertFalse(p.verified)
        self.assertEqual(p.reason, "quote not found in the letter")

    def test_near_match_accepted_and_shows_letter_text(self):
        p = self._one(_kind="actions", text="pay", quote="Bitte begleichen Sie den Betrag bis zum 30. Nov. 2026")
        self.assertTrue(p.verified, p.reason)
        self.assertEqual(p.match, "near")
        self.assertIn("30. November 2026", p.evidence)

    def test_deadline_date_must_be_in_quoted_words(self):
        ok = self._one(_kind="deadlines", date="2026-11-30", text="pay", quote="bis zum 30. November 2026")
        bad = self._one(_kind="deadlines", date="2026-12-30", text="pay", quote="bis zum 30. November 2026")
        self.assertTrue(ok.verified)
        self.assertFalse(bad.verified)
        self.assertIn("2026-12-30", bad.reason)

    def test_relative_deadline_without_date_is_kept(self):
        letter = "Bitte bezahlen Sie die Busse innert 30 Tagen."
        p = V.verify(V.points_from_model({"deadlines": [{"date": None, "text": "30 days", "quote": "innert 30 Tagen"}]}), letter)[0]
        self.assertTrue(p.verified)
        self.assertIsNone(p.date)

    def test_missing_date_is_read_from_the_quoted_letter_words(self):
        letter = "Lacfictif, le 20 octobre 2026\n\nNous vous convoquons le mercredi 4 novembre 2026 à 9h30."
        p = V.verify(V.points_from_model({"deadlines": [
            {"date": None, "text": "appointment", "quote": "le mercredi 4 novembre 2026 à 9h30"}]}), letter)[0]
        self.assertTrue(p.verified)
        self.assertEqual(p.date, "2026-11-04")
        self.assertTrue(p.date_from_letter)

    def test_amount_must_be_in_quoted_words(self):
        ok = self._one(_kind="amounts", chf=120, text="tax", quote="Hundesteuer 2026 von CHF 120.00")
        bad = self._one(_kind="amounts", chf=210, text="tax", quote="Hundesteuer 2026 von CHF 120.00")
        self.assertTrue(ok.verified)
        self.assertFalse(bad.verified)

    def test_closing_guillemet_belonging_to_letter_survives(self):
        p = self._one(_kind="amounts", chf=120, text="tax", quote="CHF 120.00 für Ihren Hund «Luna»")
        self.assertTrue(p.verified)
        self.assertEqual(p.evidence, "CHF 120.00 für Ihren Hund «Luna»")

    def test_wrapping_quotes_removed(self):
        p = self._one(_kind="actions", text="pay", quote="“Bitte begleichen Sie den Betrag”")
        self.assertTrue(p.verified)

    def test_expired_date_is_not_a_deadline(self):
        letter = ("Musterstadt, 12. Oktober 2026\n\nDie Frist ist am 31. März 2026 abgelaufen. "
                  "Wir gewähren eine Nachfrist bis zum 30. November 2026.")
        pts = V.verify(V.points_from_model({"deadlines": [
            {"date": "2026-03-31", "text": "old", "quote": "am 31. März 2026 abgelaufen"},
            {"date": "2026-11-30", "text": "new", "quote": "Nachfrist bis zum 30. November 2026"}]}), letter)
        self.assertFalse(pts[0].verified)
        self.assertIn("already passed", pts[0].reason)
        self.assertTrue(pts[1].verified)

    def test_paraphrased_opening_matches_partly_on_letter_words(self):
        letter = "Bei Fragen erreichen Sie uns unter 044 999 12 34 oder steueramt@beispielwil.ch."
        p = V.verify(V.points_from_model({"contact": [{"text": "call", "quote":
            "Falls Sie Fragen haben, erreichen Sie uns unter 044 999 12 34 oder steueramt@beispielwil.ch."}]}), letter)[0]
        self.assertTrue(p.verified, p.reason)
        self.assertEqual(p.match, "partial")
        self.assertTrue(p.evidence.startswith("erreichen Sie uns unter 044 999 12 34"))

    def test_mostly_invented_quote_is_not_rescued_by_a_short_overlap(self):
        letter = "Bei Fragen erreichen Sie uns unter 044 999 12 34."
        p = V.verify(V.points_from_model({"actions": [{"text": "x", "quote":
            "Sie müssen sofort persönlich am Schalter erscheinen und erreichen Sie uns"}]}), letter)[0]
        self.assertFalse(p.verified)

    def test_partial_match_still_needs_the_amount_in_the_words(self):
        letter = "Die Gebühr beträgt CHF 95.00 und wird Ihnen nach Abschluss des Verfahrens in Rechnung gestellt."
        p = V.verify(V.points_from_model({"amounts": [{"chf": 59, "text": "fee", "quote":
            "Kosten von CHF 59.00, die Ihnen nach Abschluss des Verfahrens in Rechnung gestellt"}]}), letter)[0]
        self.assertEqual(p.match, "partial")
        self.assertFalse(p.verified)
        self.assertIn("CHF 59.00", p.reason)

    def test_mixed_language_deadline_quote_is_found_by_its_date(self):
        letter = "Bitte begleichen Sie den Betrag bis zum 30. November 2026 mit dem beiliegenden Einzahlungsschein."
        p = V.verify(V.points_from_model({"deadlines": [{"date": "2026-11-30", "text": "pay",
            "quote": "bis zum 30. November 2026 with the enclosed deposit slip."}]}), letter)[0]
        self.assertTrue(p.verified, p.reason)
        self.assertIn("30. November 2026", p.evidence)

    def test_reworded_deadline_quote_needs_the_same_date(self):
        letter = "Il nuovo termine è il 31 gennaio 2027. Non verranno concesse ulteriori proroghe."
        ok = V.verify(V.points_from_model({"deadlines": [{"date": "2027-01-31", "text": "x", "quote": "entro il 31 gennaio 2027"}]}), letter)[0]
        bad = V.verify(V.points_from_model({"deadlines": [{"date": "2027-02-28", "text": "x", "quote": "entro il 28 febbraio 2027"}]}), letter)[0]
        self.assertTrue(ok.verified, ok.reason)
        self.assertFalse(bad.verified)

    def test_short_overlap_does_not_rescue_an_action(self):
        letter = "Il nuovo termine è il 31 gennaio 2027."
        p = V.verify(V.points_from_model({"actions": [{"text": "x", "quote": "entro il 31 gennaio 2027"}]}), letter)[0]
        self.assertFalse(p.verified)

    def test_too_short_quote_rejected(self):
        p = self._one(_kind="actions", text="x", quote="die")
        self.assertFalse(p.verified)


class ModelOutputTests(unittest.TestCase):
    def test_parse_json_with_chatter_and_fence(self):
        self.assertEqual(parse_json('Sure!\n```json\n{"a": 1}\n```'), {"a": 1})
        self.assertIsNone(parse_json("no json here"))

    def test_from_model_text_end_to_end(self):
        raw = json.dumps({"summary": "Dog tax", "letter_language": "de",
                          "deadlines": [{"date": "2026-11-30", "text": "pay", "quote": "bis zum 30. November 2026"}],
                          "amounts": [{"chf": "CHF 20.00", "text": "fee", "quote": "Mahngebühr von CHF 20.00"}],
                          "actions": "pay the tax"})
        expl, ok = from_model_text(raw, LETTER, "en")
        self.assertTrue(ok)
        self.assertEqual(len(expl.shown), 2)
        self.assertEqual(expl.unverified[0].reason, "no quote given")


class LanguageTests(unittest.TestCase):
    def test_detect_letter_languages(self):
        for l in E.load_letters():
            with self.subTest(l["id"]):
                self.assertEqual(LC.detect(l["text"]), l["lang"])

    def test_in_language(self):
        self.assertTrue(LC.in_language("Податкове управління нагадує про сплату", "uk"))
        self.assertFalse(LC.in_language("Die Stadtpolizei hat eine Busse erhoben und Sie müssen bis", "uk"))
        self.assertFalse(LC.in_language("Il comune ha inflitto una multa che deve essere pagata entro", "en"))
        self.assertTrue(LC.in_language("You must pay the fine by the date in the letter and it is", "en"))
        self.assertIsNone(LC.in_language("CHF 40.00", "en"))


class _FakeLLM:
    """Stands in for the model: returns the queued replies in order and records the calls."""

    def __init__(self, *texts):
        self.texts, self.calls = list(texts), []

    def chat(self, messages, **kw):
        self.calls.append(messages)
        return Reply(text=self.texts.pop(0), prompt_tokens=10, completion_tokens=5, seconds=1.0,
                     output_mode="json_schema")


class ExplainSeamTests(unittest.TestCase):
    LETTER = "Musterstadt, 1. Oktober 2026\n\nBitte bezahlen Sie CHF 40.00 bis zum 30. November 2026 und senden Sie das Formular."

    def _reply(self, summary, text):
        return json.dumps({"summary": summary, "letter_language": "de", "deadlines": [
            {"date": "2026-11-30", "text": text, "quote": "bis zum 30. November 2026"}]}, ensure_ascii=False)

    def test_wrong_language_is_retried_once_and_fixed(self):
        llm = _FakeLLM(self._reply("Sie müssen die Busse bezahlen und das Formular senden.", "bezahlen bis"),
                       self._reply("Ви повинні сплатити штраф і надіслати форму.", "сплатити до"))
        res = explain(self.LETTER, "uk", llm)
        self.assertTrue(res.retried)
        self.assertTrue(res.language_ok)
        self.assertEqual(len(llm.calls), 2)
        self.assertIn("Rewrite the same JSON", llm.calls[1][-1]["content"])
        self.assertEqual(res.explanation.shown[0].date, "2026-11-30")
        self.assertEqual(res.seconds, 2.0)

    def test_right_language_is_not_retried(self):
        llm = _FakeLLM(self._reply("You must pay the fine and send the form by the date.", "pay by"))
        res = explain(self.LETTER, "en", llm)
        self.assertFalse(res.retried)
        self.assertTrue(res.language_ok)
        self.assertEqual(len(llm.calls), 1)

    def test_still_wrong_after_retry_is_reported_not_hidden(self):
        wrong = self._reply("Sie müssen die Busse bezahlen und das Formular senden.", "bezahlen bis")
        res = explain(self.LETTER, "uk", _FakeLLM(wrong, wrong))
        self.assertTrue(res.retried)
        self.assertFalse(res.language_ok)
        self.assertEqual(len(res.explanation.shown), 1)   # the checked facts are still shown

    def test_prompt_restates_reader_language_after_the_letter(self):
        llm = _FakeLLM(self._reply("You must pay the fine and send the form by the date.", "pay by"))
        explain(self.LETTER, "en", llm)
        user = llm.calls[0][1]["content"]
        self.assertGreater(user.rindex("English"), user.index(">>>"))
        self.assertIn("The letter is in German", user)


class DatasetTests(unittest.TestCase):
    def test_gold_labels_agree_with_letter_text(self):
        for l in E.load_letters():
            with self.subTest(l["id"]):
                self.assertEqual(sorted(parse.find_amounts(l["text"])), sorted(l["gold"]["amounts"]))
                dates = {d.isoformat() for d in parse.find_dates(l["text"])}
                self.assertTrue(set(l["gold"]["deadlines"]) <= dates)

    def test_true_deadlines_follow_the_letter_date(self):
        for l in E.load_letters():
            written = parse.letter_date(l["text"])
            for d in l["gold"]["deadlines"]:
                with self.subTest(l["id"]):
                    self.assertTrue(written is None or d >= written.isoformat())

    def test_scoring_counts_invented_deadline(self):
        letter = next(l for l in E.load_letters() if l["id"] == "de-06")
        raw = json.dumps({"summary": "x", "deadlines": [
            {"date": "2027-01-06", "text": "new day", "quote": "Ab dem 6. Januar 2027"}]})
        s = E.score_records([{"letter_id": "de-06", "target_lang": "en", "raw_text": raw}])["summary"]
        self.assertEqual(s["invented_deadline_on_no_deadline_letters"], {"raw": 1, "shown": 1, "of": 1})
        self.assertEqual(s["wrong_dates_shown"]["misread_from_letter"], 1)


if __name__ == "__main__":
    unittest.main()
