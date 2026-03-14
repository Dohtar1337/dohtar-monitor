#!/usr/bin/env python3
"""
Dohtar Monitor — Windows Service Manager
Manages Windows services for the OCM agent using Task Scheduler or NSSM.
"""

import os
import sys
import subprocess
import tempfile
import shutil
from pathlib import Path
from typing import Dict, Optional
from urllib.request import urlopen


NSSM_URL = "https://nssm.cc/download/nssm-2.24-101-g897c7ad.zip"
NSSM_VERSION = "2.24"
SERVICE_NAME = "ocm-agent"
TASK_NAME = "Dohtar\\MonitorAgent"


def _run_command(cmd: list, check: bool = True, shell: bool = False) -> str:
    """Run a shell command and return output."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=check,
            shell=shell
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Command failed: {' '.join(str(c) for c in cmd)}\n{e.stderr}")


def _is_admin() -> bool:
    """Check if running as administrator."""
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() == 1
    except Exception:
        return False


def _get_python_exe() -> str:
    """Get the path to the current Python executable."""
    return sys.executable


def _get_nssm_exe() -> Optional[Path]:
    """Find NSSM on PATH or in common locations."""
    # Check PATH first
    nssm_on_path = shutil.which("nssm")
    if nssm_on_path:
        return Path(nssm_on_path)

    common_paths = [
        Path("C:\\Program Files\\nssm\\nssm.exe"),
        Path("C:\\Program Files (x86)\\nssm\\nssm.exe"),
        Path("C:\\nssm\\nssm.exe"),
    ]

    for path in common_paths:
        if path.exists():
            return path

    # Try downloading
    try:
        return _download_and_extract_nssm()
    except Exception:
        return None


def _download_and_extract_nssm() -> Optional[Path]:
    """Download NSSM from official source and extract."""
    import zipfile
    try:
        temp_dir = Path(tempfile.gettempdir())
        nssm_zip = temp_dir / f"nssm-{NSSM_VERSION}.zip"

        if not nssm_zip.exists():
            print("  Downloading NSSM...")
            response = urlopen(NSSM_URL, timeout=30)
            nssm_zip.write_bytes(response.read())

        extract_dir = temp_dir / "nssm-extracted"
        if extract_dir.exists():
            shutil.rmtree(extract_dir)

        with zipfile.ZipFile(nssm_zip, 'r') as zf:
            zf.extractall(extract_dir)

        # Find the 64-bit exe
        for root, dirs, files in os.walk(extract_dir):
            if "nssm.exe" in files and "win64" in root:
                return Path(root) / "nssm.exe"
        # Fallback to any nssm.exe
        for root, dirs, files in os.walk(extract_dir):
            if "nssm.exe" in files:
                return Path(root) / "nssm.exe"

        return None
    except Exception as e:
        print(f"  Warning: Failed to download NSSM: {e}")
        return None


def _install_with_nssm(install_path: str, config: Dict) -> bool:
    """Install service using NSSM."""
    try:
        nssm_exe = _get_nssm_exe()
        if not nssm_exe:
            return False

        python_exe = _get_python_exe()
        agent_script = str(Path(install_path) / "agent.py")
        config_file = str(Path(install_path) / "config.json")

        # Remove existing service if any
        _run_command([str(nssm_exe), "stop", SERVICE_NAME], check=False)
        _run_command([str(nssm_exe), "remove", SERVICE_NAME, "confirm"], check=False)

        # Install new service
        _run_command([
            str(nssm_exe), "install", SERVICE_NAME,
            python_exe, agent_script,
            "--config", config_file
        ])

        _run_command([str(nssm_exe), "set", SERVICE_NAME, "AppDirectory", install_path])
        _run_command([str(nssm_exe), "set", SERVICE_NAME, "AppNoConsole", "1"])
        _run_command([str(nssm_exe), "set", SERVICE_NAME, "DisplayName", "Dohtar Monitor Agent"])
        _run_command([str(nssm_exe), "set", SERVICE_NAME, "Description", "Pushes system metrics to Dohtar Monitor backend"])
        _run_command([str(nssm_exe), "set", SERVICE_NAME, "Start", "SERVICE_AUTO_START"])

        # Log rotation
        log_path = str(Path(install_path) / "ocm-agent.log")
        _run_command([str(nssm_exe), "set", SERVICE_NAME, "AppStdout", log_path])
        _run_command([str(nssm_exe), "set", SERVICE_NAME, "AppStderr", log_path])
        _run_command([str(nssm_exe), "set", SERVICE_NAME, "AppRotateFiles", "1"])
        _run_command([str(nssm_exe), "set", SERVICE_NAME, "AppRotateOnline", "1"])
        _run_command([str(nssm_exe), "set", SERVICE_NAME, "AppRotateBytes", "10485760"])

        _run_command([str(nssm_exe), "start", SERVICE_NAME])

        return True
    except Exception as e:
        print(f"  Warning: NSSM installation failed: {e}")
        return False


def _install_with_task_scheduler(install_path: str, config: Dict) -> bool:
    """Install service using Windows Task Scheduler (compatible XML)."""
    try:
        python_exe = _get_python_exe()
        config_file = str(Path(install_path) / "config.json")

        # Create a simple bat wrapper for reliability
        bat_path = Path(install_path) / "ocm-agent.bat"
        bat_content = f"""@echo off
:start
cd /d "{install_path}"
"{python_exe}" agent.py --config "{config_file}"
echo Agent exited with code %errorlevel%, restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto start
"""
        bat_path.write_text(bat_content)

        # Remove existing task if any
        _run_command(
            ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
            shell=True, check=False
        )

        # Use schtasks command directly instead of XML (more compatible)
        _run_command([
            "schtasks", "/create",
            "/tn", TASK_NAME,
            "/tr", f'"{bat_path}"',
            "/sc", "ONSTART",
            "/ru", "SYSTEM",
            "/rl", "HIGHEST",
            "/f",
        ], shell=True)

        # Run it now
        _run_command(
            ["schtasks", "/run", "/tn", TASK_NAME],
            shell=True, check=False
        )

        return True
    except Exception as e:
        print(f"  Warning: Task Scheduler installation failed: {e}")
        return False


def install_service(install_path: str, config: Dict) -> None:
    """Install OCM agent as a Windows service."""
    if not _is_admin():
        raise RuntimeError(
            "Administrator privileges required.\n"
            "  Right-click PowerShell -> 'Run as administrator' and try again."
        )

    # Try NSSM first (proper Windows service)
    success = _install_with_nssm(install_path, config)

    if not success:
        print("  Falling back to Task Scheduler...")
        success = _install_with_task_scheduler(install_path, config)

    if success:
        print(f"  Service installed successfully")
    else:
        raise RuntimeError("Failed to install service using both NSSM and Task Scheduler")


def uninstall_service() -> None:
    """Uninstall OCM agent Windows service."""
    # Try NSSM first
    nssm_exe = _get_nssm_exe()
    if nssm_exe:
        try:
            _run_command([str(nssm_exe), "stop", SERVICE_NAME], check=False)
            _run_command([str(nssm_exe), "remove", SERVICE_NAME, "confirm"], check=False)
            print(f"  Service {SERVICE_NAME} removed via NSSM")
            return
        except Exception:
            pass

    # Try Task Scheduler
    try:
        _run_command(
            ["schtasks", "/end", "/tn", TASK_NAME],
            shell=True, check=False
        )
        _run_command(
            ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
            shell=True, check=False
        )
        print(f"  Scheduled task {TASK_NAME} removed")
    except Exception:
        pass


def restart_service() -> None:
    """Restart the OCM agent service."""
    # Try NSSM
    nssm_exe = _get_nssm_exe()
    if nssm_exe:
        try:
            _run_command([str(nssm_exe), "restart", SERVICE_NAME], check=False)
            print(f"  Service {SERVICE_NAME} restarted")
            return
        except Exception:
            pass

    # Try Task Scheduler (end then run)
    try:
        _run_command(["schtasks", "/end", "/tn", TASK_NAME], shell=True, check=False)
        _run_command(["schtasks", "/run", "/tn", TASK_NAME], shell=True, check=False)
        print(f"  Scheduled task {TASK_NAME} restarted")
    except Exception:
        pass


def stop_service() -> None:
    """Stop the OCM agent service."""
    nssm_exe = _get_nssm_exe()
    if nssm_exe:
        try:
            _run_command([str(nssm_exe), "stop", SERVICE_NAME], check=False)
            print(f"  Service {SERVICE_NAME} stopped")
            return
        except Exception:
            pass

    try:
        _run_command(["schtasks", "/end", "/tn", TASK_NAME], shell=True, check=False)
        print(f"  Scheduled task {TASK_NAME} stopped")
    except Exception:
        pass


def start_service() -> None:
    """Start the OCM agent service."""
    nssm_exe = _get_nssm_exe()
    if nssm_exe:
        try:
            _run_command([str(nssm_exe), "start", SERVICE_NAME], check=False)
            print(f"  Service {SERVICE_NAME} started")
            return
        except Exception:
            pass

    try:
        _run_command(["schtasks", "/run", "/tn", TASK_NAME], shell=True, check=False)
        print(f"  Scheduled task {TASK_NAME} started")
    except Exception:
        pass


if __name__ == "__main__":
    print("This module is meant to be imported by install.py")
    sys.exit(1)
