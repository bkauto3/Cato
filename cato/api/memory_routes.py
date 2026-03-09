"""
cato/api/memory_routes.py — Semantic memory search API endpoints.

Endpoints for searching indexed memory files using semantic similarity.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

# Global search engine instance (initialized on first request)
_search_engine = None


def _get_search_engine():
    """Lazy-load search engine on first use."""
    global _search_engine
    if _search_engine is None:
        try:
            from cato.core.semantic_search import SemanticSearchEngine
            _search_engine = SemanticSearchEngine()
            logger.info("Initialized semantic search engine")
        except Exception as e:
            logger.error(f"Failed to initialize search engine: {e}")
            raise
    return _search_engine


async def search_memory(request: web.Request) -> web.Response:
    """GET /api/memory/search?q=<query>&top_k=4 — Search indexed memory files."""
    try:
        query = request.query.get("q", "").strip()
        top_k = request.query.get("top_k", "4")

        if not query:
            return web.json_response({
                "success": False,
                "error": "Query parameter 'q' is required"
            }, status=400)

        try:
            top_k = int(top_k)
        except ValueError:
            top_k = 4

        engine = _get_search_engine()

        # Load memory file if not yet indexed
        from cato.config import CatoConfig
        config = CatoConfig.load()
        workspace_dir = Path(config.workspace_dir or Path.home() / ".cato" / "workspace").expanduser()
        memory_path = workspace_dir / "MEMORY.md"

        if memory_path.exists() and not engine.chunks:
            engine.load_memory_file(memory_path)
            logger.debug(f"Loaded memory file for search: {memory_path}")

        # Perform search
        results = engine.search(query, top_k=top_k)

        return web.json_response({
            "success": True,
            "query": query,
            "results": results,
            "count": len(results),
            "stats": engine.stats()
        })
    except Exception as e:
        logger.exception(f"Error searching memory: {e}")
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def index_memory(request: web.Request) -> web.Response:
    """POST /api/memory/index — Re-index memory file (clear and reload)."""
    try:
        engine = _get_search_engine()
        engine.clear()

        from cato.config import CatoConfig
        config = CatoConfig.load()
        workspace_dir = Path(config.workspace_dir or Path.home() / ".cato" / "workspace").expanduser()
        memory_path = workspace_dir / "MEMORY.md"

        if not memory_path.exists():
            return web.json_response({
                "success": False,
                "error": f"Memory file not found: {memory_path}"
            }, status=404)

        count = engine.load_memory_file(memory_path)

        return web.json_response({
            "success": True,
            "chunks_indexed": count,
            "stats": engine.stats()
        })
    except Exception as e:
        logger.exception(f"Error indexing memory: {e}")
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


async def memory_stats(request: web.Request) -> web.Response:
    """GET /api/memory/stats — Get search engine statistics."""
    try:
        engine = _get_search_engine()
        return web.json_response({
            "success": True,
            "stats": engine.stats()
        })
    except Exception as e:
        logger.exception(f"Error getting stats: {e}")
        return web.json_response({
            "success": False,
            "error": str(e)
        }, status=500)


def register_routes(app: web.Application) -> None:
    """Register memory search routes with the aiohttp Application."""
    app.router.add_get("/api/memory/search", search_memory)
    app.router.add_post("/api/memory/index", index_memory)
    app.router.add_get("/api/memory/stats", memory_stats)
    logger.info("Memory search routes registered")
