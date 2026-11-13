import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

from backend.services.ocr_service import OCRError
from backend.services.pdf_service import BookMetadata, list_pages, register_pdf
from backend.services.printed_page_service import printed_page_candidates


class PrintedPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)

    def register(self, label):
        document = fitz.open()
        page = document.new_page(width=400, height=600)
        if label:
            page.insert_text((25, 20), label)
            page.insert_text((85, 20), "6")
        page.insert_text((100, 200), "Body 123")
        data = document.tobytes()
        document.close()
        book = register_pdf(io.BytesIO(data), "test.pdf", BookMetadata("Book"), self.data)
        return list_pages(self.data, book)[0]["id"]

    def test_text_corner_ignores_chapter_and_body_numbers(self):
        page_id = self.register("224")
        with patch("backend.services.printed_page_service.subprocess.run") as ocr:
            self.assertEqual(printed_page_candidates(self.data, page_id), ["224"])
            ocr.assert_not_called()

    def test_ocr_corner_candidates_and_error(self):
        page_id = self.register("")
        with patch("backend.services.printed_page_service.subprocess.run", side_effect=[
            subprocess.CompletedProcess([], 0, stdout=b"123\n", stderr=b""),
            subprocess.CompletedProcess([], 0, stdout=b"", stderr=b""),
        ]):
            self.assertEqual(printed_page_candidates(self.data, page_id), ["123"])
        with patch("backend.services.printed_page_service.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("tesseract", 30)):
            with self.assertRaises(OCRError):
                printed_page_candidates(self.data, page_id)
