"""
Probe for Ollama service in Dohtar Monitor Agent.
Queries /api/tags and /api/ps endpoints.
"""

import logging
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


class OllamaProbe:
    """Probes Ollama server endpoints."""

    def __init__(self, endpoint: str):
        """
        Initialize Ollama probe.

        Args:
            endpoint: Host:port string (e.g., "localhost:11434")
        """
        self.endpoint = endpoint
        self.base_url = f"http://{endpoint}"
        self.timeout = aiohttp.ClientTimeout(total=2.0)

    async def probe(self, session: aiohttp.ClientSession = None) -> Dict[str, Any]:
        """
        Probe Ollama service.

        Args:
            session: Optional shared aiohttp session. Creates one if not provided.

        Returns:
            Dict with service status and loaded models.
        """
        result = {
            "type": "llm",
            "probe": "ollama",
            "endpoint": self.endpoint,
            "status": "down",
            "models": [],
        }

        own_session = session is None
        try:
            if own_session:
                session = aiohttp.ClientSession(timeout=self.timeout)

            # Get loaded models
            models = await self._get_loaded_models(session)
            result["models"] = models
            result["status"] = "up" if models else "idle"

            return result

        except Exception as e:
            logger.debug(f"Ollama probe failed for {self.endpoint}: {e}")
            return result
        finally:
            if own_session and session:
                await session.close()

    async def _get_loaded_models(
        self, session: aiohttp.ClientSession
    ) -> List[Dict[str, Any]]:
        """Get currently loaded models from /api/ps endpoint."""
        try:
            async with session.get(f"{self.base_url}/api/ps") as resp:
                if resp.status != 200:
                    return []

                data = await resp.json()
                models = data.get("models", [])

                result = []
                for model in models:
                    try:
                        result.append(
                            {
                                "name": model.get("name", "unknown"),
                                "vram": round(model.get("size", 0) / (1024**3), 2),
                            }
                        )
                    except Exception as e:
                        logger.debug(f"Error parsing model info: {e}")
                        continue

                return result

        except Exception as e:
            logger.debug(f"Failed to get Ollama models: {e}")
            return []

    async def _get_all_models(self, session: aiohttp.ClientSession) -> List[str]:
        """Get all available models (not just loaded) from /api/tags endpoint."""
        try:
            async with session.get(f"{self.base_url}/api/tags") as resp:
                if resp.status != 200:
                    return []

                data = await resp.json()
                models = data.get("models", [])

                return [model.get("name") for model in models if model.get("name")]

        except Exception as e:
            logger.debug(f"Failed to get Ollama tags: {e}")
            return []
