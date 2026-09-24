"""Send minimal, neutral ntfy invitations."""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)
_TOPIC = re.compile(r"^[A-Za-z0-9_-]+$")

_MESSAGES = {
    "plan_review": "有一项方案待确认，请打开私人项目页。",
    "decision": "有一项选择待确认，请打开私人项目页。",
    "waiting_decision": "有一项选择待确认，请打开私人项目页。",
    "ready": "项目有新结果，请打开私人项目页。",
    "failed": "项目状态已更新，请打开私人项目页。",
}


async def send_notification(kind: str, project_url: str) -> bool:
    """Publish one neutral message. State change deduplication belongs to core."""
    topic = os.getenv("NTFY_TOPIC", "").strip()
    base_url = os.getenv("NTFY_BASE_URL", "https://ntfy.sh").rstrip("/")
    token = os.getenv("NTFY_TOKEN", "").strip()
    parsed = urlparse(project_url)
    if kind not in _MESSAGES or not _TOPIC.fullmatch(topic):
        return False
    # On a public ntfy server, anyone who guesses a topic can subscribe.
    # Require a long, nontrivial topic when no authentication is configured.
    if not token and (len(topic) < 24 or len(set(topic)) < 10):
        logger.warning("ntfy skipped: unauthenticated topic must be a random 24+ character name")
        return False
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return False
    # A query or fragment could carry a login token onto a public ntfy server.
    if parsed.query or parsed.fragment:
        return False
    if urlparse(base_url).scheme != "https":
        return False
    try:
        import httpx
    except ImportError:
        logger.warning("ntfy skipped: httpx is not installed")
        return False
    headers = {"Title": "Project update", "Click": project_url}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{base_url}/{topic}",
                content=_MESSAGES[kind].encode("utf-8"),
                headers=headers,
            )
            response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.warning("ntfy delivery failed: %s", type(exc).__name__)
        return False
