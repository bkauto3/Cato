"""
WebSearchTool — API-based multi-engine search with fallback chain.

Replaces fragile DDG browser scrape. Uses DDG Instant Answer API, Brave, Exa,
Tavily, and optional SearXNG. Query classification routes to code/news/academic/general
chains. Results are cached in memory with TTL. conduit_bridge.browser.search
remains the zero-config fallback when all API providers fail.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

logger = logging.getLogger(__name__)

QueryType = Literal["code", "news", "academic", "general"]

cato/tools/web_search.py — Web-Search-Plus multi-engine search tool.

Provides async search across multiple engines with:
- Query classification (code / news / academic / general)
- Engine-specific fallback chains
- Heuristic confidence scoring + cross-engine agreement boosting
- Result caching via core/memory.py
- Rate-limit handling (429 → 60s cooldown per provider)
- All API keys loaded from Vault — never hardcoded

Supported engines:
    DDG Instant Answer API  — no key needed
    Brave Search            — vault key: brave_api_key
    Exa                     — vault key: exa_api_key
    Tavily                  — vault key: tavily_api_key
    arXiv Atom XML          — no key needed
    Semantic Scholar        — optional vault key: semantic_scholar_api_key
    PubMed / NCBI Entrez    — no key needed (3 req/sec limit enforced)
    SearXNG (self-hosted)   — config key: searxng_url
    Perplexity              — vault key: perplexity_api_key (--depth deep only)
"""

from __future__ import annotations

import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Literal, Optional
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    source_engine: str
    confidence: float = 0.0
    rank: int = 0
    published_date: str = ""
    # Academic-only
    authors: str = ""
    doi: str = ""
    year: str = ""
    citation_count: int = 0
    pdf_url: str = ""
    venue: str = ""
    source: str = ""
    rerank_score: float = 0.0

    def to_dict(self) -> dict:
        d = {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source_engine": self.source_engine,
            "confidence": round(self.confidence, 3),
            "rank": self.rank,
            "published_date": self.published_date,
        }
        if self.rerank_score:
            d["rerank_score"] = round(self.rerank_score, 4)
        if self.authors:
            d["authors"] = self.authors
        if self.doi:
            d["doi"] = self.doi
        if self.year:
            d["year"] = self.year
        if self.citation_count:
            d["citation_count"] = self.citation_count
        if self.pdf_url:
            d["pdf_url"] = self.pdf_url
        if self.venue:
            d["venue"] = self.venue
        if self.source:
            d["source"] = self.source
        return d


def classify_query(query: str) -> QueryType:
    """Classify query for engine routing."""
    q = query.lower()
    code_terms = ("github", "stackoverflow", "docs", "api", "function", "library", "error", "bug")
    if any(t in q for t in code_terms):
        return "code"
    academic_terms = (
        "arxiv", "paper", "doi", "cite", "journal", "study", "et al", "meta-analysis",
        "published in", "systematic review", "literature review", "peer-reviewed",
    )
    if any(t in q for t in academic_terms):
        return "academic"
    news_terms = ("today", "latest", "breaking", "announced")
    if any(t in q for t in news_terms):
        return "news"
    return "general"


# Fallback chains per query type (C1 spec). ddg_browser is ultimate fallback (caller uses conduit_bridge.browser.search).
_CHAIN_CODE = ["exa", "brave", "searxng", "ddg_api"]
_CHAIN_NEWS = ["tavily", "brave", "searxng", "ddg_api"]
_CHAIN_ACADEMIC = ["semantic_scholar", "arxiv", "pubmed", "exa"]
_CHAIN_GENERAL = ["brave", "searxng", "ddg_api"]


def _heuristic_confidence(query: str, result: SearchResult) -> float:
    """Compute confidence score: rank, domain authority, freshness, query overlap. Cap at 1.0."""
    score = 1.0 / (1 + result.rank)
    url_lower = (result.url or "").lower()
    if ".edu" in url_lower or ".gov" in url_lower:
        score += 0.15
    elif ".org" in url_lower:
        score += 0.10
    if result.published_date:
        try:
            from datetime import datetime, timedelta
            # Accept YYYY-MM-DD or YYYY
            s = result.published_date.strip()[:10]
            if len(s) >= 4:
                year = int(s[:4])
                month = int(s[5:7]) if len(s) >= 7 else 1
                day = int(s[8:10]) if len(s) >= 10 else 1
                pub = datetime(year, month, day)
                if (datetime.utcnow() - pub).days <= 180:
                    score += 0.10
        except Exception:
            pass
    q_terms = set(query.lower().split())
    text = f"{result.title} {result.snippet}".lower()
    overlap = sum(1 for t in q_terms if len(t) > 1 and t in text) / max(len(q_terms), 1)
    score += overlap * 0.2
    return min(1.0, score)


def _cross_engine_agreement(results: list[SearchResult]) -> list[SearchResult]:
    """Results appearing 2+ times (same URL) get +0.15 confidence boost."""
    url_count: dict[str, int] = {}
    for r in results:
        u = (r.url or "").strip()
        if u:
            url_count[u] = url_count.get(u, 0) + 1
    out = []
    for r in results:
        r2 = SearchResult(
            title=r.title, url=r.url, snippet=r.snippet, source_engine=r.source_engine,
            confidence=r.confidence, rank=r.rank, published_date=r.published_date,
            authors=r.authors, doi=r.doi, year=r.year, citation_count=r.citation_count,
            pdf_url=r.pdf_url, venue=r.venue, source=r.source, rerank_score=getattr(r, "rerank_score", 0.0),
        )
        if url_count.get(r.url, 0) >= 2:
            r2.confidence = min(1.0, r2.confidence + 0.15)
        out.append(r2)
    return out


class WebSearchTool:
    """Multi-engine web search with ordered fallback and optional cache."""

    def __init__(
        self,
        config: Optional[Any] = None,
        vault: Optional[dict] = None,
        cache: Optional[Any] = None,
        cache_ttl_sec: int = 3600,
        academic_cache_ttl_sec: int = 86400,
    ) -> None:
        self._config = config
        self._vault = vault or (getattr(config, "vault", None) if config else None) or {}
        self._cache = cache
        self._cache_ttl = cache_ttl_sec
        self._academic_cache_ttl = academic_cache_ttl_sec
        self._rate_limited: dict[str, float] = {}
        self._rate_limit_window_sec = 60

    def _get_key(self, key: str):
        if self._vault and isinstance(self._vault, dict):
            return self._vault.get(key)
        if self._config and hasattr(self._config, "get"):
            return self._config.get(key)
        return None

    def _is_rate_limited(self, provider: str) -> bool:
        until = self._rate_limited.get(provider, 0)
        return time.monotonic() < until

    def _mark_rate_limited(self, provider: str) -> None:
        self._rate_limited[provider] = time.monotonic() + self._rate_limit_window_sec
        logger.warning("Provider %s marked rate-limited for 60s", provider)

    async def _search_ddg_api(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """DDG Instant Answer API — no key, no browser."""
        try:
            import aiohttp
            url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 429:
                        self._mark_rate_limited("ddg_api")
                        return []
                    data = await resp.json()
            results = []
            for i, r in enumerate((data.get("RelatedTopics") or [])[:max_results]):
                if isinstance(r, dict) and "FirstURL" in r:
                    results.append(SearchResult(
                        title=r.get("Text", r.get("FirstURL", ""))[:200],
                        url=r.get("FirstURL", ""),
                        snippet=r.get("Text", "")[:500],
                        source_engine="ddg_api",
                        rank=i + 1,
                    ))
                elif isinstance(r, dict) and "Topics" in r:
                    for t in r.get("Topics", [])[:3]:
                        if isinstance(t, dict) and t.get("FirstURL"):
                            results.append(SearchResult(
                                title=t.get("Text", t.get("FirstURL", ""))[:200],
                                url=t.get("FirstURL", ""),
                                snippet=t.get("Text", "")[:500],
                                source_engine="ddg_api",
                                rank=len(results) + 1,
                            ))
            return results[:max_results]
        except Exception as e:
            logger.debug("DDG API search failed: %s", e)
            return []

    async def _search_brave(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """Brave Search API."""
        key = self._get_key("brave_api_key")
        if not key:
            return []
        try:
            import aiohttp
            url = "https://api.search.brave.com/res/v1/web/search"
            headers = {"X-Subscription-Token": key, "Accept": "application/json"}
            params = {"q": query, "count": max_results}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 429:
                        self._mark_rate_limited("brave")
                        return []
                    data = await resp.json()
            results = []
            for i, w in enumerate((data.get("web", {}).get("results") or [])[:max_results]):
                results.append(SearchResult(
                    title=w.get("title", "")[:200],
                    url=w.get("url", ""),
                    snippet=w.get("description", "")[:500],
                    source_engine="brave",
                    rank=i + 1,
                    published_date=w.get("age", "") or "",
                ))
            return results
        except Exception as e:
            logger.debug("Brave search failed: %s", e)
            return []

    async def _search_exa(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """Exa API."""
        key = self._get_key("exa_api_key")
        if not key:
            return []
        try:
            import aiohttp
            url = "https://api.exa.ai/search"
            headers = {"x-api-key": key, "Content-Type": "application/json"}
            payload = {"query": query, "numResults": max_results}
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 429:
                        self._mark_rate_limited("exa")
                        return []
                    data = await resp.json()
            results = []
            for i, r in enumerate((data.get("results") or [])[:max_results]):
                results.append(SearchResult(
                    title=(r.get("title") or "")[:200],
                    url=r.get("url", ""),
                    snippet=(r.get("text") or r.get("snippet") or "")[:500],
                    source_engine="exa",
                    rank=i + 1,
                    published_date=r.get("publishedDate", "") or "",
                ))
            return results
        except Exception as e:
            logger.debug("Exa search failed: %s", e)
            return []

    async def _search_tavily(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """Tavily API."""
        key = self._get_key("tavily_api_key")
        if not key:
            return []
        try:
            import aiohttp
            url = "https://api.tavily.com/search"
            payload = {"api_key": key, "query": query, "include_answer": False, "max_results": max_results}
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 429:
                        self._mark_rate_limited("tavily")
                        return []
                    data = await resp.json()
            results = []
            for i, r in enumerate((data.get("results") or [])[:max_results]):
                results.append(SearchResult(
                    title=(r.get("title") or "")[:200],
                    url=r.get("url", ""),
                    snippet=(r.get("content") or "")[:500],
                    source_engine="tavily",
                    rank=i + 1,
                ))
            return results
        except Exception as e:
            logger.debug("Tavily search failed: %s", e)
            return []

    async def _search_searxng(self, query: str, instance_url: str, max_results: int = 10) -> list[SearchResult]:
        """SearXNG — privacy fallback, no API key."""
        if not instance_url.strip():
            return []
        try:
            import aiohttp
            base = instance_url.rstrip("/")
            url = f"{base}/search?q={urllib.parse.quote(query)}&format=json"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 429:
                        self._mark_rate_limited("searxng")
                        return []
                    data = await resp.json()
            results = []
            for i, r in enumerate((data.get("results") or [])[:max_results]):
                results.append(SearchResult(
                    title=(r.get("title") or "")[:200],
                    url=r.get("url", ""),
                    snippet=(r.get("content") or "")[:500],
                    source_engine="searxng",
                    rank=i + 1,
                ))
            return results
        except Exception as e:
            logger.debug("SearXNG search failed: %s", e)
            return []

    def _get_chain(self, query_type: QueryType) -> list[str]:
        if query_type == "code":
            return list(_CHAIN_CODE)
        if query_type == "news":
            return list(_CHAIN_NEWS)
        if query_type == "academic":
            return list(_CHAIN_ACADEMIC)
        return list(_CHAIN_GENERAL)

    async def _run_chain(
        self,
        query: str,
        chain: list[str],
        max_results: int = 10,
        searxng_url: str = "",
    ) -> list[SearchResult]:
        """Try each provider in order; return first non-empty list or []."""
        for provider in chain:
            if self._is_rate_limited(provider):
                continue
            try:
                if provider == "ddg_api":
                    results = await self._search_ddg_api(query, max_results)
                elif provider == "brave":
                    results = await self._search_brave(query, max_results)
                elif provider == "exa":
                    results = await self._search_exa(query, max_results)
                elif provider == "tavily":
                    results = await self._search_tavily(query, max_results)
                elif provider == "searxng" and searxng_url:
                    results = await self._search_searxng(query, searxng_url, max_results)
                else:
                    continue
                if results:
                    return results
            except (asyncio.TimeoutError, OSError) as e:
                logger.warning("Provider %s failed: %s", provider, e)
        return []
    confidence: float
    rank: int
    published_date: str = ""


QueryType = Literal["code", "news", "academic", "general"]
DepthType = Literal["normal", "deep"]

# ---------------------------------------------------------------------------
# Query classifier
# ---------------------------------------------------------------------------

_CODE_KEYWORDS = frozenset({
    "github", "stackoverflow", "docs", "api", "function",
    "library", "error", "bug", "python", "javascript", "typescript",
    "npm", "pip", "import", "class", "def ", "async", "await",
})
_ACADEMIC_KEYWORDS = frozenset({
    "arxiv", "paper", "doi", "cite", "journal", "study", "et al",
    "abstract", "research", "thesis", "publication", "review",
})
_NEWS_KEYWORDS = frozenset({
    "today", "latest", "breaking", "announced", "release", "launches",
    "just in", "new report", "update",
})


def classify_query(query: str) -> QueryType:
    """
    Return the most appropriate search category for *query*.

    Priority: code > academic > news > general
    """
    q_lower = query.lower()
    for kw in _CODE_KEYWORDS:
        if kw in q_lower:
            return "code"
    for kw in _ACADEMIC_KEYWORDS:
        if kw in q_lower:
            return "academic"
    for kw in _NEWS_KEYWORDS:
        if kw in q_lower:
            return "news"
    return "general"


# ---------------------------------------------------------------------------
# Fallback chains per query type
# ---------------------------------------------------------------------------

_FALLBACK_CHAINS: dict[QueryType, list[str]] = {
    "code":     ["exa", "brave", "searxng", "ddg_api"],
    "news":     ["tavily", "brave", "searxng", "ddg_api"],
    "academic": ["semantic_scholar", "arxiv", "exa"],
    "general":  ["brave", "searxng", "ddg_api"],
}


# ---------------------------------------------------------------------------
# WebSearchTool
# ---------------------------------------------------------------------------

class WebSearchTool:
    """
    Multi-engine web search with fallback chains, confidence scoring, and caching.

    Usage::

        tool = WebSearchTool(vault=vault)
        results = await tool.search("Python asyncio tutorial", query_type="code")
    """

    def __init__(
        self,
        vault: Any = None,
        searxng_url: str = "",
        cache_ttl_seconds: int = 3600,
        memory: Any = None,
    ) -> None:
        self._vault = vault
        self._searxng_url = searxng_url
        self._cache_ttl = cache_ttl_seconds
        self._memory = memory

        # Per-provider rate-limit cooldown: provider_name -> cooldown_until (epoch)
        self._rate_limited_until: dict[str, float] = {}

        # PubMed rate limiter: 3 req/sec
        self._pubmed_semaphore = asyncio.Semaphore(3)

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    async def search(
        self,
        query: str,
        query_type: Optional[QueryType] = None,
        max_results: int = 10,
        use_cache: bool = True,
    ) -> dict:
        """
        Run search via classified chain. Returns {query, results, source}.
        Does not raise — returns empty results on total failure.
        """
        qtype = query_type or classify_query(query)
        searxng_url = (
            getattr(self._config, "searxng_url", "") if self._config else ""
        ) or ""

        if use_cache and self._cache:
            try:
                from cato.core import memory as _mem
                cache = self._cache if hasattr(self._cache, "get") else _mem.get_cache()
                ttl = self._cache_ttl
                source_file = "web_search_cache"
                if qtype == "academic":
                    ttl = self._academic_cache_ttl
                    source_file = "academic_cache"
                cached = cache.get(source_file, query, ttl)
                if cached is not None:
                    return cached
            except Exception:
                pass

        chain = self._get_chain(qtype)
        # Academic uses separate methods (arxiv, semantic_scholar, pubmed) — handled below
        if qtype == "academic":
            results = await self._search_academic_chain(query, max_results)
        else:
            results = await self._run_chain(query, chain, max_results, searxng_url)

        # Apply heuristic confidence, cross-engine agreement, sort by confidence desc
        for r in results:
            r.confidence = _heuristic_confidence(query, r)
        results = _cross_engine_agreement(results)
        results.sort(key=lambda r: r.confidence, reverse=True)
        # Optional cross-encoder rerank (gated by config)
        results = self._rerank(query, results, threshold=5)

        out = {
            "query": query,
            "query_type": qtype,
            "results": [r.to_dict() for r in results],
            "source": "web_search",
        }
        if use_cache and self._cache and results:
            try:
                from cato.core import memory as _mem
                cache = self._cache if hasattr(self._cache, "get") else _mem.get_cache()
                ttl = self._cache_ttl
                source_file = "web_search_cache"
                if qtype == "academic":
                    ttl = self._academic_cache_ttl
                    source_file = "academic_cache"
                cache.set(source_file, query, out, ttl)
            except Exception:
                pass
        return out

    async def _search_academic_chain(self, query: str, max_results: int) -> list[SearchResult]:
        """Academic chain: semantic_scholar, arxiv, pubmed, exa."""
        for provider in _CHAIN_ACADEMIC:
            if self._is_rate_limited(provider):
                continue
            try:
                if provider == "arxiv":
                    results = await self._search_arxiv(query, max_results)
                elif provider == "semantic_scholar":
                    results = await self._search_semantic_scholar(query, max_results)
                elif provider == "pubmed":
                    results = await self._search_pubmed(query, max_results)
                elif provider == "exa":
                    results = await self._search_exa(query, max_results)
                else:
                    continue
                if results:
                    return results
            except (asyncio.TimeoutError, OSError) as e:
                logger.warning("Academic provider %s failed: %s", provider, e)
        return []

    async def _search_arxiv(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """arXiv API — Atom XML, no key."""
        try:
            import aiohttp
            import xml.etree.ElementTree as ET
            url = f"http://export.arxiv.org/api/query?search_query={urllib.parse.quote(query)}&max_results={max_results}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 429:
                        self._mark_rate_limited("arxiv")
                        return []
                    text = await resp.text()
            root = ET.fromstring(text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            results = []
            for i, entry in enumerate(root.findall("atom:entry", ns)[:max_results]):
                title = (entry.find("atom:title", ns) or entry.find("title"))
                title_text = (title.text or "").strip().replace("\n", " ")[:200]
                link = entry.find("atom:link[@title='pdf']", ns) or entry.find("atom:link[@type='application/pdf']", ns)
                pdf_url = ""
                if link is not None and link.get("href"):
                    pdf_url = link.get("href", "")
                summary = entry.find("atom:summary", ns) or entry.find("summary")
                snippet = (summary.text or "").strip().replace("\n", " ")[:500] if summary is not None else ""
                id_el = entry.find("atom:id", ns) or entry.find("id")
                url_str = (id_el.text or "").strip() if id_el is not None else ""
                authors_el = entry.find("atom:author", ns) or entry.find("author")
                authors_str = ""
                if authors_el is not None:
                    names = entry.findall("atom:author/atom:name", ns) or entry.findall("author/name")
                    authors_str = ", ".join((n.text or "").strip() for n in names)[:200]
                published = entry.find("atom:published", ns) or entry.find("published")
                published_date = (published.text or "")[:10] if published is not None else ""
                results.append(SearchResult(
                    title=title_text,
                    url=url_str,
                    snippet=snippet,
                    source_engine="arxiv",
                    rank=i + 1,
                    published_date=published_date,
                    pdf_url=pdf_url,
                    authors=authors_str,
                    source="arxiv",
                ))
            return results
        except Exception as e:
            logger.debug("arXiv search failed: %s", e)
            return []

    async def _search_semantic_scholar(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """Semantic Scholar API — optional key, 100 req/5min without."""
        try:
            import aiohttp
            key = self._get_key("semantic_scholar_api_key")
            url = "https://api.semanticscholar.org/graph/v1/paper/search"
            params = {"query": query, "limit": max_results, "fields": "title,authors,year,citationCount,openAccessPdf,venue,url"}
            headers = {}
            if key:
                headers["x-api-key"] = key
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers or None, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 429:
                        self._mark_rate_limited("semantic_scholar")
                        return []
                    data = await resp.json()
            results = []
            for i, p in enumerate((data.get("data") or [])[:max_results]):
                pdf_url = ""
                oa = (p.get("openAccessPdf") or {})
                if isinstance(oa, dict) and oa.get("url"):
                    pdf_url = oa.get("url", "")
                authors = p.get("authors") or []
                authors_str = ", ".join(a.get("name", "") for a in authors)[:200]
                results.append(SearchResult(
                    title=(p.get("title") or "")[:200],
                    url=p.get("url", ""),
                    snippet="",
                    source_engine="semantic_scholar",
                    rank=i + 1,
                    year=str(p.get("year", "")),
                    citation_count=int(p.get("citationCount", 0) or 0),
                    pdf_url=pdf_url,
                    venue=(p.get("venue") or "")[:100],
                    authors=authors_str,
                    source="semantic_scholar",
                ))
            return results
        except Exception as e:
            logger.debug("Semantic Scholar search failed: %s", e)
            return []

    async def _search_pubmed(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """PubMed E-utilities — no key, 3 req/sec."""
        try:
            import aiohttp
            url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
            params = {"db": "pubmed", "term": query, "retmode": "json", "retmax": max_results}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 429:
                        self._mark_rate_limited("pubmed")
                        return []
                    data = await resp.json()
            id_list = (data.get("esearchresult", {}).get("idlist") or [])[:max_results]
            if not id_list:
                return []
            # Optional: efetch for titles/links — keep simple and return IDs as URLs
            results = []
            for i, pid in enumerate(id_list):
                results.append(SearchResult(
                    title=f"PubMed {pid}",
                    url=f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
                    snippet="",
                    source_engine="pubmed",
                    rank=i + 1,
                    source="pubmed",
                ))
            return results
        except Exception as e:
            logger.debug("PubMed search failed: %s", e)
            return []

    def _rerank(self, query: str, results: list[SearchResult], threshold: int = 5) -> list[SearchResult]:
        """Cross-encoder rerank when result count exceeds threshold. Gated by search_rerank_enabled."""
        if not results or len(results) <= threshold:
            return results
        enabled = getattr(self._config, "search_rerank_enabled", False) if self._config else False
        if not enabled:
            return results
        try:
            from sentence_transformers import CrossEncoder
            model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
            pairs = [[query, r.snippet or r.title] for r in results]
            scores = model.predict(pairs)
            for r, s in zip(results, scores):
                r.rerank_score = float(s)
            results.sort(key=lambda r: r.rerank_score, reverse=True)
            return results
        except Exception as e:
            logger.debug("Reranker failed: %s", e)
            return results

    async def _search_perplexity(self, query: str) -> dict:
        """Perplexity deep research — multi-source synthesis with citations. Key from vault."""
        key = self._get_key("perplexity_api_key")
        if not key:
            return {}
        try:
            import aiohttp
            url = "https://api.perplexity.ai/chat/completions"
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            payload = {
                "model": "sonar-pro",
                "messages": [{"role": "user", "content": query}],
                "max_tokens": 1024,
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status == 429:
                        self._mark_rate_limited("perplexity")
                        return {}
                    data = await resp.json()
            choices = data.get("choices") or []
            if not choices:
                return {}
            content = (choices[0].get("message") or {}).get("content", "")
            citations = (data.get("citations") or []) if isinstance(data.get("citations"), list) else []
            out = {"query": query, "answer": content, "citations": citations, "source": "perplexity"}
            if self._cache:
                try:
                    from cato.core import memory as _mem
                    cache = _mem.get_cache()
                    cache.set("web_search_cache", f"perplexity:{query}", out, self._cache_ttl)
                except Exception:
                    pass
            return out
        except Exception as e:
            logger.debug("Perplexity search failed: %s", e)
            return {}

    async def code_search(self, query: str, max_results: int = 10) -> dict:
        """Convenience: search with code chain."""
        return await self.search(query, query_type="code", max_results=max_results)

    async def news_search(self, query: str, max_results: int = 10) -> dict:
        """Convenience: search with news chain."""
        return await self.search(query, query_type="news", max_results=max_results)

    async def deep_search(self, query: str, max_results: int = 10) -> dict:
        """Perplexity deep research when key present; otherwise synthesize from top multi-engine results."""
        pp = await self._search_perplexity(query)
        if pp and pp.get("answer"):
            return pp
        # Fallback: top results from classified search
        data = await self.search(query, max_results=max_results, use_cache=True)
        data["source"] = "web_search_synthesized"
        return data


def register_web_search_tools(register_tool: Callable[[str, Any], None], config: Optional[Any] = None) -> None:
    """Register web.search, web.code, web.news with agent_loop's register_tool."""
    cache = None
    try:
        from cato.core.memory import get_cache
        cache = get_cache()
    except Exception:
        pass
    tool = WebSearchTool(config=config, vault=getattr(config, "vault", None), cache=cache)
    if config:
        tool._cache_ttl = getattr(config, "web_search_cache_ttl_sec", 3600)
        tool._academic_cache_ttl = getattr(config, "academic_cache_ttl_sec", 86400)

    async def web_search(args: dict) -> str:
        q = args.get("query", "")
        n = int(args.get("max_results", 10))
        result = await tool.search(q, max_results=n)
        return json.dumps(result)

    async def web_code(args: dict) -> str:
        q = args.get("query", "")
        n = int(args.get("max_results", 10))
        result = await tool.code_search(q, max_results=n)
        return json.dumps(result)

    async def web_news(args: dict) -> str:
        q = args.get("query", "")
        n = int(args.get("max_results", 10))
        result = await tool.news_search(q, max_results=n)
        return json.dumps(result)

    async def academic_arxiv(args: dict) -> str:
        q = args.get("query", "")
        n = int(args.get("max_results", 10))
        res = await tool._search_arxiv(q, n)
        return json.dumps({"query": q, "results": [r.to_dict() for r in res], "source": "arxiv"})

    async def academic_semantic_scholar(args: dict) -> str:
        q = args.get("query", "")
        n = int(args.get("max_results", 10))
        res = await tool._search_semantic_scholar(q, n)
        return json.dumps({"query": q, "results": [r.to_dict() for r in res], "source": "semantic_scholar"})

    async def academic_pubmed(args: dict) -> str:
        q = args.get("query", "")
        n = int(args.get("max_results", 10))
        res = await tool._search_pubmed(q, n)
        return json.dumps({"query": q, "results": [r.to_dict() for r in res], "source": "pubmed"})

    async def web_deep(args: dict) -> str:
        q = args.get("query", "")
        n = int(args.get("max_results", 10))
        result = await tool.deep_search(q, max_results=n)
        return json.dumps(result)

    register_tool("web.search", web_search)
    register_tool("web.code", web_code)
    register_tool("web.news", web_news)
    register_tool("web.deep", web_deep)
    register_tool("academic.arxiv", academic_arxiv)
    register_tool("academic.semantic_scholar", academic_semantic_scholar)
    register_tool("academic.pubmed", academic_pubmed)
        depth: DepthType = "normal",
        max_results: int = 10,
    ) -> list[SearchResult]:
        """
        Search using the best engine chain for the query type.

        Returns results sorted by confidence descending.
        """
        qt: QueryType = query_type or classify_query(query)
        chain = list(_FALLBACK_CHAINS.get(qt, _FALLBACK_CHAINS["general"]))

        # Add perplexity at the front for deep searches
        if depth == "deep":
            chain = ["perplexity"] + chain

        all_results: list[SearchResult] = []

        for provider in chain:
            if self._is_rate_limited(provider):
                logger.debug("Skipping rate-limited provider: %s", provider)
                continue
            try:
                results = await self._search_provider(provider, query)
                all_results.extend(results)
                if results:
                    break  # got results — stop chain
            except Exception as exc:
                logger.warning("Provider %s failed: %s", provider, exc)
                continue

        # If primary chain yielded nothing, try ddg_api as last resort
        if not all_results:
            try:
                all_results = await self._search_ddg_api(query)
            except Exception:
                pass

        # Cross-engine agreement boost
        boosted = self._cross_engine_agreement(all_results)

        # Sort by confidence descending, slice to max_results
        boosted.sort(key=lambda r: r.confidence, reverse=True)
        return boosted[:max_results]

    # ------------------------------------------------------------------ #
    # Provider dispatch                                                   #
    # ------------------------------------------------------------------ #

    async def _search_provider(self, provider: str, query: str) -> list[SearchResult]:
        """Dispatch to the right backend.  Marks provider rate-limited on 429."""
        dispatch = {
            "ddg_api":          self._search_ddg_api,
            "brave":            self._search_brave,
            "exa":              self._search_exa,
            "tavily":           self._search_tavily,
            "arxiv":            self._search_arxiv,
            "semantic_scholar": self._search_semantic_scholar,
            "pubmed":           self._search_pubmed,
            "searxng":          self._search_searxng_default,
            "perplexity":       self._search_perplexity,
        }
        fn = dispatch.get(provider)
        if fn is None:
            return []
        try:
            return await fn(query)
        except _RateLimitError:
            self._mark_rate_limited(provider)
            return []

    # ------------------------------------------------------------------ #
    # Rate-limit helpers                                                  #
    # ------------------------------------------------------------------ #

    def _is_rate_limited(self, provider: str) -> bool:
        until = self._rate_limited_until.get(provider, 0.0)
        return time.time() < until

    def _mark_rate_limited(self, provider: str, cooldown: float = 60.0) -> None:
        self._rate_limited_until[provider] = time.time() + cooldown
        logger.warning("Provider %s rate-limited for %.0fs", provider, cooldown)

    # ------------------------------------------------------------------ #
    # Vault helper                                                        #
    # ------------------------------------------------------------------ #

    def _vault_get(self, key: str) -> Optional[str]:
        if self._vault is None:
            return None
        try:
            return self._vault.get(key)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # Search backends                                                     #
    # ------------------------------------------------------------------ #

    async def _search_ddg_api(self, query: str) -> list[SearchResult]:
        """DuckDuckGo Instant Answer JSON API — no key needed."""
        try:
            import aiohttp
        except ImportError:
            return []

        url = f"https://api.duckduckgo.com/?q={quote_plus(query)}&format=json&no_html=1&skip_disambig=1"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 429:
                    raise _RateLimitError("ddg_api")
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)

        results: list[SearchResult] = []
        # AbstractText
        if data.get("AbstractText") and data.get("AbstractURL"):
            results.append(SearchResult(
                title=data.get("Heading", query),
                url=data["AbstractURL"],
                snippet=data["AbstractText"][:300],
                source_engine="ddg_api",
                confidence=self._heuristic_confidence(query, data["AbstractURL"], 0, data["AbstractText"]),
                rank=0,
                published_date="",
            ))
        # RelatedTopics
        for i, topic in enumerate(data.get("RelatedTopics", [])[:9]):
            if not isinstance(topic, dict):
                continue
            text = topic.get("Text", "")
            first_url = topic.get("FirstURL", "")
            if text and first_url:
                results.append(SearchResult(
                    title=text[:80],
                    url=first_url,
                    snippet=text[:300],
                    source_engine="ddg_api",
                    confidence=self._heuristic_confidence(query, first_url, i + 1, text),
                    rank=i + 1,
                    published_date="",
                ))
        return results

    async def _search_brave(self, query: str) -> list[SearchResult]:
        """Brave Search API — vault key: brave_api_key."""
        try:
            import aiohttp
        except ImportError:
            return []

        api_key = self._vault_get("brave_api_key")
        if not api_key:
            logger.debug("No brave_api_key in vault — skipping Brave")
            return []

        url = f"https://api.search.brave.com/res/v1/web/search?q={quote_plus(query)}&count=10"
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": api_key,
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 429:
                    raise _RateLimitError("brave")
                if resp.status != 200:
                    return []
                data = await resp.json()

        results: list[SearchResult] = []
        for i, item in enumerate(data.get("web", {}).get("results", [])[:10]):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("description", "")[:300],
                source_engine="brave",
                confidence=self._heuristic_confidence(
                    query, item.get("url", ""), i, item.get("description", "")
                ),
                rank=i,
                published_date=item.get("page_age", ""),
            ))
        return results

    async def _search_exa(self, query: str) -> list[SearchResult]:
        """Exa AI search — vault key: exa_api_key."""
        try:
            import aiohttp
        except ImportError:
            return []

        api_key = self._vault_get("exa_api_key")
        if not api_key:
            logger.debug("No exa_api_key in vault — skipping Exa")
            return []

        url = "https://api.exa.ai/search"
        payload = {"query": query, "numResults": 10, "useAutoprompt": True}
        headers = {"x-api-key": api_key, "Content-Type": "application/json"}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 429:
                    raise _RateLimitError("exa")
                if resp.status != 200:
                    return []
                data = await resp.json()

        results: list[SearchResult] = []
        for i, item in enumerate(data.get("results", [])[:10]):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=(item.get("text") or item.get("highlights", [""])[0])[:300],
                source_engine="exa",
                confidence=self._heuristic_confidence(
                    query, item.get("url", ""), i, item.get("text", "")
                ),
                rank=i,
                published_date=item.get("publishedDate", ""),
            ))
        return results

    async def _search_tavily(self, query: str) -> list[SearchResult]:
        """Tavily Search API — vault key: tavily_api_key."""
        try:
            import aiohttp
        except ImportError:
            return []

        api_key = self._vault_get("tavily_api_key")
        if not api_key:
            logger.debug("No tavily_api_key in vault — skipping Tavily")
            return []

        url = "https://api.tavily.com/search"
        payload = {
            "api_key": api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": 10,
            "include_answer": False,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 429:
                    raise _RateLimitError("tavily")
                if resp.status != 200:
                    return []
                data = await resp.json()

        results: list[SearchResult] = []
        for i, item in enumerate(data.get("results", [])[:10]):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", "")[:300],
                source_engine="tavily",
                confidence=self._heuristic_confidence(
                    query, item.get("url", ""), i, item.get("content", "")
                ),
                rank=i,
                published_date=item.get("published_date", ""),
            ))
        return results

    async def _search_arxiv(self, query: str) -> list[SearchResult]:
        """arXiv Atom XML API — no key needed."""
        try:
            import aiohttp
        except ImportError:
            return []

        url = (
            f"https://export.arxiv.org/api/query"
            f"?search_query=all:{quote_plus(query)}&start=0&max_results=10"
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 429:
                    raise _RateLimitError("arxiv")
                if resp.status != 200:
                    return []
                text = await resp.text()

        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return []

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)
        results: list[SearchResult] = []
        for i, entry in enumerate(entries[:10]):
            title_el = entry.find("atom:title", ns)
            summary_el = entry.find("atom:summary", ns)
            id_el = entry.find("atom:id", ns)
            published_el = entry.find("atom:published", ns)

            title = (title_el.text or "").strip() if title_el is not None else ""
            snippet = (summary_el.text or "").strip()[:300] if summary_el is not None else ""
            url_val = (id_el.text or "").strip() if id_el is not None else ""
            pub = (published_el.text or "").strip() if published_el is not None else ""

            results.append(SearchResult(
                title=title,
                url=url_val,
                snippet=snippet,
                source_engine="arxiv",
                confidence=self._heuristic_confidence(query, url_val, i, snippet),
                rank=i,
                published_date=pub,
            ))
        return results

    async def _search_semantic_scholar(self, query: str) -> list[SearchResult]:
        """Semantic Scholar API — optional vault key: semantic_scholar_api_key."""
        try:
            import aiohttp
        except ImportError:
            return []

        api_key = self._vault_get("semantic_scholar_api_key")
        headers: dict[str, str] = {}
        if api_key:
            headers["x-api-key"] = api_key

        url = (
            f"https://api.semanticscholar.org/graph/v1/paper/search"
            f"?query={quote_plus(query)}&limit=10&fields=title,abstract,externalIds,year,url"
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 429:
                    raise _RateLimitError("semantic_scholar")
                if resp.status != 200:
                    return []
                data = await resp.json()

        results: list[SearchResult] = []
        for i, paper in enumerate(data.get("data", [])[:10]):
            paper_url = paper.get("url", "")
            if not paper_url:
                ids = paper.get("externalIds", {}) or {}
                doi = ids.get("DOI", "")
                if doi:
                    paper_url = f"https://doi.org/{doi}"
            results.append(SearchResult(
                title=paper.get("title", ""),
                url=paper_url,
                snippet=(paper.get("abstract") or "")[:300],
                source_engine="semantic_scholar",
                confidence=self._heuristic_confidence(
                    query, paper_url, i, paper.get("abstract", "") or ""
                ),
                rank=i,
                published_date=str(paper.get("year", "")),
            ))
        return results

    async def _search_pubmed(self, query: str) -> list[SearchResult]:
        """NCBI PubMed Entrez API — no key needed; 3 req/sec rate limit enforced."""
        try:
            import aiohttp
        except ImportError:
            return []

        # Step 1: esearch to get PMIDs
        async with self._pubmed_semaphore:
            esearch_url = (
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
                f"?db=pubmed&term={quote_plus(query)}&retmax=10&retmode=json&usehistory=y"
            )
            async with aiohttp.ClientSession() as session:
                async with session.get(esearch_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 429:
                        raise _RateLimitError("pubmed")
                    if resp.status != 200:
                        return []
                    esearch_data = await resp.json()

            pmids: list[str] = esearch_data.get("esearchresult", {}).get("idlist", [])
            if not pmids:
                return []

            # Step 2: efetch summaries
            efetch_url = (
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
                f"?db=pubmed&id={','.join(pmids)}&retmode=json"
            )
            async with aiohttp.ClientSession() as session:
                async with session.get(efetch_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 429:
                        raise _RateLimitError("pubmed")
                    if resp.status != 200:
                        return []
                    efetch_data = await resp.json()

        results: list[SearchResult] = []
        result_set = efetch_data.get("result", {})
        for i, pmid in enumerate(pmids[:10]):
            doc = result_set.get(pmid, {})
            if not doc:
                continue
            title = doc.get("title", "")
            pub_date = doc.get("pubdate", "")
            url_val = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            results.append(SearchResult(
                title=title,
                url=url_val,
                snippet=title[:300],
                source_engine="pubmed",
                confidence=self._heuristic_confidence(query, url_val, i, title),
                rank=i,
                published_date=pub_date,
            ))
        return results

    async def _search_searxng_default(self, query: str) -> list[SearchResult]:
        """SearXNG with the configured instance URL."""
        return await self._search_searxng(query, self._searxng_url)

    async def _search_searxng(self, query: str, instance_url: str) -> list[SearchResult]:
        """SearXNG self-hosted instance — skipped if instance_url is empty."""
        if not instance_url:
            return []
        try:
            import aiohttp
        except ImportError:
            return []

        url = f"{instance_url.rstrip('/')}/search?q={quote_plus(query)}&format=json"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 429:
                    raise _RateLimitError("searxng")
                if resp.status != 200:
                    return []
                data = await resp.json()

        results: list[SearchResult] = []
        for i, item in enumerate(data.get("results", [])[:10]):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", "")[:300],
                source_engine="searxng",
                confidence=self._heuristic_confidence(
                    query, item.get("url", ""), i, item.get("content", "")
                ),
                rank=i,
                published_date=item.get("publishedDate", ""),
            ))
        return results

    async def _search_perplexity(self, query: str) -> list[SearchResult]:
        """Perplexity API — vault key: perplexity_api_key.  Used for deep searches."""
        try:
            import aiohttp
        except ImportError:
            return []

        api_key = self._vault_get("perplexity_api_key")
        if not api_key:
            logger.debug("No perplexity_api_key in vault — skipping Perplexity")
            return []

        url = "https://api.perplexity.ai/chat/completions"
        payload = {
            "model": "sonar-pro",
            "messages": [{"role": "user", "content": query}],
            "return_citations": True,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 429:
                    raise _RateLimitError("perplexity")
                if resp.status != 200:
                    return []
                data = await resp.json()

        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        citations: list[str] = data.get("citations", [])

        results: list[SearchResult] = []
        for i, cite_url in enumerate(citations[:10]):
            results.append(SearchResult(
                title=f"Perplexity citation {i + 1}",
                url=cite_url,
                snippet=content[:300] if i == 0 else "",
                source_engine="perplexity",
                confidence=self._heuristic_confidence(query, cite_url, i, content),
                rank=i,
                published_date="",
            ))
        # If no citations, wrap the answer itself
        if not citations and content:
            results.append(SearchResult(
                title="Perplexity answer",
                url="https://www.perplexity.ai/",
                snippet=content[:300],
                source_engine="perplexity",
                confidence=0.75,
                rank=0,
                published_date="",
            ))
        return results

    # ------------------------------------------------------------------ #
    # Confidence scoring                                                  #
    # ------------------------------------------------------------------ #

    def _heuristic_confidence(
        self, query: str, url: str, rank: int, snippet: str
    ) -> float:
        """
        Compute a base confidence score in [0.0, 1.0].

        Factors:
        - Rank decay: higher-ranked results get higher base score
        - Domain authority bonus: .edu / .gov / .org
        - Freshness bonus: snippet mentions year >= 2023
        - Snippet relevance: keyword overlap
        """
        # Rank decay: rank 0 → 0.85, rank 9 → ~0.40
        base = max(0.40, 0.85 - rank * 0.05)

        # Domain authority
        low_url = url.lower()
        if ".edu" in low_url or ".gov" in low_url:
            base = min(1.0, base + 0.10)
        elif ".org" in low_url:
            base = min(1.0, base + 0.05)

        # Freshness: recent year mentions in snippet
        import re
        if re.search(r"202[3-9]|2030", snippet):
            base = min(1.0, base + 0.05)

        # Keyword overlap: simple token match
        query_tokens = set(query.lower().split())
        snippet_tokens = set(snippet.lower().split())
        overlap = len(query_tokens & snippet_tokens)
        if overlap:
            base = min(1.0, base + min(0.10, overlap * 0.02))

        return round(base, 4)

    def _cross_engine_agreement(self, results: list[SearchResult]) -> list[SearchResult]:
        """
        Boost confidence for URLs appearing in 2+ engines.

        Normalises URLs (strips query string / trailing slash) for comparison.
        """
        from urllib.parse import urlparse

        def _norm(url: str) -> str:
            try:
                p = urlparse(url)
                return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/").lower()
            except Exception:
                return url.lower()

        url_count: dict[str, int] = {}
        for r in results:
            norm = _norm(r.url)
            url_count[norm] = url_count.get(norm, 0) + 1

        boosted: list[SearchResult] = []
        for r in results:
            norm = _norm(r.url)
            cnt = url_count.get(norm, 1)
            new_conf = min(1.0, r.confidence + 0.05 * (cnt - 1))
            boosted.append(SearchResult(
                title=r.title,
                url=r.url,
                snippet=r.snippet,
                source_engine=r.source_engine,
                confidence=new_conf,
                rank=r.rank,
                published_date=r.published_date,
            ))
        return boosted


# ---------------------------------------------------------------------------
# Internal exception
# ---------------------------------------------------------------------------

class _RateLimitError(Exception):
    """Raised internally when a provider returns HTTP 429."""
