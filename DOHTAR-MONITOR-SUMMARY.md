# Dohtar Monitor — Project Summary

## What Is It

Dohtar Monitor is a homelab monitoring system that tracks GPUs, CPU, RAM, disk, Docker containers, and AI services (LLM and STT) across multiple machines in real time. It replaced the previous "OpenClaw Monitor" with a push-based agent architecture.

**Dashboard:** `http://192.168.1.100:9090` (replace with your backend IP)

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  ai-server   │     │  backend     │     │  vm-agent    │
│  (2× GPU)    │     │  (1× GPU)    │     │  (no GPU)    │
│              │     │              │     │              │
│  Agent ──────┤     │  Agent ──────┤     │  Agent ──────┤
│  - System    │     │  - System    │     │  - System    │
│  - GPU ×2    │     │  - GPU ×1    │     │  - Docker    │
│  - LLM       │     │  - Docker    │     │              │
│  - STT       │     │              │     │              │
└──────┬───────┘     └──────┬───────┘     └──────┬───────┘
       │ POST /api/ingest   │                    │
       └────────────────────┼────────────────────┘
                            ▼
                ┌───────────────────────┐
                │  Backend (Docker)     │
                │  Express.js + SQLite  │
                │  WebSocket broadcast  │
                │  Port 9090 + UDP 9091 │
                └───────────┬───────────┘
                            │ WebSocket
                            ▼
                ┌───────────────────────┐
                │  Dashboard (Browser)  │
                │  React SPA            │
                │  Multi-column grid    │
                └───────────────────────┘
```

**Push-based:** Each machine runs a Python agent that collects metrics every 2 seconds and POSTs them to the backend. The backend stores metrics in SQLite and broadcasts live data to all connected browsers via WebSocket.

## Machines

| Machine | IP | OS | GPUs | AI Services | SSH |
|---------|----|----|------|-------------|-----|
| ai-server | 192.168.1.101 | Ubuntu | 2× NVIDIA | llama.cpp (8081), Whisper (8083) | `ssh -p 2224 user@192.168.1.101` |
| backend | 192.168.1.100 | Windows/Linux | 1× NVIDIA | Docker host for backend | N/A (local or replace IP) |
| vm-agent | (VM on backend) | Ubuntu VM | None | Docker containers | `ssh user@<VM-IP>` |

## File Structure

```
dohtar-monitor/
├── docker-compose.yml          # Backend deployment
├── .dockerignore
├── DOHTAR-MONITOR-SUMMARY.md   # This file
├── backend/
│   ├── Dockerfile              # Node 22, bundles agent/ for OTA updates
│   ├── package.json            # express, better-sqlite3, ws, archiver
│   ├── server.js               # API, WebSocket, metrics storage, agent commands
│   └── public/
│       └── index.html          # React SPA (Babel standalone, no build step)
└── agent/
    ├── agent.py                # Main loop — parallel collection, payload send
    ├── config.py               # Config loading and validation
    ├── config.example.json     # Example config for new installs
    ├── discovery.py            # UDP auto-discovery of backend
    ├── install.py              # Interactive installer (detects services, IPs)
    ├── version.py              # Agent version tracking (currently 2.1.0)
    ├── updater.py              # OTA self-update from backend
    ├── requirements.txt        # aiohttp, psutil
    ├── service_linux.py        # systemd service setup
    ├── service_windows.py      # Windows scheduled task setup
    ├── service_macos.py        # launchd setup
    ├── collectors/
    │   ├── system.py           # CPU, RAM, disk via psutil
    │   ├── gpu.py              # nvidia-smi (parallel queries, 2s timeout)
    │   ├── processes.py        # Top processes by CPU/RAM
    │   ├── llm.py              # LLM service probing (shared sessions)
    │   ├── stt.py              # STT service probing
    │   └── docker_stats.py     # Docker container listing
    └── probes/
        ├── llamacpp.py         # llama.cpp /health, /slots, /v1/models
        ├── ollama.py           # Ollama /api/tags, /api/ps
        ├── whisper.py          # Whisper API health check
        └── generic_http.py     # Generic HTTP endpoint probe
```

## Backend Deployment (Docker)

The backend runs in Docker. To deploy or update:

```bash
cd /opt/dohtar-monitor/    # or E:\Docker\dohtar-monitor\ on Windows
docker compose build
docker compose up -d
```

This creates a container with Express.js on port 9090 (HTTP + WebSocket) and UDP discovery on port 9091. Metrics are stored in a named volume (`metrics-data`) so they persist across rebuilds.

### Backend API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/ingest` | POST | Agents push metrics here every 2s |
| `/api/latest` | GET | Current snapshot of all agents |
| `/api/agents` | GET | List registered agents |
| `/api/discover` | GET | Service discovery for agents |
| `/api/history/:host/:metric` | GET | Historical metrics (sys/gpu) |
| `/api/history/llm/:endpoint` | GET | LLM metrics history |
| `/api/download-agent` | GET | Download agent files as zip |
| `/api/agent-version` | GET | Server's bundled agent version |
| `/api/agent-update` | POST | Queue update command for agents |
| `/api/agent-restart` | POST | Queue restart command for agents |
| `/health` | GET | Backend health check |

## Agent Installation

### Automated (Interactive Installer)

```bash
# Download agent files (replace 192.168.1.100 with your backend IP)
curl -o dohtar-agent.zip http://192.168.1.100:9090/api/download-agent
unzip dohtar-agent.zip -d /opt/dohtar-agent
cd /opt/dohtar-agent/dohtar-agent

# Install dependencies
pip3 install aiohttp psutil --break-system-packages

# Run installer (auto-detects backend, GPU, AI services)
python3 install.py
```

The installer will auto-discover the backend via UDP broadcast, detect NVIDIA GPUs, scan for running AI services (llama.cpp, Ollama, Whisper), and set up a system service.

### Manual Setup

1. Copy agent files to the target machine
2. Install Python dependencies: `pip3 install aiohttp psutil`
3. Create `config.json` (see `config.example.json`):

```json
{
  "agent_id": "unique-id-here",
  "machine_name": "MyMachine",
  "backend_url": "http://192.168.1.100:9090",
  "poll_interval": 2,
  "collectors": {
    "system": true,
    "gpu": true,
    "processes": true,
    "disk": true,
    "docker": false
  },
  "services": [
    {
      "type": "llm",
      "name": "llama.cpp",
      "endpoint": "localhost:8081",
      "probe": "llamacpp"
    }
  ]
}
```

4. Set up as a system service:

**Linux (systemd):**
```bash
sudo python3 service_linux.py
sudo systemctl start dohtar-agent
sudo systemctl status dohtar-agent
```

**Windows (Scheduled Task):**
```powershell
python service_windows.py
# Creates task at \Dohtar\MonitorAgent
```

## Agent Install Locations & Services

| Machine | OS | Source Files | Running From | Config | Service Name |
|---------|-----|-------------|-------------|--------|--------------|
| ai-server | Linux | `/opt/dohtar-agent/` | `/opt/dohtar-agent/installedLocation/` | `/opt/dohtar-agent/installedLocation/config.json` | `ocm-agent` (systemd) |
| backend | Windows | `E:\dohtar-monitor\agent\` | `E:\dohtar-monitor\agent\` | `E:\dohtar-monitor\InstallLocation\config.json` | `\Dohtar\MonitorAgent` (schtasks) |
| vm-agent | Linux | `/opt/dohtar-agent/` | `/opt/dohtar-agent/installedLocation/` | `/opt/dohtar-agent/installedLocation/config.json` | `ocm-agent` (systemd) |

**Important notes:**
- On Windows, the `InstallLocation` folder contains the config.json and old agent files from a previous install.
- On Linux machines, the service runs from `installedLocation/` subdirectory (created by the original installer). When updating, files must be copied to BOTH `/opt/dohtar-agent/` AND `/opt/dohtar-agent/installedLocation/`.
- The Windows scheduled task requires the full Python path. Adjust the path according to your Python installation.

## Agent Updates

### OTA Update via Dashboard (Recommended — for future updates)

This works now that all agents have `updater.py` and `version.py` installed.

**Steps:**
1. Edit agent files locally in `dohtar-monitor/agent/`
2. Rebuild the Docker container (this bakes new agent files into the backend):
   ```bash
   cd /opt/dohtar-monitor/  # or E:\Docker\dohtar-monitor\ on Windows
   docker compose build && docker compose up -d
   ```
3. Click "⟳ Update Agents" button in the dashboard header
4. Each agent downloads the new zip, extracts over its install directory, and restarts

**How it works internally:**
1. `docker compose build` copies the `agent/` folder into the Docker container image
2. The backend serves these files via `GET /api/download-agent` as a zip
3. Clicking "Update Agents" calls `POST /api/agent-update`, which queues an `update` command for all online agents
4. On the next poll (~2s), each agent receives the command in the `/api/ingest` response
5. The agent's `updater.py` downloads the zip, extracts to a temp directory, and overlays new files
6. Preserved files (never overwritten): `config.json`, `__pycache__`, `InstallLocation`, `InstalledFolder`, `installedLocation`
7. Agent restarts via `systemctl restart ocm-agent` (Linux) or `schtasks` (Windows)

### Manual Agent Update

If OTA doesn't work or for first-time setup, copy files manually.

**ai-server** (SSH on port 2224):
```powershell
# From Windows PowerShell — copy files to ai-server
scp -P 2224 -r "path\to\dohtar-monitor\agent" user@192.168.1.101:/tmp/

# SSH into ai-server
ssh -p 2224 user@192.168.1.101

# Copy to both locations and restart
sudo cp -r /tmp/agent/* /opt/dohtar-agent/
sudo cp -r /tmp/agent/* /opt/dohtar-agent/installedLocation/
sudo systemctl restart ocm-agent
sudo systemctl status ocm-agent
rm -rf /tmp/agent
```

**vm-agent** (files shared via VirtualBox shared folder at `/mnt/shared/`):
```bash
# On vm-agent — files should already be in /mnt/shared/agent/
sudo cp -r /mnt/shared/agent/* /opt/dohtar-agent/
sudo cp -r /mnt/shared/agent/* /opt/dohtar-agent/installedLocation/
sudo systemctl restart ocm-agent
sudo systemctl status ocm-agent
```

**backend (Windows):**
Files are already at `E:\dohtar-monitor\agent\` — just restart the task:
```powershell
schtasks /End /TN "\Dohtar\MonitorAgent"
schtasks /Run /TN "\Dohtar\MonitorAgent"
```

If the task was deleted and needs to be recreated (must run as Administrator):
```powershell
schtasks /Create /TN "\Dohtar\MonitorAgent" /TR 'path\to\python.exe E:\dohtar-monitor\agent\agent.py --config E:\dohtar-monitor\InstallLocation\config.json' /SC ONSTART /RU SYSTEM /RL HIGHEST /F
schtasks /Run /TN "\Dohtar\MonitorAgent"
```

**Note:** Use single quotes around the `/TR` value in PowerShell. Double-quote escaping with backslashes does not work in PowerShell.

## Dashboard Features

The dashboard is a React SPA served from the backend (no build step, uses Babel standalone):

- **Multi-column responsive grid:** 2 columns at 768px+, 3 columns at 1200px+, max 1440px
- **Sticky header** with agent status pills, connection indicator, and "⟳ Update Agents" button
- **GPU cards** with load ring (%), VRAM bar, temperature, power, fan speed, model tags
- **CPU ring** with compact processor name (e.g., "Ryzen 5900X (8c)")
- **RAM and disk usage bars**
- **Docker containers table:** Name, Port, Created, Status
- **LLM service cards:** Model name, status, KV cache usage, slot counts (busy/total), generation speed
- **Top processes list** by CPU and memory usage
- **GPU-to-service mapping:** Services show only on the GPU they're actually running on
- **Agent version display** on each machine card
- **History section** with per-host metric graphs

## Key Design Decisions & Fixes

### Performance Optimizations
- **Parallel collection:** All collectors run via `asyncio.gather()` — GPU, system, Docker, LLM probes, STT probes all in parallel
- **Parallel nvidia-smi:** GPU data and process queries run concurrently (was sequential, taking 6s+)
- **Non-blocking CPU:** Uses `psutil.cpu_percent(interval=None)` with primed first call
- **Shared aiohttp sessions:** One session reused across all LLM/STT probes per cycle
- **Cached probe instances:** Probes aren't recreated every cycle
- **Elapsed-aware sleep:** `max(0.1, poll_interval - elapsed)` so cycles stay at 2s
- **Result:** Cycle time went from 30-60s down to under 2s

### Networking
- **IPv6 prefix stripping:** Docker networking returns `::ffff:172.26.x.x`, stripped in server.js
- **Real LAN IPs:** Agents detect their IP via UDP socket trick (`socket.connect()` to backend host) and send it in the payload as `client_ip`
- **Virtual adapter filtering:** Installer filters VirtualBox (`192.168.56.x`), Docker (`172.x`), and NAT (`10.0.2.x`) IPs to find real LAN addresses

### LLM Slot Monitoring
- **Root cause of ?/? slots:** llama.cpp `/slots` returns a JSON array directly, but code was calling `.get("slots", [])` on it (lists don't have `.get()` — fails silently via exception)
- **Fix:** Handle both array and dict responses; parse `state` as int (0=idle, 1=processing) or string; read `cache_tokens`/`n_ctx` at top level and under `common`; extract slot counts from `/health` as fallback
- **Frontend fix:** Changed from `total_slots/total_slots` to `busy_slots/total_slots`
- **Note:** The `/slots` endpoint requires starting llama-server with `--slots` flag

### GPU-to-Service Mapping
- Services are mapped to specific GPUs using nvidia-smi's compute-apps query
- Process names (llama-server, python3) matched to GPU indices
- Frontend only shows service tags on the correct GPU card

### React Hooks
- All `useState` and `useEffect` hooks must be declared before any conditional `return` in the component
- Moving hooks after an early return causes "Rendered more hooks than during previous render" error
- This was fixed in the App component when adding the update button state

## Config Reference

### Agent config.json

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `agent_id` | string | auto-generated | Unique agent identifier |
| `machine_name` | string | hostname | Display name on dashboard |
| `backend_url` | string | `http://localhost:9090` | Backend URL |
| `poll_interval` | number | 2 | Seconds between collection cycles |
| `collectors.system` | bool | true | CPU, RAM, disk |
| `collectors.gpu` | bool | true | NVIDIA GPU metrics |
| `collectors.processes` | bool | true | Top processes |
| `collectors.docker` | bool | false | Docker containers |
| `services` | array | [] | AI services to probe |

### Service config (inside services array)

| Field | Values | Description |
|-------|--------|-------------|
| `type` | `llm`, `stt` | Service type |
| `name` | string | Display name |
| `endpoint` | `host:port` | Service address |
| `probe` | `llamacpp`, `ollama`, `whisper`, `generic_http` | Probe type |

## Useful Commands

```bash
# ── Linux Agent Management ──
sudo systemctl status ocm-agent          # Check agent status
sudo systemctl restart ocm-agent         # Restart agent
sudo systemctl stop ocm-agent            # Stop agent
sudo journalctl -u ocm-agent -f          # Follow agent logs
cat /etc/systemd/system/ocm-agent.service | grep ExecStart  # Check service path

# ── Windows Agent Management (PowerShell) ──
schtasks /Query /TN "\Dohtar\MonitorAgent" /V /FO LIST     # Full task details
schtasks /Run /TN "\Dohtar\MonitorAgent"                    # Start agent
schtasks /End /TN "\Dohtar\MonitorAgent"                    # Stop agent
schtasks /Query /FO LIST | findstr /i "dohtar"              # Find task

# ── Backend (Docker) ──
docker compose build && docker compose up -d    # Rebuild and restart
docker logs -f dohtar-backend                   # Follow backend logs
docker ps | findstr dohtar                      # Check container status

# ── Testing & Debugging ──
curl http://192.168.1.100:9090/api/latest      # Get current snapshot
curl http://192.168.1.100:9090/api/agents      # List all agents
curl http://192.168.1.100:9090/api/agent-version   # Check bundled agent version
curl http://192.168.1.100:9090/health          # Backend health check

# ── Manual Agent Run (for debugging — runs in foreground) ──
# Linux:
python3 /opt/dohtar-agent/installedLocation/agent.py --config /opt/dohtar-agent/installedLocation/config.json
# Windows:
python E:\dohtar-monitor\agent\agent.py --config E:\dohtar-monitor\InstallLocation\config.json

# ── File Transfer ──
# Windows → ai-server:
scp -P 2224 -r "path\to\dohtar-monitor\agent" user@192.168.1.101:/tmp/
# OpenClaw shared folder is at /mnt/shared/ (VirtualBox shared folder)
```

## Troubleshooting

**Agent not appearing on dashboard:**
1. Check if the agent process is running (systemctl/schtasks)
2. Verify `backend_url` in config.json points to your backend IP (e.g., `http://192.168.1.100:9090`)
3. Run agent manually in foreground to see errors
4. Check if backend is reachable: `curl http://192.168.1.100:9090/health`

**LLM slots showing ?/?:**
- Ensure llama-server was started with `--slots` flag
- If `/slots` endpoint returns 404, slot monitoring won't work (falls back to `/health` data)

**Agent showing wrong IP:**
- Agent detects LAN IP via UDP socket to backend. If backend is on same machine, may show loopback
- VirtualBox VMs may show NAT IP (`10.0.2.x`) — enter backend URL manually during install

**Windows scheduled task not running:**
- `where python` returns nothing for SYSTEM account — use full Python path in task
- Use single quotes for `/TR` value in PowerShell (double-quote escaping breaks)
- Check task: `schtasks /Query /TN "\Dohtar\MonitorAgent" /V /FO LIST`

**VM showing `10.0.2.2` as backend:**
- This is VirtualBox NAT gateway IP. Enter your backend IP manually when prompted (e.g., `192.168.1.100:9090`)
