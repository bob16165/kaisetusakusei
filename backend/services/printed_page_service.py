import os
import re
import subprocess
from pathlib import Path
from typing import List

import fitz

from backend.services.ocr_service import OCRError, _ocr_lock, _page, _tessdata_args


def printed_page_candidates(data_dir: Path, page_id: int) -> List[str]:
    page = _page(data_dir, page_id)
    path = data_dir / "original_pdfs" / page["pdf_filename"]
    with fitz.open(str(path)) as document:
        pdf_page = document.load_page(page["pdf_page_number"] - 1)
        rect = pdf_page.rect
        corners = [
            fitz.Rect(rect.x0, rect.y0, rect.x0 + rect.width * 0.15, rect.y0 + rect.height * 0.1),
            fitz.Rect(rect.x1 - rect.width * 0.15, rect.y0, rect.x1, rect.y0 + rect.height * 0.1),
        ]
        candidates = []
        for word in pdf_page.get_text("words"):
            word_rect = fitz.Rect(word[:4])
            if any(corner.contains(word_rect) for corner in corners):
                if re.fullmatch(r"[0-9]{1,4}", word[4]) and word[4] not in candidates:
                    candidates.append(word[4])
        if candidates:
            return candidates
        with _ocr_lock(data_dir):
            for corner in corners:
                if corner.width * corner.height * 4 > 3000000:
                    raise OCRError("ページの隅が大きすぎるため、印刷ページ番号のOCRを中止しました。")
                pixmap = pdf_page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=corner, alpha=False)
                env = os.environ.copy()
                env["OMP_THREAD_LIMIT"] = "1"
                try:
                    result = subprocess.run(
                        ["tesseract", "stdin", "stdout"] + _tessdata_args(data_dir)
                        + ["-l", "eng", "--psm", "6", "-c", "tessedit_char_whitelist=0123456789"],
                        input=pixmap.tobytes("png"), capture_output=True, timeout=30, check=True, env=env,
                    )
                except subprocess.CalledProcessError as error:
                    raise OCRError("印刷ページ番号のOCRに失敗しました: " + error.stderr.decode("utf-8", "replace").strip()) from error
                except (OSError, subprocess.TimeoutExpired) as error:
                    raise OCRError("印刷ページ番号のOCRを実行できません: {}".format(error)) from error
                for number in re.findall(r"(?<![0-9])[0-9]{1,4}(?![0-9])", result.stdout.decode("utf-8", "replace")):
                    if number not in candidates:
                        candidates.append(number)
        return candidates
