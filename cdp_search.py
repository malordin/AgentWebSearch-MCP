#!/usr/bin/env python3
"""
CDP-based Portal Search Module (v3.1 - 3 Chrome Parallel)

Core Principles:
1. 3 Independent Chrome instances - dedicated instance per portal
2. Session persistence - cookies/login maintained via user-data-dir
3. True parallel - ThreadPoolExecutor for concurrent search
4. Direct CDP - simplified without MCP dependency
"""

import json
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional
import httpx

from chrome_launcher import CHROME_INSTANCES, is_chrome_running, start_chrome

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """Search result"""
    title: str
    url: str
    snippet: str
    source: str  # naver, google, brave


# ========== Portal Configuration ==========
#
# [Portal Addition Guide - Claude Code can read this and add automatically]
#
# To add a new portal:
# 1. Add new entry to PORTAL_CONFIG below
# 2. Add matching key to CHROME_INSTANCES in chrome_launcher.py
#
# Required information:
# - search_url: Search URL (query appended at end)
# - extract_script: JavaScript to extract search results
#   - Use document.querySelectorAll() to select link elements
#   - Extract title, url, snippet
#   - Exclude portal's own domain (filter self-links)
#   - Return array (max 10 items)
#
# Example - Adding Bing:
# "bing": {
#     "search_url": "https://www.bing.com/search?q=",
#     "extract_script": """
#         const results = [];
#         document.querySelectorAll('#b_results .b_algo').forEach(el => {
#             const titleEl = el.querySelector('h2 a');
#             const snippetEl = el.querySelector('.b_caption p');
#             if (titleEl) {
#                 results.push({
#                     title: titleEl.textContent.trim(),
#                     url: titleEl.href,
#                     snippet: snippetEl ? snippetEl.textContent.trim() : ''
#                 });
#             }
#         });
#         return results.slice(0, 10);
#     """
# },
#
# Example - Adding DuckDuckGo:
# "duckduckgo": {
#     "search_url": "https://duckduckgo.com/?q=",
#     "extract_script": """
#         const results = [];
#         document.querySelectorAll('[data-testid="result"]').forEach(el => {
#             const titleEl = el.querySelector('a[data-testid="result-title-a"]');
#             const snippetEl = el.querySelector('[data-result="snippet"]');
#             if (titleEl) {
#                 results.push({
#                     title: titleEl.textContent.trim(),
#                     url: titleEl.href,
#                     snippet: snippetEl ? snippetEl.textContent.trim() : ''
#                 });
#             }
#         });
#         return results.slice(0, 10);
#     """
# },
#
# Notes:
# - extract_script varies based on portal's HTML structure
# - Selectors may change when portal updates
# - Use indexOf() instead of includes() for CDP compatibility
# - Use ternary operators instead of optional chaining (?.)
#

PORTAL_CONFIG = {
    # "naver": {
    #     "search_url": "https://search.naver.com/search.naver?query=",  # Integrated search
    #     "extract_script": """
    #         const results = [];
    #         document.querySelectorAll('#main_pack a[href^="http"]').forEach(a => {
    #             const href = a.href;
    #             const text = a.textContent ? a.textContent.trim() : '';
    #             if (href &&
    #                 href.indexOf('naver.com') === -1 &&
    #                 href.indexOf('javascript') === -1 &&
    #                 text && text.length > 15 && text.length < 200) {
    #                 if (!results.find(r => r.url === href)) {
    #                     results.push({
    #                         title: text.substring(0, 80),
    #                         url: href,
    #                         snippet: ''
    #                     });
    #                 }
    #             }
    #         });
    #         return results.slice(0, 10);
    #     """
    # },
    "google": {
        "search_url": "https://www.google.com/search?q=",
        # Google wraps all result links through /goto?url=REDIRECT or
        # /url?sa=t&...&url=REDIRECT tracking redirects.
        # These redirect to the real page when opened in a browser.
        # Structure: <a href="/goto?url=ENCODED"><h3>Title</h3></a>
        # We accept both raw and resolved href — either works for navigation.
        "extract_script": """
            const results = [];
            const seen = new Set();
            const searchRoot = document.querySelector('#search')
                            || document.querySelector('#rso')
                            || document.querySelector('[data-async-context]');
            if (!searchRoot) return results;
            
            // Strategy: find h3 elements, get parent <a>, extract title + url
            const h3s = searchRoot.querySelectorAll('h3');
            h3s.forEach(function(h3) {
                var a = h3.parentElement;
                // H3 may not be direct child of A; use closest()
                if (a && a.tagName !== 'A') a = h3.closest('a');
                if (!a) return;
                
                var rawHref = a.getAttribute('href') || '';
                if (!rawHref) return;
                // Skip non-navigation links (gstatic, images, etc.)
                if (rawHref.indexOf('gstatic') !== -1) return;
                if (rawHref === '#') return;
                
                // Build URL: absolute or relative
                var url;
                if (rawHref.indexOf('http') === 0) {
                    url = rawHref;
                } else {
                    url = 'https://www.google.com' + rawHref;
                }
                
                if (seen.has(url)) return;
                seen.add(url);
                
                // Find snippet text in parent block
                var snippet = '';
                var block = a.closest('[data-ved]')
                         || a.closest('div[data-sncf]')
                         || a.closest('.g');
                if (!block) {
                    // Walk up until we find a div with substantial content
                    var p = a.parentElement;
                    for (var i = 0; i < 4 && p; i++) {
                        if (p.tagName === 'DIV' || p.tagName === 'SECTION') {
                            block = p;
                            break;
                        }
                        p = p.parentElement;
                    }
                }
                // Walk up to find a container that has BOTH the title
                // section AND the description section (text > 2x link text).
                // The immediate link parent only has title+URL breadcrumb;
                // the description lives in a sibling div further up.
                var linkLen = (a.textContent || '').length;
                var container = null;
                var cur = a.parentElement;
                for (var ci = 0; ci < 7 && cur; ci++) {
                    if ((cur.textContent || '').length > linkLen * 2 &&
                            cur.children.length >= 2) {
                        container = cur;
                        break;
                    }
                    cur = cur.parentElement;
                }
                if (container) {
                    // Clone, strip title/URL elements, take remainder as snippet
                    var clone = container.cloneNode(true);
                    clone.querySelectorAll(
                        'script, style, noscript, h3, cite, a[href]'
                    ).forEach(function(el) {
                        if (el.parentNode) el.parentNode.removeChild(el);
                    });
                    var remaining = (clone.textContent || '')
                        .replace(/[ \\t\\n\\r]+/g, ' ').trim();
                    if (remaining.length > 30 &&
                        remaining.indexOf(' › ') === -1 &&
                        remaining.indexOf('https://') !== 0 &&
                        !/^(Перейти к|К основному|Skip to)/.test(remaining)) {
                        snippet = remaining.substring(0, 280);
                    }
                }
                
                results.push({
                    title: h3.textContent ? h3.textContent.trim() : '',
                    url: url,
                    snippet: snippet
                });
                
                if (results.length >= 10) return;
            });
            return results.slice(0, 10);
        """
    },
    # "brave": {
    #     "search_url": "https://search.brave.com/search?q=",
    #     "extract_script": """
    #         const results = [];
    #         const seen = new Set();
    #         document.querySelectorAll('a[href^="http"]').forEach(a => {
    #             const href = a.href;
    #             if (href &&
    #                 href.indexOf('brave.com') === -1 &&
    #                 href.indexOf('javascript') === -1 &&
    #                 !seen.has(href)) {
    #                 const text = a.textContent ? a.textContent.trim() : '';
    #                 if (text && text.length > 10 && text.length < 150) {
    #                     seen.add(href);
    #                     results.push({
    #                         title: text.substring(0, 80),
    #                         url: href,
    #                         snippet: ''
    #                     });
    #                 }
    #             }
    #         });
    #         return results.slice(0, 10);
    #     """
    # }
}

# Stealth script - bypass browser automation detection
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['ko-KR', 'ko', 'en-US', 'en'] });
window.chrome = { runtime: {} };
"""

# CAPTCHA detection script
CAPTCHA_DETECT_SCRIPT = """
    var indicators = [];

    // hCaptcha
    if (document.querySelector('iframe[src*="hcaptcha"]') ||
        document.querySelector('.h-captcha') ||
        document.querySelector('#hcaptcha')) {
        indicators.push('hcaptcha');
    }

    // Cloudflare Turnstile
    if (document.querySelector('iframe[src*="challenges.cloudflare"]') ||
        document.querySelector('.cf-turnstile')) {
        indicators.push('turnstile');
    }

    // reCAPTCHA
    if (document.querySelector('iframe[src*="recaptcha"]') ||
        document.querySelector('.g-recaptcha')) {
        indicators.push('recaptcha');
    }

    // Text-based detection
    var bodyText = document.body ? document.body.innerText.toLowerCase() : '';
    if (bodyText.indexOf('verify you are human') !== -1 ||
        bodyText.indexOf('are you a robot') !== -1 ||
        bodyText.indexOf('security check') !== -1 ||
        bodyText.indexOf('please verify') !== -1 ||
        bodyText.indexOf('captcha') !== -1) {
        indicators.push('text_hint');
    }

    // URL-based detection
    if (window.location.href.indexOf('challenge') !== -1 ||
        window.location.href.indexOf('captcha') !== -1) {
        indicators.push('url_hint');
    }

    return indicators;
"""


# ========== CDP Direct Communication ==========

class CDPClient:
    """
    CDP Direct Communication Client.

    Tab isolation model:
    - create_tab() opens a NEW browser tab with a unique page_id/ws_url.
      Each search/fetch gets its own tab so concurrent operations don't
      overwrite each other.
    - close_tab() closes the tab when work is done.
    - _reconnect() reattaches to OUR specific tab (after navigation
      may have dropped the WebSocket), not to any random tab.
    - connect(target_id) can optionally find a specific tab.
    - Fallback: if create_tab fails (old Chrome / permission issue),
      falls back to reusing an existing tab (legacy behaviour).
    """

    def __init__(self, port: int):
        self.port = port
        self.base_url = f"http://localhost:{port}"
        self.ws_url: Optional[str] = None
        self.page_id: Optional[str] = None
        self._owns_tab: bool = False  # did we create this tab?

    def connect(self, target_id: str = None) -> bool:
        """
        Connect to a tab.
        If target_id is given, find that specific tab (used by _reconnect).
        Otherwise find the first http/https page tab.
        """
        try:
            resp = httpx.get(f"{self.base_url}/json/list", timeout=5)
            tabs = resp.json()

            if target_id:
                for tab in tabs:
                    if (tab.get("id") == target_id and
                            tab.get("type") == "page"):
                        self.ws_url = tab.get("webSocketDebuggerUrl")
                        self.page_id = tab.get("id")
                        return True
                return False

            # Find actual webpage tabs starting with http/https
            for tab in tabs:
                url = tab.get("url", "")
                if (url.startswith("http://") or url.startswith("https://")) \
                        and tab.get("type") == "page":
                    self.ws_url = tab.get("webSocketDebuggerUrl")
                    self.page_id = tab.get("id")
                    return True

            # If no webpage tab, use first page-type tab
            for tab in tabs:
                if tab.get("type") == "page":
                    self.ws_url = tab.get("webSocketDebuggerUrl")
                    self.page_id = tab.get("id")
                    return True

            return False
        except Exception as e:
            logger.error(f"[CDP:{self.port}] Connection failed: {e}")
            return False

    def create_tab(self, url: str = "about:blank") -> bool:
        """
        Create a new isolated browser tab.
        Returns True if a new tab was created, False if fell back to
        an existing tab.
        """
        try:
            resp = httpx.put(f"{self.base_url}/json/new", timeout=5)
            tab = resp.json()
            self.page_id = tab.get("id")
            self.ws_url = tab.get("webSocketDebuggerUrl")
            self._owns_tab = True
            logger.debug(f"[CDP:{self.port}] Created tab {self.page_id}")
            return True
        except Exception as e:
            logger.warning(f"[CDP:{self.port}] create_tab failed ({e}), "
                           f"falling back to existing tab")
            self._owns_tab = False
            return self.connect()

    def close_tab(self):
        """Close the tab if we own it."""
        if self._owns_tab and self.page_id:
            try:
                httpx.get(f"{self.base_url}/json/close/{self.page_id}",
                          timeout=3)
                logger.debug(f"[CDP:{self.port}] Closed tab {self.page_id}")
            except Exception:
                pass
        self.ws_url = None
        self.page_id = None
        self._owns_tab = False

    def _reconnect(self):
        """Reattach to our own tab after navigation (ws may have dropped)."""
        if self.page_id:
            self.ws_url = None
            self.connect(target_id=self.page_id)
        else:
            self.ws_url = None
            self.connect()

    def navigate(self, url: str, wait_time: float = 3.0,
                 max_extra_wait: float = 15.0,
                 content_check: str = "") -> bool:
        """
        Navigate to page (using CDP Protocol with stealth script injection).

        Waiting strategy:
        1. Fixed wait_time initial sleep (lets page start loading)
        2. Dynamic poll: wait for document.readyState === 'complete'
        3. Challenge detection: if Cloudflare/Yandex SSO challenge page
           is detected, keep waiting up to max_extra_wait until it resolves.
        4. Content check: if content_check JS provided, also wait until
           it returns truthy (ensures results rendered).

        Args:
            wait_time: initial fixed sleep before polling (seconds).
            max_extra_wait: maximum additional wait time (seconds).
            content_check: optional JS expression returning truthy when
            page content is ready. E.g. a selector that exists only when
            search results have rendered.
        """
        try:
            import websocket
            import json as js

            if not self.ws_url:
                self._reconnect()

            ws = websocket.create_connection(self.ws_url, timeout=10)

            # Inject stealth script (runs before page load)
            ws.send(js.dumps({
                "id": 1,
                "method": "Page.addScriptToEvaluateOnNewDocument",
                "params": {"source": STEALTH_SCRIPT}
            }))
            ws.recv()  # Wait for response

            # Page.navigate
            ws.send(js.dumps({
                "id": 2,
                "method": "Page.navigate",
                "params": {"url": url}
            }))

            # Wait for response
            resp = ws.recv()
            ws.close()

            # Phase 1: initial fixed sleep
            time.sleep(wait_time)

            # Reconnect to OUR tab (ws may have dropped after navigation)
            self._reconnect()

            # Phase 2: dynamic wait — poll readyState + challenge + content
            self._wait_for_page_settle(max_extra_wait, content_check)

            return True

        except Exception as e:
            logger.error(f"[CDP:{self.port}] Navigate failed: {e}")
            return False

    def _wait_for_page_settle(self, max_wait: float = 15.0,
                             content_check: str = "") -> bool:
        """
        Poll page until readyState is 'complete' AND any challenge has
        resolved OR results have rendered (content_check truthy).

        Returns True if all conditions met, False if timed out or bot page.

        Handles:
        - Cloudflare / Yandex SSO: auto-resolving challenges (poll until gone)
        - Google bot-detection ("подозрительный трафик"): manual CAPTCHA —
          detected, logged, returns False so caller can handle it.
        """
        deadline = time.time() + max_wait
        poll_interval = 1.0

        while time.time() < deadline:
            title = self.evaluate("return document.title || '';") or ""
            ready = self.evaluate("return document.readyState || '';") or ""
            content_ready = True
            if content_check:
                content_ready = bool(
                    self.evaluate(f"return !!({content_check});")
                )
            self._reconnect()

            title_lower = title.lower()

            # Auto-resolving challenges (Cloudflare, Yandex SSO)
            auto_challenge_titles = [
                "один момент", "just a moment", "checking your browser",
                "cloudflare", "авторизац", "проверка безопасности",
            ]
            is_auto_challenge = any(
                t in title_lower for t in auto_challenge_titles
            )

            # Google bot-detection: body text check
            body_sample = (
                " "
                + (self.evaluate(
                    "return (document.body && "
                    "document.body.innerText || '').substring(0, 800);"
                ) or "").lower()
            )
            is_bot_page = (
                "подозрительный трафик" in body_sample
                or "suspicious traffic" in body_sample
                or "зарегистрировали подозрител" in body_sample
                or "не робот" in body_sample
                or "are you a robot" in body_sample
                or "verify you are human" in body_sample
            )

            is_challenge = is_auto_challenge or is_bot_page

            if is_bot_page and not is_auto_challenge:
                logger.warning(
                    f"[CDP:{self.port}] Google bot-detection page found "
                    f"(manual CAPTCHA needed). Body: {body_sample[:120]}"
                )
                return False  # let caller handle manual wait

            if ready == "complete" and not is_challenge and content_ready:
                return True  # all good

            if is_auto_challenge:
                logger.info(
                    f"[CDP:{self.port}] Challenge page detected "
                    f"(title='{title}'), waiting for resolution..."
                )

            time.sleep(poll_interval)

        # Timeout reached without all conditions met
        title_at_timeout = self.evaluate("return document.title || '';") or "?"
        logger.warning(
            f"[CDP:{self.port}] Page settle timed out after {max_wait}s "
            f"(title='{title_at_timeout}')"
        )
        return False

    def evaluate(self, script: str) -> any:
        """Execute JavaScript on OUR tab (never a random tab)."""
        try:
            import websocket
            import json as js

            if not self.ws_url:
                # Reconnect to OUR specific tab, not any random one
                self._reconnect()

            ws = websocket.create_connection(self.ws_url, timeout=10)

            # Runtime.evaluate (wrapped in IIFE)
            expression = f"(function() {{{script}}})()"
            ws.send(js.dumps({
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": expression,
                    "returnByValue": True
                }
            }))

            resp = js.loads(ws.recv())
            ws.close()

            # Error check
            if "error" in resp:
                logger.error(f"[CDP:{self.port}] Evaluate error: {resp['error']}")
                return None

            result = resp.get("result", {}).get("result", {}).get("value")
            logger.debug(f"[CDP:{self.port}] Evaluate result: {type(result)} - {str(result)[:100] if result else 'None'}")
            return result

        except Exception as e:
            logger.error(f"[CDP:{self.port}] Evaluate failed: {e}")
            return None


# ========== Tab Cleanup ==========

# Maximum number of page-type tabs allowed per Chrome instance.
# If exceeded, oldest about:blank tabs are closed to prevent resource leak
# from crashed/abandoned operations.
MAX_OPEN_TABS = 15


def _cleanup_stale_tabs(port: int):
    """
    Enforce tab count limit by closing excess tabs.

    IMPORTANT: This only closes tabs when the count exceeds MAX_OPEN_TABS.
    It does NOT close about:blank tabs indiscriminately — those are often
    newly-created tabs owned by concurrent threads that haven't navigated
    yet. Closing them causes race conditions where one search destroys
    another's tab.
    """
    try:
        resp = httpx.get(f"http://localhost:{port}/json/list", timeout=3)
        tabs = resp.json()
        page_tabs = [t for t in tabs if t.get("type") == "page"]

        if len(page_tabs) > MAX_OPEN_TABS:
            # Only close excess tabs, starting from the newest ones
            # (keep the oldest MAX_OPEN_TABS, close the rest)
            for tab in page_tabs[MAX_OPEN_TABS:]:
                tab_id = tab.get("id")
                if tab_id:
                    httpx.get(
                        f"http://localhost:{port}/json/close/{tab_id}",
                        timeout=2)
    except Exception:
        pass  # Best-effort cleanup


# ========== Captcha Detection & Wait ==========

_MAX_CAPTCHA_WAIT = 180   # seconds for user to manually solve
_CAPTCHA_POLL_INTERVAL = 3  # seconds between checks


def _is_on_captcha_page(client: "CDPClient") -> bool:
    """
    Detect Google bot-detection / CAPTCHA page by inspecting body text.
    """
    try:
        body = (client.evaluate(
            "return (document.body && document.body.innerText || '');"
        ) or "").lower()
        title = (client.evaluate(
            "return document.title || '';"
        ) or "").lower()
        patterns = [
            "подозрительный трафик", "suspicious traffic",
            "зарегистрировали подозрител", "не робот",
            "are you a robot", "verify you are human",
            "security check", "captcha",
        ]
        is_bot_body = any(p in body for p in patterns)
        is_bot_title = title.startswith("https://") or title.startswith("http://")
        return is_bot_body and len(body) < 2000
    except Exception:
        return False


def _wait_for_captcha_resolution(
    client: "CDPClient",
    portal: str,
    port: int,
    start_time: float,
) -> None:
    """
    Poll until user manually solves the CAPTCHA and results appear.
    Prints status to stderr for visibility in MCP server output.
    """
    import sys
    deadline = time.time() + _MAX_CAPTCHA_WAIT
    last_log = time.time()
    log_interval = 10  # seconds between status lines

    msg = (
        f"\n[{portal.upper()}] CAPTCHA/bot-detection page detected.\n"
        f"[{portal.upper()}] Solve it in Chrome (port {port}), "
        f"waiting up to {_MAX_CAPTCHA_WAIT}s...\n"
    )
    print(msg, file=sys.stderr, flush=True)
    logger.warning(
        f"[{portal}] Manual CAPTCHA detected on port {port}"
    )

    while time.time() < deadline:
        time.sleep(_CAPTCHA_POLL_INTERVAL)
        client._reconnect()

        # Check: is captcha page gone?
        still_captcha = _is_on_captcha_page(client)

        if not still_captcha:
            # Captcha resolved, check for search results
            h3_count = client.evaluate(
                "return document.querySelectorAll("
                "'#search h3, #rso h3').length;"
            ) or 0
            if h3_count > 0:
                elapsed = time.time() - start_time
                print(
                    f"[{portal.upper()}] CAPTCHA resolved, "
                    f"results found after {elapsed:.0f}s\n",
                    file=sys.stderr, flush=True,
                )
                logger.info(
                    f"[{portal}] Captcha resolved ({elapsed:.0f}s), "
                    f"{h3_count} h3 elements found"
                )
                return

        # Still waiting: status update
        now = time.time()
        if now - last_log >= log_interval:
            elapsed = now - start_time
            print(
                f"[{portal.upper()}] Still waiting... "
                f"({elapsed:.0f}s elapsed)\n",
                file=sys.stderr, flush=True,
            )
            last_log = now

    # Timeout reached
    elapsed = time.time() - start_time
    logger.warning(
        f"[{portal}] Captcha wait timed out after {elapsed:.0f}s"
    )
    print(
        f"[{portal.upper()}] CAPTCHA wait timed out after "
        f"{elapsed:.0f}s\n",
        file=sys.stderr, flush=True,
    )


# ========== Search Functions ==========

def _search_portal(portal: str, keyword: str) -> list[SearchResult]:
    """
    Single portal search (uses independent Chrome instance).

    Flow:
    1. Navigate to search URL, wait for page to settle.
    2. Extract results.
    3. If 0 results AND captcha page detected:
       → Print "solve captcha" to stderr.
       → Poll every 3s until captcha resolves OR h3 appears (120s timeout).
       → Re-extract and return results.
    """
    config = PORTAL_CONFIG.get(portal)
    chrome_config = CHROME_INSTANCES.get(portal)

    if not config or not chrome_config:
        return []

    port = chrome_config["port"]

    if not is_chrome_running(port):
        logger.warning(f"[{portal}] Chrome not running, attempting to start...")
        if not start_chrome(portal):
            logger.error(f"[{portal}] Failed to start Chrome")
            return []
        time.sleep(2)

    try:
        start_time = time.time()

        client = CDPClient(port)
        if not client.create_tab():
            return []

        try:
            import urllib.parse
            search_url = config["search_url"] + urllib.parse.quote(keyword)

            content_check = ""
            if portal == "google":
                content_check = "document.querySelector('#search h3, #rso h3')"
            client.navigate(search_url, wait_time=3.5,
                            max_extra_wait=40.0,
                            content_check=content_check)
            client._reconnect()

            # --- Extraction ---
            raw_result = client.evaluate(config["extract_script"])
            results = _parse_results(raw_result, portal)

            if len(results) == 0:
                # Diagnostic: log page state to help debug future failures
                _dbg_title = client.evaluate("return document.title || '';") or "?"
                _dbg_url = client.evaluate("return window.location.href || '';") or "?"
                logger.warning(
                    f"[{portal}] 0 results after navigation. "
                    f"title='{_dbg_title}', url='{_dbg_url[:80]}'"
                )

                # Check if we landed on a captcha page first
                is_captcha = _is_on_captcha_page(client)

                if is_captcha:
                    # Manual CAPTCHA: wait for user to solve it
                    _wait_for_captcha_resolution(
                        client, portal, port, start_time
                    )
                    raw_result = client.evaluate(config["extract_script"])
                    results = _parse_results(raw_result, portal)
                else:
                    # Not captcha — page may still be rendering.
                    # Poll up to 15s more in 3s increments (5 attempts).
                    logger.info(
                        f"[{portal}] 0 results (not captcha), "
                        f"polling for slow render up to 15s..."
                    )
                    for attempt in range(1, 6):
                        time.sleep(3)
                        client._reconnect()
                        if portal == "google":
                            h3_count = client.evaluate(
                                "return document.querySelectorAll("
                                "'#search h3, #rso h3').length;"
                            ) or 0
                            logger.info(
                                f"[{portal}] Retry {attempt}/5: "
                                f"{h3_count} h3 elements found"
                            )
                            if h3_count == 0:
                                continue
                        raw_result = client.evaluate(config["extract_script"])
                        results = _parse_results(raw_result, portal)
                        if results:
                            logger.info(
                                f"[{portal}] Got {len(results)} results "
                                f"on retry {attempt}/5"
                            )
                            break

                    # Final captcha check after retry exhaustion
                    if len(results) == 0 and _is_on_captcha_page(client):
                        _wait_for_captcha_resolution(
                            client, portal, port, start_time
                        )
                        raw_result = client.evaluate(config["extract_script"])
                        results = _parse_results(raw_result, portal)

            elapsed = time.time() - start_time
            logger.info(f"[{portal}] {len(results)} results ({elapsed:.1f}s)")
            return results

        finally:
            # Always close the tab we created, even on error
            client.close_tab()

    except Exception as e:
        logger.error(f"[{portal}] Search failed: {e}")
        return []


def _parse_results(raw_result: any, source: str) -> list[SearchResult]:
    """Parse results"""
    if not raw_result:
        return []

    try:
        items = raw_result if isinstance(raw_result, list) else []
        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("snippet", ""),
                source=source
            )
            for item in items
            if item.get("url")
        ]
    except Exception as e:
        logger.debug(f"[{source}] Parse failed: {e}")
        return []


def search_parallel(
    keyword: str,
    portals: list[str] = None,
) -> list[SearchResult]:
    """
    Parallel search (3 Chrome instances simultaneously)

    Args:
        keyword: Search query
        portals: List of portals to search (default: ["naver", "google", "brave"])

    Returns:
        Search results from all portals
    """
    if portals is None:
        portals = ["google"]  # naver/brave disabled — poor results

    logger.info(f"[CDP] Starting parallel search: {keyword} ({portals})")
    start_time = time.time()

    all_results = []

    # True parallel execution with ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(_search_portal, portal, keyword): portal
            for portal in portals
        }

        for future in as_completed(futures):
            portal = futures[future]
            try:
                results = future.result()
                all_results.extend(results)
            except Exception as e:
                logger.error(f"[{portal}] Exception: {e}")

    total_elapsed = time.time() - start_time
    logger.info(f"[CDP] Search complete: {len(all_results)} results ({total_elapsed:.1f}s)")

    return all_results


# ========== URL Content Fetch via CDP (real Chrome) ==========

# JavaScript: extract main content text, falling back to body.
# Strategies (in order): <main>/<article>/<role=main">, then body minus nav/boilerplate.
_FETCH_EXTRACT_SCRIPT = """
    function pickMain() {
        var sel = ['main', 'article', '[role="main"]', '#content', '.content',
                   '#main-content', '.main-content', '.documentation',
                   'div[class*="content"]', 'div[class*="Content"]'];
        for (var i = 0; i < sel.length; i++) {
            var el = document.querySelector(sel[i]);
            if (el && el.innerText && el.innerText.length > 200) {
                return el;
            }
        }
        return null;
    }

    // Strip boilerplate clones from a copy of body so we keep nav out.
    function cleanBodyText() {
        var clone = document.body.cloneNode(true);
        var toRemove = clone.querySelectorAll(
            'script, style, nav, header, footer, aside, noscript, svg, iframe, form, button, .nav, .menu, .sidebar'
        );
        for (var i = 0; i < toRemove.length; i++) toRemove[i].remove();
        return clone.innerText || '';
    }

    var main = pickMain();
    var text = '';
    if (main) {
        // Clean inside main too (remove scripts/styles only).
        var c = main.cloneNode(true);
        var r = c.querySelectorAll('script, style, noscript, svg, iframe');
        for (var j = 0; j < r.length; j++) r[j].remove();
        text = c.innerText || '';
    }
    if (!text || text.length < 200) {
        text = cleanBodyText();
    }
    return (text || '').substring(0, 8000);
"""

# Minimum acceptable content length (matches mcp_server MIN_CONTENT_LENGTH)
_CDP_MIN_CONTENT = 200


def _pick_fetch_port() -> Optional[int]:
    """Return CDP port of first running Chrome instance (for fetching)."""
    for portal, cfg in CHROME_INSTANCES.items():
        if is_chrome_running(cfg["port"]):
            return cfg["port"]
    return None


def fetch_url_cdp(url: str, wait_time: float = 4.0) -> dict:
    """
    Fetch URL content through real Chrome via CDP.

    This bypasses JS-based anti-bot challenges (Cloudflare, Yandex SSO, etc.)
    because it uses a real browser session.

    Returns: {"url", "title", "content"} on success, or
             {"url", "error": str} on failure.
    """
    port = _pick_fetch_port()
    if port is None:
        return {"url": url, "error": "No Chrome CDP instance running"}

    try:
        client = CDPClient(port)
        if not client.create_tab():
            return {"url": url, "error": "CDP connection failed"}

        try:
            ok = client.navigate(url, wait_time=wait_time)
            if not ok:
                return {"url": url, "error": "Navigate failed"}

            title = client.evaluate("return document.title || '';") or ""
            # Detect anti-bot / challenge pages still loaded
            cur_url = client.evaluate("return window.location.href || '';") or ""
            body_html_len = client.evaluate(
                "return (document.body && document.body.innerHTML) "
                "? document.body.innerHTML.length : 0;"
            )

            content = client.evaluate(_FETCH_EXTRACT_SCRIPT)
            content = content if isinstance(content, str) else (content or "")

            if not content or len(content) < _CDP_MIN_CONTENT:
                return {
                    "url": url,
                    "error": "Content too short (CDP)",
                    "title": title,
                    "final_url": cur_url,
                    "body_html_len": body_html_len,
                }

            return {
                "url": url,
                "title": title,
                "content": content[:8000],
            }
        finally:
            # Always close the tab, even on error
            client.close_tab()
    except Exception as e:
        return {"url": url, "error": str(e)}


# ========== SmartCrawl Compatible Interface ==========

def search_with_cdp(
    keyword: str,
    portal: str = "all",
    count: int = 10,
    search_type: str = "news",
    skip_content: bool = True
) -> dict:
    """
    CDP-based search (SmartCrawl compatible interface)
    """
    try:
        if portal == "all":
            portals = ["google"]  # naver/brave disabled — poor results
        else:
            portals = [portal]

        results = search_parallel(keyword, portals)

        data = {
            "results": [
                {
                    "url": r.url,
                    "title": r.title,
                    "snippet": r.snippet,
                    "source": r.source
                }
                for r in results
            ],
            "skip_content": skip_content
        }

        return {
            "success": True,
            "data": data,
            "count": len(results)
        }

    except Exception as e:
        logger.error(f"[CDP] Search error: {e}")
        return {"success": False, "error": str(e)}


# ========== Test ==========

if __name__ == "__main__":
    import sys

    keyword = sys.argv[1] if len(sys.argv) > 1 else "Samsung stock price"
    portal = sys.argv[2] if len(sys.argv) > 2 else "all"

    print(f"\n=== CDP Parallel Search Test (3 Chrome instances) ===")
    print(f"Keyword: {keyword}")
    print(f"Portal: {portal}\n")

    result = search_with_cdp(keyword, portal)

    if result.get("success"):
        print(f"\n[Success] {result['count']} results")
        for item in result["data"]["results"][:5]:
            print(f"  [{item['source']}] {item['title'][:40]}...")
            print(f"       {item['url'][:60]}...")
    else:
        print(f"\n[Failed] {result.get('error')}")
