import io
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import fitz

from backend.core.database import connect
from backend.services.ocr_service import save_review
from backend.services.pdf_service import BookMetadata, list_pages, register_pdf
from backend.services.retrieval_service import (
    SearchError,
    _automatic_terms,
    refresh_candidates,
    search,
    search_choice_evidence,
)


def register(data_dir, title, pages):
    document = fitz.open()
    for text in pages:
        page = document.new_page()
        if text:
            page.insert_text((72, 72), text)
    content = document.tobytes()
    document.close()
    book_id = register_pdf(io.BytesIO(content), title + ".pdf", BookMetadata(title, edition="2"), data_dir)
    return book_id, [page["id"] for page in list_pages(data_dir, book_id)]


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.book1, self.pages1 = register(self.data, "Book A", ["fracture test", ""])
        self.book2, self.pages2 = register(self.data, "Book B", ["other text"])

    def test_japanese_and_short_exact_search_and_book_filter(self):
        save_review(self.data, self.pages1[0], "大腿骨骨折の整復、骨折の分類", True)
        save_review(self.data, self.pages2[0], "上腕骨骨折の整復", False)
        response = search(self.data, "骨折")
        self.assertEqual(len(response.results), 2)
        self.assertEqual(response.results[0].score, 2)
        self.assertEqual((response.total_pages, response.unsearchable_pages), (3, 1))
        self.assertEqual(response.ocr_pending_pages, 1)
        self.assertEqual(response.results[0].review_status, "reviewed")
        self.assertEqual(response.results[0].book_id, self.book1)
        self.assertEqual(response.results[0].search_method, "keyword_literal")
        filtered = search(self.data, "骨折 整復", self.book2)
        self.assertEqual([result.title for result in filtered.results], ["Book B"])
        self.assertEqual(filtered.results[0].source, "corrected")
        self.assertEqual(filtered.results[0].pdf_page_number, 1)
        self.assertIsNone(filtered.results[0].printed_page_number)
        self.assertEqual(filtered.results[0].edition, "2")
        self.assertEqual(len(search(self.data, "大腿骨骨折").results), 1)
        self.assertEqual(search(self.data, "骨折 AND分類").results, [])

    def test_incorrect_combination_finds_page_with_only_one_choice_term(self):
        save_review(self.data, self.pages1[0], "大脳半球の運動性言語中枢は前頭葉にある。", True)
        found = search_choice_evidence(self.data, "大脳半球について誤っている組合せはどれか。",
                                       "運動性言語中枢 ― 横側頭回", self.book1)
        self.assertEqual(found.results[0].page_id, self.pages1[0])
        self.assertEqual(found.results[0].book_id, self.book1)
        self.assertIn("keyword", found.results[0].search_method)
        self.assertNotIn("横側頭回", found.results[0].text)
        self.assertEqual(search_choice_evidence(self.data, "大脳半球について誤っている組合せはどれか。",
                                               "運動性言語中枢 ― 横側頭回", self.book2).results, [])

    def test_choice_whole_terms_are_preserved_before_fragments(self):
        terms = _automatic_terms("".join(chr(0x4e00 + i) for i in range(50)) + " ― 横側頭回")
        self.assertIn("横側頭回", terms)

    def test_refreshes_after_corrections_and_ocr_without_leaking_old_text(self):
        old_candidate = search(self.data, "fracture").results[0]
        with closing(connect(self.data)) as connection, connection:
            connection.execute(
                "INSERT INTO ocr_results(page_id, text, language) VALUES (?, ?, ?)",
                (self.pages1[0], "新しい骨折", "jpn+eng"),
            )
            connection.execute("UPDATE pages SET ocr_processed=1 WHERE id=?", (self.pages1[0],))
        self.assertEqual(search(self.data, "fracture").results, [])
        self.assertEqual(search(self.data, "骨折").results[0].source, "ocr")
        with self.assertRaises(SearchError):
            refresh_candidates(self.data, [old_candidate])
        save_review(self.data, self.pages1[0], "修正済み脱臼", True)
        self.assertEqual(search(self.data, "骨折").results, [])
        result = search(self.data, "脱臼").results[0]
        self.assertTrue(result.ocr_processed)
        self.assertEqual(result.source, "corrected")
        self.assertEqual(result.review_status, "reviewed")
        save_review(self.data, self.pages1[0], "", False)
        self.assertEqual(search(self.data, "脱臼").results, [])
        self.assertEqual(search(self.data, "脱臼").unsearchable_pages, 2)

    def test_empty_ocr_does_not_hide_existing_pdf_text(self):
        with closing(connect(self.data)) as connection, connection:
            connection.execute(
                "INSERT INTO ocr_results(page_id, text, language) VALUES (?, '', 'eng')",
                (self.pages1[0],),
            )
        self.assertEqual(search(self.data, "fracture").results[0].source, "pdf")

    def test_chunk_overlap_and_special_characters(self):
        save_review(self.data, self.pages1[0], "あ" * 798 + '大腿骨骨折 "x" %_', False)
        self.assertEqual(len(search(self.data, "大腿骨骨折").results), 1)
        self.assertEqual(len(search(self.data, '"x"').results), 1)
        self.assertEqual(len(search(self.data, "%_").results), 1)
        self.assertEqual(search(self.data, "大腿骨骨折", self.book2).results, [])

    def test_invalid_queries_and_result_limit(self):
        for query in ("", "word " * 9, "a" * 161):
            with self.subTest(query=query), self.assertRaises(SearchError):
                search(self.data, query)
        save_review(self.data, self.pages1[0], "骨折" * 900, False)
        response = search(self.data, "骨折", limit=1)
        self.assertEqual(len(response.results), 1)
        self.assertTrue(response.truncated)

    def test_deleted_book_index_is_removed(self):
        search(self.data, "fracture")
        with closing(connect(self.data)) as connection, connection:
            connection.execute("DELETE FROM books WHERE id=?", (self.book1,))
        self.assertEqual(search(self.data, "fracture").results, [])
        with closing(connect(self.data)) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM search_fts WHERE rowid NOT IN (SELECT id FROM search_chunks)"
            ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
