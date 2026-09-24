"""Optional Tavily search; callers must distinguish unavailable from no hits."""

from __future__ import annotations

import os

from .errors import IntegrationUnavailable


async def search(query: str) -> list[dict[str, str]]:
    """Return cited search hits or raise IntegrationUnavailable.

    An empty list means the configured service returned no usable results. Missing
    credentials, network failures and API errors are never represented as no hits.
    """
    query = query.strip()
    if not query:
        raise ValueError("search query is empty")
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    if not api_key:
        raise IntegrationUnavailable("Tavily", "TAVILY_API_KEY is not configured")
    try:
        import httpx
    except ImportError as exc:
        raise IntegrationUnavailable("Tavily", "httpx is not installed") from exc

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "query": query,
                    "search_depth": "basic",
                    "max_results": 5,
                    "include_answer": False,
                    "include_raw_content": False,
                    "include_images": False,
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise IntegrationUnavailable("Tavily", f"search request failed: {type(exc).__name__}") from exc

    results = payload.get("results")
    if not isinstance(results, list):
        raise IntegrationUnavailable("Tavily", "response has no results list")
    hits: list[dict[str, str]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        title, url, snippet = item.get("title"), item.get("url"), item.get("content")
        if isinstance(title, str) and isinstance(url, str) and url.startswith("https://"):
            hits.append({
                "title": title.strip()[:300],
                "url": url,
                "snippet": snippet.strip()[:1000] if isinstance(snippet, str) else "",
            })
    return hits
