"""
Backend discovery module for Dohtar Monitor Agent.
Discovers backend service via UDP broadcast or HTTP probe.
"""

import asyncio
import ipaddress
import logging
import socket
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

UDP_DISCOVERY_PORT = 9091
HTTP_PORT = 9090
DISCOVERY_TIMEOUT = 3


async def discover_backend_udp(udp_port: int = UDP_DISCOVERY_PORT) -> Optional[str]:
    """
    Discover backend via UDP broadcast.
    Sends broadcast packet on UDP port 9091 and waits for response.
    The response contains the HTTP port to use.

    Returns:
        Backend URL (http://ip:port) or None if not found.
    """
    try:
        import json as _json
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(DISCOVERY_TIMEOUT)

        # Send broadcast discovery packet
        message = b"dohtar-monitor"
        sock.sendto(message, ("<broadcast>", udp_port))

        # Wait for response
        data, addr = sock.recvfrom(4096)
        sock.close()

        try:
            resp = _json.loads(data.decode())
            if resp.get("service") == "dohtar-monitor":
                ip = addr[0]
                http_port = resp.get("backend_port", HTTP_PORT)
                logger.info(f"Discovered backend at {ip}:{http_port} via UDP broadcast")
                return f"http://{ip}:{http_port}"
        except (_json.JSONDecodeError, UnicodeDecodeError):
            pass

    except Exception as e:
        logger.debug(f"UDP discovery failed: {e}")

    return None


async def discover_backend_http(
    target_ip: Optional[str] = None, port: int = HTTP_PORT
) -> Optional[str]:
    """
    Discover backend via HTTP probe on port 9090.
    If target_ip is provided, probe only that IP.
    Otherwise, probe common LAN IPs.

    Args:
        target_ip: Specific IP to probe, or None for auto-discovery.
        port: Backend HTTP port (default 9090).

    Returns:
        Backend URL or None if not found.
    """
    ips_to_try = []

    if target_ip:
        ips_to_try = [target_ip]
    else:
        # Common LAN IP ranges
        ranges = [
            "192.168.1.0/24",
            "192.168.0.0/24",
            "10.0.0.0/8",
            "172.16.0.0/12",
        ]
        for cidr in ranges:
            try:
                network = ipaddress.ip_network(cidr, strict=False)
                # Sample every 10th IP to avoid too many probes
                for ip in list(network.hosts())[::10]:
                    ips_to_try.append(str(ip))
            except Exception as e:
                logger.debug(f"Error processing CIDR {cidr}: {e}")

    connector = aiohttp.TCPConnector(limit=10)
    timeout = aiohttp.ClientTimeout(total=DISCOVERY_TIMEOUT)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        tasks = [
            _probe_http(session, ip, port)
            for ip in ips_to_try
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, str):
                return result

    return None


async def _probe_http(session: aiohttp.ClientSession, ip: str, port: int) -> Optional[str]:
    """Probe a single IP for backend service."""
    url = f"http://{ip}:{port}/api/discover"
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                logger.info(f"Discovered backend at {ip}:{port} via HTTP probe")
                return f"http://{ip}:{port}"
    except Exception as e:
        logger.debug(f"HTTP probe to {ip}:{port} failed: {e}")

    return None


async def discover_backend(
    target_ip: Optional[str] = None, http_port: int = HTTP_PORT
) -> Optional[str]:
    """
    Attempt to discover backend via UDP broadcast first, then HTTP probe.

    Args:
        target_ip: Specific IP to probe, or None for auto-discovery.
        http_port: Backend HTTP port (default 9090).

    Returns:
        Backend URL or None if not found.
    """
    # Try UDP broadcast first (uses UDP port 9091)
    result = await discover_backend_udp()
    if result:
        return result

    # Fall back to HTTP probe on port 9090
    result = await discover_backend_http(target_ip, http_port)
    if result:
        return result

    logger.warning("Backend discovery failed. Please configure backend_url explicitly.")
    return None
