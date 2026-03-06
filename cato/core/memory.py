"""
cato/core/memory.py — Hybrid memory system for CATO.

Combines BM25 keyword search with sentence-transformer semantic embeddings.
Storage backend: SQLite at ~/.cato/memory/<agent_id>.db.
Chunking: ~400 tokens per chunk with 80-token overlap.
Ranking: 0.4 * bm25_score + 0.6 * semantic_score.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer

from ..platform import get_data_dir

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MEMORY_DIR = get_data_dir() / "memory"
_CHUNK_TOKENS = 400
_CHUNK_OVERLAP_TOKENS = 80
_MODEL_NAME = "all-MiniLM-L6-v2"

# ANN index threshold: use hnswlib when chunk count exceeds this value
ANN_THRESHOLD = 5_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT    NOT NULL,
    embedding   BLOB    NOT NULL,
    source_file TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source ON chunks(source_file);
CREATE TABLE IF NOT EXISTS distilled_summaries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL,
    turn_start     INTEGER NOT NULL,
    turn_end       INTEGER NOT NULL,
    summary        TEXT NOT NULL,
    key_facts      TEXT NOT NULL,
    decisions      TEXT NOT NULL,
    open_questions TEXT NOT NULL,
    confidence     REAL NOT NULL DEFAULT 0.75,
    created_at     TEXT NOT NULL,
    embedding      BLOB
);
CREATE INDEX IF NOT EXISTS idx_distill_session ON distilled_summaries(session_id);
CREATE TABLE IF NOT EXISTS chunk_usage (
    chunk_id      TEXT PRIMARY KEY,
    chunk_text    TEXT NOT NULL,
    use_count     INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    avg_score     REAL NOT NULL DEFAULT 0.0,
    last_used     REAL NOT NULL
);
"""


# ---------------------------------------------------------------------------
# MemorySystem
# ---------------------------------------------------------------------------

class MemorySystem:
    """
    Hybrid long-term memory using BM25 + sentence-transformer embeddings.

    Usage::

        mem = MemorySystem(agent_id="my-agent")
        mem.store("The capital of France is Paris.", source_file="MEMORY.md")
        results = mem.search("France capital city", top_k=3)
        for r in results:
            print(r)
    """

    def __init__(
        self,
        agent_id: str = "default",
        memory_dir: Optional[Path] = None,
    ) -> None:
        self._agent_id = agent_id
        self._dir = (memory_dir or _MEMORY_DIR).expanduser().resolve()
        self._dir.mkdir(parents=True, exist_ok=True)

        self._db_path = self._dir / f"{agent_id}.db"
        self._write_lock = threading.Lock()
        self._conn = self._open_db()

        # Lazy-load sentence transformer (heavy — only once per process)
        self._embed_model: Optional[SentenceTransformer] = None

        # ANN index (hnswlib) — built lazily when chunk count > ANN_THRESHOLD
        self._ann_index: Optional[object] = None
        self._ann_index_ids: list[int] = []   # maps HNSW internal id -> SQLite row id
        self._ann_dirty: bool = True           # True when index needs rebuild

    # ------------------------------------------------------------------
    # Lazy embedding model
    # ------------------------------------------------------------------

    def _get_embed_model(self) -> SentenceTransformer:
        if self._embed_model is None:
            logger.info("Loading embedding model %s ...", _MODEL_NAME)
            self._embed_model = SentenceTransformer(_MODEL_NAME)
        return self._embed_model

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _open_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        return conn

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Chunking
    # ------------------------------------------------------------------

    @staticmethod
    def _tokenize_simple(text: str) -> list[str]:
        """Rough word-level tokeniser — used only for chunk sizing."""
        return text.split()

    def _chunk_text(self, text: str) -> list[str]:
        """
        Split *text* into overlapping chunks of ~_CHUNK_TOKENS words.

        Overlap of _CHUNK_OVERLAP_TOKENS words is kept between consecutive
        chunks to preserve context across boundaries.
        """
        words = self._tokenize_simple(text)
        if len(words) <= _CHUNK_TOKENS:
            return [text] if text.strip() else []

        chunks: list[str] = []
        start = 0
        step = _CHUNK_TOKENS - _CHUNK_OVERLAP_TOKENS
        while start < len(words):
            end = min(start + _CHUNK_TOKENS, len(words))
            chunk = " ".join(words[start:end])
            if chunk.strip():
                chunks.append(chunk)
            if end >= len(words):
                break
            start += step

        return chunks

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    def _embed(self, texts: list[str]) -> list[bytes]:
        """Return embedding blobs (numpy float32 arrays serialised as bytes)."""
        model = self._get_embed_model()
        vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [v.astype(np.float32).tobytes() for v in vecs]

    @staticmethod
    def _bytes_to_vec(blob: bytes) -> np.ndarray:
        return np.frombuffer(blob, dtype=np.float32)

    @staticmethod
    def _cosine(a: np.ndarray, b: np.ndarray) -> float:
        denom = np.linalg.norm(a) * np.linalg.norm(b)
        if denom == 0:
            return 0.0
        return float(np.dot(a, b) / denom)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def store(self, content: str, source_file: str = "") -> int:
        """
        Chunk *content* and store each chunk with its embedding.

        Returns the number of chunks written.
        """
        chunks = self._chunk_text(content)
        if not chunks:
            return 0

        blobs = self._embed(chunks)
        now = self._now_iso()
        rows = [
            (chunk, blob, source_file, now, now)
            for chunk, blob in zip(chunks, blobs)
        ]
        with self._write_lock:
            self._conn.executemany(
                "INSERT INTO chunks (content, embedding, source_file, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()
        self._ann_dirty = True  # Invalidate ANN index on new writes
        logger.debug("Stored %d chunks from %s", len(chunks), source_file or "<inline>")
        return len(chunks)

    # ------------------------------------------------------------------
    # ANN index (hnswlib opt-in — P2-10)
    # ------------------------------------------------------------------

    def _build_ann_index_if_needed(self) -> None:
        """
        Build an hnswlib ANN index if chunk_count > ANN_THRESHOLD and
        hnswlib is importable.

        Falls back silently to brute-force if hnswlib is not installed.
        The index is rebuilt whenever _ann_dirty is True.
        """
        if not self._ann_dirty:
            return

        count = self.chunk_count()
        if count <= ANN_THRESHOLD:
            return  # Still small enough for brute-force

        try:
            import hnswlib  # type: ignore[import]
        except ImportError:
            logger.debug("hnswlib not installed — using brute-force search (chunk_count=%d)", count)
            return

        try:
            rows = self._conn.execute(
                "SELECT id, embedding FROM chunks ORDER BY id"
            ).fetchall()

            if not rows:
                return

            # Determine embedding dimension from first row
            first_vec = self._bytes_to_vec(rows[0]["embedding"])
            dim = first_vec.shape[0]

            index = hnswlib.Index(space="cosine", dim=dim)
            index.init_index(max_elements=len(rows), ef_construction=200, M=16)

            ids: list[int] = []
            vecs = []
            for i, row in enumerate(rows):
                ids.append(row["id"])
                vecs.append(self._bytes_to_vec(row["embedding"]))

            import numpy as _np
            index.add_items(_np.array(vecs, dtype=_np.float32), list(range(len(ids))))
            index.set_ef(50)

            self._ann_index = index
            self._ann_index_ids = ids
            self._ann_dirty = False
            logger.info("ANN index built with %d chunks (dim=%d)", len(ids), dim)

        except Exception as exc:
            logger.warning("Failed to build ANN index: %s — falling back to brute-force", exc)
            self._ann_index = None

    def _search_embeddings(self, query_vec: "np.ndarray", top_k: int) -> list[dict]:
        """
        Route embedding search to HNSW ANN index or brute-force cosine scan.

        Returns list of dicts with keys: id, content, score.
        """
        self._build_ann_index_if_needed()

        rows = self._conn.execute(
            "SELECT id, content, embedding FROM chunks"
        ).fetchall()

        if not rows:
            return []

        if self._ann_index is not None and len(rows) > ANN_THRESHOLD:
            # HNSW fast path
            try:
                labels, distances = self._ann_index.knn_query(
                    query_vec.reshape(1, -1), k=min(top_k, len(self._ann_index_ids))
                )
                results = []
                for label, dist in zip(labels[0], distances[0]):
                    if label < len(self._ann_index_ids):
                        row_id = self._ann_index_ids[label]
                        row = self._conn.execute(
                            "SELECT content FROM chunks WHERE id = ?", (row_id,)
                        ).fetchone()
                        if row:
                            results.append({"id": row_id, "content": row["content"], "score": float(1 - dist)})
                return results
            except Exception as exc:
                logger.warning("ANN search failed: %s — falling back to brute-force", exc)

        # Brute-force fallback
        contents = [r["content"] for r in rows]
        embeddings = [self._bytes_to_vec(r["embedding"]) for r in rows]
        import numpy as _np
        scores = _np.array([self._cosine(query_vec, e) for e in embeddings])
        top_indices = _np.argsort(scores)[::-1][:top_k]
        return [
            {"id": rows[i]["id"], "content": contents[i], "score": float(scores[i])}
            for i in top_indices
        ]

    def search(self, query: str, top_k: int = 5) -> list[str]:
        """
        Hybrid BM25 + semantic search.  Returns top_k chunk strings.

        Scoring: 0.4 * normalised_bm25 + 0.6 * cosine_similarity.
        Routes to HNSW ANN index when chunk_count > ANN_THRESHOLD.
        """
        rows = self._conn.execute(
            "SELECT id, content, embedding FROM chunks"
        ).fetchall()

        if not rows:
            return []

        contents = [r["content"] for r in rows]
        embeddings = [self._bytes_to_vec(r["embedding"]) for r in rows]

        # BM25
        tokenized_corpus = [c.lower().split() for c in contents]
        bm25 = BM25Okapi(tokenized_corpus)
        query_tokens = query.lower().split()
        bm25_scores_raw = bm25.get_scores(query_tokens)

        # Normalise BM25 to [0, 1]
        bm25_max = float(np.max(bm25_scores_raw)) if np.max(bm25_scores_raw) > 0 else 1.0
        bm25_scores = bm25_scores_raw / bm25_max

        # Semantic
        q_vec = self._get_embed_model().encode(
            [query], normalize_embeddings=True, show_progress_bar=False
        )[0].astype(np.float32)
        sem_scores = np.array([self._cosine(q_vec, e) for e in embeddings])

        # Combined
        combined = 0.4 * bm25_scores + 0.6 * sem_scores
        top_indices = np.argsort(combined)[::-1][:top_k]

        return [contents[i] for i in top_indices]

    def flush_to_disk(self, content: str, date_str: str) -> Path:
        """
        Write *content* to the daily memory log file for *date_str* (YYYY-MM-DD).

        Appends if the file already exists so multiple flush calls accumulate.
        Returns the path written to.
        """
        out_path = self._dir / f"{date_str}.md"
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        entry = f"\n\n<!-- flushed at {ts} -->\n{content.strip()}\n"
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(entry)
        logger.debug("Flushed %d chars to %s", len(content), out_path)
        return out_path

    def load_workspace_files(self, workspace_dir: Path) -> int:
        """
        Index all .md files in *workspace_dir* that have not yet been stored.

        Compares source_file paths to avoid re-indexing unchanged files.
        Returns the number of new chunks written.
        """
        workspace_dir = workspace_dir.expanduser().resolve()
        md_files = sorted(workspace_dir.glob("**/*.md"))

        # Fetch already-indexed paths
        existing = {
            r[0]
            for r in self._conn.execute(
                "SELECT DISTINCT source_file FROM chunks"
            ).fetchall()
        }

        total_chunks = 0
        for md_file in md_files:
            path_key = str(md_file)
            if path_key in existing:
                logger.debug("Skipping already-indexed %s", md_file.name)
                continue
            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
                n = self.store(content, source_file=path_key)
                total_chunks += n
                logger.info("Indexed %s: %d chunks", md_file.name, n)
            except OSError as exc:
                logger.warning("Could not read %s: %s", md_file, exc)

        return total_chunks

    # ------------------------------------------------------------------
    # Async wrappers
    # ------------------------------------------------------------------

    async def astore(self, content: str, source_file: str = "") -> int:
        """Async wrapper around :meth:`store`."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.store, content, source_file)

    async def asearch(self, query: str, top_k: int = 5) -> list[str]:
        """Async wrapper around :meth:`search`."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.search, query, top_k)

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def delete_by_source(self, source_file: str) -> int:
        """Delete all chunks for a given source file. Returns deleted count."""
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM chunks WHERE source_file = ?", (source_file,)
            )
            self._conn.commit()
        return cur.rowcount

    def chunk_count(self) -> int:
        """Return total number of stored chunks."""
        return self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    # ------------------------------------------------------------------
    # Distillation support
    # ------------------------------------------------------------------

    def store_distillation(self, result: "DistillationResult") -> int:
        """
        Persist a :class:`DistillationResult` to the ``distilled_summaries`` table.

        Returns the SQLite rowid of the inserted row.
        """
        # Import here to avoid circular dependency with distiller module
        from .distiller import DistillationResult  # noqa: F401

        key_facts_json = json.dumps(result.key_facts)
        decisions_json = json.dumps(result.decisions)
        open_questions_json = json.dumps(result.open_questions)

        with self._write_lock:
            cur = self._conn.execute(
                """
                INSERT INTO distilled_summaries
                    (session_id, turn_start, turn_end, summary,
                     key_facts, decisions, open_questions,
                     confidence, created_at, embedding)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.session_id,
                    result.turn_start,
                    result.turn_end,
                    result.summary,
                    key_facts_json,
                    decisions_json,
                    open_questions_json,
                    result.confidence,
                    result.created_at,
                    result.embedding,
                ),
            )
            self._conn.commit()
        return cur.lastrowid

    def search_distilled(
        self,
        query: str,
        session_id: str | None = None,
        top_k: int = 3,
    ) -> list[dict]:
        """
        Cosine similarity search over distilled summary embeddings.

        Args:
            query: Search query string.
            session_id: If provided, restrict search to this session.
            top_k: Maximum number of results to return.

        Returns:
            List of dicts with keys: id, session_id, turn_start, turn_end,
            summary, key_facts, decisions, open_questions, confidence, score,
            source_file.
        """
        if session_id:
            rows = self._conn.execute(
                "SELECT * FROM distilled_summaries WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM distilled_summaries"
            ).fetchall()

        if not rows:
            return []

        # Build query embedding
        q_vec = self._get_embed_model().encode(
            [query], normalize_embeddings=True, show_progress_bar=False
        )[0].astype(np.float32)

        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            if row["embedding"] is None:
                score = 0.0
            else:
                row_vec = self._bytes_to_vec(row["embedding"])
                score = self._cosine(q_vec, row_vec)
            scored.append((score, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_k]

        results = []
        for score, row in top:
            sid = row["session_id"]
            ts = row["turn_start"]
            te = row["turn_end"]
            results.append(
                {
                    "id": row["id"],
                    "session_id": sid,
                    "turn_start": ts,
                    "turn_end": te,
                    "summary": row["summary"],
                    "key_facts": json.loads(row["key_facts"]),
                    "decisions": json.loads(row["decisions"]),
                    "open_questions": json.loads(row["open_questions"]),
                    "confidence": row["confidence"],
                    "score": score,
                    "source_file": f"distill:{sid}:{ts}-{te}",
                }
            )
        return results

    def close(self) -> None:
        """Close the SQLite connection."""
        self._conn.close()
