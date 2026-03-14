#!/usr/bin/env python3
"""
Main agent loop for Dohtar Monitor.
Collects metrics from multiple sources and sends to backend.
"""

import argparse
import asyncio
import json
import logging
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aiohttp

from config import Config, ConfigError
from discovery import discover_backend
from version import AGENT_VERSION
from updater import AgentUpdater
from collectors import (
    SystemCollector,
    GPUCollector,
    ProcessCollector,
    LLMCollector,
    STTCollector,
    DockerCollector,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class Agent:
    """Main Dohtar Monitor Agent."""

    def __init__(self, config: Config):
        """
        Initialize agent.

        Args:
            config: Config instance.
        """
        self.config = config
        self.system_collector = SystemCollector()
        self.gpu_collector = GPUCollector()
        self.process_collector = ProcessCollector(gpu_collector=self.gpu_collector)
        self.llm_collector = LLMCollector(config.services)
        self.stt_collector = STTCollector(config.services)
        self.docker_collector = DockerCollector()

        self.session: Optional[aiohttp.ClientSession] = None
        self.backoff_factor = 1.0
        self.max_backoff = 60.0
        self.last_successful_post = time.time()

        # Agent updater
        self.updater = AgentUpdater(config.backend_url)

        # Cache GPU data between collectors to avoid double nvidia-smi calls
        self._cached_gpu_data: Optional[Dict[str, Any]] = None

    async def start(self) -> None:
        """Start agent loop."""
        logger.info(f"Dohtar Monitor Agent starting (ID: {self.config.agent_id})")
        logger.info(f"Machine: {self.config.machine_name}")
        logger.info(f"Backend: {self.config.backend_url}")
        logger.info(f"Poll interval: {self.config.poll_interval}s")

        # Validate config
        try:
            self.config.validate()
        except ConfigError as e:
            logger.error(f"Config validation failed: {e}")
            return

        # Create HTTP session
        self.session = aiohttp.ClientSession()

        try:
            # Main collection loop
            while True:
                try:
                    cycle_start = time.monotonic()

                    # Collect all metrics (parallel)
                    payload = await self._collect_metrics()

                    # Send to backend
                    success = await self._send_payload(payload)

                    if success:
                        self.last_successful_post = time.time()
                        self.backoff_factor = 1.0
                    else:
                        self.backoff_factor = min(
                            self.backoff_factor * 1.5, self.max_backoff
                        )

                    # Elapsed-aware sleep: only sleep the remaining time
                    elapsed = time.monotonic() - cycle_start
                    sleep_time = max(0.1, self.config.poll_interval - elapsed)
                    logger.debug(
                        f"Cycle took {elapsed:.2f}s, sleeping {sleep_time:.2f}s"
                    )
                    await asyncio.sleep(sleep_time)

                except KeyboardInterrupt:
                    logger.info("Agent interrupted by user")
                    break
                except Exception as e:
                    logger.error(f"Error in collection loop: {e}")
                    await asyncio.sleep(min(5.0, self.config.poll_interval))

        finally:
            if self.session:
                await self.session.close()
            logger.info("Agent stopped")

    def _get_local_ip(self) -> Optional[str]:
        """
        Determine the real LAN IP by connecting a UDP socket to the backend.
        This avoids hardcoding the interface and works even in Docker.

        Returns:
            Local IP address as string, or None if unable to determine.
        """
        try:
            backend_url = self.config.backend_url
            parsed = urlparse(backend_url)
            backend_host = parsed.hostname or "localhost"
            backend_port = parsed.port or 80

            # Create a UDP socket and "connect" (doesn't actually send data)
            # This forces the OS to select the local address that would be used
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect((backend_host, backend_port))
            local_ip = sock.getsockname()[0]
            sock.close()

            logger.debug(f"Determined local IP: {local_ip}")
            return local_ip
        except Exception as e:
            logger.warning(f"Failed to determine local IP: {e}")
            return None

    def _map_services_to_gpus(
        self, services: List[Dict[str, Any]], gpu_processes: Dict[int, List[Dict[str, Any]]]
    ) -> List[Dict[str, Any]]:
        """
        Map services to GPU indices based on process names and GPU memory usage.
        Matches service processes (llama-server, python3 for whisper) to GPUs.

        Args:
            services: List of service dicts (llm/stt).
            gpu_processes: Dict mapping GPU index to list of processes using that GPU.

        Returns:
            Services list with added gpu_indices field.
        """
        # Build a mapping of process name patterns to GPU indices
        process_to_gpus: Dict[str, set] = {}

        for gpu_idx, procs in gpu_processes.items():
            for proc in procs:
                proc_name = proc.get("name", "").lower()
                if proc_name not in process_to_gpus:
                    process_to_gpus[proc_name] = set()
                process_to_gpus[proc_name].add(gpu_idx)

        # Map each service to GPUs based on its likely process name
        for service in services:
            gpu_indices = set()
            service_type = service.get("type")

            if service_type == "llm":
                # LLM services typically run as llama-server, llama.cpp, or ollama
                for proc_name in process_to_gpus:
                    if any(
                        name in proc_name
                        for name in ["llama", "ollama", "vllm", "text-generation"]
                    ):
                        gpu_indices.update(process_to_gpus[proc_name])

            elif service_type == "stt":
                # STT services typically run as python3 or whisper process
                for proc_name in process_to_gpus:
                    if any(
                        name in proc_name for name in ["python", "whisper", "faster_whisper"]
                    ):
                        gpu_indices.update(process_to_gpus[proc_name])

            # Add gpu_indices field (sorted list for consistency)
            service["gpu_indices"] = sorted(list(gpu_indices)) if gpu_indices else []

        return services

    async def _collect_metrics(self) -> Dict[str, Any]:
        """
        Collect all enabled metrics in parallel using asyncio.gather.

        Returns:
            Payload dict ready to send to backend.
        """
        payload = {
            "agent_id": self.config.agent_id,
            "machine_name": self.config.machine_name,
            "agent_version": AGENT_VERSION,
            "ts": int(time.time() * 1000),  # Milliseconds
            "client_ip": self._get_local_ip(),  # Real LAN IP, not Docker bridge
        }

        # Phase 1: Collect system, GPU, docker in parallel (these are independent)
        tasks_phase1 = {}

        if self.config.is_collector_enabled("system"):
            tasks_phase1["system"] = self.system_collector.collect()

        if self.config.is_collector_enabled("gpu"):
            tasks_phase1["gpu"] = self.gpu_collector.collect()

        if self.config.is_collector_enabled("docker"):
            tasks_phase1["docker"] = self.docker_collector.collect()

        # Also run LLM/STT probes in parallel (they're network I/O, independent)
        if self.config.services:
            tasks_phase1["llm"] = self.llm_collector.collect()
            tasks_phase1["stt"] = self.stt_collector.collect()

        # Run all phase 1 tasks in parallel
        if tasks_phase1:
            keys = list(tasks_phase1.keys())
            results = await asyncio.gather(
                *tasks_phase1.values(), return_exceptions=True
            )

            phase1_results = {}
            for key, result in zip(keys, results):
                if isinstance(result, Exception):
                    logger.error(f"{key} collector failed: {result}")
                    phase1_results[key] = {}
                else:
                    phase1_results[key] = result

            # Store results
            if "system" in phase1_results and phase1_results["system"]:
                payload["system"] = phase1_results["system"]

            if "gpu" in phase1_results:
                gpu_data = phase1_results["gpu"]
                if gpu_data.get("gpus"):
                    payload["gpus"] = gpu_data["gpus"]
                # Cache GPU data for ProcessCollector
                self._cached_gpu_data = gpu_data
            else:
                self._cached_gpu_data = None

            if "docker" in phase1_results:
                if phase1_results["docker"].get("containers"):
                    payload["containers"] = phase1_results["docker"]["containers"]

            # Merge LLM + STT services
            services = []
            if "llm" in phase1_results:
                services.extend(phase1_results["llm"].get("services", []))
            if "stt" in phase1_results:
                services.extend(phase1_results["stt"].get("services", []))

            # Map services to GPU indices based on process GPU usage
            if services and self._cached_gpu_data:
                services = self._map_services_to_gpus(
                    services, self._cached_gpu_data.get("gpu_processes", {})
                )

            if services:
                payload["services"] = services

        # Phase 2: Process collector (uses cached GPU data, no extra nvidia-smi call)
        if self.config.is_collector_enabled("processes"):
            try:
                proc_data = await self.process_collector.collect(
                    cached_gpu_data=self._cached_gpu_data
                )
                payload["processes"] = proc_data.get("processes", [])
            except Exception as e:
                logger.error(f"Process collector failed: {e}")

        return payload

    async def _send_payload(self, payload: Dict[str, Any]) -> bool:
        """
        Send collected payload to backend.

        Args:
            payload: Data to send.

        Returns:
            True if successful, False otherwise.
        """
        if not self.session:
            return False

        url = f"{self.config.backend_url}/api/ingest"
        timeout = aiohttp.ClientTimeout(total=5.0)

        try:
            async with self.session.post(
                url,
                json=payload,
                timeout=timeout,
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status == 200:
                    logger.debug(f"Payload sent successfully (status: {resp.status})")

                    # Check for commands from backend (e.g., update)
                    try:
                        resp_data = await resp.json()
                        commands = resp_data.get("commands", [])
                        if commands:
                            logger.info(f"Received {len(commands)} command(s) from backend: {[c.get('type') for c in commands]}")
                        for cmd in commands:
                            await self._handle_command(cmd)
                    except Exception as e:
                        logger.warning(f"Failed to process backend response/commands: {e}")

                    return True
                else:
                    logger.warning(
                        f"Backend returned status {resp.status} for payload"
                    )
                    return False

        except asyncio.TimeoutError:
            logger.warning(f"Timeout sending payload to {url}")
            return False
        except aiohttp.ClientConnectorError as e:
            logger.warning(f"Connection error to backend: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to send payload: {e}")
            return False

    async def _handle_command(self, command: Dict[str, Any]) -> None:
        """Handle a command received from the backend."""
        cmd_type = command.get("type")

        if cmd_type == "update":
            logger.info("Received update command from backend")
            success = await self.updater.perform_update()
            if success:
                logger.info("Update successful, restarting...")
                # Close session cleanly before restart
                if self.session:
                    await self.session.close()
                self.updater.restart_service()

        elif cmd_type == "restart":
            logger.info("Received restart command from backend")
            if self.session:
                await self.session.close()
            self.updater.restart_service()

        else:
            logger.debug(f"Unknown command type: {cmd_type}")


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Dohtar Monitor Agent")
    parser.add_argument(
        "--config",
        type=str,
        help="Path to config.json",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Debug logging enabled")

    try:
        # Load config
        config = Config(config_path=args.config)
        config.validate()

        # Start agent
        agent = Agent(config)
        await agent.start()

    except ConfigError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
