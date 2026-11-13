import sqlite3

import streamlit as st

from backend.core.config import settings
from backend.services.ocr_service import OCRError, render_preview
from backend.services.pdf_service import list_books
from backend.services.retrieval_service import SearchError, search


SOURCES = {"pdf": "PDF抽出", "ocr": "OCR結果", "corrected": "手動修正"}


def show_search() -> None:
    st.header("書籍内検索")
    st.caption("専門用語の完全一致検索です。空白区切りの複数語は同じ文章内のAND検索になります。")
    try:
        books = list_books(settings.data_dir)
        if not books:
            st.info("先に書籍を登録してください。")
            return
        choices = {"": "全書籍"}
        choices.update({book["id"]: "{}（{}）".format(book["title"], book["id"][:8]) for book in books})
        with st.form("search_form"):
            query = st.text_input("検索語")
            book_id = st.selectbox("対象書籍", list(choices), format_func=choices.get)
            submitted = st.form_submit_button("検索")
        if submitted:
            st.session_state["mke_search"] = (query, book_id)
        if "mke_search" not in st.session_state:
            return
        query, book_id = st.session_state["mke_search"]
        response = search(settings.data_dir, query, book_id or None)
        st.caption("検索語: {} / 対象: {}".format(query, choices.get(book_id, "選択した書籍")))
        if response.unsearchable_pages:
            st.warning(
                "{} / {} ページは検索できるテキストがありません。OCR未実施や空白ページの可能性があります。"
                "検索結果がないことは、書籍に記載がないことを意味しません。".format(
                    response.unsearchable_pages, response.total_pages
                )
            )
        if response.ocr_pending_pages:
            st.warning(
                "{}ページは画像を含むか文字がなく、OCR未実施です。"
                "抽出文字があるページでも、画像内の文字は検索できない場合があります。".format(
                    response.ocr_pending_pages
                )
            )
        st.info("意味検索は未実装です。検索スコアは文字列の出現回数で、医学的確実性ではありません。")
        if not response.results:
            st.warning("一致する文章が見つかりませんでした。別の専門用語も試し、原本を確認してください。")
            return
        st.write("{}件{}を表示".format(len(response.results), "以上" if response.truncated else ""))
        for index, result in enumerate(response.results):
            st.subheader("{} — PDF内部 {} ページ".format(result.title, result.pdf_page_number))
            st.caption(
                "版: {} / 印刷ページ: {} / スコア: {} / テキスト: {} / 確認: {}".format(
                    result.edition or "未設定", result.printed_page_number or "未設定",
                    result.score, SOURCES[result.source],
                    "確認済み" if result.review_status == "reviewed" else "未確認",
                )
            )
            if result.review_status != "reviewed":
                st.warning("未確認の抽出テキストです。原本と照合してください。")
            if result.source == "corrected" and result.ocr_processed and result.review_status != "reviewed":
                st.warning("手動修正は最新OCR結果と異なる場合があります。")
            st.text(result.text)
            if st.checkbox("原本ページを表示", key="search_preview_{}_{}".format(index, result.page_id)):
                try:
                    st.image(render_preview(settings.data_dir, result.page_id), caption="PDF内部ページ")
                except (OCRError, OSError, sqlite3.Error, RuntimeError) as error:
                    st.error("原本を表示できませんでした: {}".format(error))
    except (SearchError, sqlite3.Error, OSError) as error:
        st.error("検索できませんでした: {}".format(error))
