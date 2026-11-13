import hashlib
import os
import sqlite3
import tempfile
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, List, Optional

import fitz

from backend.core.database import connect, initialize


class PDFRegistrationError(ValueError):
    pass


@dataclass(frozen=True)
class BookMetadata:
    title: str
    author: str = ""
    publisher: str = ""
    edition: str = ""
    publication_year: Optional[int] = None


def classify_pdf(has_text: bool, has_images: bool) -> str:
    if has_text and has_images:
        return "mixed"
    if has_text:
        return "text"
    if has_images:
        return "scan"
    return "undetermined"


def register_pdf(
    source: BinaryIO, filename: str, metadata: BookMetadata, data_dir: Path
) -> str:
    title = metadata.title.strip()
    if not title:
        raise PDFRegistrationError("書籍名を入力してください。")
    if metadata.publication_year is not None and not 1 <= metadata.publication_year <= 9999:
        raise PDFRegistrationError("発行年は1〜9999の範囲で入力してください。")
    if Path(filename).suffix.lower() != ".pdf":
        raise PDFRegistrationError("PDFファイルを選択してください。")

    initialize(data_dir)
    pdf_dir = data_dir / "original_pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    book_id = uuid.uuid4().hex
    final_path = pdf_dir / (book_id + ".pdf")
    digest = hashlib.sha256()
    temp_path = None
    committed = False
    final_created = False
    try:
        with tempfile.NamedTemporaryFile(dir=str(pdf_dir), suffix=".tmp", delete=False) as temp:
            temp_path = Path(temp.name)
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
                temp.write(block)

        try:
            document = fitz.open(str(temp_path), filetype="pdf")
        except (fitz.FileDataError, fitz.EmptyFileError) as error:
            raise PDFRegistrationError("PDFを開けません。ファイルが破損していないか確認してください。") from error

        with document:
            if document.needs_pass:
                raise PDFRegistrationError("パスワード保護されたPDFには対応していません。")
            if document.page_count == 0:
                raise PDFRegistrationError("ページのないPDFは登録できません。")
            with closing(connect(data_dir)) as connection, connection:
                if connection.execute(
                    "SELECT 1 FROM books WHERE sha256 = ?", (digest.hexdigest(),)
                ).fetchone():
                    raise PDFRegistrationError("同じPDFはすでに登録されています。")
                connection.execute(
                    """INSERT INTO books
                       (id, title, author, publisher, edition, publication_year,
                        original_filename, pdf_filename, sha256, page_count, pdf_type)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'undetermined')""",
                    (
                        book_id, title, metadata.author.strip(), metadata.publisher.strip(),
                        metadata.edition.strip(), metadata.publication_year, Path(filename).name,
                        final_path.name, digest.hexdigest(), document.page_count,
                    ),
                )
                any_text = False
                any_images = False
                for page_index in range(document.page_count):
                    page = document.load_page(page_index)
                    text = page.get_text("text")
                    has_text = bool(text.strip())
                    # get_image_info finds displayed images, including inline images.
                    has_images = bool(page.get_image_info())
                    any_text = any_text or has_text
                    any_images = any_images or has_images
                    connection.execute(
                        """INSERT INTO pages
                           (book_id, pdf_page_number, extracted_text, has_text, has_images, ocr_needed)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (book_id, page_index + 1, text, int(has_text), int(has_images),
                         int(has_images or not has_text)),
                    )
                connection.execute(
                    "UPDATE books SET pdf_type = ? WHERE id = ?",
                    (classify_pdf(any_text, any_images), book_id),
                )
                os.replace(str(temp_path), str(final_path))
                final_created = True
            committed = True
        return book_id
    except sqlite3.IntegrityError as error:
        if "books.sha256" in str(error):
            raise PDFRegistrationError("同じPDFはすでに登録されています。") from error
        raise
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        if final_created and not committed:
            final_path.unlink()


def list_books(data_dir: Path) -> List[sqlite3.Row]:
    initialize(data_dir)
    with closing(connect(data_dir)) as connection:
        return connection.execute(
            """SELECT books.*, SUM(pages.ocr_needed) AS ocr_needed_count
               FROM books LEFT JOIN pages ON pages.book_id = books.id
               GROUP BY books.id ORDER BY books.created_at DESC, books.rowid DESC"""
        ).fetchall()


def list_pages(data_dir: Path, book_id: str) -> List[sqlite3.Row]:
    initialize(data_dir)
    with closing(connect(data_dir)) as connection:
        return connection.execute(
            "SELECT * FROM pages WHERE book_id = ? ORDER BY pdf_page_number", (book_id,)
        ).fetchall()
