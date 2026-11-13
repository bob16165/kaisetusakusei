import sqlite3

import streamlit as st

from backend.core.config import settings
from backend.services.ocr_service import OCRError, render_preview
from backend.services.pdf_service import list_books
from backend.services.rag_service import AnswerError, answer_question
from backend.services.retrieval_service import SearchError, search
from app.search import SOURCES


def show_question_answer() -> None:
    st.header("根拠付き質問回答")
    st.warning(
        "研究・学習用です。AIの引用が実在しても、回答内容の医学的正確性は保証できません。"
        "診療判断には使わず、必ず原本と照合してください。"
    )
    try:
        books = list_books(settings.data_dir)
        if not books:
            st.info("先に書籍を登録してください。")
            return
        choices = {"": "全書籍"}
        choices.update({book["id"]: "{}（{}）".format(book["title"], book["id"][:8]) for book in books})
        with st.form("qa_search"):
            question = st.text_area("質問", max_chars=1000)
            keywords = st.text_input("検索語（専門用語を入力）")
            book_id = st.selectbox("対象書籍", list(choices), format_func=choices.get)
            submitted = st.form_submit_button("根拠を検索")
        if submitted:
            if not question.strip():
                st.error("質問を入力してください。")
                return
            result = search(settings.data_dir, keywords, book_id or None, limit=10)
            st.session_state["mke_qa_search"] = (question, keywords, book_id, result)
            st.session_state["mke_qa_generation"] = st.session_state.get("mke_qa_generation", 0) + 1
            st.session_state.pop("mke_qa_answer", None)
        if "mke_qa_search" not in st.session_state:
            return
        question, keywords, book_id, result = st.session_state["mke_qa_search"]
        st.caption("質問: {} / 検索語: {} / 対象: {}".format(
            question, keywords, choices.get(book_id, "選択した書籍")
        ))
        if result.unsearchable_pages or result.ocr_pending_pages:
            st.warning(
                "検索できる文字がないページ: {}、OCR未実施の確認対象: {}。"
                "見つからないことは書籍に記載がない証拠ではありません。".format(
                    result.unsearchable_pages, result.ocr_pending_pages
                )
            )
        if result.truncated:
            st.warning("検索候補は上位10件のみ表示しています。検索語を変更して再確認してください。")
        if not result.results:
            st.warning("根拠不足：一致する文章がないため回答は生成しません。検索語やOCRを確認してください。")
            return
        st.write("質問に関係する候補を原本と照合し、最大3件を選択してください。")
        st.caption("文字列の一致は、質問に答える根拠として十分であることを保証しません。")
        selected = []
        for index, item in enumerate(result.results):
            st.subheader("{} — PDF内部 {} ページ".format(item.title, item.pdf_page_number))
            st.caption("版: {} / 印刷ページ: {} / {} / {}".format(
                item.edition or "未設定", item.printed_page_number or "未設定",
                SOURCES[item.source], "確認済み" if item.review_status == "reviewed" else "未確認",
            ))
            if item.review_status != "reviewed":
                st.warning("未確認のテキストです。OCR・抽出誤りの可能性があるため原本と照合してください。")
            st.text(item.text)
            generation = st.session_state["mke_qa_generation"]
            if st.checkbox("原本を表示", key="qa_preview_{}_{}_{}".format(generation, index, item.page_id)):
                st.image(render_preview(settings.data_dir, item.page_id))
            if st.checkbox("この文章を根拠として選択", key="qa_select_{}_{}_{}".format(generation, index, item.page_id)):
                selected.append(item)
        if st.button("選択した根拠で回答を生成"):
            st.session_state.pop("mke_qa_answer", None)
            if not selected:
                st.warning("根拠不足：根拠を選択していないため回答は生成しません。")
            else:
                latest = search(settings.data_dir, keywords, book_id or None, limit=10)
                if any(item not in latest.results for item in selected):
                    raise AnswerError("検索後に根拠が更新されました。もう一度根拠を検索してください。")
                with st.spinner("ローカルAIで回答・関連箇所を確認中です。数分かかる場合があります。"):
                    answer = answer_question(settings, question, selected)
                st.session_state["mke_qa_answer"] = (question, answer)
        if "mke_qa_answer" in st.session_state:
            answered_question, answer = st.session_state["mke_qa_answer"]
            st.subheader("回答")
            st.caption("質問: {}".format(answered_question))
            if answer.status == "insufficient":
                st.warning("根拠不足：選択した文章から回答できません。検索語や原本を再確認してください。")
            else:
                if answer.status == "extractive":
                    st.warning(
                        "AIは十分な回答を生成できませんでした。代わりに選択した資料から"
                        "関連する可能性のある文章を抜粋しています。質問への回答とは限らないため、"
                        "必ず前後の文脈と原本を確認してください。"
                    )
                else:
                    st.warning("AIによる解釈です。引用の実在を確認していますが、根拠との意味的な一致・医学的正確性は未検証です。")
                for claim in answer.claims:
                    item = answer.evidence[claim.evidence_index]
                    st.write(claim.text)
                    st.caption("{} / 版: {} / PDF内部 {} ページ / 印刷ページ: {} / {} / {}".format(
                        item.title, item.edition or "未設定", item.pdf_page_number,
                        item.printed_page_number or "未設定", SOURCES[item.source],
                        "確認済み" if item.review_status == "reviewed" else "未確認",
                    ))
                    if answer.status != "extractive":
                        st.text("引用: " + claim.quote)
                st.caption("確実性: 自動判定不可。各引用と原本を人が確認してください。")
    except (AnswerError, SearchError, OCRError, sqlite3.Error, OSError, RuntimeError) as error:
        st.error("処理できませんでした: {}".format(error))
