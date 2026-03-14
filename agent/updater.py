"""
Agent self-updater for Dohtar Monitor.
Downloads new agent files from the backend and restarts the service.
"""

import asyncio
import io
import logging
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class AgentUpdater:
    """Handles downloading new agent code and restarting the service."""

    def __init__(self, backend_url: str, install_dir: Optional[str] = None):
        """
        Args:
            backend_url: Backend base URL (e.g., http://192.168.1.100:9090)
            install_dir: Directory where agent is installed. Auto-detected if None.
        """
        self.backend_url = backend_url.rstrip("/")
        self.install_dir = install_dir or self._detect_install_dir()
        self._updating = False

    def _detect_linux_service(self) -> str:
        """Detect the systemd service name for this agent."""
        # Check common service names
        for name in ["ocm-agent", "dohtar-agent", "monitor-agent"]:
            try:
                result = subprocess.run(
                    ["systemctl", "is-enabled", name],
                    capture_output=True, text=True, timeout=3
                )
                if result.returncode == 0:
                    return name
            except Exception:
                continue
        # Fallback
        return "ocm-agent"

    def _detect_windows_task(self) -> Optional[str]:
        """Detect the Windows scheduled task name for this agent."""
        candidates = [
            "\\Dohtar\\MonitorAgent",
            "\\OCM\\MonitorAgent",
            "\\MonitorAgent",
            "DohtarMonitorAgent",
            "OCMAgent",
        ]
        for name in candidates:
            try:
                result = subprocess.run(
                    ["schtasks", "/Query", "/TN", name],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    logger.info(f"Found Windows scheduled task: {name}")
                    return name
            except Exception:
                continue

        # Try to find any task with "monitor" or "dohtar" or "ocm" in the name
        try:
            result = subprocess.run(
                ["schtasks", "/Query", "/FO", "CSV"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                for line in result.stdout.split('\n'):
                    lower = line.lower()
                    if any(kw in lower for kw in ['monitor', 'dohtar', 'ocm-agent', 'ocm agent']):
                        # Extract task name (first CSV field, quoted)
                        parts = line.strip().split(',')
                        if parts:
                            task_name = parts[0].strip('"')
                            logger.info(f"Found Windows scheduled task by search: {task_name}")
                            return task_name
        except Exception:
            pass

        logger.warning("No Windows scheduled task found for agent")
        return None

    def _detect_install_dir(self) -> str:
        """Detect the directory where this agent is running from."""
        # The agent.py file location is the install dir
        return str(Path(__file__).parent.resolve())

    async def check_for_update(self, current_version: str) -> Optional[dict]:
        """
        Check if an update is available.

        Args:
            current_version: Current agent version string.

        Returns:
            Dict with update info if available, None otherwise.
        """
        try:
            timeout = aiohttp.ClientTimeout(total=5.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{self.backend_url}/api/agent-version") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        server_version = data.get("version", "0.0.0")
                        if server_version != current_version:
                            return {
                                "current": current_version,
                                "available": server_version,
                                "url": f"{self.backend_url}/api/download-agent",
                            }
            return None
        except Exception as e:
            logger.debug(f"Update check failed: {e}")
            return None

    async def perform_update(self) -> bool:
        """
        Download new agent files and replace current installation.

        Returns:
            True if update was successful and restart is needed.
        """
        if self._updating:
            logger.warning("Update already in progress")
            return False

        self._updating = True
        try:
            logger.info(f"Starting agent update from {self.backend_url}")

            # Download agent zip
            zip_data = await self._download_agent_zip()
            if not zip_data:
                logger.error("Failed to download agent zip")
                return False

            # Extract to temporary directory first
            temp_dir = Path(self.install_dir) / ".update_tmp"
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
            temp_dir.mkdir(parents=True)

            try:
                with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
                    names = zf.namelist()
                    logger.info(f"Zip contains {len(names)} files: {names[:10]}{'...' if len(names) > 10 else ''}")
                    zf.extractall(temp_dir)

                # The zip contains files under "dohtar-agent/" prefix
                extracted_dir = temp_dir / "dohtar-agent"
                if not extracted_dir.exists():
                    # Try without prefix
                    logger.info(f"No 'dohtar-agent/' prefix found, using temp_dir directly")
                    extracted_dir = temp_dir
                else:
                    logger.info(f"Found 'dohtar-agent/' prefix in zip")

                # Preserve config.json (don't overwrite with default)
                config_path = Path(self.install_dir) / "config.json"
                config_backup = None
                if config_path.exists():
                    config_backup = config_path.read_text()

                # Copy new files over existing ones (overlay only — never deletes local files)
                # These files/dirs are preserved and never overwritten:
                PRESERVE = {"config.json", "__pycache__", ".update_tmp", "InstalledFolder", "InstallLocation"}

                for item in extracted_dir.rglob("*"):
                    if item.is_file():
                        rel_path = item.relative_to(extracted_dir)

                        # Skip preserved files and anything inside preserved dirs
                        if rel_path.name in PRESERVE:
                            continue
                        if any(part in PRESERVE for part in rel_path.parts):
                            continue

                        dest = Path(self.install_dir) / rel_path
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, dest)
                        logger.debug(f"Updated: {rel_path}")

                # Restore config
                if config_backup:
                    config_path.write_text(config_backup)

                # Log the new version after update
                new_version_file = Path(self.install_dir) / "version.py"
                if new_version_file.exists():
                    content = new_version_file.read_text()
                    logger.info(f"Agent files updated successfully. New version.py: {content.strip()}")
                else:
                    logger.info("Agent files updated successfully")
                return True

            finally:
                # Clean up temp dir
                if temp_dir.exists():
                    shutil.rmtree(temp_dir, ignore_errors=True)

        except Exception as e:
            logger.error(f"Update failed: {e}")
            return False
        finally:
            self._updating = False

    async def _download_agent_zip(self) -> Optional[bytes]:
        """Download agent zip from backend."""
        try:
            timeout = aiohttp.ClientTimeout(total=30.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                url = f"{self.backend_url}/api/download-agent"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        logger.info(f"Downloaded agent zip: {len(data)} bytes")
                        return data
                    else:
                        logger.error(f"Download failed with status {resp.status}")
                        return None
        except Exception as e:
            logger.error(f"Download failed: {e}")
            return None

    def restart_service(self) -> None:
        """Restart the agent service based on the platform."""
        system = platform.system()
        logger.info(f"Attempting restart on platform: {system}")

        try:
            if system == "Linux":
                service_name = self._detect_linux_service()
                logger.info(f"Restarting agent via systemd (service: {service_name})...")
                result = subprocess.run(
                    ["sudo", "systemctl", "restart", service_name],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode != 0:
                    logger.error(f"systemctl restart failed (rc={result.returncode}): {result.stderr}")
                else:
                    logger.info(f"systemctl restart succeeded for {service_name}")

            elif system == "Windows":
                # Try to find the scheduled task name
                task_name = self._detect_windows_task()
                if task_name:
                    logger.info(f"Restarting agent via schtasks (task: {task_name})...")
                    end_result = subprocess.run(
                        ["schtasks", "/End", "/TN", task_name],
                        capture_output=True, text=True, timeout=10
                    )
                    logger.info(f"schtasks /End result: rc={end_result.returncode}, {end_result.stdout.strip()} {end_result.stderr.strip()}")

                    import time
                    time.sleep(2)

                    run_result = subprocess.run(
                        ["schtasks", "/Run", "/TN", task_name],
                        capture_output=True, text=True, timeout=10
                    )
                    logger.info(f"schtasks /Run result: rc={run_result.returncode}, {run_result.stdout.strip()} {run_result.stderr.strip()}")
                else:
                    # No scheduled task found — just re-exec the process
                    logger.info("No scheduled task found, restarting by re-exec...")
                    os.execv(sys.executable, [sys.executable] + sys.argv)

            else:
                logger.info("Restarting by re-exec...")
                os.execv(sys.executable, [sys.executable] + sys.argv)

        except Exception as e:
            logger.error(f"Failed to restart service: {e}", exc_info=True)
