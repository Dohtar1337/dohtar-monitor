# Dohtar Monitor

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://www.python.org/)
[![Node.js 18+](https://img.shields.io/badge/Node.js-18+-green.svg)](https://nodejs.org/)
[![Docker](https://img.shields.io/badge/Docker-Supported-blue.svg)](https://www.docker.com/)

A self-hosted homelab monitoring system for tracking GPU metrics, system resources, Docker containers, and LLM instances across multiple machines in real-time.

## Features

- **GPU Monitoring**: VRAM usage, temperature, utilization, power draw, and fan speed via nvidia-smi or pynvml
- **System Metrics**: CPU, RAM, disk usage, and uptime across all monitored machines
- **Per-Process Tracking**: Detailed GPU memory mapping at the process level
- **Docker Support**: Real-time container health monitoring with CPU %, memory, restart counts, and status
- **LLM Instance Monitoring**: Track llama.cpp instances with idle/generating status, live token/s throughput, KV cache usage, and slot tracking
- **Generation History**: Detailed per-request metrics including exact token/s speed, token counts, and duration
- **Over-The-Air Updates**: Agents auto-update from the dashboard
- **Multi-Machine Support**: Monitor unlimited machines with unique IDs and real-time WebSocket synchronization
- **Zero Build Step Dashboard**: Single-file React SPA with in-browser Babel compilation — no build process needed

## Table of Contents

- [Quick Start](#quick-start)
- [Architecture](#architecture)
- [Installation](#installation)
  - [Backend Setup](#backend-setup)
  - [Agent Setup (Linux)](#agent-setup-linux)
  - [Agent Setup (Windows)](#agent-setup-windows)
- [Configuration](#configuration)
  - [Agent Config Reference](#agent-config-reference)
- [LLM Monitoring](#llm-monitoring)
- [API Endpoints](#api-endpoints)
- [Dashboard](#dashboard)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)

## Quick Start

### Backend (Docker)
```bash
docker-compose up -d
```
Backend will be available at `http://localhost:3000`

### Agent (Linux)
```bash
pip install aiohttp psutil pynvml
cp config.example.json config.json
# Edit config.json with your backend URL and machine details
python agent.py --config config.json
```

### Agent (Windows)
```powershell
pip install aiohttp psutil pynvml
copy config.example.json config.json
# Edit config.json with your backend URL and machine details
python agent.py --config config.json
```

## Architecture

Dohtar Monitor follows a distributed agent-based architecture with a centralized backend:

```
┌─────────────────────────────────────────────────────────────┐
│                    Dashboard (React SPA)                     │
│              Single HTML file, Babel in-browser              │
│         Real-time updates via WebSocket connection           │
└──────────────────────┬──────────────────────────────────────┘
                       │ HTTP/WebSocket
┌──────────────────────▼──────────────────────────────────────┐
│              Backend (Express.js + SQLite)                   │
│                                                              │
│  • API endpoints for metric ingestion and querying          │
│  • WebSocket server for real-time dashboard updates        │
│  • OTA update queue management                             │
│  • Metrics storage (SQLite with better-sqlite3)            │
└──────────────────────┬──────────────────────────────────────┘
                       │ REST API (POST /api/ingest)
        ┌──────────────┼──────────────┐
        │              │              │
┌───────▼──────┐ ┌────▼──────┐ ┌────▼──────┐
│  Agent 1     │ │  Agent 2  │ │  Agent N  │
│  (Linux)     │ │  (Windows)│ │  (Linux)  │
│              │ │           │ │           │
│ • Metrics    │ │ • Metrics │ │ • Metrics │
│   collection │ │   collect │ │   collect │
│ • GPU stats  │ │ • GPU     │ │ • GPU     │
│ • Docker mon │ │   stats   │ │   stats   │
│ • LLM track  │ │ • Docker  │ │ • LLM     │
└──────────────┘ │   mon     │ │   track   │
                 │ • LLM     │ │           │
                 │   track   │ └───────────┘
                 └───────────┘

Every 2 seconds: Agent → Backend (metrics POST)
Continuous: Backend → Dashboard (WebSocket push)
```

## Installation

### Backend Setup

#### Prerequisites
- Node.js 18 or higher
- Docker and Docker Compose (recommended) or local SQLite3

#### Using Docker (Recommended)

1. Clone or download the backend code
2. Create a `docker-compose.yml` file:

```yaml
version: '3.8'

services:
  dohtar-backend:
    build: .
    ports:
      - "3000:3000"
    environment:
      - NODE_ENV=production
      - PORT=3000
    volumes:
      - ./data:/app/data
    restart: unless-stopped
```

3. Build and run:
```bash
docker-compose up -d
```

4. Verify the backend is running:
```bash
curl http://localhost:3000/
```

#### Manual Setup (Linux/macOS/Windows)

1. Install dependencies:
```bash
npm install
```

2. Start the backend:
```bash
npm start
```

The backend will listen on port 3000 by default. Adjust the `PORT` environment variable to use a different port.

### Agent Setup (Linux)

#### Prerequisites
- Python 3.9 or higher
- pip package manager

#### Installation

1. Install Python dependencies:
```bash
pip install aiohttp psutil pynvml
```

2. Copy and configure:
```bash
cp config.example.json config.json
```

3. Edit `config.json` with your settings:
```json
{
  "agent_id": "homelab-gpu-1",
  "machine_name": "GPU Workstation",
  "backend_url": "http://192.168.1.100:3000",
  "poll_interval": 2,
  "gpus": [0, 1],
  "services": [
    {
      "name": "LLM Primary",
      "endpoint": "http://127.0.0.1:8000",
      "type": "llm",
      "probe": "llamacpp"
    }
  ]
}
```

4. Test the agent:
```bash
python agent.py --config config.json
```

#### Running as a Systemd Service

1. Copy the service file:
```bash
sudo cp dohtar-agent.service /etc/systemd/system/
```

2. Edit the service file to match your installation path:
```bash
sudo nano /etc/systemd/system/dohtar-agent.service
```

3. Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable dohtar-agent
sudo systemctl start dohtar-agent
```

4. Check status:
```bash
sudo systemctl status dohtar-agent
```

### Agent Setup (Windows)

#### Prerequisites
- Python 3.9 or higher (installed and in PATH)
- pip package manager

#### Installation

1. Install Python dependencies:
```powershell
pip install aiohttp psutil pynvml
```

2. Copy and configure:
```powershell
copy config.example.json config.json
```

3. Edit `config.json` with your settings (same format as Linux)

4. Test the agent:
```powershell
python agent.py --config config.json
```

#### Running at Startup (Optional)

Create a shortcut in the Startup folder that runs the agent:

1. Create a batch file `run_agent.bat`:
```batch
@echo off
cd /d C:\path\to\dohtar-agent
python agent.py --config config.json
pause
```

2. Place the batch file in `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`

Alternatively, use Windows Task Scheduler to run the agent at startup with elevated privileges if needed.

## Configuration

### Agent Config Reference

Create a `config.json` file in the agent directory with the following structure:

| Key | Type | Required | Example | Description |
|-----|------|----------|---------|-------------|
| `agent_id` | string | Yes | `"homelab-1"` | Unique identifier for this agent. Used to distinguish machines in the dashboard. Must be unique across all agents. |
| `machine_name` | string | Yes | `"GPU Workstation"` | Human-readable display name shown in the dashboard. |
| `backend_url` | string | Yes | `"http://192.168.1.100:3000"` | Full URL to the Dohtar backend. Include protocol and port. |
| `poll_interval` | integer | No (default: 2) | `2` | Seconds between metric collection and submission to backend. Lower values increase accuracy but raise network load. |
| `gpus` | array | No | `[0, 1]` | List of GPU indices to monitor (0-indexed). Omit to skip GPU monitoring. |
| `services` | array | No | `[ { ... } ]` | Array of LLM services to monitor. Each service requires `name`, `endpoint`, `type`, and `probe` keys. See LLM Monitoring section. |

### Example Config

```json
{
  "agent_id": "homelab-primary",
  "machine_name": "Main Inference Box",
  "backend_url": "http://192.168.1.50:3000",
  "poll_interval": 2,
  "gpus": [0, 1],
  "services": [
    {
      "name": "Primary LLM",
      "endpoint": "http://127.0.0.1:8000",
      "type": "llm",
      "probe": "llamacpp"
    },
    {
      "name": "Secondary LLM",
      "endpoint": "http://127.0.0.1:8001",
      "type": "llm",
      "probe": "llamacpp"
    }
  ]
}
```

## LLM Monitoring

Dohtar Monitor tracks llama.cpp instances in real-time, providing visibility into inference workloads, KV cache usage, and throughput metrics.

### Setup Requirements

To enable LLM monitoring, start your llama.cpp instance with the `--metrics` and `--slots` flags:

```bash
./main -m model.gguf --metrics --slots -ngl 35 -c 2048
```

**Important flags:**
- `--metrics`: Enables the Prometheus metrics endpoint (required for monitoring)
- `--slots`: Enables KV cache slot tracking (required for accurate cache monitoring)

### Monitored Metrics

The dashboard displays:

- **Status**: Current state (idle, generating, or error)
- **Live Token/s**: Real-time inference speed during generation
- **KV Cache**: Current usage and total capacity
- **Slot Count**: Number of concurrent generation slots and their usage
- **Generations History**: Table with per-request metrics:
  - Token count
  - Exact tokens/second speed
  - Duration
  - Timestamp

### Configuration

In your `config.json`, add each llama.cpp instance under the `services` array:

```json
"services": [
  {
    "name": "My LLM",
    "endpoint": "http://127.0.0.1:8000",
    "type": "llm",
    "probe": "llamacpp"
  }
]
```

- `name`: Display name in the dashboard
- `endpoint`: Full URL including scheme and port
- `type`: Always `"llm"`
- `probe`: Always `"llamacpp"` (for llama.cpp compatibility)

## API Endpoints

### Metrics Ingestion
**POST `/api/ingest`**

Agents send metrics to this endpoint every 2 seconds.

Request body:
```json
{
  "agent_id": "homelab-1",
  "timestamp": 1710432000,
  "system": {
    "cpu_percent": 25.5,
    "ram_percent": 60.2,
    "uptime_seconds": 864000
  },
  "gpu": [...],
  "processes": [...],
  "docker": [...],
  "services": [...]
}
```

### Latest Metrics
**GET `/api/latest`**

Retrieve the latest metrics snapshot for all agents (also broadcast via WebSocket).

Response: Latest metrics from all agents with timestamps.

### LLM Generation History
**GET `/api/history/llm/:endpoint/runs`**

Retrieve recent generation runs for a specific LLM endpoint.

Parameters:
- `:endpoint`: URL-encoded endpoint URL (e.g., `http%3A%2F%2F127.0.0.1%3A8000`)
- `?limit=100` (optional): Number of recent runs to return

Response:
```json
[
  {
    "timestamp": 1710432000,
    "tokens": 256,
    "duration_ms": 8192,
    "tokens_per_second": 31.25
  }
]
```

### Queue Agent Update
**POST `/api/agent-update`**

Queue an OTA update for a specific agent.

Request body:
```json
{
  "agent_id": "homelab-1"
}
```

### Download Agent Package
**GET `/api/download-agent`**

Download the agent zip file for manual installation or OTA updates.

## Dashboard

> Screenshot coming soon

The Dohtar Monitor dashboard is a single-file React application served by the backend. No build process is required—Babel compiles JSX in the browser.

### Features

- **Real-time Updates**: WebSocket connection delivers metrics instantly as they arrive from agents
- **Multi-Machine View**: All agents displayed in a unified interface with individual machine tabs
- **GPU Metrics**: Live VRAM, temperature, utilization, power draw, and per-process breakdowns
- **System Overview**: CPU, RAM, disk, and uptime at a glance
- **Container Monitoring**: Docker container health and resource usage
- **LLM Insights**: Real-time inference speed, KV cache usage, and generation history
- **Agent Management**: Trigger OTA updates from the dashboard

### Accessing the Dashboard

Open your browser and navigate to:
```
http://<backend-ip>:3000
```

## Troubleshooting

### Agent Cannot Connect to Backend

**Symptom**: Agent logs show connection errors or timeouts.

**Solutions**:
1. Verify the `backend_url` in `config.json` is correct and reachable:
   ```bash
   curl http://<backend-ip>:3000/api/latest
   ```
2. Check firewall rules allow outbound connections to the backend port
3. Ensure the backend is running:
   ```bash
   docker ps  # if using Docker
   ```
4. Try using the backend machine's IP instead of localhost

### GPU Metrics Not Appearing

**Symptom**: CPU and RAM show, but GPU metrics are missing.

**Solutions**:
1. Verify `pynvml` is installed:
   ```bash
   pip install pynvml
   ```
2. Check that GPU indices in `config.json` are correct (use `nvidia-smi` to verify)
3. Ensure `nvidia-smi` is available on the system
4. If using WSL2, verify GPU passthrough is configured correctly
5. Check agent logs for GPU initialization errors

### LLM Monitoring Not Working

**Symptom**: LLM services show as offline or unreachable.

**Solutions**:
1. Verify llama.cpp is started with `--metrics` and `--slots` flags
2. Test the metrics endpoint manually:
   ```bash
   curl http://127.0.0.1:8000/metrics
   ```
3. Check the `endpoint` in `config.json` matches your llama.cpp server URL
4. Ensure the endpoint is reachable from the agent machine (not just localhost)
5. Check agent logs for HTTP errors when connecting to the endpoint

### Docker Container Monitoring Missing

**Symptom**: No Docker containers appear in the dashboard.

**Solutions**:
1. Verify Docker is installed and running
2. Check that the agent process has permission to access the Docker socket (Linux):
   ```bash
   sudo usermod -aG docker $USER
   # Then log out and back in
   ```
3. On Windows, ensure Docker Desktop is running
4. Check Docker daemon connectivity from the agent machine

### WebSocket Connection Drops

**Symptom**: Real-time updates stop, dashboard becomes stale.

**Solutions**:
1. Check network connectivity between client and backend
2. Verify firewall rules allow WebSocket connections (port 3000)
3. Restart the backend service
4. Check backend logs for connection errors
5. Try accessing the dashboard from a different machine to isolate client issues

### High CPU Usage by Agent

**Symptom**: The Python agent process consumes significant CPU.

**Solutions**:
1. Increase the `poll_interval` in `config.json` (default is 2 seconds)
2. Reduce the number of GPUs being monitored if possible
3. Remove unused services from the `services` array
4. Check if GPU metrics collection is stalling (verify `nvidia-smi` performance)

## Contributing

Contributions are welcome! Please feel free to open issues for bugs, feature requests, or documentation improvements.

### Development Setup

1. Clone the repository
2. Install backend dependencies: `npm install`
3. Install agent dependencies: `pip install aiohttp psutil pynvml`
4. Start the backend locally and agents on test machines
5. Make your changes and test thoroughly
6. Submit a pull request with a clear description of your changes

## License

Dohtar Monitor is licensed under the MIT License. See the LICENSE file for details.

---

**Current Agent Version**: 2.5.2

For support, issues, or questions, please open an issue on the GitHub repository.
