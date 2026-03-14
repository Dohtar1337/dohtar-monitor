"""
Probe for Whisper (STT) service in Dohtar Monitor Agent.
Simple health check probe for Whisper/speech-to-text services.
"""

import asyncio
import logging
from typing import Any, Dict

import aiohttp

logger = logging.getLogger(__name__)


class WhisperProbe:
    """Probes Whisper/STT server endpoints."""

    def __init__(self, endpoint: str):
        """
        Initialize Whisper probe.

        Args:
            endpoint: Host:port string (e.g., "localhost:8083")
        """
        self.endpoint = endpoint
        self.base_url = f"http://{endpoint}"
        self.timeout = aiohttp.ClientTimeout(total=2.0)

    async def probe(self, session: aiohttp.ClientSession = None) -> Dict[str, Any]:
        """
        Probe Whisper service by checking health endpoints in parallel.

        Args:
            session: Optional shared aiohttp session. Creates one if not provided.

        Returns:
            Dict with service status.
        """
        result = {
            "type": "stt",
            "probe": "whisper",
            "endpoint": self.endpoint,
            "status": "down",
        }

        own_session = session is None
        try:
            if own_session:
                session = aiohttp.ClientSession(timeout=self.timeout)

            # Check all health endpoints in parallel
            endpoints_to_try = [
                "/health",
                "/api/health",
                "/status",
                "/api/status",
            ]

            checks = [
                self._check_endpoint(session, path)
                for path in endpoints_to_try
            ]
            results = await asyncio.gather(*checks, return_exceptions=True)

            # If any endpoint responded successfully, service is up
            for r in results:
                if r is True:
                    result["status"] = "up"
                    return result

            # Fallback: basic connectivity check
            if await self._check_connectivity(session):
                result["status"] = "up"

            return result

        except Exception as e:
            logger.debug(f"Whisper probe failed for {self.endpoint}: {e}")
            return result
        finally:
            if own_session and session:
                await session.close()

    async def _check_endpoint(self, session: aiohttp.ClientSession, path: str) -> bool:
        """Check if an endpoint returns 200."""
        try:
            async with session.get(f"{self.base_url}{path}") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def _check_connectivity(self, session: aiohttp.ClientSession) -> bool:
        """Check basic connectivity to the service."""
        try:
            # Try a simple GET to root
            async with session.get(self.base_url, allow_redirects=False) as resp:
                # Accept various success codes
                return resp.status < 500
        except Exception:
            return False
