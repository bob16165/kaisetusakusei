import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz
from PIL import Image

from backend.services.pdf_service import BookMetadata, PDFRegistrationError, list_books, list_pages, register_pdf


def make_pdf(page_types):
    image = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(image, format="PNG")
    document = fitz.open()
    for page_type in page_types:
        page = document.new_page()
        if "text" in page_type:
            page.insert_text((72, 72), "Medical text")
        if "image" in page_type:
            page.insert_image(fitz.Rect(72, 100, 200, 200), stream=image.getvalue())
    result = document.tobytes()
    document.close()
    return result


class PDFServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name)
        self.metadata = BookMetadata("  Test book  ", "Author", "Publisher", "2nd", 2024)

    def register(self, content, filename="book.pdf"):
        return register_pdf(io.BytesIO(content), filename, self.metadata, self.data_dir)

    def test_text_pdf_registers_metadata_pages_and_unchanged_original(self):
        content = make_pdf([{"text"}, {"text"}])
        book_id = self.register(content)
        book = list_books(self.data_dir)[0]
        pages = list_pages(self.data_dir, book_id)
        self.assertEqual((book["title"], book["edition"], book["publication_year"]),
                         ("Test book", "2nd", 2024))
        self.assertEqual((book["page_count"], book["pdf_type"], book["ocr_needed_count"]),
                         (2, "text", 0))
        self.assertEqual([page["pdf_page_number"] for page in pages], [1, 2])
        self.assertTrue(all("Medical text" in page["extracted_text"] for page in pages))
        self.assertTrue(all(page["printed_page_number"] is None for page in pages))
        self.assertTrue(all(not page["ocr_processed"] for page in pages))
        self.assertEqual((self.data_dir / "original_pdfs" / book["pdf_filename"]).read_bytes(), content)

    def test_scanned_mixed_and_blank_pages(self):
        book_id = self.register(make_pdf([{"image"}, {"text", "image"}, set()]))
        book = list_books(self.data_dir)[0]
        pages = list_pages(self.data_dir, book_id)
        self.assertEqual(book["pdf_type"], "mixed")
        self.assertEqual([(p["has_text"], p["has_images"], p["ocr_needed"]) for p in pages],
                         [(0, 1, 1), (1, 1, 1), (0, 0, 1)])
        self.assertEqual(book["ocr_needed_count"], 3)

    def test_scan_and_undetermined_classification(self):
        self.register(make_pdf([{"image"}]))
        self.register(make_pdf([set()]), "blank.pdf")
        self.assertEqual({book["pdf_type"] for book in list_books(self.data_dir)},
                         {"scan", "undetermined"})

    def test_invalid_inputs_and_duplicate_do_not_create_extra_records(self):
        content = make_pdf([{"text"}])
        self.register(content)
        for invalid, filename in ((content, "book.txt"), (b"not a pdf", "bad.pdf"),
                                  (b"", "empty.pdf"), (content, "duplicate.pdf")):
            with self.subTest(filename=filename), self.assertRaises(PDFRegistrationError):
                self.register(invalid, filename)
        self.assertEqual(len(list_books(self.data_dir)), 1)
        self.assertEqual(len(list((self.data_dir / "original_pdfs").iterdir())), 1)

    def test_encrypted_pdf_rejected(self):
        document = fitz.open()
        document.new_page()
        content = document.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256,
                                   user_pw="secret", owner_pw="owner")
        document.close()
        with self.assertRaisesRegex(PDFRegistrationError, "パスワード"):
            self.register(content)
        self.assertEqual(list_books(self.data_dir), [])
        self.assertEqual(list((self.data_dir / "original_pdfs").iterdir()), [])

    def test_failed_registration_rolls_back_database_and_original(self):
        with patch("backend.services.pdf_service.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                self.register(make_pdf([{"text"}]))
        self.assertEqual(list_books(self.data_dir), [])
        self.assertEqual(list((self.data_dir / "original_pdfs").iterdir()), [])

    def test_invalid_metadata_is_rejected(self):
        with self.assertRaises(PDFRegistrationError):
            register_pdf(io.BytesIO(b""), "test.pdf", BookMetadata("  "), self.data_dir)
        with self.assertRaises(PDFRegistrationError):
            register_pdf(io.BytesIO(b""), "test.pdf", BookMetadata("Test", publication_year=10000), self.data_dir)


if __name__ == "__main__":
    unittest.main()
