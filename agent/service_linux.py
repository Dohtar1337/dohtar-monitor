#!/usr/bin/env python3
"""
Dohtar Monitor — Linux systemd Service Manager
Manages systemd unit for the OCM agent on Linux systems.
"""

import os
import sys
import subprocess
from pathlib import Path
from typing import Dict, Optional


SYSTEMD_UNIT_TEMPLATE = """[Unit]
Description=Dohtar Monitor Agent
Documentation=https://dohtar.ai
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory={install_path}
ExecStart=/usr/bin/python3 {install_path}/agent.py --config {install_path}/config.json
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ocm-agent
Environment="PATH=/usr/local/bin:/usr/bin:/bin"

[Install]
WantedBy=multi-user.target
"""


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


def _is_root() -> bool:
    """Check if running as root."""
    return os.geteuid() == 0


def install_service(install_path: str, config: Dict) -> None:
    """Install OCM agent as a systemd service."""
    if not _is_root():
        print("Error: Must run as root to install system service")
        sys.exit(1)

    unit_content = SYSTEMD_UNIT_TEMPLATE.format(install_path=install_path)
    unit_path = Path("/etc/systemd/system/ocm-agent.service")

    unit_path.write_text(unit_content)
    print(f"✓ Wrote systemd unit: {unit_path}")

    _run_command(["systemctl", "daemon-reload"])
    print("✓ Reloaded systemd configuration")

    _run_command(["systemctl", "enable", "ocm-agent.service"])
    print("✓ Enabled ocm-agent service")

    _run_command(["systemctl", "start", "ocm-agent.service"])
    print("✓ Started ocm-agent service")


def uninstall_service() -> None:
    """Uninstall OCM agent systemd service."""
    if not _is_root():
        print("Error: Must run as root to uninstall system service")
        sys.exit(1)

    unit_path = Path("/etc/systemd/system/ocm-agent.service")

    if unit_path.exists():
        _run_command(["systemctl", "stop", "ocm-agent.service"], check=False)
        print("✓ Stopped ocm-agent service")

        _run_command(["systemctl", "disable", "ocm-agent.service"], check=False)
        print("✓ Disabled ocm-agent service")

        unit_path.unlink()
        print(f"✓ Removed systemd unit: {unit_path}")

        _run_command(["systemctl", "daemon-reload"])
        print("✓ Reloaded systemd configuration")
    else:
        print("Service not installed")


def restart_service() -> None:
    """Restart the OCM agent service."""
    if not _is_root():
        print("Error: Must run as root to manage system service")
        sys.exit(1)

    _run_command(["systemctl", "restart", "ocm-agent.service"])
    print("✓ Restarted ocm-agent service")


def stop_service() -> None:
    """Stop the OCM agent service."""
    if not _is_root():
        print("Error: Must run as root to manage system service")
        sys.exit(1)

    _run_command(["systemctl", "stop", "ocm-agent.service"], check=False)
    print("✓ Stopped ocm-agent service")


def start_service() -> None:
    """Start the OCM agent service."""
    if not _is_root():
        print("Error: Must run as root to manage system service")
        sys.exit(1)

    _run_command(["systemctl", "start", "ocm-agent.service"], check=False)
    print("✓ Started ocm-agent service")


def get_service_status() -> Optional[str]:
    """Get the status of the OCM agent service."""
    try:
        return _run_command(["systemctl", "status", "ocm-agent.service"], check=False)
    except Exception:
        return None


def is_service_running() -> bool:
    """Check if the OCM agent service is running."""
    try:
        _run_command(["systemctl", "is-active", "ocm-agent.service"], check=True)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    print("This module is meant to be imported by install.py")
    sys.exit(1)
