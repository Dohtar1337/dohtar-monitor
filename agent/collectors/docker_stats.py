"""
Docker stats collector for Dohtar Monitor Agent.
Collects Docker container metrics: CPU, memory, restart count, uptime.
Supports both Docker SDK (Linux) and shell commands (Windows/fallback).
"""

import asyncio
import json
import logging
import shutil
import subprocess
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class DockerCollector:
    """Collects Docker container stats including CPU, memory, restarts, and uptime."""

    def __init__(self):
        self._use_sdk = None  # None = auto-detect, True/False = cached result

    async def collect(self) -> Dict[str, Any]:
        """Collect Docker container metrics. Auto-detects SDK vs shell."""
        # Try SDK first, fall back to shell commands
        if self._use_sdk is None:
            try:
                import docker
                client = docker.from_env()
                client.ping()
                self._use_sdk = True
            except Exception:
                self._use_sdk = False

        if self._use_sdk:
            return await self._collect_sdk()
        else:
            return await self._collect_shell()

    async def _collect_shell(self) -> Dict[str, Any]:
        """Collect Docker stats using shell commands (works on Windows + Linux)."""
        # Check if docker CLI is available
        if not shutil.which("docker"):
            return {"containers": []}

        try:
            loop = asyncio.get_running_loop()

            # Get container list with details
            ps_result = await loop.run_in_executor(None, lambda: subprocess.run(
                ["docker", "ps", "-a", "--format",
                 '{"id":"{{.ID}}","name":"{{.Names}}","image":"{{.Image}}","status":"{{.Status}}","state":"{{.State}}","ports":"{{.Ports}}","created":"{{.CreatedAt}}"}'],
                capture_output=True, text=True, timeout=5
            ))

            if ps_result.returncode != 0:
                return {"containers": []}

            containers = []
            for line in ps_result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    c = json.loads(line)
                    containers.append(c)
                except json.JSONDecodeError:
                    continue

            if not containers:
                return {"containers": []}

            # Get stats for running containers
            stats_map = {}
            stats_result = await loop.run_in_executor(None, lambda: subprocess.run(
                ["docker", "stats", "--no-stream", "--format",
                 '{"name":"{{.Name}}","cpu":"{{.CPUPerc}}","mem_usage":"{{.MemUsage}}","mem_perc":"{{.MemPerc}}"}'],
                capture_output=True, text=True, timeout=10
            ))

            if stats_result.returncode == 0:
                for line in stats_result.stdout.strip().split("\n"):
                    if not line.strip():
                        continue
                    try:
                        s = json.loads(line)
                        stats_map[s["name"]] = s
                    except (json.JSONDecodeError, KeyError):
                        continue

            # Get inspect data for restart counts and start times
            container_ids = [c["id"] for c in containers]
            inspect_map = {}
            if container_ids:
                inspect_result = await loop.run_in_executor(None, lambda: subprocess.run(
                    ["docker", "inspect", "--format",
                     '{"id":"{{.ID}}","restart_count":{{.RestartCount}},"started_at":"{{.State.StartedAt}}","health":"{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"}']
                    + [c["id"] for c in containers],
                    capture_output=True, text=True, timeout=5
                ))
                if inspect_result.returncode == 0:
                    for line in inspect_result.stdout.strip().split("\n"):
                        if not line.strip():
                            continue
                        try:
                            d = json.loads(line)
                            inspect_map[d["id"][:12]] = d
                        except (json.JSONDecodeError, KeyError):
                            continue

            # Build result
            result = []
            for c in containers:
                stats = stats_map.get(c["name"], {})
                inspect = inspect_map.get(c["id"][:12], {})

                cpu_str = stats.get("cpu", "0%").replace("%", "")
                mem_perc_str = stats.get("mem_perc", "0%").replace("%", "")

                # Parse memory usage like "150.3MiB / 31.35GiB"
                mem_used_mb = 0
                mem_limit_mb = 0
                mem_usage = stats.get("mem_usage", "")
                if " / " in mem_usage:
                    parts = mem_usage.split(" / ")
                    mem_used_mb = self._parse_mem_str(parts[0].strip())
                    mem_limit_mb = self._parse_mem_str(parts[1].strip())

                is_running = c.get("state", "").lower() == "running"

                result.append({
                    "id": c["id"][:12],
                    "name": c["name"],
                    "image": c["image"],
                    "status": c.get("state", c.get("status", "unknown")),
                    "created": c.get("created"),
                    "cpu_percent": float(cpu_str) if cpu_str else 0,
                    "memory_percent": float(mem_perc_str) if mem_perc_str else 0,
                    "memory_used": mem_used_mb,
                    "memory_limit": mem_limit_mb,
                    "restart_count": inspect.get("restart_count", 0),
                    "started_at": inspect.get("started_at") if is_running else None,
                    "health": inspect.get("health", "none"),
                    "ports": self._parse_ports_str(c.get("ports", "")),
                })

            return {"containers": result}

        except Exception as e:
            logger.debug(f"Failed to collect Docker stats via shell: {e}")
            return {"containers": []}

    @staticmethod
    def _parse_mem_str(s: str) -> float:
        """Parse memory string like '150.3MiB' or '1.5GiB' to MB."""
        s = s.strip()
        try:
            if "GiB" in s:
                return round(float(s.replace("GiB", "").strip()) * 1024, 1)
            elif "MiB" in s:
                return round(float(s.replace("MiB", "").strip()), 1)
            elif "KiB" in s:
                return round(float(s.replace("KiB", "").strip()) / 1024, 1)
            elif "GB" in s:
                return round(float(s.replace("GB", "").strip()) * 1000, 1)
            elif "MB" in s:
                return round(float(s.replace("MB", "").strip()), 1)
            elif "kB" in s:
                return round(float(s.replace("kB", "").strip()) / 1000, 1)
            elif "B" in s:
                return round(float(s.replace("B", "").strip()) / (1024 * 1024), 1)
        except ValueError:
            pass
        return 0

    @staticmethod
    def _parse_ports_str(s: str) -> List[Dict[str, str]]:
        """Parse docker ports string like '0.0.0.0:9090->9090/tcp'."""
        if not s:
            return []
        ports = []
        for part in s.split(", "):
            part = part.strip()
            if "->" in part:
                public, private = part.split("->", 1)
                ports.append({"public": public, "private": private})
            elif part:
                ports.append({"private": part})
        return ports

    async def _collect_sdk(self) -> Dict[str, Any]:
        """Collect Docker stats using the Python Docker SDK."""
        try:
            import docker
            client = docker.from_env()
            containers = client.containers.list(all=True)

            container_stats = []
            for container in containers:
                try:
                    container_info = {
                        "id": container.id[:12],
                        "name": container.name,
                        "image": container.image.tags[0] if container.image and container.image.tags else container.image.id[:12] if container.image else "unknown",
                        "status": container.status,
                        "created": container.attrs.get("Created", None),
                        "cpu_percent": 0,
                        "memory_percent": 0,
                        "memory_used": 0,
                        "memory_limit": 0,
                        "restart_count": 0,
                        "started_at": None,
                        "health": None,
                    }

                    container_info["restart_count"] = container.attrs.get("RestartCount", 0)
                    state = container.attrs.get("State", {})
                    container_info["started_at"] = state.get("StartedAt", None)
                    health = state.get("Health", {})
                    if health:
                        container_info["health"] = health.get("Status", None)

                    if container.status == "running":
                        try:
                            stats = container.stats(stream=False)
                            container_info["cpu_percent"] = self._calculate_cpu_percent(stats)
                            mem_info = self._calculate_memory(stats)
                            container_info["memory_percent"] = mem_info["percent"]
                            container_info["memory_used"] = mem_info["used_mb"]
                            container_info["memory_limit"] = mem_info["limit_mb"]
                        except Exception as e:
                            logger.debug(f"Failed to get stats for {container.name}: {e}")

                    ports = []
                    if "NetworkSettings" in container.attrs and "Ports" in container.attrs["NetworkSettings"]:
                        port_bindings = container.attrs["NetworkSettings"]["Ports"]
                        for internal_port, external_list in port_bindings.items():
                            if external_list:
                                for external in external_list:
                                    ports.append({
                                        "private": internal_port,
                                        "public": f"{external.get('HostIp', '0.0.0.0')}:{external.get('HostPort', '?')}"
                                    })
                    container_info["ports"] = ports
                    container_stats.append(container_info)
                except Exception as e:
                    logger.debug(f"Failed to get info for container: {e}")
                    continue

            return {"containers": container_stats}

        except Exception as e:
            logger.debug(f"Failed to collect Docker stats via SDK: {e}")
            return {"containers": []}

    def _calculate_cpu_percent(self, stats: Dict) -> float:
        try:
            cpu_delta = (
                stats["cpu_stats"]["cpu_usage"]["total_usage"]
                - stats["precpu_stats"]["cpu_usage"]["total_usage"]
            )
            system_delta = (
                stats["cpu_stats"]["system_cpu_usage"]
                - stats["precpu_stats"]["system_cpu_usage"]
            )
            cpu_count = len(stats["cpu_stats"]["cpu_usage"].get("percpu_usage", []))
            if cpu_count == 0:
                cpu_count = stats["cpu_stats"].get("online_cpus", 1)
            if system_delta == 0:
                return 0.0
            return round((cpu_delta / system_delta) * cpu_count * 100.0, 2)
        except Exception:
            return 0.0

    def _calculate_memory(self, stats: Dict) -> Dict[str, float]:
        try:
            used = stats["memory_stats"].get("usage", 0)
            total = stats["memory_stats"].get("limit", 0)
            cache = stats["memory_stats"].get("stats", {}).get("cache", 0)
            used_net = used - cache if cache else used
            percent = round((used_net / total) * 100.0, 2) if total > 0 else 0.0
            return {"percent": percent, "used_mb": round(used_net / (1024 * 1024), 1), "limit_mb": round(total / (1024 * 1024), 0)}
        except Exception:
            return {"percent": 0.0, "used_mb": 0, "limit_mb": 0}
