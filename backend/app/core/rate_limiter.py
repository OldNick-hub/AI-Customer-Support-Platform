"""Application-Layer HTTP Request Rate Limiting Infrastructure.

Provides an in-memory sliding-window request throttle:
1. Endpoint-specific limits (Chat: 30/min, Evidence Upload: 10/min).
2. Identity-aware key resolution (authenticated session_token / staff user_id, fallback to trusted client IP).
3. Anti-spoofing defense: Forwarded headers (X-Forwarded-For, X-Real-IP) are trusted ONLY when the direct
   peer socket is an authorized proxy (TRUSTED_PROXIES).
4. Standard RFC 6585 HTTP 429 response with 'Retry-After' header.
5. In-memory sliding window requiring zero external Redis infrastructure.
"""

import asyncio
from collections import deque
import ipaddress
import math
import time
from typing import Dict, List, Optional, Tuple

from fastapi import Depends, HTTPException, Request, status

from app.api.deps import ResolvedAuthContext, get_auth_context
from app.core.config import settings


class SlidingWindowRateLimiter:
    """Thread-safe, asyncio-compatible in-memory sliding window rate limiter."""

    def __init__(self) -> None:
        self._windows: Dict[str, deque] = {}
        self._lock = asyncio.Lock()

    async def check_rate_limit(
        self,
        key: str,
        max_requests: int,
        window_seconds: float = 60.0,
    ) -> Tuple[bool, int]:
        """Evaluates whether the request is allowed under the rate limit.

        Args:
            key: Unique client / endpoint identifier.
            max_requests: Maximum allowed requests within window_seconds.
            window_seconds: Duration of sliding window in seconds (default: 60.0).

        Returns:
            Tuple[bool, int]: (is_allowed, retry_after_seconds)
        """
        now = time.monotonic()
        cutoff = now - window_seconds

        async with self._lock:
            window = self._windows.setdefault(key, deque())

            # Prune timestamps older than window cutoff
            while window and window[0] <= cutoff:
                window.popleft()

            if len(window) >= max_requests:
                # Oldest timestamp determines when the next slot frees up
                oldest_ts = window[0]
                retry_after = max(1, math.ceil(oldest_ts + window_seconds - now))
                return False, retry_after

            window.append(now)
            return True, 0

    def reset(self) -> None:
        """Clears all sliding windows. Intended for testing."""
        self._windows.clear()


default_rate_limiter = SlidingWindowRateLimiter()


def is_trusted_proxy(client_host: Optional[str], trusted_proxies: Optional[List[str]]) -> bool:
    """Safely determines if the immediate TCP socket client_host is an authorized reverse proxy."""
    if not client_host or not trusted_proxies:
        return False

    client_host_clean = client_host.strip()
    if client_host_clean in trusted_proxies:
        return True

    try:
        client_ip = ipaddress.ip_address(client_host_clean)
        for proxy_entry in trusted_proxies:
            if not proxy_entry:
                continue
            proxy_clean = str(proxy_entry).strip()
            try:
                if "/" in proxy_clean:
                    net = ipaddress.ip_network(proxy_clean, strict=False)
                    if client_ip in net:
                        return True
                else:
                    ip = ipaddress.ip_address(proxy_clean)
                    if client_ip == ip:
                        return True
            except ValueError:
                continue
    except ValueError:
        return False

    return False


def resolve_rate_limit_key(
    request: Request,
    auth: Optional[ResolvedAuthContext],
    endpoint_tag: str,
) -> str:
    """Derives a rate-limiting key bound to client identity or trusted IP.

    Priority:
    1. Authenticated customer session token -> session:<token>
    2. Authenticated staff user ID / email -> staff:<user_id>
    3. Authenticated customer ID -> cust:<customer_id>
    4. Client IP address (with strict trusted-proxy forwarding header validation) -> ip:<client_ip>
    """
    if auth:
        if auth.session_token:
            return f"{endpoint_tag}:session:{auth.session_token[:32]}"
        if auth.user_id_or_email:
            return f"{endpoint_tag}:staff:{auth.user_id_or_email}"
        if auth.customer_id:
            return f"{endpoint_tag}:cust:{auth.customer_id}"

    # Unauthenticated / IP-based fallback
    client_host = request.client.host if request.client else "127.0.0.1"
    trusted_proxies = getattr(settings, "TRUSTED_PROXIES", ["127.0.0.1", "::1"])

    if is_trusted_proxy(client_host, trusted_proxies):
        client_ip = None

        # 1. Prefer authoritative X-Real-IP set/overwritten by the trusted reverse proxy
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            try:
                candidate = ipaddress.ip_address(real_ip.strip())
                client_ip = str(candidate)
            except ValueError:
                client_ip = None

        # 2. Fallback to X-Forwarded-For trusted chain traversal (right-to-left)
        if not client_ip:
            x_forwarded_for = request.headers.get("X-Forwarded-For")
            if x_forwarded_for:
                raw_entries = [e.strip() for e in x_forwarded_for.split(",") if e.strip()]
                # Traverse right-to-left: skip hops that are themselves in TRUSTED_PROXIES;
                # the first non-trusted valid IP is the authentic client.
                for entry in reversed(raw_entries):
                    try:
                        candidate_ip = ipaddress.ip_address(entry)
                        candidate_str = str(candidate_ip)
                        if not is_trusted_proxy(candidate_str, trusted_proxies):
                            client_ip = candidate_str
                            break
                    except ValueError:
                        continue

                # If all valid entries in chain were trusted proxies, take the leftmost valid one
                if not client_ip:
                    for entry in raw_entries:
                        try:
                            client_ip = str(ipaddress.ip_address(entry))
                            break
                        except ValueError:
                            continue

        # 3. Ultimate fallback to direct socket client host
        if not client_ip:
            client_ip = client_host
    else:
        # Untrusted direct socket: do NOT trust spoofable forwarding headers!
        client_ip = client_host

    return f"{endpoint_tag}:ip:{client_ip}"


async def check_chat_rate_limit(
    request: Request,
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> None:
    """FastAPI dependency enforcing chat turn query rate limit (default 30/min)."""
    if not getattr(settings, "RATE_LIMIT_ENABLED", True):
        return

    key = resolve_rate_limit_key(request, auth, "chat")
    limit = getattr(settings, "RATE_LIMIT_CHAT_PER_MINUTE", 30)

    allowed, retry_after = await default_rate_limiter.check_rate_limit(key, limit)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please retry later.",
            headers={"Retry-After": str(retry_after)},
        )


async def check_evidence_rate_limit(
    request: Request,
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> None:
    """FastAPI dependency enforcing multimodal evidence upload rate limit (default 10/min)."""
    if not getattr(settings, "RATE_LIMIT_ENABLED", True):
        return

    key = resolve_rate_limit_key(request, auth, "evidence")
    limit = getattr(settings, "RATE_LIMIT_EVIDENCE_PER_MINUTE", 10)

    allowed, retry_after = await default_rate_limiter.check_rate_limit(key, limit)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please retry later.",
            headers={"Retry-After": str(retry_after)},
        )

