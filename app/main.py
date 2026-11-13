import importlib.metadata
import json
import platform
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

import streamlit as st

project_root = str(Path(__file__).resolve().parents[1])
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from backend.core.config import settings
from app.book_manager import show_book_manager
from app.ocr_review import show_ocr_review
from app.search import show_search
from app.question_answer import show_question_answer
from app.exam_analyzer import show_exam_analyzer


st.set_page_config(page_title="MKE 環境診断", page_icon="📚", layout="wide")
st.title("MKE — Medical Knowledge Engine")
section = st.sidebar.radio("画面", ["環境診断", "書籍管理", "OCR確認", "書籍内検索", "質問回答", "4択問題・解説"])


def package_version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "未導入"


def check_fts5() -> str:
    try:
        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE VIRTUAL TABLE mke_fts_check USING fts5(content)")
        return "利用可能"
    except sqlite3.OperationalError as error:
        return "利用不可: {}".format(error)


def check_ollama() -> str:
    url = "{}/api/tags".format(settings.ollama_base_url.rstrip("/"))
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            models = json.loads(response.read().decode("utf-8")).get("models", [])
        names = [model.get("name", "") for model in models]
        if settings.ollama_model in names:
            return "接続済み（{} 利用可能）".format(settings.ollama_model)
        if any(name.split(":")[0] == settings.ollama_model.split(":")[0] for name in names):
            return "接続済み（設定モデルの別タグあり）"
        return "接続済み（設定モデル {} は未取得）".format(settings.ollama_model)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        return "未接続: {}".format(error)


if section == "環境診断":
    st.caption("ローカル開発環境の確認")
    checks = [
        ("Python", sys.version.split()[0]),
        ("CPUアーキテクチャ", platform.machine()),
        ("Streamlit", package_version("streamlit")),
        ("PyMuPDF", package_version("PyMuPDF")),
        ("FastAPI", package_version("fastapi")),
        ("openpyxl", package_version("openpyxl")),
        ("SQLite FTS5", check_fts5()),
        ("Tesseract", shutil.which("tesseract") or "未導入"),
        ("Ollama", check_ollama()),
    ]
    left, right = st.columns(2)
    for index, (name, value) in enumerate(checks):
        with (left if index % 2 == 0 else right):
            st.metric(name, value)
    st.info("意味検索はまだ未実装です。")
elif section == "書籍管理":
    show_book_manager()
elif section == "OCR確認":
    show_ocr_review()
elif section == "書籍内検索":
    show_search()
elif section == "質問回答":
    show_question_answer()
else:
    show_exam_analyzer()
