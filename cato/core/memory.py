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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MEMORY_DIR = Path.home() / ".cato" / "memory"
_CHUNK_TOKENS = 400
_CHUNK_OVERLAP_TOKENS = 80
_MODEL_NAME = "all-MiniLM-L6-v2"

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
        logger.debug("Stored %d chunks from %s", len(chunks), source_file or "<inline>")
        return len(chunks)

    def search(self, query: str, top_k: int = 5) -> list[str]:
        """
        Hybrid BM25 + semantic search.  Returns top_k chunk strings.

        Scoring: 0.4 * normalised_bm25 + 0.6 * cosine_similarity.
        """
        # TODO v0.2: For corpora >5,000 chunks, replace with ANN index (faiss/hnswlib).
        # Current full-scan: ~15MB memory per search at 10K chunks.
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

    def close(self) -> None:
        """Close the SQLite connection."""
        self._conn.close()
