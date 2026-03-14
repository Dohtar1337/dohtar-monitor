"""
Process metrics collector for Dohtar Monitor Agent.
Collects top processes by memory and enriches with GPU memory info.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import psutil

logger = logging.getLogger(__name__)


class ProcessCollector:
    """Collects process metrics with GPU enrichment."""

    def __init__(self, gpu_collector: Optional[Any] = None):
        """
        Initialize process collector.

        Args:
            gpu_collector: Optional GPU collector instance for GPU memory enrichment.
        """
        self.gpu_collector = gpu_collector

    async def collect(
        self, cached_gpu_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Collect process metrics.

        Args:
            cached_gpu_data: Pre-collected GPU data from the main loop.
                             Avoids a redundant nvidia-smi call.

        Returns:
            Dict with list of top processes.
        """
        try:
            # Run blocking process enumeration in a thread pool
            loop = asyncio.get_running_loop()
            processes = await loop.run_in_executor(
                None, self._get_top_processes, 12
            )

            # Use cached GPU data if available, else fetch fresh
            gpu_processes = {}
            if cached_gpu_data is not None:
                gpu_processes = cached_gpu_data.get("gpu_processes", {})
            elif self.gpu_collector:
                gpu_data = await self.gpu_collector.collect()
                gpu_processes = gpu_data.get("gpu_processes", {})

            if gpu_processes:
                processes = self._enrich_with_gpu_memory(processes, gpu_processes)

            return {"processes": processes}
        except Exception as e:
            logger.error(f"Failed to collect process metrics: {e}")
            return {"processes": []}

    def _get_top_processes(self, limit: int = 12) -> List[Dict[str, Any]]:
        """
        Get top processes by memory usage.
        Non-blocking: uses cpu_percent(interval=None) which returns
        the delta since the last call (instant, no sleep).
        """
        processes = []

        try:
            for proc in psutil.process_iter(attrs=["pid", "name", "memory_info"]):
                try:
                    pinfo = proc.as_dict(attrs=["pid", "name"])
                    # interval=None is instant — returns % since last call.
                    # First call per process returns 0.0 which is fine;
                    # subsequent cycles will have meaningful values.
                    cpu_percent = proc.cpu_percent(interval=None)
                    mem_info = proc.memory_info()
                    mem_gb = mem_info.rss / (1024**3)

                    processes.append(
                        {
                            "name": pinfo["name"],
                            "pid": pinfo["pid"],
                            "cpu": round(cpu_percent, 2),
                            "ram": round(mem_gb, 2),
                            "gpu_mem": None,
                            "gpu": None,
                        }
                    )
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue

            # Sort by RAM and take top N
            processes.sort(key=lambda x: x["ram"], reverse=True)
            return processes[:limit]

        except Exception as e:
            logger.error(f"Error enumerating processes: {e}")
            return []

    def _enrich_with_gpu_memory(
        self,
        processes: List[Dict[str, Any]],
        gpu_processes: Dict[int, List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """Enrich process list with GPU memory info."""
        # Build PID -> GPU mapping
        pid_gpu_map = {}
        for gpu_idx, procs in gpu_processes.items():
            for proc in procs:
                pid = proc["pid"]
                mem_mb = proc["mem_mb"]
                if pid not in pid_gpu_map:
                    pid_gpu_map[pid] = []
                pid_gpu_map[pid].append((gpu_idx, mem_mb))

        # Enrich processes
        for proc in processes:
            pid = proc["pid"]
            if pid in pid_gpu_map:
                # Take first GPU assignment (usually only one)
                gpu_idx, mem_mb = pid_gpu_map[pid][0]
                proc["gpu_mem"] = round(mem_mb / 1024, 2)  # Convert MB to GB
                proc["gpu"] = str(gpu_idx)

        return processes
