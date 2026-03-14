#!/usr/bin/env python3
"""
Dohtar Monitor — macOS Service Manager
Manages launchd service for the OCM agent on macOS systems.
"""

import os
import sys
import subprocess
import plistlib
from pathlib import Path
from typing import Dict, Optional


LAUNCHD_IDENTIFIER = "com.dohtar.monitor.agent"


def _run_command(cmd: list, check: bool = True) -> str:
    """Run a shell command and return output."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=check
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{e.stderr}")


def _get_launchd_plist_path() -> Path:
    """Get the path to the launchd plist file."""
    home = Path.home()
    return home / "Library" / "LaunchAgents" / f"{LAUNCHD_IDENTIFIER}.plist"


def _create_plist(install_path: str, config: Dict) -> Dict:
    """Create a launchd plist dictionary."""
    python_exe = subprocess.check_output(["which", "python3"]).decode().strip()

    log_dir = Path(install_path) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"

    plist = {
        "Label": LAUNCHD_IDENTIFIER,
        "ProgramArguments": [
            python_exe,
            f"{install_path}/agent.py",
            "--config",
            f"{install_path}/config.json"
        ],
        "WorkingDirectory": install_path,
        "RunAtLoad": True,
        "KeepAlive": {
            "SuccessfulExit": False
        },
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
        "SoftResourceLimits": {
            "NumberOfOpenFiles": 1024
        }
    }

    return plist


def install_service(install_path: str, config: Dict) -> None:
    """Install OCM agent as a launchd service."""
    plist_path = _get_launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    plist = _create_plist(install_path, config)

    with open(plist_path, 'wb') as f:
        plistlib.dump(plist, f)

    plist_path.chmod(0o644)
    print(f"✓ Wrote launchd plist: {plist_path}")

    try:
        _run_command(["launchctl", "load", str(plist_path)])
        print("✓ Loaded OCM agent service")
    except RuntimeError:
        print(f"Warning: Failed to load service. You may need to run:")
        print(f"  launchctl load {plist_path}")


def uninstall_service() -> None:
    """Uninstall OCM agent launchd service."""
    plist_path = _get_launchd_plist_path()

    if plist_path.exists():
        try:
            _run_command(["launchctl", "unload", str(plist_path)], check=False)
            print("✓ Unloaded OCM agent service")
        except Exception:
            print(f"Warning: Failed to unload service. You may need to run:")
            print(f"  launchctl unload {plist_path}")

        plist_path.unlink()
        print(f"✓ Removed launchd plist: {plist_path}")
    else:
        print("Service not installed")


def restart_service() -> None:
    """Restart the OCM agent service."""
    plist_path = _get_launchd_plist_path()

    if not plist_path.exists():
        print("Error: Service not installed")
        return

    try:
        _run_command(["launchctl", "unload", str(plist_path)], check=False)
    except Exception:
        pass

    try:
        _run_command(["launchctl", "load", str(plist_path)])
        print("✓ Restarted OCM agent service")
    except RuntimeError:
        print(f"Warning: Failed to load service. You may need to run:")
        print(f"  launchctl load {plist_path}")


def stop_service() -> None:
    """Stop the OCM agent service."""
    try:
        _run_command(["launchctl", "stop", LAUNCHD_IDENTIFIER], check=False)
        print("✓ Stopped OCM agent service")
    except Exception:
        print(f"Warning: Failed to stop service. You may need to run:")
        print(f"  launchctl stop {LAUNCHD_IDENTIFIER}")


def start_service() -> None:
    """Start the OCM agent service."""
    try:
        _run_command(["launchctl", "start", LAUNCHD_IDENTIFIER], check=False)
        print("✓ Started OCM agent service")
    except Exception:
        print(f"Warning: Failed to start service. You may need to run:")
        print(f"  launchctl start {LAUNCHD_IDENTIFIER}")


def get_service_status() -> Optional[str]:
    """Get the status of the OCM agent service."""
    try:
        return _run_command(["launchctl", "list"], check=False)
    except Exception:
        return None


def is_service_running() -> bool:
    """Check if the OCM agent service is running."""
    try:
        output = _run_command(["launchctl", "list"], check=False)
        return LAUNCHD_IDENTIFIER in output
    except Exception:
        return False


if __name__ == "__main__":
    print("This module is meant to be imported by install.py")
    sys.exit(1)
