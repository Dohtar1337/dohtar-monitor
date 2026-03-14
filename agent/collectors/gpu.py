"""
GPU metrics collector for Dohtar Monitor Agent.
Collects NVIDIA GPU stats using nvidia-smi.
"""

import asyncio
import json
import logging
import platform
import shutil
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class GPUCollector:
    """Collects NVIDIA GPU metrics via nvidia-smi."""

    def __init__(self):
        """Initialize GPU collector."""
        self.nvidia_smi_path = self._find_nvidia_smi()

    def _find_nvidia_smi(self) -> Optional[str]:
        """Find nvidia-smi executable path."""
        # First try direct shutil.which
        path = shutil.which("nvidia-smi")
        if path:
            return path

        # Windows-specific paths
        if platform.system() == "Windows":
            common_paths = [
                r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
                r"C:\Program Files (x86)\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
            ]
            for p in common_paths:
                try:
                    # Try to run it
                    subprocess.run(
                        [p, "--version"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=2,
                    )
                    return p
                except Exception:
                    continue

        return None

    async def collect(self) -> Dict[str, Any]:
        """
        Collect GPU metrics.

        Returns:
            Dict with list of GPU info dicts. Empty list if no GPUs found.
        """
        if not self.nvidia_smi_path:
            logger.debug("nvidia-smi not found. GPU metrics unavailable.")
            return {"gpus": []}

        try:
            # Run both nvidia-smi queries in PARALLEL (not sequential)
            gpus_data, gpu_processes = await asyncio.gather(
                self._query_nvidia_smi(),
                self._get_gpu_processes(),
            )
            return {"gpus": gpus_data, "gpu_processes": gpu_processes or {}}
        except Exception as e:
            logger.error(f"Failed to collect GPU metrics: {e}")
            return {"gpus": [], "gpu_processes": {}}

    async def _query_nvidia_smi(self) -> List[Dict[str, Any]]:
        """Query nvidia-smi for GPU metrics."""
        query_fields = (
            "index,name,memory.used,memory.total,"
            "temperature.gpu,utilization.gpu,power.draw,fan.speed"
        )

        try:
            # Run nvidia-smi with CSV output
            result = await asyncio.wait_for(
                self._run_command(
                    [
                        self.nvidia_smi_path,
                        f"--query-gpu={query_fields}",
                        "--format=csv,noheader,nounits",
                    ]
                ),
                timeout=2.0,
            )

            if not result:
                return []

            gpus = []
            for line in result.strip().split("\n"):
                if not line.strip():
                    continue

                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 8:
                    continue

                try:
                    gpu = {
                        "idx": int(parts[0]),
                        "name": parts[1],
                        "vram_used": float(parts[2]),
                        "vram_total": float(parts[3]),
                        "temp": float(parts[4]) if parts[4] else None,
                        "load": float(parts[5]) if parts[5] else None,
                        "power": float(parts[6]) if parts[6] else None,
                        "fan": float(parts[7]) if parts[7] else None,
                        "models": [],
                    }
                    gpus.append(gpu)
                except (ValueError, IndexError) as e:
                    logger.debug(f"Error parsing GPU line: {line}, error: {e}")
                    continue

            return gpus
        except asyncio.TimeoutError:
            logger.warning("nvidia-smi query timed out")
            return []
        except Exception as e:
            logger.error(f"nvidia-smi query failed: {e}")
            return []

    async def _get_gpu_processes(self) -> Dict[int, List[Dict[str, Any]]]:
        """Get list of processes using GPU memory, indexed by GPU index."""
        if not self.nvidia_smi_path:
            return {}

        try:
            result = await asyncio.wait_for(
                self._run_command(
                    [
                        self.nvidia_smi_path,
                        "--query-compute-apps=index,process_name,pid,used_memory",
                        "--format=csv,noheader,nounits",
                    ]
                ),
                timeout=2.0,
            )

            gpu_procs = {}
            if not result:
                return gpu_procs

            for line in result.strip().split("\n"):
                if not line.strip():
                    continue

                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 4:
                    continue

                try:
                    gpu_idx = int(parts[0])
                    proc_name = parts[1]
                    pid = int(parts[2])
                    mem_mb = float(parts[3])

                    if gpu_idx not in gpu_procs:
                        gpu_procs[gpu_idx] = []

                    gpu_procs[gpu_idx].append(
                        {
                            "name": proc_name,
                            "pid": pid,
                            "mem_mb": mem_mb,
                        }
                    )
                except (ValueError, IndexError) as e:
                    logger.debug(f"Error parsing GPU process line: {line}, error: {e}")
                    continue

            return gpu_procs
        except asyncio.TimeoutError:
            logger.warning("GPU process query timed out")
            return {}
        except Exception as e:
            logger.debug(f"Failed to get GPU processes: {e}")
            return {}

    async def _run_command(self, cmd: List[str]) -> str:
        """Run command asynchronously."""
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(
                f"Command failed with code {process.returncode}: {stderr.decode()}"
            )

        return stdout.decode().strip()
