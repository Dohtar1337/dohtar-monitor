"""
STT service collector for Dohtar Monitor Agent.
Uses probes to monitor STT services (Whisper, etc).
"""

import asyncio
import logging
from typing import Any, Dict, List

import aiohttp

from probes.whisper import WhisperProbe
from probes.generic_http import GenericHttpProbe

logger = logging.getLogger(__name__)


class STTCollector:
    """Collects STT service metrics using specialized probes."""

    def __init__(self, services: List[Dict[str, Any]]):
        """
        Initialize STT collector.

        Args:
            services: List of service config dicts with type, endpoint, probe fields.
        """
        self.services = [s for s in services if s.get("type") == "stt"]
        self.timeout = aiohttp.ClientTimeout(total=2.0)

        # Cache probe instances (avoid re-creating every cycle)
        self._probes: Dict[str, Any] = {}
        for sc in self.services:
            endpoint = sc.get("endpoint")
            probe_type = sc.get("probe")
            if endpoint and probe_type:
                key = f"{probe_type}:{endpoint}"
                if probe_type == "whisper":
                    self._probes[key] = WhisperProbe(endpoint)
                elif probe_type == "generic":
                    url = endpoint if endpoint.startswith("http") else f"http://{endpoint}"
                    self._probes[key] = GenericHttpProbe(url)

    async def collect(self) -> Dict[str, Any]:
        """
        Probe all configured STT services in parallel using a shared session.

        Returns:
            Dict with list of service status dicts.
        """
        if not self.services:
            return {"services": []}

        # Use a SINGLE shared session for all probes (connection reuse)
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            tasks = [self._probe_service(sc, session) for sc in self.services]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        services_data = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                sc = self.services[i]
                logger.error(f"Failed to probe STT service {sc.get('name')}: {result}")
                services_data.append(
                    {
                        "name": sc.get("name", "unknown"),
                        "endpoint": sc.get("endpoint"),
                        "type": "stt",
                        "probe": sc.get("probe"),
                        "status": "error",
                    }
                )
            elif result is not None:
                services_data.append(result)

        return {"services": services_data}

    async def _probe_service(
        self, service_config: Dict[str, Any], session: aiohttp.ClientSession
    ) -> Dict[str, Any]:
        """Probe a single STT service using a shared session."""
        endpoint = service_config.get("endpoint")
        probe_type = service_config.get("probe")
        service_name = service_config.get("name")

        if not endpoint:
            logger.warning(f"STT service has no endpoint: {service_name}")
            return None

        # Use cached probe instance
        key = f"{probe_type}:{endpoint}"
        probe = self._probes.get(key)

        if not probe:
            logger.warning(f"Unknown STT probe type: {probe_type}")
            return None

        # Pass shared session to probe
        result = await probe.probe(session=session)
        result["name"] = service_name
        result["endpoint"] = endpoint
        result["type"] = "stt"

        return result
