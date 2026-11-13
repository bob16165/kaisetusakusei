import sqlite3

import streamlit as st

from backend.core.config import settings
from backend.services.pdf_service import (
    BookMetadata,
    PDFRegistrationError,
    list_books,
    list_pages,
    register_pdf,
)


PDF_TYPES = {
    "text": "テキストPDF（推定）",
    "scan": "スキャンPDF（推定）",
    "mixed": "混在PDF（推定）",
    "undetermined": "判定不能",
}


def show_book_manager() -> None:
    st.header("書籍管理")
    st.caption("登録したPDFと抽出テキストはこのMac内に保存されます。")
    with st.form("register_book"):
        uploaded = st.file_uploader("PDFファイル", type=["pdf"])
        title = st.text_input("書籍名（必須）")
        author = st.text_input("著者")
        publisher = st.text_input("出版社")
        edition = st.text_input("版")
        year = st.text_input("発行年（任意・西暦）")
        submitted = st.form_submit_button("PDFを登録")
        if submitted:
            if uploaded is None:
                st.error("PDFファイルを選択してください。")
            elif year.strip() and (not year.strip().isascii() or not year.strip().isdigit()):
                st.error("発行年は半角数字で入力してください。")
            else:
                try:
                    register_pdf(
                        uploaded,
                        uploaded.name,
                        BookMetadata(
                            title=title, author=author, publisher=publisher, edition=edition,
                            publication_year=int(year.strip()) if year.strip() else None,
                        ),
                        settings.data_dir,
                    )
                except (PDFRegistrationError, OSError, sqlite3.Error, RuntimeError) as error:
                    st.error("登録できませんでした: {}".format(error))
                else:
                    st.success("PDFを登録しました。")

    st.subheader("登録済み書籍")
    try:
        books = list_books(settings.data_dir)
        if not books:
            st.info("登録済みの書籍はありません。")
            return
        book_by_id = {book["id"]: book for book in books}
        selected_id = st.selectbox(
            "書籍を選択",
            list(book_by_id),
            format_func=lambda book_id: "{}（{}）".format(
                book_by_id[book_id]["title"], book_id[:8]
            ),
        )
        book = book_by_id[selected_id]
        st.write(
            "著者: {}　出版社: {}　版: {}　発行年: {}".format(
                book["author"] or "未設定", book["publisher"] or "未設定",
                book["edition"] or "未設定", book["publication_year"] or "未設定",
            )
        )
        st.write(
            "{} / PDF内部 {} ページ / OCR確認対象 {} ページ".format(
                PDF_TYPES[book["pdf_type"]], book["page_count"], book["ocr_needed_count"]
            )
        )
        st.caption(
            "種類は文字と画像の有無による暫定判定です。図だけの画像や"
            "文字付きスキャンも混在と判定するため、原本を確認してください。"
        )
        pages = list_pages(settings.data_dir, selected_id)
        page_number = st.selectbox(
            "PDF内部ページ番号", [page["pdf_page_number"] for page in pages]
        )
        page = pages[page_number - 1]
        st.write("印刷ページ番号: 未設定")
        st.write("抽出文字: {} / 画像: {} / OCR確認対象: {}".format(
            "あり" if page["has_text"] else "なし",
            "あり" if page["has_images"] else "なし",
            "はい" if page["ocr_needed"] else "いいえ",
        ))
        st.text_area("抽出テキスト（読み取り専用）", page["extracted_text"], height=200, disabled=True)
    except (OSError, sqlite3.Error) as error:
        st.error("書籍情報を読み込めませんでした: {}".format(error))
