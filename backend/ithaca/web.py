"""Web search and page fetching with an SSRF guard."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from . import settings

# Page fetches identify honestly as an automated assistant (Wikimedia and
# others block generic or spoofed agents). The DuckDuckGo HTML endpoint is
# built for simple browsers, so search uses a browser-style agent.
USER_AGENT = "Ithaca/1.0 (personal AI research assistant; low-volume; user-initiated)"


def fetch_user_agent() -> str:
    """Some sites (e.g. Wikipedia) also require contact info; it's opt-in."""
    contact = str(settings.get("web.contact") or "").strip()
    return f"Ithaca/1.0 (personal AI research assistant; {contact})" if contact else USER_AGENT
SEARCH_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Ithaca/1.0"
MAX_BYTES = 3_000_000


class WebError(RuntimeError):
    pass


async def _assert_public(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise WebError("Only http and https URLs can be fetched.")
    host = parsed.hostname
    if not host:
        raise WebError("URL has no host.")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise WebError(f"Could not resolve {host}.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise WebError(f"{host} resolves to a private or local address; fetching it is blocked.")


async def fetch(url: str, max_chars: int = 20_000) -> dict[str, Any]:
    """Fetch a public URL and return readable text."""
    current = url
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False, headers={"User-Agent": fetch_user_agent()}) as client:
        for _ in range(6):
            await _assert_public(current)
            async with client.stream("GET", current) as resp:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    current = urljoin(current, resp.headers["location"])
                    continue
                if resp.status_code >= 400:
                    raise WebError(f"HTTP {resp.status_code} for {current}")
                ctype = resp.headers.get("content-type", "")
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        break
                break
        else:
            raise WebError("Too many redirects.")

    text_body = body.decode(resp.encoding or "utf-8", "replace")
    if "html" in ctype or text_body.lstrip().startswith("<"):
        title, text = extract_readable(text_body)
    elif ctype.startswith("text/") or "json" in ctype or "xml" in ctype:
        title, text = "", text_body
    else:
        raise WebError(f"Unsupported content type: {ctype or 'unknown'}")
    truncated = len(text) > max_chars
    return {"url": current, "title": title, "text": text[:max_chars], "truncated": truncated}


def extract_readable(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form", "iframe"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    lines = []
    for el in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "pre", "td", "blockquote"]):
        t = " ".join(el.get_text(" ", strip=True).split())
        if not t:
            continue
        if el.name in ("h1", "h2", "h3", "h4"):
            t = "#" * int(el.name[1]) + " " + t
        elif el.name == "li":
            t = "- " + t
        lines.append(t)
    text = "\n".join(lines) if lines else " ".join(root.get_text(" ", strip=True).split())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return title, text


async def search(query: str, limit: int = 8) -> list[dict[str, str]]:
    backend = settings.get("web.search_backend")
    if backend == "searxng" and settings.get("web.searxng_url"):
        return await _searxng(query, limit)
    return await _duckduckgo(query, limit)


async def _searxng(query: str, limit: int) -> list[dict[str, str]]:
    base = str(settings.get("web.searxng_url")).rstrip("/")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{base}/search", params={"q": query, "format": "json"})
    if resp.status_code >= 400:
        raise WebError(f"SearXNG returned {resp.status_code}")
    out = []
    for r in resp.json().get("results", [])[:limit]:
        out.append({"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")})
    return out


async def _duckduckgo(query: str, limit: int) -> list[dict[str, str]]:
    async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": SEARCH_USER_AGENT}, follow_redirects=True) as client:
        resp = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
    if resp.status_code >= 400:
        raise WebError(f"DuckDuckGo returned {resp.status_code}")
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for res in soup.select("div.result"):
        a = res.select_one("a.result__a")
        if not a or not a.get("href"):
            continue
        href = a["href"]
        if "duckduckgo.com/l/" in href:
            qs = parse_qs(urlparse(href if href.startswith("http") else "https:" + href).query)
            href = unquote(qs.get("uddg", [href])[0])
        if "duckduckgo.com/y.js" in href:
            continue  # ads
        snippet = res.select_one(".result__snippet")
        out.append({
            "title": a.get_text(" ", strip=True),
            "url": href,
            "snippet": snippet.get_text(" ", strip=True) if snippet else "",
        })
        if len(out) >= limit:
            break
    return out
