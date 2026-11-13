import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class StreamlitStartupTests(unittest.TestCase):
    def test_app_renders_from_outside_project_directory(self):
        project_root = Path(__file__).resolve().parents[1]
        app_path = project_root / "app" / "main.py"
        script = """
import sys
from streamlit.testing.v1 import AppTest

app = AppTest.from_file(sys.argv[1], default_timeout=15).run()
if app.exception:
    for exception in app.exception:
        print(exception.message, file=sys.stderr)
    sys.exit(1)
assert app.title[0].value == "MKE — Medical Knowledge Engine"
assert any(metric.label == "SQLite FTS5" for metric in app.metric)
app.sidebar.radio[0].set_value("書籍管理").run()
if app.exception:
    for exception in app.exception:
        print(exception.message, file=sys.stderr)
    sys.exit(1)
assert any(header.value == "書籍管理" for header in app.header)
assert any("登録済みの書籍はありません" in info.value for info in app.info)
import io
import fitz
from backend.core.config import settings
from backend.services.pdf_service import BookMetadata, register_pdf
document = fitz.open()
page = document.new_page()
page.insert_text((72, 72), "Medical text")
content = document.tobytes()
document.close()
register_pdf(io.BytesIO(content), "book.pdf", BookMetadata("Test book", edition="2nd"), settings.data_dir)
app.run()
if app.exception:
    for exception in app.exception:
        print(exception.message, file=sys.stderr)
    sys.exit(1)
assert "Test book" in app.selectbox[0].options[0]
assert app.selectbox[1].value == 1
assert app.text_area[0].value.strip() == "Medical text"
app.sidebar.radio[0].set_value("OCR確認").run()
if app.exception:
    for exception in app.exception:
        print(exception.message, file=sys.stderr)
    sys.exit(1)
assert any(header.value == "OCR確認" for header in app.header)
assert app.text_area[0].value.strip() == "Medical text"
app.text_area[0].set_value("Corrected text")
app.checkbox[0].check()
app.button(key="FormSubmitter:review_1_0-修正・確認状態を保存").click().run()
if app.exception:
    for exception in app.exception:
        print(exception.message, file=sys.stderr)
    sys.exit(1)
app.run()
assert "確認状態: 確認済み" in [m.value for m in app.markdown]
assert app.text_area[0].value == "Corrected text"
app.sidebar.radio[0].set_value("書籍内検索").run()
assert not app.exception
app.text_input[0].set_value("Corrected")
app.button(key="FormSubmitter:search_form-検索").click().run()
assert not app.exception
assert any("Test book" in header.value for header in app.subheader)
assert any("確認済み" in caption.value for caption in app.caption)
app.checkbox[0].check().run()
assert not app.exception
app.sidebar.radio[0].set_value("質問回答").run()
assert not app.exception
app.text_area[0].set_value("What text is corrected?")
app.text_input[0].set_value("Corrected")
app.button(key="FormSubmitter:qa_search-根拠を検索").click().run()
assert not app.exception
app.button[-1].click().run()
assert any("根拠を選択していない" in warning.value for warning in app.warning)
app.checkbox(key="qa_select_1_0_1").check()
from unittest.mock import patch
import json
response = {"done": True, "response": json.dumps({"status": "answered", "claims": [
    {"text": "Corrected text.", "evidence_id": 1, "quote": "Corrected text"}
]})}
with patch("backend.services.rag_service._generate", return_value=response):
    app.button[-1].click().run()
assert not app.exception
assert any("Corrected text." in markdown.value for markdown in app.markdown)
assert any("医学的正確性" in warning.value for warning in app.warning)
insufficient = {"done": True, "response": '{"status":"insufficient","claims":[]}'}
with patch("backend.services.rag_service._generate", side_effect=[
    insufficient, {"done": True, "response": '{"ids":[1]}'}
]):
    app.button[-1].click().run()
assert not app.exception
assert any("回答とは限らない" in warning.value for warning in app.warning)
assert any("Corrected text" in markdown.value for markdown in app.markdown)
app.sidebar.radio[0].set_value("4択問題・解説").run()
assert not app.exception
app.text_input[0].set_value("exam-test")
app.text_area[0].set_value("正しいのはどれか。")
for i, value in enumerate(["Corrected first", "Corrected second", "Corrected third", "Corrected fourth"], 1):
    app.text_input[i].set_value(value)
app.selectbox(key="exam_answer").set_value(1)
app.radio(key="exam_kind").set_value("正しいもの")
app.button(key="FormSubmitter:new_exam-問題を保存").click().run()
assert not app.exception
assert any("模範解答との照合" in info.value for info in app.info)
with patch("backend.services.printed_page_service.subprocess.run") as page_ocr:
    page_ocr.return_value.stdout = b"12\\n"
    app.button(key="exam_candidate_search").click().run()
assert not app.exception
assert any("印刷ページ候補: 12" in caption.value for caption in app.caption)
from backend.services.exam_service import get_question, save_choice_evidence
from backend.services.retrieval_service import search_choice_evidence
question, choices, _ = get_question(settings.data_dir, "exam-test")
first_root_key = "exam_all_exam-test_{}_1".format(choices[0]["id"])
app.checkbox(key=first_root_key + "_evidence_0").check().run()
app.selectbox(key=first_root_key + "_status").set_value("根拠確認済み").run()
app.checkbox(key=first_root_key + "_reviewed").check().run()
app.button(key=first_root_key + "_save").click().run()
assert not app.exception
for choice in choices[1:]:
    candidates = search_choice_evidence(
        settings.data_dir, question["question_text"], choice["text"]
    ).results
    assert candidates
    save_choice_evidence(
        settings.data_dir, choice["id"], candidates[:1], "confirmed", "reviewed"
    )
app.run()
assert not app.exception
app.text_input[-1].set_value("Corrected")
app.button(key="FormSubmitter:exam_search-この選択肢の根拠を検索").click().run()
assert not app.exception
exam_response = {"done": True, "response": json.dumps({"verdict": "true", "reason": "解説です。",
    "evidence_id": 1, "quote": "Corrected text"})}
with patch("backend.services.exam_service._generate", return_value=exam_response):
    app.button(key="exam_generate_one").click().run()
assert not app.exception
assert any("設問に合う" in m.value for m in app.markdown)
app.text_area[-1].set_value("編集済み解説")
app.button(key="FormSubmitter:edit_explanation_1-解説を保存").click().run()
assert not app.exception
assert app.text_area[-1].value == "編集済み解説"
def response_for_saved_evidence(_settings, payload):
    if not payload.get("prompt"):
        return {}
    data = json.loads(payload["prompt"].rsplit("\\n", 1)[-1])
    return {"done": True, "response": json.dumps({"verdict": "true", "reason": "解説です。",
        "evidence_id": data["confirmed_evidence"][0]["id"], "quote": "Corrected text"})}
with patch("backend.services.exam_service._generate", side_effect=response_for_saved_evidence):
    app.button(key="exam_auto_generate").click().run()
assert not app.exception
assert any("3選択肢を保存" in info.value for info in app.info)
assert any(text.value == "編集済み解説" for text in app.text_area)
assert len([m for m in app.markdown if "AI判定:" in m.value]) == 4
"""
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as working_directory:
            environment["MKE_DATA_DIR"] = working_directory
            result = subprocess.run(
                [sys.executable, "-c", script, str(app_path)],
                cwd=working_directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
