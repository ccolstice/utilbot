from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse, urlunparse

import aiohttp

from src.utils.safe_http import PublicResolver, validate_public_http_url


URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.IGNORECASE)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass(slots=True)
class LinkInfo:
    url: str
    domain: str
    title: str | None = None


class LinkService:
    @staticmethod
    def extract_urls(text: str | None) -> list[str]:
        if not text:
            return []
        urls: list[str] = []
        for match in URL_RE.findall(text):
            url = match.rstrip(".,;:!?)>]}")
            if url and url not in urls:
                urls.append(url)
        return urls

    @staticmethod
    def normalize_url(url: str) -> str:
        parsed = urlparse(url.strip())
        scheme = parsed.scheme.lower() or "https"
        netloc = parsed.netloc.lower()
        path = parsed.path or ""
        return urlunparse((scheme, netloc, path, "", parsed.query, ""))

    @staticmethod
    def domain(url: str) -> str:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain

    @classmethod
    def first_link(cls, text: str | None) -> LinkInfo | None:
        urls = cls.extract_urls(text)
        if not urls:
            return None
        url = cls.normalize_url(urls[0])
        return LinkInfo(url=url, domain=cls.domain(url))

    @classmethod
    async def enrich_first_link(cls, text: str | None, *, timeout_seconds: float = 3.0) -> LinkInfo | None:
        info = cls.first_link(text)
        if not info:
            return None

        try:
            timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=timeout_seconds)
            headers = {"User-Agent": "tg-util-bot/1.0"}
            connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
            current_url = info.url
            async with aiohttp.ClientSession(timeout=timeout, headers=headers, connector=connector) as session:
                for redirect_count in range(6):
                    validate_public_http_url(current_url)
                    async with session.get(current_url, allow_redirects=False) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.headers.get("Location")
                            if not location or redirect_count == 5:
                                return info
                            current_url = urljoin(current_url, location)
                            continue
                        if response.status >= 400:
                            return info
                        content_type = response.headers.get("content-type", "")
                        if "text/html" not in content_type.lower():
                            return info
                        declared_size = response.content_length
                        if declared_size is not None and declared_size > 200_000:
                            return info
                        body = (await response.content.read(200_001)).decode(
                            response.charset or "utf-8", errors="ignore"
                        )
                        if len(body.encode("utf-8", errors="ignore")) > 200_000:
                            body = body[:200_000]
                        break
                else:
                    return info
        except (ValueError, OSError, aiohttp.ClientError, asyncio.TimeoutError):
            return info
        except Exception:
            return info

        match = TITLE_RE.search(body[:200_000])
        if not match:
            return info

        title = html.unescape(re.sub(r"\s+", " ", match.group(1))).strip()
        if title:
            info.title = title[:300]
        return info
