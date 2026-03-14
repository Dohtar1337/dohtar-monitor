"""
Generic HTTP probe for custom health checks in Dohtar Monitor Agent.
Can be configured to probe any HTTP endpoint.
"""

import asyncio
import logging
import time
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)


class GenericHttpProbe:
    """Probes arbitrary HTTP endpoints for health status."""

    def __init__(self, url: str, expected_status: int = 200, timeout: float = 2.0):
        """
        Initialize generic HTTP probe.

        Args:
            url: Full URL to probe (e.g., "http://localhost:8080/health")
            expected_status: Expected HTTP status code (default 200)
            timeout: Request timeout in seconds
        """
        self.url = url
        self.expected_status = expected_status
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def probe(self, session: aiohttp.ClientSession = None) -> Dict[str, Any]:
        """
        Probe HTTP endpoint.

        Args:
            session: Optional shared aiohttp session. Creates one if not provided.

        Returns:
            Dict with service status and response details.
        """
        result = {
            "endpoint": self.url,
            "status": "down",
            "status_code": None,
            "response_time_ms": None,
        }

        own_session = session is None
        try:
            if own_session:
                session = aiohttp.ClientSession(timeout=self.timeout)

            start_time = time.time()
            async with session.get(self.url) as resp:
                elapsed = (time.time() - start_time) * 1000

                result["status_code"] = resp.status
                result["response_time_ms"] = round(elapsed, 2)

                if resp.status == self.expected_status:
                    result["status"] = "up"
                else:
                    result["status"] = "degraded"

            return result

        except asyncio.TimeoutError:
            logger.debug(f"HTTP probe timeout for {self.url}")
            result["status"] = "timeout"
            return result

        except Exception as e:
            logger.debug(f"HTTP probe failed for {self.url}: {e}")
            return result
        finally:
            if own_session and session:
                await session.close()
