import csv
import hashlib
import io
import shutil
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import fitz
from PIL import Image, ImageDraw, ImageFont

from backend.core.database import connect
from backend.services.ocr_service import (
    OCRError,
    _ocr_lock,
    _read_confidence,
    available_languages,
    get_review,
    render_preview,
    run_ocr,
    save_review,
)
from backend.services.pdf_service import BookMetadata, list_books, list_pages, register_pdf


class OCRServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name)
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 100), "MEDICAL TEST 123", fontsize=30)
        self.pdf = document.tobytes()
        document.close()
        book_id = register_pdf(io.BytesIO(self.pdf), "book.pdf", BookMetadata("Test"), self.data_dir)
        self.page_id = list_pages(self.data_dir, book_id)[0]["id"]
        self.pdf_path = self.data_dir / "original_pdfs" / list_books(self.data_dir)[0]["pdf_filename"]

    def test_real_ocr_and_manual_review_preserve_original_and_corrections(self):
        if shutil.which("tesseract") is None or "eng" not in available_languages(self.data_dir):
            self.skipTest("Tesseract English data not available")
        original_hash = hashlib.sha256(self.pdf_path.read_bytes()).hexdigest()
        self.assertTrue(render_preview(self.data_dir, self.page_id).startswith(b"\x89PNG"))
        run_ocr(self.data_dir, self.page_id, "eng")
        page, result, correction = get_review(self.data_dir, self.page_id)
        self.assertIn("MEDICAL", result["text"])
        self.assertIn("123", result["text"])
        self.assertEqual(page["ocr_processed"], 1)
        self.assertIsNotNone(result["confidence"])
        self.assertIsNone(correction)
        self.assertIn("MEDICAL TEST", page["extracted_text"])
        save_review(self.data_dir, self.page_id, "corrected 医学 123", True)
        self.assertEqual(get_review(self.data_dir, self.page_id)[0]["review_status"], "reviewed")
        run_ocr(self.data_dir, self.page_id, "eng")
        page, second_result, correction = get_review(self.data_dir, self.page_id)
        self.assertNotEqual(result["id"], second_result["id"])
        self.assertEqual(correction, "corrected 医学 123")
        self.assertEqual(page["review_status"], "unreviewed")
        self.assertEqual(hashlib.sha256(self.pdf_path.read_bytes()).hexdigest(), original_hash)
        self.assertEqual(list(self.data_dir.glob("mke-ocr-*")), [])

    def test_missing_language_and_failure_leave_data_unchanged(self):
        with patch("backend.services.ocr_service.available_languages", return_value=["eng"]):
            with self.assertRaisesRegex(OCRError, "jpn"):
                run_ocr(self.data_dir, self.page_id, "jpn+eng")
        page, result, correction = get_review(self.data_dir, self.page_id)
        self.assertEqual(page["ocr_processed"], 0)
        self.assertIsNone(result)
        self.assertIsNone(correction)

    def test_japanese_ocr_with_project_language_data(self):
        language_dir = Path(__file__).resolve().parents[1] / "storage" / "tessdata"
        font_path = Path("/System/Library/Fonts/Hiragino Sans GB.ttc")
        if not (language_dir / "jpn.traineddata").exists() or not font_path.exists():
            self.skipTest("Japanese OCR data or sample font not available")
        (self.data_dir / "tessdata").symlink_to(language_dir, target_is_directory=True)
        image = Image.new("RGB", (1600, 360), "white")
        font = ImageFont.truetype(str(font_path), 72)
        ImageDraw.Draw(image).text((40, 100), "日本語 医学 123 ABC", font=font, fill="black")
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        document = fitz.open()
        pdf_page = document.new_page(width=800, height=180)
        pdf_page.insert_image(pdf_page.rect, stream=buffer.getvalue())
        content = document.tobytes()
        document.close()
        book_id = register_pdf(io.BytesIO(content), "japanese.pdf", BookMetadata("Japanese"), self.data_dir)
        page_id = list_pages(self.data_dir, book_id)[0]["id"]
        run_ocr(self.data_dir, page_id, "jpn+eng")
        _, result, _ = get_review(self.data_dir, page_id)
        self.assertIn("日本語", result["text"])
        self.assertIn("123", result["text"])
        self.assertIsNotNone(result["confidence"])

    def test_missing_page_and_concurrent_lock(self):
        with self.assertRaises(OCRError):
            save_review(self.data_dir, -1, "test", True)
        with _ocr_lock(self.data_dir):
            with self.assertRaisesRegex(OCRError, "実行中"):
                with _ocr_lock(self.data_dir):
                    pass

    def test_existing_database_is_extended_without_changing_registered_pages(self):
        with closing(connect(self.data_dir)) as connection, connection:
            connection.execute("DROP TABLE page_corrections")
            connection.execute("DROP TABLE ocr_results")
        page, result, correction = get_review(self.data_dir, self.page_id)
        self.assertIn("MEDICAL TEST", page["extracted_text"])
        self.assertIsNone(result)
        self.assertIsNone(correction)
        save_review(self.data_dir, self.page_id, "Manual correction", True)
        self.assertEqual(get_review(self.data_dir, self.page_id)[2], "Manual correction")

    def test_confidence_ignores_negative_and_flags_low_scores(self):
        path = self.data_dir / "sample.tsv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["text", "conf"], delimiter="\t")
            writer.writeheader()
            writer.writerows([
                {"text": "医学", "conf": "40"}, {"text": "123", "conf": "90"},
                {"text": "", "conf": "-1"},
            ])
        confidence, suspects = _read_confidence(path)
        self.assertEqual(confidence, 65)
        self.assertEqual(suspects, [{"text": "医学", "confidence": 40}])


if __name__ == "__main__":
    unittest.main()
