import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import fitz
from openpyxl import load_workbook

from backend.core.config import Settings
from backend.core.database import connect, initialize
from backend.services.exam_service import (
    ChoiceExplanation, ExamError, auto_generate_question, comparison, create_question, edit_explanation, export_excel,
    generate_choice, get_choice_evidence, get_question, list_questions, save_choice_evidence,
    save_explanation, set_explanation_review,
)
from backend.services.pdf_service import BookMetadata, list_pages, register_pdf
from backend.services.ocr_service import save_review
from backend.services.retrieval_service import search


class ExamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.settings = Settings(self.data, self.data, "http://127.0.0.1:11434", "qwen2.5:3b", False)
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "Medical test")
        document.new_page().insert_text((72, 72), "Alternate source")
        content = document.tobytes()
        document.close()
        book = register_pdf(io.BytesIO(content), "test.pdf", BookMetadata("Book", edition="2"), self.data)
        page_ids = [page["id"] for page in list_pages(self.data, book)]
        save_review(self.data, page_ids[0], "The documented result is blue and clearly stated.", True)
        save_review(self.data, page_ids[1], "An unrelated alternate source says red.", True)
        self.evidence = search(self.data, "documented").results[0]
        self.alternate_evidence = search(self.data, "alternate").results[0]
        create_question(self.data, "Q-1", "誤っているのはどれか。", ["A", "B", "C", "D"],
                        2, 2024, "科目", "分野", "incorrect")

    def confirm(self, choice_id, *items, status="confirmed", reviewed="reviewed"):
        save_choice_evidence(
            self.data, choice_id, list(items), status, reviewed
        )

    def confirm_question(self, question_id, *items):
        _, rows, _ = get_question(self.data, question_id)
        for row in rows:
            self.confirm(row["id"], *(items or (self.evidence,)))

    def generated_response(self, payload, verdict="false", reason="理由です。", quote="documented result is blue"):
        if not payload.get("prompt"):
            return {}
        data = json.loads(payload["prompt"].rsplit("\n", 1)[-1])
        evidence_id = data["confirmed_evidence"][0]["id"]
        return {
            "done": True,
            "response": json.dumps({
                "verdict": verdict, "reason": reason,
                "evidence_id": evidence_id, "quote": quote,
            }, ensure_ascii=False),
        }

    def test_create_and_validate(self):
        self.assertEqual(len(list_questions(self.data)), 1)

    def test_existing_database_migration_is_additive(self):
        other = self.data / "old"
        other.mkdir()
        with sqlite3.connect(str(other / "mke.sqlite3")) as connection:
            connection.execute(
                """CREATE TABLE questions(id TEXT PRIMARY KEY, question_text TEXT NOT NULL,
                   answer_number INTEGER NOT NULL, year INTEGER, subject TEXT NOT NULL DEFAULT '',
                   field TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
            )
            connection.execute("INSERT INTO questions(id,question_text,answer_number) VALUES ('old','test',1)")
        initialize(other)
        with sqlite3.connect(str(other / "mke.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT id, question_kind FROM questions").fetchone(), ("old", None))
        for choices in (["a"] * 3, ["a", "", "c", "d"]):
            with self.assertRaises(ExamError):
                create_question(self.data, "new", "問題", choices, 1, None, "", "", "correct")
        with self.assertRaises(ExamError):
            create_question(self.data, "Q-1", "問題", ["a"] * 4, 1, None, "", "", "correct")
        self.assertEqual(len(list_questions(self.data)), 1)

    def test_generation_verifies_citations_and_does_not_send_answer_key(self):
        _, choices, _ = get_question(self.data, "Q-1")
        self.confirm(choices[0]["id"], self.evidence)
        result = {"done": True, "response": json.dumps({
            "verdict": "false", "reason": "資料の説明と異なるため誤りです。",
            "evidence_id": 1, "quote": "documented result is blue"
        }, ensure_ascii=False)}
        with patch("backend.services.exam_service._generate", return_value=result) as generate:
            answer = generate_choice(self.settings, "Q-1", choices[0]["id"])
        self.assertEqual(answer.verdict, "matches")
        self.assertIn("選択肢の記述そのもの", generate.call_args.args[1]["prompt"])
        self.assertNotIn("模範解答", generate.call_args.args[1]["prompt"])
        for reference, quote in ((1, "invented quote"), (2, "documented result is blue")):
            result["response"] = json.dumps({"verdict": "false", "reason": "理由",
                                             "evidence_id": reference, "quote": quote})
            with patch("backend.services.exam_service._generate", return_value=result), self.assertRaises(ExamError):
                generate_choice(self.settings, "Q-1", choices[0]["id"], overwrite=True)
        _, updated, _ = get_question(self.data, "Q-1")
        self.assertEqual(updated[0]["verdict"], "matches")
        self.assertEqual(updated[0]["last_attempt_citation_status"], "mismatch")

    def test_abstention_no_calls_unknown_and_invalid_response(self):
        _, choices, _ = get_question(self.data, "Q-1")
        with patch("backend.services.exam_service._generate") as generate:
            with self.assertRaises(ExamError):
                generate_choice(self.settings, "Q-1", choices[0]["id"])
            generate.assert_not_called()
        self.confirm(choices[0]["id"], self.evidence)
        with patch("backend.services.exam_service._generate", return_value={
            "done": True, "response": '{"verdict":"unknown","reason":"推測"}'
        }):
            self.assertEqual(
                generate_choice(self.settings, "Q-1", choices[0]["id"]).verdict, "unknown"
            )
        for result in ({"done": False}, {"done": True, "response": "bad"},
                       {"done": True, "response": '{"verdict":"yes"}'}):
            with patch("backend.services.exam_service._generate", return_value=result), self.assertRaises(ExamError):
                generate_choice(self.settings, "Q-1", choices[0]["id"], overwrite=True)

    def test_correct_incorrect_stem_mapping(self):
        _, choices, _ = get_question(self.data, "Q-1")
        self.confirm(choices[0]["id"], self.evidence)
        for factual, kind, expected in (("true", "correct", "matches"), ("true", "incorrect", "not_matches"),
                                        ("false", "correct", "not_matches"), ("false", "incorrect", "matches")):
            with closing(connect(self.data)) as connection, connection:
                connection.execute(
                    "UPDATE questions SET question_kind=? WHERE id='Q-1'", (kind,)
                )
            response = {"done": True, "response": json.dumps({"verdict": factual, "reason": "理由",
                       "evidence_id": 1, "quote": "documented result is blue"})}
            with self.subTest(factual=factual, kind=kind), patch("backend.services.exam_service._generate", return_value=response):
                self.assertEqual(
                    generate_choice(self.settings, "Q-1", choices[0]["id"], overwrite=True).verdict,
                    expected,
                )

    def test_absence_from_source_does_not_prove_choice_false(self):
        _, choices, _ = get_question(self.data, "Q-1")
        self.confirm(choices[0]["id"], self.evidence)
        response = {"done": True, "response": json.dumps({
            "verdict": "false", "reason": "資料に必ず手術が必要とは記載されていない。",
            "evidence_id": 1, "quote": "documented result is blue"
        }, ensure_ascii=False)}
        with patch("backend.services.exam_service._generate", return_value=response):
            result = generate_choice(
                self.settings, "Q-1", choices[0]["id"], overwrite=True
            )
        self.assertEqual(result.verdict, "unknown")
        self.assertEqual(result.citation_status, "match")

    def test_auto_does_not_save_definitive_verdict_if_reason_is_uncertain(self):
        create_question(self.data, "uncertain", "骨折について正しいのはどれか。",
                        ["骨折は骨の連続性が損なわれる。"] * 4, 1, None, "", "", "correct")
        self.confirm_question("uncertain", self.evidence)
        with patch(
            "backend.services.exam_service._generate",
            side_effect=lambda *args: self.generated_response(
                args[1], reason="選択肢と一致しませんが、正しいかどうかは不明です。"
            ),
        ):
            count, _ = auto_generate_question(self.settings, "uncertain")
        self.assertEqual(count, 4)
        self.assertTrue(all(row["verdict"] == "unknown" for row in get_question(self.data, "uncertain")[1]))

    def test_saved_verdicts_comparison_edit_and_excel(self):
        question, choices, _ = get_question(self.data, "Q-1")
        self.assertIn("要確認", comparison(question["answer_number"], choices))
        for c in choices:
            self.confirm(c["id"], self.evidence)
            verdict = "matches" if c["number"] == 2 else "not_matches"
            save_explanation(self.data, c["id"], ChoiceExplanation(
                verdict, "学生向けの説明", self.evidence, "documented result is blue",
                citation_status="match"))
            set_explanation_review(self.data, c["id"], True)
        question, choices, evidence = get_question(self.data, "Q-1")
        self.assertIn("一致", comparison(question["answer_number"], choices))
        self.assertEqual(len(evidence), 4)
        edit_explanation(self.data, choices[0]["id"], "=1+1")
        data = export_excel(self.data, "Q-1")
        sheet = load_workbook(io.BytesIO(data), data_only=False).active
        self.assertEqual(sheet["D11"].value, "=1+1")
        self.assertEqual(sheet["D11"].data_type, "s")
        self.assertEqual(sheet["F11"].value, "Book")
        self.assertEqual(sheet["H11"].value, 1)
        self.assertEqual(sheet["J11"].value, "documented result is blue")
        self.assertEqual(sheet["M11"].value, "confirmed")
        self.assertEqual(sheet["N11"].value, "reviewed")
        self.assertEqual(sheet["O11"].value, "match")
        self.assertEqual(sheet["P11"].value, "unreviewed")
        save_explanation(self.data, choices[0]["id"], ChoiceExplanation("unknown", "根拠不足"))
        _, updated, refs = get_question(self.data, "Q-1")
        self.assertEqual(len(refs), 3)
        self.assertIn("要確認", comparison(2, updated))
        save_explanation(self.data, choices[0]["id"], ChoiceExplanation(
            "matches", "説明", self.evidence, "documented result is blue", citation_status="match"))
        set_explanation_review(self.data, choices[0]["id"], True)
        _, updated, _ = get_question(self.data, "Q-1")
        self.assertIn("一致しません", comparison(2, updated))

    def test_invalid_saved_evidence_is_rejected(self):
        _, choices, _ = get_question(self.data, "Q-1")
        with self.assertRaises(ExamError):
            save_explanation(self.data, choices[0]["id"], ChoiceExplanation(
                "matches", "説明", citation_status="match"))
        with self.assertRaises(ExamError):
            save_explanation(self.data, choices[0]["id"], ChoiceExplanation(
                "matches", "説明", self.evidence, "documented result is blue",
                citation_status="match"))
        self.confirm(choices[0]["id"], self.evidence)
        with self.assertRaises(ExamError):
            save_explanation(self.data, choices[0]["id"], ChoiceExplanation(
                "matches", "説明", self.evidence, "invented quote", citation_status="match"))

    def test_auto_generates_four_choices_and_preserves_existing_edits(self):
        create_question(self.data, "auto", "骨折について正しいのはどれか。",
                        ["骨折は骨の連続性が損なわれる。", "骨折は骨の連続性が保たれる。",
                         "骨折では関節のみが損傷する。", "骨折は必ず手術が必要。"],
                        1, None, "", "", "correct")
        _, rows, _ = get_question(self.data, "auto")
        self.confirm_question("auto", self.evidence)
        save_explanation(self.data, rows[0]["id"], ChoiceExplanation("unknown", "既存の手作業"))
        edit_explanation(self.data, rows[0]["id"], "手動で修正した解説")
        with patch(
            "backend.services.exam_service._generate",
            side_effect=lambda *args: self.generated_response(
                args[1], reason="資料の定義と異なります。"
            ),
        ) as generate:
            count, _ = auto_generate_question(self.settings, "auto")
        self.assertEqual(count, 3)
        self.assertEqual(generate.call_count, 4)
        self.assertEqual(generate.call_args_list[-1].args[1]["keep_alive"], 0)
        _, updated, evidence = get_question(self.data, "auto")
        self.assertEqual(updated[0]["explanation"], "手動で修正した解説")
        self.assertTrue(updated[0]["edited"])
        self.assertEqual(len(evidence), 3)
        self.assertEqual([row["verdict"] for row in updated], ["unknown", "not_matches", "not_matches", "not_matches"])
        self.assertIn("要確認", comparison(1, updated))
        with self.assertRaises(ExamError):
            auto_generate_question(self.settings, "auto")
        with patch(
            "backend.services.exam_service._generate",
            side_effect=lambda *args: self.generated_response(
                args[1], reason="資料の定義と異なります。"
            ),
        ):
            count, _ = auto_generate_question(self.settings, "auto", regenerate=True)
        self.assertEqual(count, 4)
        _, replaced, _ = get_question(self.data, "auto")
        self.assertEqual(replaced[0]["explanation"], "資料の定義と異なります。")
        self.assertFalse(replaced[0]["edited"])

    def test_auto_no_evidence_and_unreliable_output_are_explicit(self):
        create_question(self.data, "empty", "説明がない事項", ["ab", "cd", "ef", "gh"],
                        1, None, "", "", "correct")
        with patch("backend.services.exam_service._generate") as generate:
            count, warnings = auto_generate_question(self.settings, "empty")
        self.assertEqual(count, 0)
        generate.assert_not_called()
        self.assertEqual(len(warnings), 4)
        self.assertTrue(all(row["verdict"] is None for row in get_question(self.data, "empty")[1]))

    def test_batch_generation_requires_confirmed_reviewed_evidence(self):
        create_question(self.data, "blocked", "問題", ["A", "B", "C", "D"],
                        1, None, "", "", "correct")
        _, rows, _ = get_question(self.data, "blocked")
        self.confirm(rows[0]["id"], self.evidence, status="confirmed", reviewed="unreviewed")
        self.confirm(rows[1]["id"], status="insufficient")
        self.confirm(rows[2]["id"], status="contradictory")
        with patch("backend.services.exam_service._generate") as generate:
            count, warnings = auto_generate_question(self.settings, "blocked")
            with self.assertRaises(ExamError):
                generate_choice(self.settings, "blocked", rows[0]["id"])
        self.assertEqual(count, 0)
        self.assertEqual(len(warnings), 4)
        generate.assert_not_called()
        self.assertEqual(get_choice_evidence(self.data, rows[1]["id"])[0], "insufficient")
        self.assertEqual(get_choice_evidence(self.data, rows[2]["id"])[0], "contradictory")

    def test_only_saved_evidence_is_sent_and_shared_generation_checks_quotes(self):
        _, rows, _ = get_question(self.data, "Q-1")
        self.confirm(rows[0]["id"], self.evidence)
        result = {"done": True, "response": json.dumps({
            "verdict": "true", "reason": "資料の記載と一致します。",
            "evidence_id": 1, "quote": "documented result is blue",
        })}
        with patch("backend.services.exam_service._generate", return_value=result) as generate:
            generate_choice(self.settings, "Q-1", rows[0]["id"])
        prompt = generate.call_args.args[1]["prompt"]
        self.assertIn(self.evidence.text, prompt)
        self.assertNotIn(self.alternate_evidence.text, prompt)
        _, batch_rows, _ = get_question(self.data, "Q-1")
        self.confirm(batch_rows[1]["id"], self.evidence)
        with patch(
            "backend.services.exam_service._generate",
            side_effect=lambda *args: self.generated_response(
                args[1], verdict="true", quote="wrong source quote"
            ),
        ):
            count, warnings = auto_generate_question(self.settings, "Q-1")
            self.assertEqual(count, 0)
            self.assertTrue(warnings)
        _, updated, _ = get_question(self.data, "Q-1")
        self.assertIsNone(updated[1]["verdict"])
        self.assertEqual(updated[1]["last_attempt_citation_status"], "mismatch")

    def test_additive_migration_preserves_existing_medical_data_and_explanations(self):
        other = self.data / "legacy"
        other.mkdir()
        path = other / "mke.sqlite3"
        with sqlite3.connect(str(path)) as connection:
            connection.executescript(
                """
                CREATE TABLE books(id TEXT PRIMARY KEY, title TEXT NOT NULL, edition TEXT NOT NULL);
                CREATE TABLE pages(id INTEGER PRIMARY KEY, book_id TEXT, pdf_page_number INTEGER,
                    printed_page_number TEXT, extracted_text TEXT, review_status TEXT, ocr_processed INTEGER);
                CREATE TABLE ocr_results(id INTEGER PRIMARY KEY, page_id INTEGER, text TEXT);
                CREATE TABLE page_corrections(page_id INTEGER PRIMARY KEY, corrected_text TEXT);
                CREATE TABLE questions(id TEXT PRIMARY KEY, question_text TEXT NOT NULL,
                    answer_number INTEGER NOT NULL, year INTEGER, subject TEXT NOT NULL DEFAULT '',
                    field TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE choices(id INTEGER PRIMARY KEY, question_id TEXT, number INTEGER, text TEXT);
                CREATE TABLE explanations(id INTEGER PRIMARY KEY, choice_id INTEGER, verdict TEXT,
                    text TEXT, edited INTEGER DEFAULT 0, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE evidence(id INTEGER PRIMARY KEY, explanation_id INTEGER, page_id INTEGER,
                    book_title TEXT, edition TEXT, pdf_page_number INTEGER, printed_page_number TEXT,
                    source TEXT, review_status TEXT, quote TEXT, context TEXT);
                INSERT INTO books VALUES ('b', 'Book', '2');
                INSERT INTO pages VALUES (7, 'b', 1, '12', 'PDF body', 'reviewed', 1);
                INSERT INTO ocr_results VALUES (3, 7, 'OCR history');
                INSERT INTO page_corrections VALUES (7, 'Manual correction');
                INSERT INTO questions(id, question_text, answer_number) VALUES ('q', 'Question', 1);
                INSERT INTO choices VALUES (9, 'q', 1, 'Choice');
                INSERT INTO explanations(id, choice_id, verdict, text, edited) VALUES
                    (11, 9, 'matches', 'Existing explanation', 1);
                INSERT INTO evidence VALUES
                    (13, 11, 7, 'Book', '2', 1, '12', 'corrected', 'reviewed', 'Quote', 'Snapshot');
                """
            )
        initialize(other)
        with sqlite3.connect(str(path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM books").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT extracted_text FROM pages WHERE id=7").fetchone()[0], "PDF body")
            self.assertEqual(connection.execute("SELECT text FROM ocr_results WHERE id=3").fetchone()[0], "OCR history")
            self.assertEqual(connection.execute("SELECT corrected_text FROM page_corrections WHERE page_id=7").fetchone()[0], "Manual correction")
            self.assertEqual(connection.execute("SELECT text FROM explanations WHERE id=11").fetchone()[0], "Existing explanation")
            self.assertEqual(connection.execute("SELECT context FROM evidence WHERE id=13").fetchone()[0], "Snapshot")
            self.assertEqual(
                connection.execute("SELECT citation_status, human_review_status FROM explanations WHERE id=11").fetchone(),
                ("unperformed", "unreviewed"),
            )


if __name__ == "__main__":
    unittest.main()
