"""
System metrics collector for Dohtar Monitor Agent.
Collects CPU, RAM, disk, and temperature metrics using psutil.
"""

import asyncio
import logging
from typing import Any, Dict, Optional

import psutil

logger = logging.getLogger(__name__)


class SystemCollector:
    """Collects system-level metrics: CPU, RAM, disk, temperature."""

    def __init__(self):
        """Initialize system collector."""
        # Prime the cpu_percent counter so future calls with interval=None
        # return meaningful values instantly (non-blocking).
        psutil.cpu_percent(interval=None)

    async def collect(self) -> Dict[str, Any]:
        """
        Collect system metrics (non-blocking).

        Returns:
            Dict with cpu_load, cpu_temp, cpu_cores, ram_used, ram_total, disk_used, disk_total.
        """
        try:
            # Non-blocking CPU percent: returns delta since last call.
            # We primed in __init__, so this is instant.
            cpu_percent = psutil.cpu_percent(interval=None)
            cpu_count = psutil.cpu_count(logical=False) or psutil.cpu_count()

            # Temperature can be slow on some systems — run in executor
            loop = asyncio.get_running_loop()
            cpu_temp = await loop.run_in_executor(None, self._get_cpu_temp)

            # RAM metrics
            ram = psutil.virtual_memory()
            ram_used_gb = ram.used / (1024**3)
            ram_total_gb = ram.total / (1024**3)

            # Disk metrics (root partition)
            disk = psutil.disk_usage("/")
            disk_used_gb = disk.used / (1024**3)
            disk_total_gb = disk.total / (1024**3)

            # System uptime
            boot_time = psutil.boot_time()
            import time
            uptime_seconds = int(time.time() - boot_time)

            return {
                "cpu_load": round(cpu_percent, 2),
                "cpu_temp": cpu_temp,
                "cpu_cores": cpu_count,
                "ram_used": round(ram_used_gb, 2),
                "ram_total": round(ram_total_gb, 2),
                "disk_used": round(disk_used_gb, 2),
                "disk_total": round(disk_total_gb, 2),
                "uptime": uptime_seconds,
                "boot_time": int(boot_time * 1000),  # ms epoch for frontend
            }
        except Exception as e:
            logger.error(f"Failed to collect system metrics: {e}")
            return {}

    def _get_cpu_temp(self) -> Optional[float]:
        """Get CPU temperature. Returns None if not available."""
        try:
            temps = psutil.sensors_temperatures()
            if not temps:
                return None

            # Try to find a sensible temperature reading
            # Prefer 'coretemp' on Linux, 'hwmon' on some systems
            for sensor_name, entries in temps.items():
                if entries:
                    # Return average of all cores
                    avg_temp = sum(e.current for e in entries) / len(entries)
                    return round(avg_temp, 1)

            return None
        except Exception as e:
            logger.debug(f"Failed to read CPU temperature: {e}")
            return None
