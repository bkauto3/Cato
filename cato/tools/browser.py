"""
cato/tools/browser.py — Browser automation using Patchright (stealth Playwright fork).

Actions: navigate, snapshot, click, type, screenshot, pdf, search
Search engine: DuckDuckGo only (Google/Brave block bots).
Browser: Chromium only with persistent profile at ~/.cato/browser_profile/.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CATO_DIR = Path.home() / ".cato"
_PROFILE_DIR = _CATO_DIR / "browser_profile"
_SCREENSHOT_DIR = _CATO_DIR / "workspace" / "screenshots"
_PDF_DIR = _CATO_DIR / "workspace" / "pdfs"


class BrowserTool:
    """Browser automation using Patchright (stealth Playwright fork).

    Provides:
    - navigate(url): Go to URL, return page title + visible text
    - snapshot():    Return current page title + text + interactive elements
    - click(selector): Click element by CSS selector or text
    - type(selector, text): Type text into input
    - screenshot():  Take screenshot, save to workspace, return path
    - pdf(filename): Save page as PDF
    - search(query): DuckDuckGo search, return top 5 results

    Uses persistent browser profile at ~/.cato/browser_profile/
    Chromium only (no Firefox, no WebKit)
    """

    def __init__(self) -> None:
        self._browser = None
        self._page = None
        self._playwright = None
        _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        _PDF_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def execute(self, args: dict[str, Any]) -> str:
        """Dispatch from agent_loop tool registry (receives raw args dict)."""
        action = args.pop("action", "") if isinstance(args, dict) else ""
        result = await self._dispatch(action, args)
        return json.dumps(result)

    async def _dispatch(self, action: str, kwargs: dict) -> dict:
        """Ensure browser is running, then dispatch to sub-action."""
        await self._ensure_browser()

        actions = {
            "navigate":   self._navigate,
            "snapshot":   self._snapshot,
            "click":      self._click,
            "type":       self._type,
            "screenshot": self._screenshot,
            "pdf":        self._pdf,
            "search":     self._search,
        }

        if action not in actions:
            return {"error": f"Unknown browser action: {action!r}. Valid: {list(actions)}"}

        try:
            return await actions[action](**kwargs)
        except Exception as exc:
            logger.error("Browser action %s failed: %s", action, exc)
            return {"error": str(exc), "action": action}

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------

    async def _ensure_browser(self) -> None:
        """Launch Patchright browser if not already running."""
        if self._browser is not None:
            try:
                if self._browser.is_connected():
                    return
            except Exception:
                pass

        from patchright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(_PROFILE_DIR),
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        self._page = await self._browser.new_page()
        logger.debug("Patchright browser launched with profile %s", _PROFILE_DIR)

    async def close(self) -> None:
        """Gracefully close the browser and Playwright instance."""
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
            self._page = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    # ------------------------------------------------------------------
    # Action implementations
    # ------------------------------------------------------------------

    async def _navigate(self, url: str, wait_until: str = "domcontentloaded") -> dict:
        """Navigate to URL and return title + visible text (first 3000 chars)."""
        # Validate URL scheme (no file://, no internal IPs)
        import ipaddress
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return {"error": f"Blocked URL scheme: {parsed.scheme}. Only http/https allowed."}
        # Block RFC-1918 and link-local
        try:
            host = parsed.hostname
            addr = ipaddress.ip_address(host) if host else None
            if addr and (addr.is_private or addr.is_link_local or addr.is_loopback):
                return {"error": f"Blocked internal IP: {host}"}
        except ValueError:
            pass  # hostname, not IP — allow

        await self._page.goto(url, wait_until=wait_until, timeout=30000)
        title = await self._page.title()
        text = await self._page.evaluate("document.body.innerText")
        return {
            "title": title,
            "url": self._page.url,
            "text": text[:3000],
        }

    async def _snapshot(self) -> dict:
        """Return current page state: title, URL, visible text, interactive elements."""
        title = await self._page.title()
        text = await self._page.evaluate("document.body.innerText")

        elements = await self._page.evaluate("""
            () => {
                const els = [];
                document.querySelectorAll('a, button, input, select, textarea').forEach(el => {
                    els.push({
                        tag: el.tagName.toLowerCase(),
                        text: (el.innerText || el.value || el.placeholder || '').substring(0, 100),
                        href: el.href || null,
                        id: el.id || null,
                        type: el.type || null
                    });
                });
                return els.slice(0, 50);
            }
        """)

        return {
            "title": title,
            "url": self._page.url,
            "text": text[:2000],
            "elements": elements,
        }

    async def _click(self, selector: str) -> dict:
        """Click element by CSS selector."""
        try:
            await self._page.click(selector, timeout=10000)
            return {"success": True, "selector": selector, "url": self._page.url}
        except Exception as exc:
            return {"success": False, "selector": selector, "error": str(exc)}

    async def _type(self, selector: str, text: str) -> dict:
        """Type text into an input element."""
        try:
            await self._page.fill(selector, text, timeout=10000)
            return {"success": True, "selector": selector, "typed": text}
        except Exception as exc:
            return {"success": False, "selector": selector, "error": str(exc)}

    async def _screenshot(self, filename: str = None) -> dict:
        """Take a full-page screenshot and save to workspace."""
        if not filename:
            import time
            filename = f"screenshot_{int(time.time())}.png"
        if not filename.endswith(".png"):
            filename += ".png"

        out_path = _SCREENSHOT_DIR / filename
        await self._page.screenshot(path=str(out_path), full_page=True)
        return {"success": True, "path": str(out_path), "url": self._page.url}

    async def _pdf(self, filename: str = None) -> dict:
        """Save the current page as a PDF."""
        if not filename:
            import time
            filename = f"page_{int(time.time())}.pdf"
        if not filename.endswith(".pdf"):
            filename += ".pdf"

        out_path = _PDF_DIR / filename
        await self._page.pdf(path=str(out_path))
        return {"success": True, "path": str(out_path), "url": self._page.url}

    async def _search(self, query: str) -> dict:
        """DuckDuckGo search — returns top 5 results."""
        search_url = f"https://duckduckgo.com/?q={urllib.parse.quote(query)}&ia=web"
        await self._page.goto(search_url, wait_until="domcontentloaded", timeout=30000)

        results = await self._page.evaluate("""
            () => {
                const results = [];
                document.querySelectorAll('[data-testid="result"]').forEach(r => {
                    const titleEl = r.querySelector('h2 a');
                    const snippetEl = r.querySelector('[data-result="snippet"]');
                    if (titleEl) {
                        results.push({
                            title: titleEl.innerText,
                            url: titleEl.href,
                            snippet: snippetEl ? snippetEl.innerText : ''
                        });
                    }
                });
                return results.slice(0, 5);
            }
        """)

        return {"query": query, "results": results}
