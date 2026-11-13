import hashlib
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from backend.core.database import connect, initialize


CHUNK_SIZE = 800
CHUNK_OVERLAP = 160
MAX_TERMS = 8
MAX_TERM_LENGTH = CHUNK_OVERLAP


class SearchError(ValueError):
    pass


@dataclass(frozen=True)
class SearchResult:
    page_id: int
    title: str
    edition: str
    pdf_page_number: int
    printed_page_number: Optional[str]
    text: str
    score: int
    source: str
    review_status: str
    ocr_processed: bool
    book_id: Optional[str] = None
    search_method: str = "keyword"
    evidence_id: Optional[int] = None


@dataclass(frozen=True)
class SearchResponse:
    results: List[SearchResult]
    unsearchable_pages: int
    total_pages: int
    ocr_pending_pages: int
    truncated: bool


def _chunks(text: str):
    start = 0
    while start < len(text):
        yield text[start:start + CHUNK_SIZE]
        if start + CHUNK_SIZE >= len(text):
            break
        start += CHUNK_SIZE - CHUNK_OVERLAP


def _setup(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS search_pages (
           page_id INTEGER PRIMARY KEY REFERENCES pages(id) ON DELETE CASCADE,
           content_hash TEXT NOT NULL, source TEXT NOT NULL)"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS search_chunks (
           id INTEGER PRIMARY KEY, page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
           chunk_number INTEGER NOT NULL, text TEXT NOT NULL,
           UNIQUE(page_id, chunk_number))"""
    )
    connection.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(text, tokenize='trigram')"
    )


def _refresh(connection: sqlite3.Connection) -> None:
    _setup(connection)
    orphans = connection.execute(
        "SELECT rowid FROM search_fts WHERE rowid NOT IN (SELECT id FROM search_chunks)"
    ).fetchall()
    connection.executemany("DELETE FROM search_fts WHERE rowid = ?", orphans)
    pages = connection.execute(
        """SELECT p.id, p.extracted_text, c.corrected_text, o.text AS ocr_text,
                  sp.content_hash AS previous_hash
           FROM pages p
           LEFT JOIN page_corrections c ON c.page_id = p.id
           LEFT JOIN ocr_results o ON o.id = (
               SELECT id FROM ocr_results WHERE page_id = p.id ORDER BY id DESC LIMIT 1
           )
           LEFT JOIN search_pages sp ON sp.page_id = p.id ORDER BY p.id"""
    )
    for page in pages:
        if page["corrected_text"] is not None:
            text, source = page["corrected_text"], "corrected"
        elif page["ocr_text"] is not None and (page["ocr_text"].strip() or not page["extracted_text"].strip()):
            text, source = page["ocr_text"], "ocr"
        else:
            text, source = page["extracted_text"], "pdf"
        digest = hashlib.sha256((source + "\0" + text).encode("utf-8")).hexdigest()
        if digest == page["previous_hash"]:
            continue
        ids = connection.execute(
            "SELECT id FROM search_chunks WHERE page_id = ?", (page["id"],)
        ).fetchall()
        connection.executemany("DELETE FROM search_fts WHERE rowid = ?", ids)
        connection.execute("DELETE FROM search_chunks WHERE page_id = ?", (page["id"],))
        for number, chunk in enumerate(_chunks(text)):
            cursor = connection.execute(
                "INSERT INTO search_chunks(page_id, chunk_number, text) VALUES (?, ?, ?)",
                (page["id"], number, chunk),
            )
            connection.execute(
                "INSERT INTO search_fts(rowid, text) VALUES (?, ?)", (cursor.lastrowid, chunk)
            )
        connection.execute(
            """INSERT INTO search_pages(page_id, content_hash, source) VALUES (?, ?, ?)
               ON CONFLICT(page_id) DO UPDATE SET content_hash=excluded.content_hash,
               source=excluded.source""", (page["id"], digest, source)
        )


def search(data_dir: Path, query: str, book_id: Optional[str] = None,
           limit: int = 30) -> SearchResponse:
    terms = query.split()
    if not terms:
        raise SearchError("検索語を入力してください。")
    if len(terms) > MAX_TERMS or any(len(term) > MAX_TERM_LENGTH for term in terms):
        raise SearchError("検索語は{}語以内、各{}文字以内にしてください。".format(
            MAX_TERMS, MAX_TERM_LENGTH
        ))
    if not 1 <= limit <= 100:
        raise SearchError("検索結果の上限は1〜100件にしてください。")
    initialize(data_dir)
    with closing(connect(data_dir)) as connection, connection:
        _refresh(connection)
        book_clause = " AND p.book_id = ?" if book_id else ""
        book_params = [book_id] if book_id else []
        total, unsearchable, ocr_pending = connection.execute(
            """SELECT COUNT(*), COALESCE(SUM(CASE WHEN NOT EXISTS (
               SELECT 1 FROM search_chunks sc WHERE sc.page_id = p.id AND length(trim(sc.text)) > 0
               ) THEN 1 ELSE 0 END), 0),
               COALESCE(SUM(CASE WHEN p.ocr_needed = 1 AND p.ocr_processed = 0
               THEN 1 ELSE 0 END), 0) FROM pages p WHERE 1=1""" + book_clause,
            book_params,
        ).fetchone()
        conditions = " AND ".join("instr(sc.text, ?) > 0" for _ in terms)
        long_terms = [term for term in terms if len(term) >= 3]
        fts_clause = ""
        fts_params = []
        if long_terms:
            fts_clause = " AND sc.id IN (SELECT rowid FROM search_fts WHERE search_fts MATCH ?)"
            fts_params = [" AND ".join('"' + term.replace('"', '""') + '"' for term in long_terms)]
        score_sql = " + ".join(
            "(length(sc.text) - length(replace(sc.text, ?, ''))) / ?" for _ in terms
        )
        score_params = [item for term in terms for item in (term, len(term))]
        rows = connection.execute(
            """SELECT p.id AS page_id, b.id AS book_id, b.title, b.edition, p.pdf_page_number,
                      p.printed_page_number, sc.text, sp.source, p.review_status,
                      p.ocr_processed, (""" + score_sql + """) AS score
               FROM search_chunks sc JOIN pages p ON p.id = sc.page_id
               JOIN books b ON b.id = p.book_id
               JOIN search_pages sp ON sp.page_id = p.id
               WHERE """ + conditions + book_clause + fts_clause +
            " ORDER BY score DESC, b.title, p.pdf_page_number, sc.chunk_number LIMIT ?",
            score_params + terms + book_params + fts_params + [limit + 1],
        ).fetchall()
    return SearchResponse(
        results=[
            SearchResult(
                page_id=row["page_id"], book_id=row["book_id"], title=row["title"], edition=row["edition"],
                pdf_page_number=row["pdf_page_number"],
                printed_page_number=row["printed_page_number"], text=row["text"],
                score=row["score"], source=row["source"], review_status=row["review_status"],
                ocr_processed=bool(row["ocr_processed"]),
                search_method="keyword_fts5" if long_terms else "keyword_literal",
            ) for row in rows[:limit]
        ],
        total_pages=total, unsearchable_pages=unsearchable,
        ocr_pending_pages=ocr_pending, truncated=len(rows) > limit,
    )


def _primary_terms(text: str) -> List[str]:
    text = re.sub(
        r"正しいもの|誤っているもの|誤りは|どれか|選びなさい|選択せよ|"
        r"について|正しいのは|誤っているのは|いずれか", " ", text,
    )
    return list(dict.fromkeys(re.findall(r"[一-龯々〆ヶ]{2,}|[ァ-ヶー]{3,}|[A-Za-z0-9]{3,}", text)))


def _automatic_terms(text: str) -> List[str]:
    tokens = _primary_terms(text)
    terms = tokens[:40]
    for token in tokens:
        if len(token) > 3:
            for index in range(len(token) - 2):
                part = token[index:index + 3]
                if part not in terms:
                    terms.append(part)
        if re.fullmatch(r"[一-龯々〆ヶ]+", token) and len(token) > 2:
            for index in range(len(token) - 1):
                part = token[index:index + 2]
                if part not in terms:
                    terms.append(part)
    return terms[:40]


def search_choice_evidence(data_dir: Path, question: str, choice: str,
                           book_id: Optional[str] = None, limit: int = 3) -> SearchResponse:
    if not question.strip() or not choice.strip() or not 1 <= limit <= 3:
        raise SearchError("問題、選択肢、検索件数を確認してください。")
    choice_terms = _automatic_terms(choice)
    question_terms = _automatic_terms(question)
    primary_choice_terms = _primary_terms(choice)
    terms = list(dict.fromkeys(choice_terms + question_terms))
    initialize(data_dir)
    with closing(connect(data_dir)) as db, db:
        _refresh(db)
        book_clause = " AND p.book_id = ?" if book_id else ""
        book_params = [book_id] if book_id else []
        total, unsearchable, pending = db.execute(
            """SELECT COUNT(*), COALESCE(SUM(CASE WHEN NOT EXISTS (
               SELECT 1 FROM search_chunks sc WHERE sc.page_id=p.id AND length(trim(sc.text))>0
               ) THEN 1 ELSE 0 END), 0),
               COALESCE(SUM(CASE WHEN p.ocr_needed=1 AND p.ocr_processed=0
               THEN 1 ELSE 0 END), 0) FROM pages p WHERE 1=1""" + book_clause, book_params,
        ).fetchone()
        if not terms:
            return SearchResponse([], unsearchable, total, pending, False)
        long_terms = [term for term in terms if len(term) >= 3]
        short_terms = [term for term in terms if len(term) == 2]
        conditions = []
        params = []
        if long_terms:
            conditions.append("sc.id IN (SELECT rowid FROM search_fts WHERE search_fts MATCH ?)")
            params.append(" OR ".join('"' + term.replace('"', '""') + '"' for term in long_terms))
        if short_terms:
            conditions.append("(" + " OR ".join("instr(sc.text, ?) > 0" for _ in short_terms) + ")")
            params.extend(short_terms)
        if not conditions:
            return SearchResponse([], unsearchable, total, pending, False)
        rows = db.execute(
            """SELECT p.id AS page_id, b.id AS book_id, b.title, b.edition, p.pdf_page_number,
                      p.printed_page_number, sc.text, sp.source, p.review_status, p.ocr_processed
               FROM search_chunks sc JOIN pages p ON p.id=sc.page_id
               JOIN books b ON b.id=p.book_id JOIN search_pages sp ON sp.page_id=p.id
               WHERE (""" + " OR ".join(conditions) + ")" + book_clause,
            params + book_params,
        )
        ranked = []
        for row in rows:
            text = row["text"]
            choice_score = sum(len(term) for term in choice_terms if term in text)
            question_score = sum(len(term) for term in question_terms if term in text)
            exact_choice_score = sum(len(term) for term in primary_choice_terms if term in text)
            score = 2 * choice_score + question_score + 6 * exact_choice_score
            if score and (choice_score or not choice_terms):
                ranked.append((score, row))
        ranked.sort(key=lambda pair: (-pair[0], pair[1]["title"], pair[1]["pdf_page_number"]))
        results = []
        seen_pages = set()
        for score, row in ranked:
            if row["page_id"] in seen_pages:
                continue
            seen_pages.add(row["page_id"])
            results.append(SearchResult(
                row["page_id"], row["title"], row["edition"], row["pdf_page_number"],
                row["printed_page_number"], row["text"], score, row["source"],
                row["review_status"], bool(row["ocr_processed"]), row["book_id"],
                "keyword_fts5+literal" if long_terms and short_terms else
                "keyword_fts5" if long_terms else "keyword_literal",
            ))
            if len(results) > limit:
                break
    return SearchResponse(results[:limit], unsearchable, total, pending, len(results) > limit)


def refresh_candidates(data_dir: Path, candidates: Sequence[SearchResult]) -> List[SearchResult]:
    """Validate selected search candidates against the current derived index."""
    initialize(data_dir)
    with closing(connect(data_dir)) as db, db:
        _refresh(db)
        refreshed = []
        for candidate in candidates:
            row = db.execute(
                """SELECT p.id AS page_id, b.id AS book_id, b.title, b.edition,
                          p.pdf_page_number, p.printed_page_number, sc.text,
                          sp.source, p.review_status, p.ocr_processed
                   FROM search_chunks sc
                   JOIN pages p ON p.id = sc.page_id
                   JOIN books b ON b.id = p.book_id
                   JOIN search_pages sp ON sp.page_id = p.id
                   WHERE p.id = ? AND sc.text = ? AND sp.source = ?""",
                (candidate.page_id, candidate.text, candidate.source),
            ).fetchone()
            if row is None:
                raise SearchError("検索候補が更新されました。再検索して根拠を選び直してください。")
            refreshed.append(SearchResult(
                page_id=row["page_id"], title=row["title"], edition=row["edition"],
                pdf_page_number=row["pdf_page_number"],
                printed_page_number=row["printed_page_number"], text=row["text"],
                score=candidate.score, source=row["source"], review_status=row["review_status"],
                ocr_processed=bool(row["ocr_processed"]), book_id=row["book_id"],
                search_method=candidate.search_method,
            ))
    return refreshed
