import sqlite3

import streamlit as st

from app.search import SOURCES
from backend.core.config import settings
from backend.services.exam_service import (
    ExamError, auto_generate_question, comparison, create_question, edit_explanation,
    export_excel, generate_choice, get_choice_evidence, get_question, list_questions,
    save_choice_evidence, set_explanation_review, verdict_label,
)
from backend.services.ocr_service import OCRError, render_preview
from backend.services.pdf_service import list_books
from backend.services.printed_page_service import printed_page_candidates
from backend.services.rag_service import AnswerError
from backend.services.retrieval_service import SearchError, search, search_choice_evidence


EVIDENCE_STATUS_LABELS = {
    "未選択": "unselected",
    "根拠確認済み": "confirmed",
    "根拠不足": "insufficient",
    "書籍間の記載矛盾": "contradictory",
}
EVIDENCE_STATUS_NAMES = {value: key for key, value in EVIDENCE_STATUS_LABELS.items()}


def _render_candidate_selection(choice, candidates, key_prefix) -> None:
    status, review_status, saved = get_choice_evidence(settings.data_dir, choice["id"])
    combined = []
    seen = set()
    for item in list(saved) + list(candidates):
        key = (item.page_id, item.text, item.source)
        if key not in seen:
            seen.add(key)
            combined.append(item)

    st.caption(
        "現在の保存状態: {} / 人による根拠確認: {}".format(
            EVIDENCE_STATUS_NAMES[status],
            "確認済み" if review_status == "reviewed" else "未確認",
        )
    )
    st.caption("検索スコアは候補の並べ替え用で、根拠の正しさを示しません。")
    saved_keys = {(item.page_id, item.text, item.source) for item in saved}
    selected = []
    for index, item in enumerate(combined):
        identity = (item.page_id, item.text, item.source)
        is_saved = identity in saved_keys
        st.markdown(
            "**{} / 版: {} / PDF内部 {}ページ / 印刷ページ: {}**".format(
                item.title, item.edition or "未設定", item.pdf_page_number,
                item.printed_page_number or "未設定",
            )
        )
        st.caption(
            "{} / OCR確認: {} / 検索方式: {} / スコア: {}".format(
                SOURCES[item.source],
                "確認済み" if item.review_status == "reviewed" else "未確認",
                item.search_method, item.score,
            )
        )
        if is_saved:
            st.caption("保存済み確定根拠ID: {}（スナップショット）".format(item.evidence_id))
        if item.page_id < 0:
            st.warning("原本ページが見つからない保存済みスナップショットです。新たな確定根拠には選べません。")
        else:
            checked = st.checkbox(
                "この文章を選択根拠に含める",
                value=is_saved,
                key="{}_evidence_{}".format(key_prefix, index),
            )
            if checked:
                selected.append(item)
            if st.checkbox(
                "原本を表示",
                key="{}_preview_{}".format(key_prefix, index),
            ):
                st.image(render_preview(settings.data_dir, item.page_id))
        st.text(item.text)

    status_options = list(EVIDENCE_STATUS_LABELS)
    selected_status = st.selectbox(
        "根拠状態",
        status_options,
        index=list(EVIDENCE_STATUS_LABELS.values()).index(status),
        key="{}_status".format(key_prefix),
    )
    reviewed = st.checkbox(
        "選択した根拠と原本を照合し、人による根拠確認を完了した",
        value=review_status == "reviewed",
        key="{}_reviewed".format(key_prefix),
    )
    if st.button("選択根拠と状態を保存", key="{}_save".format(key_prefix)):
        save_choice_evidence(
            settings.data_dir, choice["id"], selected,
            EVIDENCE_STATUS_LABELS[selected_status],
            "reviewed" if reviewed else "unreviewed",
        )
        st.success("選択根拠と確認状態を保存しました。")
        st.rerun()


def show_exam_analyzer() -> None:
    st.header("4択問題・解説")
    st.warning(
        "学習用の下書きです。AIは仮判定を行い、表示する解説は確定根拠からの原文抜粋です。"
        "引用の関連性・医学的正確性は自動保証できません。"
        "原本を照合し、教員等が確認してください。"
    )
    try:
        with st.expander("新しい問題を登録", expanded=False):
            with st.form("new_exam"):
                question_id = st.text_input("問題ID（重複不可）")
                question_text = st.text_area("問題文（「正しい」「誤り」など設問の要求を含める）")
                choices = [st.text_input("選択肢{}".format(i)) for i in range(1, 5)]
                answer = st.selectbox("模範解答（1つ）", [1, 2, 3, 4], index=None, key="exam_answer")
                kind = st.radio("設問は何を選ぶ問題ですか", ["正しいもの", "誤っているもの"],
                                index=None, key="exam_kind")
                year = st.text_input("出題年度（任意・西暦）")
                subject = st.text_input("科目")
                field = st.text_input("分野（任意）")
                submitted = st.form_submit_button("問題を保存")
            if submitted:
                if year.strip() and (not year.strip().isascii() or not year.strip().isdigit()):
                    raise ExamError("年度は半角数字で入力してください。")
                create_question(settings.data_dir, question_id, question_text, choices, answer,
                                int(year.strip()) if year.strip() else None, subject, field,
                                {"正しいもの": "correct", "誤っているもの": "incorrect"}.get(kind, ""))
                st.success("問題を保存しました。")

        questions = list_questions(settings.data_dir)
        if not questions:
            st.info("まず4択問題を登録してください。")
            return
        by_id = {q["id"]: q for q in questions}
        question_id = st.selectbox("保存済み問題", list(by_id),
                                   format_func=lambda key: "{}: {}".format(key, by_id[key]["question_text"][:50]))
        question, choices, evidence = get_question(settings.data_dir, question_id)
        st.subheader("問題")
        st.write(question["question_text"])
        st.caption("設問の種類: {}".format({"correct": "正しいものを選ぶ", "incorrect": "誤っているものを選ぶ"}.get(
            question["question_kind"], "未設定（この問題では生成できません）")))
        st.write("模範解答: {}".format(question["answer_number"]))
        for choice in choices:
            st.write("{}. {}".format(choice["number"], choice["text"]))
        st.info("模範解答との照合: " + comparison(question["answer_number"], choices))
        st.caption("引用の実在確認は、引用が確定根拠内にあることのみを示します。引用の意味的支持や医学的妥当性は保証しません。")

        books = list_books(settings.data_dir)
        book_options = {"": "全書籍"}
        book_options.update({b["id"]: "{}（{}）".format(b["title"], b["id"][:8]) for b in books})
        st.subheader("4択の根拠候補を検索・選定")
        candidate_book = st.selectbox("関連ページの対象書籍", list(book_options),
                                      format_func=book_options.get, key="exam_candidate_book")
        st.caption("各選択肢の候補を表示します。検索順位・スコアは医学的根拠の確からしさではありません。")
        if st.button("4択の関連ページを検索", key="exam_candidate_search"):
            found_choices = []
            page_numbers = {}
            with st.spinner("関連ページと印刷ページ番号の候補を探しています…"):
                for row in choices:
                    found = search_choice_evidence(
                        settings.data_dir, question["question_text"], row["text"], candidate_book or None, limit=3
                    )
                    found_choices.append(found)
                    for item in found.results:
                        if item.page_id not in page_numbers:
                            try:
                                page_numbers[item.page_id] = (printed_page_candidates(settings.data_dir, item.page_id), "")
                            except (OCRError, OSError, RuntimeError) as error:
                                page_numbers[item.page_id] = ([], str(error))
            generation = st.session_state.get("exam_candidate_generation", 0) + 1
            st.session_state["exam_candidate_generation"] = generation
            st.session_state["exam_candidate_results"] = (
                question_id, candidate_book, found_choices, page_numbers, generation
            )
        candidate_state = st.session_state.get("exam_candidate_results")
        if candidate_state and candidate_state[0] == question_id and candidate_state[1] == candidate_book:
            _, _, found_choices, page_numbers, generation = candidate_state
            st.warning("候補は根拠や正誤を保証しません。確定前に原本と照合してください。")
            for row, found in zip(choices, found_choices):
                st.markdown("**選択肢{}: {}**".format(row["number"], row["text"]))
                if found.ocr_pending_pages or found.unsearchable_pages:
                    st.caption("OCR未実施の確認対象: {}ページ / 検索できる文字がないページ: {}。"
                               "画像中の文字や言い換えは見落とすことがあります。".format(
                                   found.ocr_pending_pages, found.unsearchable_pages))
                if not found.results:
                    st.info("関連ページの候補が見つかりませんでした。")
                for item in found.results:
                    numbers, error = page_numbers[item.page_id]
                    st.caption("印刷ページ候補: {} / PDF内部 {}ページ".format(
                        "・".join(numbers) if numbers else "取得できず", item.pdf_page_number))
                    if error:
                        st.warning("印刷ページ番号を取得できません: " + error)
                _render_candidate_selection(
                    row, found.results,
                    "exam_all_{}_{}_{}".format(question_id, row["id"], generation),
                )

        st.subheader("保存済み確定根拠から一括生成")
        st.caption("検索は実行しません。根拠状態が確定済みで、人による確認が完了した選択肢だけを生成します。")
        regenerate = False
        if any(row["explanation_id"] is not None for row in choices):
            regenerate = st.checkbox(
                "保存済み・手動修正済みの解説も上書きして4択を再生成する",
                key="exam_auto_replace_{}".format(question_id),
            )
        if st.button("確定根拠から4択の解説を生成", key="exam_auto_generate"):
            bar = st.progress(0, text="保存済み根拠を確認しています…")
            pending_count = sum(regenerate or row["explanation_id"] is None for row in choices)
            def update_progress(index, message):
                bar.progress(min((index - 1) * 100 // max(pending_count, 1), 100), text=message)
            saved, warnings = auto_generate_question(
                settings, question_id, update_progress, regenerate
            )
            st.session_state["exam_auto_result"] = (question_id, saved, warnings)
            st.rerun()
        auto_result = st.session_state.get("exam_auto_result")
        if auto_result and auto_result[0] == question_id:
            st.info("{}選択肢を保存しました。判定不能・要確認の内容と出典を確認してください。".format(auto_result[1]))
            for warning in auto_result[2]:
                st.warning(warning)

        st.subheader("個別に根拠を確認・再生成")
        number = st.selectbox("根拠を探す選択肢", [1, 2, 3, 4], key="exam_choice")
        choice = choices[number - 1]
        st.subheader("選択肢{}: {}".format(number, choice["text"]))
        with st.form("exam_search"):
            keywords = st.text_input("根拠検索語（この選択肢に関する専門用語）")
            book_id = st.selectbox("検索対象", list(book_options), format_func=book_options.get)
            submit_search = st.form_submit_button("この選択肢の根拠を検索")
        if submit_search:
            found = search(settings.data_dir, keywords, book_id or None, limit=8)
            st.session_state["exam_search_results"] = (question_id, number, keywords, book_id, found)
            st.session_state["exam_search_generation"] = st.session_state.get("exam_search_generation", 0) + 1
        state = st.session_state.get("exam_search_results")
        if state and state[0] == question_id and state[1] == number:
            _, _, keywords, book_id, found = state
            st.caption("検索語: {}".format(keywords))
            if found.unsearchable_pages or found.ocr_pending_pages:
                st.warning("検索不能 {}ページ、OCR未実施の確認対象 {}ページ。検索で見つからなくても記載がないとは限りません。".format(
                    found.unsearchable_pages, found.ocr_pending_pages))
            if found.truncated:
                st.warning("候補は上位8件のみ表示しています。検索語を変更して確認してください。")
            if not found.results:
                st.warning("根拠が見つかりません。検索語やOCRを確認してください。")
            generation = st.session_state["exam_search_generation"]
            _render_candidate_selection(
                choice, found.results,
                "exam_one_{}_{}_{}".format(question_id, choice["id"], generation),
            )

        st.caption("個別生成は、保存済みの根拠状態が「根拠確認済み」かつ原本確認済みの場合のみ実行できます。")
        if choice["explanation_id"]:
            overwrite = st.checkbox(
                "保存済み解説を明示的に上書きする（手動編集も置き換わります）",
                key="exam_replace_{}_{}".format(question_id, number),
            )
        else:
            overwrite = False
        if st.button("保存済み確定根拠でこの選択肢を生成", key="exam_generate_one"):
            with st.spinner("この選択肢をローカルAIで分析中…数分かかる場合があります。"):
                generate_choice(settings, question_id, choice["id"], overwrite=overwrite)
            st.rerun()

        st.subheader("保存済み解説")
        for row in choices:
            st.markdown("**選択肢{}: {}**".format(row["number"], row["text"]))
            if row["verdict"] is None:
                st.write("未生成")
                continue
            st.write("AI判定: {} / {}".format(verdict_label(row["verdict"], question["question_kind"]),
                                             "手動編集あり" if row["edited"] else "未編集"))
            st.caption(
                "根拠状態: {} / 根拠の人手確認: {} / 引用検証: {} / 解説の人手確認: {}".format(
                    EVIDENCE_STATUS_NAMES[row["evidence_status"]],
                    "確認済み" if row["evidence_review_status"] == "reviewed" else "未確認",
                    {"unperformed": "未実施", "match": "原文内に一致", "mismatch": "不一致"}[
                        row["citation_status"]
                    ],
                    "確認済み" if row["human_review_status"] == "reviewed" else "未確認",
                )
            )
            if row["last_attempt_details"] and row["last_attempt_citation_status"] == "mismatch":
                st.warning("最新の生成試行は引用不一致で棄却されました: " + row["last_attempt_details"])
            refs = [ref for ref in evidence if ref["choice_id"] == row["id"]]
            for ref in refs:
                st.caption("{} / 版: {} / PDF内部 {}ページ / 印刷ページ: {} / {} / 確認状態（生成時）: {}".format(
                    ref["book_title"], ref["edition"] or "未設定", ref["pdf_page_number"],
                    ref["printed_page_number"] or "未設定", SOURCES[ref["source"]],
                    "確認済み" if ref["review_status"] == "reviewed" else "未確認"))
                st.text("引用: " + ref["quote"])
                with st.expander("引用元の抽出テキスト"):
                    st.text(ref["context"])
                if ref["page_id"] and st.checkbox("保存済み根拠の原本を表示", key="saved_preview_{}".format(ref["id"])):
                    st.image(render_preview(settings.data_dir, ref["page_id"]))
            if row["citation_status"] == "match":
                st.warning("引用検証は原文中の実在確認のみです。引用が判定を支持するか、医学的妥当性は未検証です。")
            if row["verdict"] == "unknown":
                explanation_label = "判定不能メッセージ・補足（編集可能）"
            elif row["edited"]:
                explanation_label = "手動編集済み解説（編集可能）"
            else:
                explanation_label = "解説（確定根拠からの原文抜粋。編集可能）"
            with st.form("edit_explanation_{}".format(row["id"])):
                text = st.text_area(explanation_label, row["explanation"], height=100)
                save = st.form_submit_button("解説を保存")
            if save:
                edit_explanation(settings.data_dir, row["id"], text)
                st.rerun()
            if row["edited"]:
                st.caption("手動編集後の解説と引用の整合性は自動検証されていません。AI判定は生成時のままです。")
            reviewed = st.checkbox(
                "解説と引用を原本照合し、人による解説確認を完了した",
                value=row["human_review_status"] == "reviewed",
                key="explanation_review_{}".format(row["id"]),
            )
            if st.button("解説の人手確認状態を保存", key="save_explanation_review_{}".format(row["id"])):
                set_explanation_review(settings.data_dir, row["id"], reviewed)
                st.rerun()

        st.download_button(
            "この問題をExcel出力", data=export_excel(settings.data_dir, question_id),
            file_name="mke_exam.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except (ExamError, AnswerError, SearchError, OCRError, sqlite3.Error, OSError, RuntimeError) as error:
        st.error("処理できませんでした: {}".format(error))
