import csv
import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import fitz

from backend.core.database import connect, initialize


class OCRError(ValueError):
    pass


def _page(data_dir: Path, page_id: int) -> sqlite3.Row:
    initialize(data_dir)
    with closing(connect(data_dir)) as connection:
        page = connection.execute(
            """SELECT pages.*, books.pdf_filename FROM pages
               JOIN books ON books.id = pages.book_id WHERE pages.id = ?""", (page_id,)
        ).fetchone()
    if page is None:
        raise OCRError("ページが見つかりません。")
    return page


def _render(data_dir: Path, page: sqlite3.Row, dpi: int, max_pixels: int) -> bytes:
    pdf_path = data_dir / "original_pdfs" / page["pdf_filename"]
    with fitz.open(str(pdf_path)) as document:
        pdf_page = document.load_page(page["pdf_page_number"] - 1)
        rect = pdf_page.rect
        scale = dpi / 72
        if rect.width * rect.height * scale * scale > max_pixels:
            raise OCRError("ページ画像が大きすぎます。安全のため処理を中止しました。")
        pixmap = pdf_page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        return pixmap.tobytes("png")


def render_preview(data_dir: Path, page_id: int) -> bytes:
    return _render(data_dir, _page(data_dir, page_id), dpi=100, max_pixels=12000000)


def _tessdata_args(data_dir: Path) -> List[str]:
    directory = data_dir / "tessdata"
    return ["--tessdata-dir", str(directory)] if directory.is_dir() else []


def available_languages(data_dir: Path) -> List[str]:
    if shutil.which("tesseract") is None:
        raise OCRError("Tesseractが見つかりません。インストールしてください。")
    try:
        result = subprocess.run(
            ["tesseract"] + _tessdata_args(data_dir) + ["--list-langs"],
            capture_output=True, text=True, timeout=15, check=True
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise OCRError("Tesseractの言語一覧を取得できません: {}".format(error)) from error
    return [line.strip() for line in result.stdout.splitlines()[1:] if line.strip()]


@contextmanager
def _ocr_lock(data_dir: Path) -> Iterator[None]:
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / ".ocr.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise OCRError("別のOCRまたはAI処理が実行中です。完了後に再試行してください。") from error
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _read_confidence(tsv_path: Path) -> Tuple[Optional[float], List[Dict[str, object]]]:
    scores = []
    suspects = []
    with tsv_path.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
            text = row.get("text", "").strip()
            if not text:
                continue
            try:
                confidence = float(row["conf"])
            except (KeyError, ValueError) as error:
                raise OCRError("OCR信頼度データが不正です。") from error
            if confidence < 0:
                continue
            scores.append(confidence)
            if confidence < 70:
                suspects.append({"text": text, "confidence": round(confidence, 1)})
    return (round(sum(scores) / len(scores), 1) if scores else None, suspects)


def run_ocr(data_dir: Path, page_id: int, language: str) -> None:
    allowed = {"jpn+eng", "jpn_vert+eng", "eng"}
    if language not in allowed:
        raise OCRError("サポートされていないOCR言語です。")
    missing = set(language.split("+")) - set(available_languages(data_dir))
    if missing:
        raise OCRError("OCR言語データがありません: {}。READMEのOCRセットアップを確認してください。".format(
            ", ".join(sorted(missing))
        ))
    with _ocr_lock(data_dir):
        page = _page(data_dir, page_id)
        image = _render(data_dir, page, dpi=200, max_pixels=25000000)
        with tempfile.TemporaryDirectory(prefix="mke-ocr-", dir=str(data_dir)) as directory:
            image_path = Path(directory) / "page.png"
            output = Path(directory) / "result"
            image_path.write_bytes(image)
            del image
            env = os.environ.copy()
            env["OMP_THREAD_LIMIT"] = "1"
            try:
                subprocess.run(
                    ["tesseract", str(image_path), str(output)] + _tessdata_args(data_dir)
                    + ["-l", language, "-c", "tessedit_create_txt=1",
                       "-c", "tessedit_create_tsv=1"],
                    capture_output=True, text=True, timeout=180, check=True, env=env,
                )
            except subprocess.CalledProcessError as error:
                raise OCRError("OCRに失敗しました: {}".format(error.stderr.strip())) from error
            except (OSError, subprocess.TimeoutExpired) as error:
                raise OCRError("OCRを実行できませんでした: {}".format(error)) from error
            try:
                text = output.with_suffix(".txt").read_text(encoding="utf-8")
                confidence, suspects = _read_confidence(output.with_suffix(".tsv"))
            except (OSError, UnicodeError) as error:
                raise OCRError("OCR結果を読み取れません: {}".format(error)) from error
        with closing(connect(data_dir)) as connection, connection:
            connection.execute(
                """INSERT INTO ocr_results (page_id, text, confidence, suspect_words, language)
                   VALUES (?, ?, ?, ?, ?)""",
                (page_id, text, confidence, json.dumps(suspects, ensure_ascii=False), language),
            )
            result = connection.execute(
                """UPDATE pages SET ocr_processed = 1, ocr_confidence = ?,
                   review_status = 'unreviewed', processed_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (confidence, page_id),
            )
            if result.rowcount != 1:
                raise OCRError("ページが見つかりません。OCR結果は保存されませんでした。")


def get_review(data_dir: Path, page_id: int) -> Tuple[sqlite3.Row, Optional[sqlite3.Row], Optional[str]]:
    page = _page(data_dir, page_id)
    with closing(connect(data_dir)) as connection:
        result = connection.execute(
            "SELECT * FROM ocr_results WHERE page_id = ? ORDER BY id DESC LIMIT 1", (page_id,)
        ).fetchone()
        correction = connection.execute(
            "SELECT corrected_text FROM page_corrections WHERE page_id = ?", (page_id,)
        ).fetchone()
    return page, result, correction["corrected_text"] if correction is not None else None


def save_review(data_dir: Path, page_id: int, text: str, reviewed: bool) -> None:
    initialize(data_dir)
    with closing(connect(data_dir)) as connection, connection:
        result = connection.execute(
            "UPDATE pages SET review_status = ? WHERE id = ?",
            ("reviewed" if reviewed else "unreviewed", page_id),
        )
        if result.rowcount != 1:
            raise OCRError("ページが見つかりません。")
        connection.execute(
            """INSERT INTO page_corrections(page_id, corrected_text) VALUES (?, ?)
               ON CONFLICT(page_id) DO UPDATE SET corrected_text = excluded.corrected_text,
               updated_at = CURRENT_TIMESTAMP""",
            (page_id, text),
        )
