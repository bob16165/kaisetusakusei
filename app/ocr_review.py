import json
import sqlite3

import streamlit as st

from backend.core.config import settings
from backend.services.ocr_service import (
    OCRError,
    available_languages,
    get_review,
    render_preview,
    run_ocr,
    save_review,
)
from backend.services.pdf_service import list_books, list_pages


LANGUAGES = {
    "日本語・英語（横書き）": "jpn+eng",
    "日本語・英語（縦書き）": "jpn_vert+eng",
    "英語": "eng",
}


def show_ocr_review() -> None:
    st.header("OCR確認")
    st.caption("OCRは1ページずつ実行します。原本PDFと抽出された文字は変更しません。")
    try:
        books = list_books(settings.data_dir)
        if not books:
            st.info("先に書籍を登録してください。")
            return
        book_by_id = {book["id"]: book for book in books}
        selected = st.selectbox(
            "書籍", list(book_by_id),
            format_func=lambda book_id: "{}（{}）".format(book_by_id[book_id]["title"], book_id[:8]),
        )
        pages = list_pages(settings.data_dir, selected)
        pages_by_number = {page["pdf_page_number"]: page for page in pages}
        number = st.selectbox("PDF内部ページ番号", list(pages_by_number))
        page_id = pages_by_number[number]["id"]
        page, result, correction = get_review(settings.data_dir, page_id)
        st.write("印刷ページ番号: {}".format(page["printed_page_number"] or "未設定"))
        st.write("確認状態: {}".format("確認済み" if page["review_status"] == "reviewed" else "未確認"))
        try:
            languages = available_languages(settings.data_dir)
            missing = [code for code in ("jpn", "jpn_vert", "eng") if code not in languages]
            if missing:
                st.warning("不足している言語データ: {}。READMEのOCRセットアップを確認してください。".format(
                    ", ".join(missing)
                ))
        except OCRError as error:
            st.error(str(error))
        st.image(render_preview(settings.data_dir, page_id), caption="原本PDF・内部 {} ページ".format(number))
        st.caption("医学用語、漢字、数字、英字、単位、記号を原本と照合してください。")

        language = st.selectbox("OCR言語", list(LANGUAGES))
        if result is not None:
            st.info("再実行しても手動修正は保持されます。OCR結果は更新され、確認状態は未確認になります。")
        if st.button("このページをOCR"):
            try:
                with st.spinner("OCR処理中…"):
                    run_ocr(settings.data_dir, page_id, LANGUAGES[language])
            except (OCRError, OSError, sqlite3.Error, RuntimeError) as error:
                st.error("OCRを実行できませんでした: {}".format(error))
            else:
                st.rerun()

        if result is None:
            st.info("このページのOCRは未実行です。以下はPDFから直接抽出したテキストです。")
        else:
            confidence = result["confidence"]
            st.write("OCR信頼度（単語の平均）: {}".format(
                "{:.1f} / 100".format(confidence) if confidence is not None else "取得不可"
            ))
            st.caption("OCR信頼度は文字認識の指標であり、医学的な正確性を保証しません。")
            suspects = json.loads(result["suspect_words"])
            if suspects:
                st.warning("信頼度70未満の語: " + "、".join(
                    "{} ({:.1f})".format(item["text"], item["confidence"]) for item in suspects[:100]
                ))
                if len(suspects) > 100:
                    st.caption("疑わしい語は先頭100件を表示しています。")
            with st.expander("OCRの生データ"):
                st.text(result["text"] or "（文字が認識されませんでした）")
            if correction is not None:
                st.warning("手動修正を表示しています。最新OCR結果と異なる場合は必ず照合してください。")

        initial = correction if correction is not None else (
            result["text"] if result is not None else page["extracted_text"]
        )
        with st.form("review_{}_{}".format(page_id, result["id"] if result else 0)):
            text = st.text_area("確認・修正するテキスト", initial, height=300)
            reviewed = st.checkbox("原本と照合し、確認済みにする", value=page["review_status"] == "reviewed")
            submitted = st.form_submit_button("修正・確認状態を保存")
        if submitted:
            save_review(settings.data_dir, page_id, text, reviewed)
            st.success("保存しました。")
    except (OCRError, OSError, sqlite3.Error, RuntimeError) as error:
        st.error("ページを表示できませんでした: {}".format(error))
