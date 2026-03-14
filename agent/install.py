#!/usr/bin/env python3
"""
Dohtar Monitor — Agent Installer
Interactive CLI for installing, configuring, and uninstalling the OCM agent.
"""

import os
import sys
import json
import socket
import secrets
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from urllib.request import urlopen
from urllib.error import URLError


class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'


# ── Well-known service ports to scan ─────────────────────────────────

KNOWN_PORTS = [8080, 8081, 8082, 8083, 8084, 8085, 11434, 5000, 5001, 3000]


class Installer:
    def __init__(self):
        self.os_type = platform.system()
        self.config = {}
        self.install_path = None
        self.script_dir = Path(__file__).parent.resolve()

    # ── Printing helpers ─────────────────────────────────────────────

    def print_header(self):
        print(f"\n{Colors.BOLD}{'=' * 44}{Colors.ENDC}")
        print(f"{Colors.BOLD}   Dohtar Monitor - Agent Installer{Colors.ENDC}")
        print(f"{Colors.BOLD}{'=' * 44}{Colors.ENDC}\n")

    def print_step(self, step_num: int, title: str):
        print(f"\n{Colors.CYAN}-- Step {step_num}: {title} --{Colors.ENDC}")

    def print_success(self, msg: str):
        print(f"{Colors.GREEN}  + {msg}{Colors.ENDC}")

    def print_warn(self, msg: str):
        print(f"{Colors.YELLOW}  ! {msg}{Colors.ENDC}")

    def print_error(self, msg: str):
        print(f"{Colors.RED}  x {msg}{Colors.ENDC}")

    def print_info(self, msg: str):
        print(f"{Colors.BLUE}  i {msg}{Colors.ENDC}")

    def prompt(self, msg: str, default: Optional[str] = None) -> str:
        if default:
            prompt_str = f"  {msg} [{default}]: "
        else:
            prompt_str = f"  {msg}: "
        response = input(prompt_str).strip()
        return response if response else (default or "")

    def prompt_yes_no(self, msg: str, default: bool = True) -> bool:
        default_str = "Y/n" if default else "y/N"
        response = input(f"  {msg} [{default_str}]: ").strip().lower()
        if not response:
            return default
        return response in ('y', 'yes')

    # ── Utility ──────────────────────────────────────────────────────

    def generate_agent_id(self) -> str:
        return "ocm-" + secrets.token_hex(6)

    def is_admin(self) -> bool:
        """Check if running with elevated privileges."""
        if self.os_type == "Windows":
            try:
                import ctypes
                return ctypes.windll.shell32.IsUserAnAdmin() == 1
            except Exception:
                return False
        else:
            return os.getuid() == 0

    def _get_all_local_ips(self) -> List[str]:
        """Get all IPv4 addresses from all network interfaces."""
        ips = []
        try:
            # Works cross-platform: get all addresses for this hostname
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                ip = info[4][0]
                if ip not in ips and ip != '127.0.0.1':
                    ips.append(ip)
        except Exception:
            pass

        # Also try netifaces-style via connecting to external addresses
        # This catches IPs that getaddrinfo might miss
        for test_target in ['192.168.1.1', '10.0.0.1', '8.8.8.8']:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.settimeout(0.1)
                s.connect((test_target, 1))
                ip = s.getsockname()[0]
                if ip not in ips and ip != '127.0.0.1':
                    ips.append(ip)
                s.close()
            except Exception:
                pass

        return ips

    # ── Existing install detection ───────────────────────────────────

    def find_existing_install(self) -> Optional[Path]:
        """Look for an existing OCM agent installation."""
        # Check default location relative to this script
        default = self.script_dir / "installedLocation"
        if (default / "config.json").exists():
            return default

        # Check common system paths
        common_paths = []
        if self.os_type == "Windows":
            common_paths.append(Path(r"C:\ocm-agent"))
        elif self.os_type == "Darwin":
            common_paths.append(Path("/usr/local/ocm-agent"))
        else:
            common_paths.append(Path("/opt/ocm-agent"))

        common_paths.append(Path.home() / ".ocm-agent")

        for p in common_paths:
            if (p / "config.json").exists():
                return p

        return None

    def handle_existing_install(self, existing_path: Path) -> str:
        """
        Handle existing installation. Returns action:
        'reconfigure', 'clean', 'uninstall', or 'cancel'
        """
        try:
            with open(existing_path / "config.json", 'r') as f:
                existing_config = json.load(f)
        except Exception:
            existing_config = {}

        agent_id = existing_config.get('agent_id', '?')
        machine_name = existing_config.get('machine_name', '?')
        backend_url = existing_config.get('backend_url', '?')

        print(f"\n{Colors.YELLOW}  Existing OCM agent found at {existing_path}{Colors.ENDC}")
        print(f"    Agent ID:  {agent_id}")
        print(f"    Machine:   {machine_name}")
        print(f"    Backend:   {backend_url}")
        print()
        print(f"  [1] {Colors.BOLD}Reconfigure{Colors.ENDC} - keep agent ID, update settings")
        print(f"  [2] {Colors.BOLD}Clean install{Colors.ENDC} - wipe and start fresh")
        print(f"  [3] {Colors.BOLD}Uninstall{Colors.ENDC} - remove agent and service")
        print(f"  [4] {Colors.BOLD}Cancel{Colors.ENDC}")
        print()

        while True:
            choice = input("  Choose [1-4]: ").strip()
            if choice == '1':
                self.config = existing_config
                self.install_path = str(existing_path)
                return 'reconfigure'
            elif choice == '2':
                return 'clean'
            elif choice == '3':
                self.config = existing_config
                self.install_path = str(existing_path)
                return 'uninstall'
            elif choice == '4':
                return 'cancel'
            else:
                print("  Please enter 1, 2, 3, or 4.")

    # ── Service scanning ─────────────────────────────────────────────

    def scan_port(self, host: str, port: int, timeout: float = 0.5) -> bool:
        """Check if a TCP port is open."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((host, port))
            sock.close()
            return result == 0
        except Exception:
            return False

    def identify_service(self, host: str, port: int) -> Optional[Dict]:
        """Probe a port to identify what service is running."""
        url_base = f"http://{host}:{port}"

        # Try llama.cpp /health
        try:
            resp = urlopen(f"{url_base}/health", timeout=2)
            data = json.loads(resp.read().decode())
            if data.get("status") in ("ok", "no slot available", "loading model"):
                model_name = "unknown"
                # Try to get model name from /v1/models
                try:
                    resp2 = urlopen(f"{url_base}/v1/models", timeout=2)
                    models_data = json.loads(resp2.read().decode())
                    if models_data.get("data"):
                        model_name = models_data["data"][0].get("id", "unknown")
                except Exception:
                    try:
                        resp3 = urlopen(f"{url_base}/props", timeout=2)
                        props = json.loads(resp3.read().decode())
                        model_name = props.get("default_generation_settings", {}).get("model", "unknown")
                    except Exception:
                        pass
                return {
                    "type": "llm",
                    "probe": "llamacpp",
                    "name": f"llama.cpp ({model_name})",
                    "model": model_name,
                }
        except Exception:
            pass

        # Try Ollama /api/tags
        try:
            resp = urlopen(f"{url_base}/api/tags", timeout=2)
            data = json.loads(resp.read().decode())
            if "models" in data:
                model_names = [m.get("name", "?") for m in data["models"][:3]]
                return {
                    "type": "llm",
                    "probe": "ollama",
                    "name": f"Ollama ({', '.join(model_names)})" if model_names else "Ollama",
                    "model": model_names[0] if model_names else "unknown",
                }
        except Exception:
            pass

        # Try Whisper-compatible endpoints
        for whisper_path in ["/", "/health", "/v1/audio"]:
            try:
                resp = urlopen(f"{url_base}{whisper_path}", timeout=2)
                content = resp.read().decode().lower()
                if any(kw in content for kw in ["whisper", "transcri", "audio", "stt"]):
                    return {
                        "type": "stt",
                        "probe": "whisper",
                        "name": "Whisper STT",
                    }
            except Exception:
                pass

        # Port is open but unidentified
        try:
            resp = urlopen(f"{url_base}/", timeout=2)
            return {
                "type": "unknown",
                "probe": "generic",
                "name": f"HTTP service on :{port}",
            }
        except Exception:
            pass

        return None

    def scan_for_services(self) -> List[Dict]:
        """Scan well-known ports for LLM/STT services."""
        found = []

        print(f"  Scanning localhost for services...", end='', flush=True)

        for port in KNOWN_PORTS:
            if self.scan_port("127.0.0.1", port):
                svc = self.identify_service("127.0.0.1", port)
                if svc:
                    svc["endpoint"] = f"localhost:{port}"
                    svc["port"] = port
                    found.append(svc)

        # Clear the scanning line
        print(f"\r{' ' * 60}\r", end='')

        return found

    # ── Backend discovery ────────────────────────────────────────────

    # IPs from virtual adapters (VirtualBox, VMware, NAT, APIPA)
    VIRTUAL_PREFIXES = ('192.168.56.', '192.168.57.', '172.16.',
                        '10.0.2.', '169.254.')

    def _is_virtual_ip(self, ip: str) -> bool:
        """Check if an IP belongs to a virtual adapter."""
        return any(ip.startswith(p) for p in self.VIRTUAL_PREFIXES)

    def discover_backend(self) -> Optional[Tuple[str, int]]:
        """Try to discover OCM backend via UDP broadcast or network scan."""
        print(f"  {Colors.BLUE}Scanning LAN for OCM backend...{Colors.ENDC}", end='', flush=True)

        # Try UDP broadcast on port 9091
        # Collect ALL responses, prefer non-virtual IPs
        candidates = []
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(2)
            sock.sendto(b'dohtar-monitor', ('<broadcast>', 9091))

            try:
                while True:
                    data, addr = sock.recvfrom(4096)
                    try:
                        resp = json.loads(data.decode())
                        if resp.get("service") == "dohtar-monitor":
                            http_port = resp.get("backend_port", 9090)
                            candidates.append((addr[0], http_port))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
            except socket.timeout:
                pass
            finally:
                sock.close()
        except Exception:
            pass

        # Return first non-virtual candidate, or virtual as fallback
        if candidates:
            real = [c for c in candidates if not self._is_virtual_ip(c[0])]
            if real:
                print(f"\r{' ' * 60}\r", end='')
                return real[0]
            print(f"\r{' ' * 60}\r", end='')
            return candidates[0]

        # Try common subnet IPs on port 9090
        # Scan ALL network interfaces, prioritize real LAN over virtual adapters
        try:
            subnets_to_scan = []
            all_ips = self._get_all_local_ips()

            # Skip virtual adapters (VirtualBox, VMware, NAT, APIPA)
            real_ips = [ip for ip in all_ips if not self._is_virtual_ip(ip)]
            virtual_ips = [ip for ip in all_ips if self._is_virtual_ip(ip)]

            # Real adapters first, virtual as fallback
            for ip in real_ips + virtual_ips:
                subnet = '.'.join(ip.split('.')[:3])
                if subnet not in subnets_to_scan:
                    subnets_to_scan.append(subnet)

            # Fallback if we got nothing
            if not subnets_to_scan:
                hostname = socket.gethostname()
                local_ip = socket.gethostbyname(hostname)
                subnets_to_scan.append('.'.join(local_ip.split('.')[:3]))

            for subnet in subnets_to_scan:
                for i in range(1, 255):
                    test_ip = f"{subnet}.{i}"
                    if self.scan_port(test_ip, 9090, timeout=0.3):
                        try:
                            resp = urlopen(f"http://{test_ip}:9090/api/discover", timeout=2)
                            data = json.loads(resp.read().decode())
                            if data.get("service") == "dohtar-monitor":
                                print(f"\r{' ' * 60}\r", end='')
                                return test_ip, 9090
                        except Exception:
                            pass
        except Exception:
            pass

        print(f"\r{' ' * 60}\r", end='')
        return None

    # ── Installation steps ───────────────────────────────────────────

    def step_backend_connection(self):
        self.print_step(1, "Backend Connection")

        discovered = self.discover_backend()
        if discovered:
            ip, port = discovered
            self.print_success(f"Found backend at {ip}:{port}")
            if self.prompt_yes_no("Use this backend?", True):
                self.config['backend_url'] = f"http://{ip}:{port}"
                return

        manual_entry = self.prompt("Enter backend address (IP:PORT)", "localhost:9090")
        if ':' in manual_entry:
            self.config['backend_url'] = f"http://{manual_entry}"
        else:
            self.config['backend_url'] = f"http://{manual_entry}:9090"

    def step_agent_identity(self):
        self.print_step(2, "Agent Identity")

        # Only generate new ID if we don't already have one (reconfigure keeps old)
        if 'agent_id' not in self.config:
            agent_id = self.generate_agent_id()
            self.config['agent_id'] = agent_id
        else:
            agent_id = self.config['agent_id']

        print(f"  Agent ID: {Colors.BOLD}{agent_id}{Colors.ENDC}")

        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = "Monitor"

        current_name = self.config.get('machine_name', hostname)
        machine_name = self.prompt("Machine name (shown in dashboard)", current_name)
        self.config['machine_name'] = machine_name

    def step_install_location(self):
        self.print_step(3, "Install Location")

        # Default to script_dir/installedLocation
        default_path = str(self.script_dir / "installedLocation")

        print(f"  Where to install the agent?")
        install_path = self.prompt("Install path", default_path)
        self.install_path = install_path
        self.config['install_path'] = install_path

    def step_metrics_collection(self):
        self.print_step(4, "Metrics to Collect")

        existing = self.config.get('collectors', {})

        metrics = [
            ('system',    'System metrics (CPU, RAM)',  existing.get('system', True)),
            ('gpu',       'GPU metrics (nvidia-smi)',   existing.get('gpu', True)),
            ('processes', 'Top processes',              existing.get('processes', True)),
            ('disk',      'Disk usage',                 existing.get('disk', True)),
            ('docker',    'Docker container stats',     existing.get('docker', False)),
        ]

        collectors = {}
        for key, label, default in metrics:
            enabled = self.prompt_yes_no(label, default)
            collectors[key] = enabled

        self.config['collectors'] = collectors

    def step_service_probing(self):
        self.print_step(5, "Service Discovery")

        services = []

        # Auto-scan for services
        found = self.scan_for_services()

        if found:
            print()
            for i, svc in enumerate(found, 1):
                type_label = svc.get('type', '?').upper()
                print(f"  {Colors.GREEN}+ [{i}] {type_label}: {svc['name']}{Colors.ENDC}  at  {svc['endpoint']}")
            print()

            print(f"  Enter numbers to monitor (comma-separated), or ENTER for all:")
            selection = input(f"  > ").strip()

            if not selection:
                selected_indices = list(range(len(found)))
            else:
                selected_indices = []
                for part in selection.split(','):
                    part = part.strip()
                    if part.isdigit():
                        idx = int(part) - 1
                        if 0 <= idx < len(found):
                            selected_indices.append(idx)

            for idx in selected_indices:
                svc = found[idx]
                services.append({
                    'type': svc['type'],
                    'name': svc['name'],
                    'endpoint': svc['endpoint'],
                    'probe': svc['probe'],
                })

            if selected_indices:
                print()
                for idx in selected_indices:
                    self.print_success(f"Monitoring {found[idx]['name']} at {found[idx]['endpoint']}")
        else:
            self.print_info("No LLM/STT services detected on well-known ports.")

        # Offer to add custom endpoints
        print()
        if self.prompt_yes_no("Add additional service endpoints manually?", False):
            print(f"  {Colors.DIM}Enter endpoints one per line. Empty line to finish.{Colors.ENDC}")
            print(f"  {Colors.DIM}Format: TYPE ENDPOINT  (e.g. 'llm localhost:8081' or 'stt 10.0.0.5:8083'){Colors.ENDC}")
            while True:
                entry = input(f"  > ").strip()
                if not entry:
                    break
                parts = entry.split(None, 1)
                if len(parts) == 2:
                    svc_type, endpoint = parts
                    svc_type = svc_type.lower()
                    if svc_type not in ('llm', 'stt', 'custom'):
                        print(f"  {Colors.YELLOW}Unknown type '{svc_type}'. Use: llm, stt, custom{Colors.ENDC}")
                        continue
                    probe = 'llamacpp' if svc_type == 'llm' else ('whisper' if svc_type == 'stt' else 'generic')
                    services.append({
                        'type': svc_type,
                        'name': f"{svc_type.upper()} Service",
                        'endpoint': endpoint,
                        'probe': probe,
                    })
                    self.print_success(f"Added {svc_type} at {endpoint}")
                elif len(parts) == 1:
                    services.append({
                        'type': 'llm',
                        'name': 'LLM Service',
                        'endpoint': parts[0],
                        'probe': 'llamacpp',
                    })
                    self.print_success(f"Added llm at {parts[0]}")

        self.config['services'] = services

    def step_polling_interval(self):
        self.print_step(6, "Polling Interval")

        current = self.config.get('poll_interval', 2)
        interval = self.prompt("Poll interval (seconds)", str(current))
        try:
            self.config['poll_interval'] = int(interval)
        except ValueError:
            self.config['poll_interval'] = 2

    def step_system_service(self):
        self.print_step(7, "System Service")

        print("  This registers ocm-agent to start automatically when")
        print("  your machine boots, and restart if it crashes.")
        if self.os_type == "Linux":
            print(f"  {Colors.DIM}Linux: creates a systemd unit (ocm-agent.service){Colors.ENDC}")
        elif self.os_type == "Windows":
            print(f"  {Colors.DIM}Windows: creates a scheduled task via Task Scheduler{Colors.ENDC}")
        elif self.os_type == "Darwin":
            print(f"  {Colors.DIM}macOS: creates a launchd plist{Colors.ENDC}")

        # Admin privilege check
        if not self.is_admin():
            print()
            self.print_warn("Installing a system service requires Administrator privileges.")
            if self.os_type == "Windows":
                self.print_warn("Re-run this installer as Administrator")
                self.print_warn("(right-click PowerShell -> Run as administrator)")
            else:
                self.print_warn("Re-run with: sudo python3 install.py")
            print()
            self.print_info("You can skip this and run the agent manually instead.")

        install_service = self.prompt_yes_no("Install system service?", self.is_admin())
        self.config['install_service'] = install_service

        if install_service and not self.is_admin():
            self.print_warn("Service installation will likely fail without admin privileges.")
            if not self.prompt_yes_no("Try anyway?", False):
                self.config['install_service'] = False

    # ── File operations ──────────────────────────────────────────────

    def copy_agent_files(self):
        """Copy agent files from source to install path."""
        install_path = Path(self.install_path)
        install_path.mkdir(parents=True, exist_ok=True)

        files_to_copy = [
            'agent.py',
            'discovery.py',
            'config.py',
            'requirements.txt',
        ]

        for file in files_to_copy:
            src = self.script_dir / file
            dst = install_path / file
            if src.exists():
                shutil.copy2(src, dst)

        dirs_to_copy = ['collectors', 'probes']
        for dir_name in dirs_to_copy:
            src_dir = self.script_dir / dir_name
            dst_dir = install_path / dir_name
            if src_dir.exists():
                if dst_dir.exists():
                    shutil.rmtree(dst_dir)
                shutil.copytree(src_dir, dst_dir)

    def write_config(self):
        """Write configuration to config.json."""
        config_path = Path(self.install_path) / 'config.json'
        with open(config_path, 'w') as f:
            json.dump(self.config, f, indent=2)

    def install_service_handler(self):
        """Install system service. Returns True if successful."""
        if not self.config.get('install_service'):
            return False

        try:
            if self.os_type == "Linux":
                from service_linux import install_service as _install
                _install(self.install_path, self.config)
                return True
            elif self.os_type == "Windows":
                from service_windows import install_service as _install
                _install(self.install_path, self.config)
                return True
            elif self.os_type == "Darwin":
                from service_macos import install_service as _install
                _install(self.install_path, self.config)
                return True
        except Exception as e:
            self.print_error(f"Service installation failed: {e}")
            return False

        return False

    # ── Post-install ─────────────────────────────────────────────────

    def print_post_install(self, service_installed: bool):
        """Show post-install summary and instructions."""
        config_path = Path(self.install_path) / "config.json"
        backend_url = self.config.get('backend_url', 'http://localhost:9090')

        print(f"\n{'=' * 44}")
        print(f"{Colors.GREEN}{Colors.BOLD}  Installation complete!{Colors.ENDC}")
        print(f"{'=' * 44}")
        print(f"  Agent ID:  {self.config.get('agent_id')}")
        print(f"  Machine:   {self.config.get('machine_name')}")
        print(f"  Backend:   {backend_url}")
        print(f"  Config:    {config_path}")
        print(f"  Services:  {len(self.config.get('services', []))} monitored")
        print(f"  Polling:   every {self.config.get('poll_interval', 2)}s")

        if service_installed:
            print(f"\n{Colors.GREEN}  Service is installed and running.{Colors.ENDC}")
            if self.os_type == "Windows":
                print(f"\n  {Colors.DIM}Useful commands (run as Administrator):{Colors.ENDC}")
                print(f"    Status:   schtasks /query /tn \"Dohtar\\MonitorAgent\"")
                print(f"    Stop:     schtasks /end /tn \"Dohtar\\MonitorAgent\"")
                print(f"    Start:    schtasks /run /tn \"Dohtar\\MonitorAgent\"")
            elif self.os_type == "Linux":
                print(f"\n  {Colors.DIM}Useful commands:{Colors.ENDC}")
                print(f"    Status:   sudo systemctl status ocm-agent")
                print(f"    Logs:     sudo journalctl -u ocm-agent -f")
                print(f"    Stop:     sudo systemctl stop ocm-agent")
                print(f"    Restart:  sudo systemctl restart ocm-agent")
            elif self.os_type == "Darwin":
                print(f"\n  {Colors.DIM}Useful commands:{Colors.ENDC}")
                print(f"    Status:   launchctl list | grep ocm")
                print(f"    Stop:     launchctl unload ~/Library/LaunchAgents/com.dohtar.monitor.plist")
                print(f"    Start:    launchctl load ~/Library/LaunchAgents/com.dohtar.monitor.plist")
        else:
            print(f"\n  {Colors.YELLOW}No system service installed.{Colors.ENDC}")
            print(f"  To run the agent manually:\n")
            if self.os_type == "Windows":
                print(f"    cd {self.install_path}")
                print(f"    python agent.py --config config.json")
            else:
                print(f"    cd {self.install_path}")
                print(f"    python3 agent.py --config config.json")
            print(f"\n  Add --debug for verbose output.")

            # Offer to start it now
            print()
            if self.prompt_yes_no("Start the agent now?", True):
                self.start_agent_foreground()
                return

        # Dashboard link
        try:
            backend_host = backend_url.split('://')[1].split(':')[0]
        except Exception:
            backend_host = "localhost"
        print(f"\n  {Colors.BOLD}Dashboard: http://{backend_host}:9090{Colors.ENDC}\n")

    def start_agent_foreground(self):
        """Start the agent in the foreground."""
        config_path = Path(self.install_path) / "config.json"
        python_exe = sys.executable

        print(f"\n  Starting agent... (Ctrl+C to stop)\n")
        print(f"{'=' * 44}\n")

        try:
            subprocess.run(
                [python_exe, "agent.py", "--config", str(config_path)],
                cwd=self.install_path,
            )
        except KeyboardInterrupt:
            print(f"\n\n  Agent stopped.")

    # ── Main flows ───────────────────────────────────────────────────

    def run_install(self):
        """Run the full installation flow."""
        self.print_header()

        try:
            self.step_backend_connection()
            self.step_agent_identity()
            self.step_install_location()
            self.step_metrics_collection()
            self.step_service_probing()
            self.step_polling_interval()
            self.step_system_service()

            print(f"\n{Colors.CYAN}-- Installing --{Colors.ENDC}")

            self.copy_agent_files()
            self.print_success(f"Agent files installed to {self.install_path}")

            self.write_config()
            self.print_success(f"Config saved to {self.install_path}/config.json")

            service_installed = False
            if self.config.get('install_service'):
                service_installed = self.install_service_handler()

            self.print_post_install(service_installed)

        except KeyboardInterrupt:
            print(f"\n\n  {Colors.YELLOW}Installation cancelled.{Colors.ENDC}\n")
            sys.exit(0)
        except Exception as e:
            self.print_error(f"Installation failed: {e}")
            sys.exit(1)

    def run_reconfigure(self):
        """Reconfigure an existing installation (keeps agent ID)."""
        self.print_header()
        print(f"  {Colors.CYAN}Reconfiguring agent: {self.config.get('agent_id')}{Colors.ENDC}")

        try:
            self.step_backend_connection()
            self.step_agent_identity()
            self.step_metrics_collection()
            self.step_service_probing()
            self.step_polling_interval()

            self.write_config()
            self.print_success(f"Config updated: {self.install_path}/config.json")

            self.copy_agent_files()
            self.print_success(f"Agent files updated")

            # Try to restart service
            restarted = False
            try:
                if self.os_type == "Linux":
                    from service_linux import restart_service
                    restart_service()
                    restarted = True
                elif self.os_type == "Windows":
                    from service_windows import restart_service
                    restart_service()
                    restarted = True
                elif self.os_type == "Darwin":
                    from service_macos import restart_service
                    restart_service()
                    restarted = True
            except Exception:
                pass

            if restarted:
                self.print_success("Service restarted with new config")
            else:
                print()
                if self.prompt_yes_no("Start the agent now?", True):
                    self.start_agent_foreground()

        except KeyboardInterrupt:
            print(f"\n\n  {Colors.YELLOW}Reconfiguration cancelled.{Colors.ENDC}\n")
            sys.exit(0)
        except Exception as e:
            self.print_error(f"Reconfiguration failed: {e}")
            sys.exit(1)

    def run_uninstall(self):
        """Uninstall the agent."""
        self.print_header()

        if not self.install_path:
            existing = self.find_existing_install()
            if not existing:
                self.print_error("No OCM agent installation found.")
                sys.exit(1)
            self.install_path = str(existing)
            try:
                with open(existing / "config.json", 'r') as f:
                    self.config = json.load(f)
            except Exception:
                pass

        print(f"  Uninstalling agent from: {self.install_path}")
        if not self.prompt_yes_no("Continue?", False):
            print("  Cancelled.")
            return

        # Stop and remove service
        try:
            if self.os_type == "Linux":
                from service_linux import uninstall_service
                uninstall_service()
                self.print_success("Service removed")
            elif self.os_type == "Windows":
                from service_windows import uninstall_service
                uninstall_service()
                self.print_success("Service removed")
            elif self.os_type == "Darwin":
                from service_macos import uninstall_service
                uninstall_service()
                self.print_success("Service removed")
        except Exception as e:
            self.print_warn(f"Could not remove service: {e}")

        if self.prompt_yes_no(f"Delete agent files from {self.install_path}?", False):
            try:
                shutil.rmtree(self.install_path)
                self.print_success(f"Agent files removed")
            except Exception as e:
                self.print_error(f"Failed to remove files: {e}")

        print(f"\n  {Colors.GREEN}Uninstall complete.{Colors.ENDC}\n")


# ── Entry point ──────────────────────────────────────────────────────

def main():
    installer = Installer()

    # Handle explicit subcommands
    if len(sys.argv) > 1:
        command = sys.argv[1].lower()
        if command == "uninstall":
            installer.run_uninstall()
            return
        elif command == "configure":
            existing = installer.find_existing_install()
            if existing:
                installer.config = json.loads((existing / "config.json").read_text())
                installer.install_path = str(existing)
                installer.run_reconfigure()
            else:
                installer.print_error("No existing installation found to reconfigure.")
                sys.exit(1)
            return
        elif command not in ("install",):
            print(f"  Unknown command: {command}")
            print(f"  Usage: python install.py [install|configure|uninstall]")
            sys.exit(1)

    # Default flow: check for existing install first
    existing = installer.find_existing_install()
    if existing:
        action = installer.handle_existing_install(existing)
        if action == 'reconfigure':
            installer.run_reconfigure()
        elif action == 'clean':
            installer.run_install()
        elif action == 'uninstall':
            installer.run_uninstall()
        elif action == 'cancel':
            print(f"\n  Cancelled.\n")
            sys.exit(0)
    else:
        installer.run_install()


if __name__ == "__main__":
    main()
