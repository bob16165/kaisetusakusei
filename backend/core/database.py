import sqlite3
from contextlib import closing
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    publisher TEXT NOT NULL DEFAULT '',
    edition TEXT NOT NULL DEFAULT '',
    publication_year INTEGER,
    original_filename TEXT NOT NULL,
    pdf_filename TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL UNIQUE,
    page_count INTEGER NOT NULL,
    pdf_type TEXT NOT NULL CHECK (pdf_type IN ('text', 'scan', 'mixed', 'undetermined')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    pdf_page_number INTEGER NOT NULL CHECK (pdf_page_number > 0),
    printed_page_number TEXT,
    extracted_text TEXT NOT NULL,
    has_text INTEGER NOT NULL CHECK (has_text IN (0, 1)),
    has_images INTEGER NOT NULL CHECK (has_images IN (0, 1)),
    ocr_needed INTEGER NOT NULL CHECK (ocr_needed IN (0, 1)),
    ocr_processed INTEGER NOT NULL DEFAULT 0 CHECK (ocr_processed IN (0, 1)),
    ocr_confidence REAL,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(book_id, pdf_page_number)
);
CREATE TABLE IF NOT EXISTS ocr_results (
    id INTEGER PRIMARY KEY,
    page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    confidence REAL,
    suspect_words TEXT NOT NULL DEFAULT '[]',
    language TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ocr_results_page ON ocr_results(page_id, id);
CREATE TABLE IF NOT EXISTS page_corrections (
    page_id INTEGER PRIMARY KEY REFERENCES pages(id) ON DELETE CASCADE,
    corrected_text TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY,
    question_text TEXT NOT NULL,
    answer_number INTEGER NOT NULL CHECK(answer_number BETWEEN 1 AND 4),
    question_kind TEXT CHECK(question_kind IN ('correct', 'incorrect')),
    year INTEGER,
    subject TEXT NOT NULL DEFAULT '',
    field TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS choices (
    id INTEGER PRIMARY KEY,
    question_id TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    number INTEGER NOT NULL CHECK(number BETWEEN 1 AND 4),
    text TEXT NOT NULL,
    UNIQUE(question_id, number)
);
CREATE TABLE IF NOT EXISTS explanations (
    id INTEGER PRIMARY KEY,
    choice_id INTEGER NOT NULL UNIQUE REFERENCES choices(id) ON DELETE CASCADE,
    verdict TEXT NOT NULL CHECK(verdict IN ('matches', 'not_matches', 'unknown')),
    text TEXT NOT NULL,
    edited INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY,
    explanation_id INTEGER NOT NULL REFERENCES explanations(id) ON DELETE CASCADE,
    page_id INTEGER REFERENCES pages(id) ON DELETE SET NULL,
    book_title TEXT NOT NULL,
    edition TEXT NOT NULL,
    pdf_page_number INTEGER NOT NULL,
    printed_page_number TEXT,
    source TEXT NOT NULL,
    review_status TEXT NOT NULL,
    quote TEXT NOT NULL,
    context TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS choice_evidence_state (
    choice_id INTEGER PRIMARY KEY REFERENCES choices(id) ON DELETE CASCADE,
    evidence_status TEXT NOT NULL CHECK(
        evidence_status IN ('unselected', 'confirmed', 'insufficient', 'contradictory')
    ),
    human_review_status TEXT NOT NULL CHECK(
        human_review_status IN ('unreviewed', 'reviewed')
    ),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS choice_evidence (
    id INTEGER PRIMARY KEY,
    choice_id INTEGER NOT NULL REFERENCES choices(id) ON DELETE CASCADE,
    page_id INTEGER REFERENCES pages(id) ON DELETE SET NULL,
    book_id TEXT NOT NULL,
    book_title TEXT NOT NULL,
    edition TEXT NOT NULL,
    pdf_page_number INTEGER NOT NULL,
    printed_page_number TEXT,
    source TEXT NOT NULL,
    search_method TEXT NOT NULL,
    search_score REAL NOT NULL,
    review_status TEXT NOT NULL,
    ocr_processed INTEGER NOT NULL CHECK(ocr_processed IN (0, 1)),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS choice_evidence_choice ON choice_evidence(choice_id, id);
CREATE TABLE IF NOT EXISTS explanation_attempts (
    id INTEGER PRIMARY KEY,
    choice_id INTEGER NOT NULL REFERENCES choices(id) ON DELETE CASCADE,
    citation_status TEXT NOT NULL CHECK(
        citation_status IN ('unperformed', 'match', 'mismatch')
    ),
    outcome TEXT NOT NULL,
    details TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS explanation_attempts_choice
    ON explanation_attempts(choice_id, id);
"""


def connect(data_dir: Path) -> sqlite3.Connection:
    data_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(data_dir / "mke.sqlite3"))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def initialize(data_dir: Path) -> None:
    with closing(connect(data_dir)) as connection:
        with connection:
            connection.executescript(SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(questions)")}
            if "question_kind" not in columns:
                connection.execute(
                    "ALTER TABLE questions ADD COLUMN question_kind TEXT "
                    "CHECK(question_kind IN ('correct', 'incorrect'))"
                )
            explanation_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(explanations)")
            }
            if "citation_status" not in explanation_columns:
                connection.execute(
                    "ALTER TABLE explanations ADD COLUMN citation_status TEXT NOT NULL "
                    "DEFAULT 'unperformed' CHECK(citation_status IN "
                    "('unperformed', 'match', 'mismatch'))"
                )
            if "human_review_status" not in explanation_columns:
                connection.execute(
                    "ALTER TABLE explanations ADD COLUMN human_review_status TEXT NOT NULL "
                    "DEFAULT 'unreviewed' CHECK(human_review_status IN ('unreviewed', 'reviewed'))"
                )
