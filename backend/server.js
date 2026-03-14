const express = require('express');
const Database = require('better-sqlite3');
const WebSocket = require('ws');
const http = require('http');
const dgram = require('dgram');
const path = require('path');
const fs = require('fs');

const app = express();
app.use(express.json({ limit: '50mb' }));
app.use(express.static(path.join(__dirname, 'public')));

const PORT = process.env.PORT || 9090;
const DISCOVERY_PORT = 9091;
const DB_PATH = process.env.DB_PATH || '/data/metrics.db';

const AGENT_STALE_THRESHOLD = 10000;
const AGENT_OFFLINE_THRESHOLD = 60000;
const METRICS_RETENTION_DAYS = 30;
const PROC_RETENTION_DAYS = 7;

ensureDirectory(path.dirname(DB_PATH));
const db = new Database(DB_PATH);

const agentRegistry = new Map();
const latestPayloads = new Map(); // agent_id → last raw payload from agent
const wsClients = new Set();
const pendingCommands = new Map(); // agent_id → [commands] (consumed on next ingest)
const serviceUptime = new Map();   // endpoint → { since: ts, status: 'idle'|'generating'|'down' }
let latestSnapshot = {
  ts: Date.now(),
  agents: {},
};

initializeDatabase();
startCleanupTasks();

function initializeDatabase() {
  db.pragma('journal_mode = WAL');

  db.exec(`
    CREATE TABLE IF NOT EXISTS gpu_metrics (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts INTEGER NOT NULL,
      host TEXT NOT NULL,
      gpu_idx INTEGER NOT NULL,
      gpu_name TEXT,
      vram_used REAL,
      vram_total REAL,
      temp REAL,
      load REAL,
      power REAL,
      fan REAL,
      models_json TEXT,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS sys_metrics (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts INTEGER NOT NULL,
      host TEXT NOT NULL,
      cpu_load REAL,
      cpu_temp REAL,
      cpu_cores INTEGER,
      ram_used REAL,
      ram_total REAL,
      disk_used REAL,
      disk_total REAL,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS llm_metrics (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts INTEGER NOT NULL,
      endpoint TEXT NOT NULL,
      model TEXT,
      status TEXT,
      slot_state TEXT,
      prompt_progress INTEGER,
      tok_pred INTEGER,
      tok_eval INTEGER,
      speed_gen REAL,
      speed_pp REAL,
      kv_usage REAL,
      queue INTEGER,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS proc_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts INTEGER NOT NULL,
      host TEXT NOT NULL,
      procs_json TEXT,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS agents (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      agent_id TEXT UNIQUE NOT NULL,
      machine_name TEXT NOT NULL,
      ip TEXT,
      capabilities TEXT,
      services TEXT,
      first_seen INTEGER,
      last_seen INTEGER,
      status TEXT,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_gpu_metrics_host_ts ON gpu_metrics(host, ts DESC);
    CREATE INDEX IF NOT EXISTS idx_gpu_metrics_ts ON gpu_metrics(ts DESC);
    CREATE INDEX IF NOT EXISTS idx_sys_metrics_host_ts ON sys_metrics(host, ts DESC);
    CREATE INDEX IF NOT EXISTS idx_sys_metrics_ts ON sys_metrics(ts DESC);
    CREATE INDEX IF NOT EXISTS idx_llm_metrics_endpoint_ts ON llm_metrics(endpoint, ts DESC);
    CREATE INDEX IF NOT EXISTS idx_llm_metrics_ts ON llm_metrics(ts DESC);
    CREATE INDEX IF NOT EXISTS idx_proc_snapshots_host_ts ON proc_snapshots(host, ts DESC);
    CREATE INDEX IF NOT EXISTS idx_proc_snapshots_ts ON proc_snapshots(ts DESC);
    CREATE INDEX IF NOT EXISTS idx_agents_last_seen ON agents(last_seen DESC);

    CREATE TABLE IF NOT EXISTS llm_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts INTEGER NOT NULL,
      endpoint TEXT NOT NULL,
      model TEXT,
      slot_idx INTEGER DEFAULT 0,
      tok_generated INTEGER NOT NULL,
      tok_prompt INTEGER DEFAULT 0,
      speed_gen REAL NOT NULL,
      speed_pp REAL,
      t_generation_ms REAL NOT NULL,
      t_prompt_ms REAL DEFAULT 0,
      t_total_ms REAL NOT NULL,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_llm_runs_endpoint_ts ON llm_runs(endpoint, ts DESC);
    CREATE INDEX IF NOT EXISTS idx_llm_runs_ts ON llm_runs(ts DESC);
  `);
}

function ensureDirectory(dir) {
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
}

function registerAgent(agentId, machineName, ip, capabilities = [], services = [], agentVersion = null) {
  const now = Date.now();
  const existing = agentRegistry.get(agentId);

  if (existing) {
    existing.last_seen = now;
    existing.status = 'online';
    existing.machine_name = machineName;
    existing.ip = ip;
    existing.capabilities = capabilities;
    existing.services = services;
    if (agentVersion) existing.agent_version = agentVersion;
  } else {
    agentRegistry.set(agentId, {
      agent_id: agentId,
      machine_name: machineName,
      ip: ip,
      capabilities: capabilities,
      services: services,
      agent_version: agentVersion || 'unknown',
      first_seen: now,
      last_seen: now,
      status: 'online',
    });
  }

  const agent = agentRegistry.get(agentId);
  db.prepare(`
    INSERT INTO agents (agent_id, machine_name, ip, capabilities, services, first_seen, last_seen, status)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(agent_id) DO UPDATE SET
      machine_name = excluded.machine_name,
      ip = excluded.ip,
      capabilities = excluded.capabilities,
      services = excluded.services,
      last_seen = excluded.last_seen,
      status = excluded.status,
      updated_at = CURRENT_TIMESTAMP
  `).run(
    agentId,
    machineName,
    ip,
    JSON.stringify(capabilities),
    JSON.stringify(services),
    now,
    now,
    'online'
  );

  return agent;
}

function updateAgentStatuses() {
  const now = Date.now();

  for (const [agentId, agent] of agentRegistry.entries()) {
    const timeSinceSeen = now - agent.last_seen;

    if (agent.status === 'online' && timeSinceSeen > AGENT_OFFLINE_THRESHOLD) {
      agent.status = 'offline';
      db.prepare('UPDATE agents SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE agent_id = ?')
        .run('offline', agentId);
    } else if (agent.status === 'online' && timeSinceSeen > AGENT_STALE_THRESHOLD) {
      agent.status = 'stale';
      db.prepare('UPDATE agents SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE agent_id = ?')
        .run('stale', agentId);
    }
  }
}

function storeMetrics(payload) {
  const { agent_id, machine_name, ts, system, gpus = [], processes = [], services = [] } = payload;
  const host = machine_name;

  const stmtSys = db.prepare(`
    INSERT INTO sys_metrics (ts, host, cpu_load, cpu_temp, cpu_cores, ram_used, ram_total, disk_used, disk_total)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
  `);

  if (system) {
    stmtSys.run(
      ts,
      host,
      system.cpu_load || 0,
      system.cpu_temp || 0,
      system.cpu_cores || 0,
      system.ram_used || 0,
      system.ram_total || 0,
      system.disk_used || 0,
      system.disk_total || 0
    );
  }

  const stmtGpu = db.prepare(`
    INSERT INTO gpu_metrics (ts, host, gpu_idx, gpu_name, vram_used, vram_total, temp, load, power, fan, models_json)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  `);

  for (const gpu of gpus) {
    stmtGpu.run(
      ts,
      host,
      gpu.idx || 0,
      gpu.name || 'Unknown',
      gpu.vram_used || 0,
      gpu.vram_total || 0,
      gpu.temp || 0,
      gpu.load || 0,
      gpu.power || 0,
      gpu.fan || 0,
      JSON.stringify(gpu.models || [])
    );
  }

  const stmtProc = db.prepare(`
    INSERT INTO proc_snapshots (ts, host, procs_json)
    VALUES (?, ?, ?)
  `);

  if (processes.length > 0) {
    stmtProc.run(ts, host, JSON.stringify(processes));
  }

  const stmtLlm = db.prepare(`
    INSERT INTO llm_metrics (ts, endpoint, model, status, slot_state, prompt_progress, tok_pred, tok_eval, speed_gen, speed_pp, kv_usage, queue)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  `);

  const stmtRun = db.prepare(`
    INSERT INTO llm_runs (ts, endpoint, model, slot_idx, tok_generated, tok_prompt, speed_gen, speed_pp, t_generation_ms, t_prompt_ms, t_total_ms)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  `);

  for (const service of services) {
    if (service.type === 'llm') {
      const endpoint = service.endpoint || service.name || 'unknown';

      // Track service uptime
      const currentStatus = service.status || 'unknown';
      const prev = serviceUptime.get(endpoint);
      if (!prev) {
        serviceUptime.set(endpoint, { since: ts, status: currentStatus });
      } else if (prev.status === 'down' && currentStatus !== 'down') {
        serviceUptime.set(endpoint, { since: ts, status: currentStatus });
      } else {
        prev.status = currentStatus;
      }

      // Store live status metrics (for timeline/graphs)
      const isActive = currentStatus === 'generating';
      stmtLlm.run(
        ts,
        endpoint,
        service.model || 'unknown',
        currentStatus,
        service.slot || 'idle',
        service.gen_progress != null ? Math.round(service.gen_progress * 100) : 0,
        isActive ? (service.tok_pred || 0) : 0,
        0, // tok_eval no longer tracked via cumulative
        isActive ? (service.speed_gen || 0) : 0,
        isActive ? (service.speed_pp || 0) : 0,
        service.kv_usage || 0,
        service.queue || 0
      );

      // Store completed generation runs (exact per-request data from slot transitions)
      const completedRuns = service.completed_runs || [];
      for (const run of completedRuns) {
        stmtRun.run(
          run.ts || ts,
          endpoint,
          run.model || service.model || 'unknown',
          run.slot_idx || 0,
          run.tok_generated || 0,
          run.tok_prompt || 0,
          run.speed_gen || 0,
          run.speed_pp || 0,
          run.t_generation_ms || 0,
          run.t_prompt_ms || 0,
          run.t_total_ms || 0
        );
        console.log(`[${new Date().toISOString()}] Stored run: ${endpoint} slot${run.slot_idx} — ${run.tok_generated} tok @ ${run.speed_gen} tok/s, gen ${(run.t_generation_ms/1000).toFixed(1)}s`);
      }
    }
  }
}

function buildLatestSnapshot() {
  const snapshot = {
    ts: Date.now(),
    agents: {},
  };

  for (const [agentId, agent] of agentRegistry.entries()) {
    // Use the latest in-memory payload for real-time data (preserves all nested fields)
    const payload = latestPayloads.get(agentId);

    // Enrich services with uptime info
    const enrichedServices = (payload?.services || []).map(s => {
      const ut = serviceUptime.get(s.endpoint);
      return { ...s, service_since: ut ? ut.since : null };
    });

    snapshot.agents[agentId] = {
      agent_id: agent.agent_id,
      machine_name: agent.machine_name,
      ip: agent.ip,
      status: agent.status,
      agent_version: agent.agent_version || 'unknown',
      first_seen: agent.first_seen,
      last_seen: agent.last_seen,
      system: payload?.system || null,
      gpus: payload?.gpus || [],
      processes: payload?.processes || [],
      services: enrichedServices,
      containers: payload?.containers || [],
    };
  }

  latestSnapshot = snapshot;
  return snapshot;
}

function broadcastToClients(data) {
  const message = JSON.stringify(data);
  for (const client of wsClients) {
    if (client.readyState === WebSocket.OPEN) {
      client.send(message, (err) => {
        if (err) {
          wsClients.delete(client);
        }
      });
    }
  }
}

app.post('/api/ingest', (req, res) => {
  try {
    const payload = req.body;
    const { agent_id, machine_name, ts, system, gpus, processes, services, client_ip, agent_version } = payload;

    if (!agent_id || !machine_name) {
      return res.status(400).json({ error: 'Missing agent_id or machine_name' });
    }

    // Prefer client_ip from agent payload (real LAN IP), fallback to req.ip
    let clientIp = client_ip;
    if (!clientIp) {
      clientIp = req.ip || req.connection.remoteAddress || 'unknown';
      // Strip IPv6-mapped IPv4 prefix (::ffff:) from Docker networking
      if (clientIp.startsWith('::ffff:')) clientIp = clientIp.slice(7);
    }

    const capabilities = [];
    if (system) capabilities.push('system');
    if (gpus && gpus.length > 0) capabilities.push('gpu');
    if (processes && processes.length > 0) capabilities.push('processes');
    if (services && services.length > 0) capabilities.push('services');

    const serviceConfigs = (services || []).map((s) => ({
      type: s.type,
      name: s.name,
      endpoint: s.endpoint,
      gpu_indices: s.gpu_indices || [],
    }));

    registerAgent(agent_id, machine_name, clientIp, capabilities, serviceConfigs, agent_version);
    latestPayloads.set(agent_id, payload);
    storeMetrics(payload);
    buildLatestSnapshot();
    broadcastToClients(latestSnapshot);

    // Check for pending commands for this agent
    const commands = pendingCommands.get(agent_id) || [];
    if (commands.length > 0) {
      console.log(`[${new Date().toISOString()}] Sending ${commands.length} command(s) to ${agent_id}: ${JSON.stringify(commands.map(c=>c.type))}`);
      pendingCommands.delete(agent_id); // Consume after logging
    }

    res.json({ status: 'ok', agent_id, commands });
  } catch (err) {
    console.error('Error in /api/ingest:', err);
    res.status(500).json({ error: err.message });
  }
});

app.get('/api/discover', (req, res) => {
  res.json({
    service: 'dohtar-monitor',
    version: '2.0.0',
    ingest: '/api/ingest',
    ts: Date.now(),
  });
});

app.get('/api/agents', (req, res) => {
  updateAgentStatuses();
  const agents = Array.from(agentRegistry.values());
  res.json(agents);
});

app.get('/api/latest', (req, res) => {
  updateAgentStatuses();
  const snapshot = buildLatestSnapshot();
  res.json(snapshot);
});

app.get('/api/history/:host/:metric', (req, res) => {
  try {
    const { host, metric } = req.params;
    const hours = parseInt(req.query.hours || '24', 10);
    const since = Date.now() - hours * 3600 * 1000;

    let query;
    if (metric === 'system' || metric === 'sys') {
      query = db.prepare(
        `SELECT ts, cpu_load, cpu_temp, cpu_cores, ram_used, ram_total, disk_used, disk_total
         FROM sys_metrics WHERE host = ? AND ts > ? ORDER BY ts ASC`
      );
    } else if (metric === 'gpu') {
      query = db.prepare(
        `SELECT ts, gpu_idx, gpu_name, vram_used, vram_total, temp, load, power, fan
         FROM gpu_metrics WHERE host = ? AND ts > ? ORDER BY ts ASC, gpu_idx ASC`
      );
    } else {
      return res.status(400).json({ error: 'Unknown metric type' });
    }

    const data = query.all(host, since);
    res.json({ metric, host, hours, data });
  } catch (err) {
    console.error('Error in /api/history:', err);
    res.status(500).json({ error: err.message });
  }
});

app.get('/api/history/llm/:endpoint', (req, res) => {
  try {
    const { endpoint } = req.params;
    const hours = parseInt(req.query.hours || '24', 10);
    const since = Date.now() - hours * 3600 * 1000;

    const data = db.prepare(
      `SELECT ts, model, status, slot_state, prompt_progress, tok_pred, tok_eval, speed_gen, speed_pp, kv_usage, queue
       FROM llm_metrics WHERE endpoint = ? AND ts > ? ORDER BY ts ASC`
    ).all(endpoint, since);

    res.json({ endpoint, hours, data });
  } catch (err) {
    console.error('Error in /api/history/llm:', err);
    res.status(500).json({ error: err.message });
  }
});

// Last N generation runs for an LLM endpoint — exact per-request data from slot transitions
app.get('/api/history/llm/:endpoint/runs', (req, res) => {
  try {
    const { endpoint } = req.params;
    const limit = Math.min(parseInt(req.query.limit || '5', 10), 50);

    const runs = db.prepare(`
      SELECT ts, model, slot_idx, tok_generated, tok_prompt,
             speed_gen, speed_pp, t_generation_ms, t_prompt_ms, t_total_ms
      FROM llm_runs
      WHERE endpoint = ?
      ORDER BY ts DESC
      LIMIT ?
    `).all(endpoint, limit);

    res.json({ endpoint, runs });
  } catch (err) {
    console.error('Error in /api/history/llm/runs:', err);
    res.status(500).json({ error: err.message });
  }
});

function startCleanupTasks() {
  setInterval(() => {
    updateAgentStatuses();
  }, 5000);

  setInterval(() => {
    const retentionMs = METRICS_RETENTION_DAYS * 24 * 3600 * 1000;
    const cutoff = Date.now() - retentionMs;

    db.prepare('DELETE FROM gpu_metrics WHERE ts < ?').run(cutoff);
    db.prepare('DELETE FROM sys_metrics WHERE ts < ?').run(cutoff);
    db.prepare('DELETE FROM llm_metrics WHERE ts < ?').run(cutoff);
    db.prepare('DELETE FROM llm_runs WHERE ts < ?').run(cutoff);

    const procRetentionMs = PROC_RETENTION_DAYS * 24 * 3600 * 1000;
    const procCutoff = Date.now() - procRetentionMs;
    db.prepare('DELETE FROM proc_snapshots WHERE ts < ?').run(procCutoff);

    console.log(`[${new Date().toISOString()}] Cleanup: removed metrics older than ${METRICS_RETENTION_DAYS}d and procs older than ${PROC_RETENTION_DAYS}d`);
  }, 3600000);
}

function startUdpDiscoveryListener() {
  const server = dgram.createSocket('udp4');

  server.on('message', (msg, rinfo) => {
    try {
      const message = msg.toString('utf8');
      if (message.includes('dohtar-monitor')) {
        const response = JSON.stringify({
          service: 'dohtar-monitor',
          version: '2.0.0',
          backend_address: process.env.BACKEND_ADDRESS || 'localhost',
          backend_port: PORT,
          ingest_endpoint: '/api/ingest',
          discover_endpoint: '/api/discover',
          ts: Date.now(),
        });
        server.send(response, 0, response.length, rinfo.port, rinfo.address, (err) => {
          if (err) console.error('UDP response error:', err);
        });
      }
    } catch (err) {
      console.error('UDP listener error:', err);
    }
  });

  server.on('error', (err) => {
    console.error('UDP server error:', err);
  });

  server.bind(DISCOVERY_PORT, () => {
    console.log(`UDP discovery listener started on port ${DISCOVERY_PORT}`);
  });
}

function startWebSocketServer(httpServer) {
  const wss = new WebSocket.Server({ server: httpServer });

  wss.on('connection', (ws) => {
    wsClients.add(ws);
    updateAgentStatuses();
    const snapshot = buildLatestSnapshot();
    ws.send(JSON.stringify(snapshot), (err) => {
      if (err) {
        wsClients.delete(ws);
      }
    });

    ws.on('close', () => {
      wsClients.delete(ws);
    });

    ws.on('error', (err) => {
      console.error('WebSocket error:', err);
      wsClients.delete(ws);
    });
  });

  console.log(`WebSocket server started on port ${PORT}`);
}

const httpServer = http.createServer(app);

app.get('/health', (req, res) => {
  res.json({ status: 'ok', ts: Date.now(), agents: agentRegistry.size });
});

// API info (only if Accept: application/json, otherwise serve SPA)
app.get('/api', (req, res) => {
  res.json({
    service: 'dohtar-monitor-backend',
    version: '2.0.0',
    endpoints: {
      discover: '/api/discover',
      ingest: 'POST /api/ingest',
      agents: '/api/agents',
      latest: '/api/latest',
      history: 'GET /api/history/:host/:metric',
      llm_history: 'GET /api/history/llm/:endpoint',
      health: '/health',
    },
  });
});

// Agent version info — agents check this to know if update is available
app.get('/api/agent-version', (req, res) => {
  // Read version from bundled agent files (check Docker path first, then dev path)
  try {
    const candidates = [
      path.join(__dirname, 'agent', 'version.py'),
      path.join(__dirname, '..', 'agent', 'version.py'),
    ];
    const versionFile = candidates.find(f => fs.existsSync(f));
    if (versionFile) {
      const content = fs.readFileSync(versionFile, 'utf8');
      const match = content.match(/AGENT_VERSION\s*=\s*["']([^"']+)["']/);
      if (match) {
        return res.json({ version: match[1] });
      }
    }
    res.json({ version: 'unknown' });
  } catch (err) {
    res.json({ version: 'unknown' });
  }
});

// Trigger agent update — sends update command to specific agent or all agents
app.post('/api/agent-update', (req, res) => {
  try {
    const { agent_id } = req.body || {};
    const updateCmd = { type: 'update' };

    if (agent_id) {
      // Update specific agent
      if (!agentRegistry.has(agent_id)) {
        return res.status(404).json({ error: 'Agent not found' });
      }
      const existing = pendingCommands.get(agent_id) || [];
      existing.push(updateCmd);
      pendingCommands.set(agent_id, existing);
      console.log(`[${new Date().toISOString()}] Update queued for agent: ${agent_id}`);
      res.json({ status: 'ok', queued: [agent_id] });
    } else {
      // Update ALL online agents
      const queued = [];
      for (const [id, agent] of agentRegistry.entries()) {
        if (agent.status === 'online') {
          const existing = pendingCommands.get(id) || [];
          existing.push(updateCmd);
          pendingCommands.set(id, existing);
          queued.push(id);
        }
      }
      console.log(`[${new Date().toISOString()}] Update queued for ${queued.length} agents`);
      res.json({ status: 'ok', queued });
    }
  } catch (err) {
    console.error('Error in /api/agent-update:', err);
    res.status(500).json({ error: err.message });
  }
});

// Restart agent — sends restart command
app.post('/api/agent-restart', (req, res) => {
  try {
    const { agent_id } = req.body || {};
    const restartCmd = { type: 'restart' };

    if (agent_id) {
      if (!agentRegistry.has(agent_id)) {
        return res.status(404).json({ error: 'Agent not found' });
      }
      const existing = pendingCommands.get(agent_id) || [];
      existing.push(restartCmd);
      pendingCommands.set(agent_id, existing);
      res.json({ status: 'ok', queued: [agent_id] });
    } else {
      const queued = [];
      for (const [id, agent] of agentRegistry.entries()) {
        if (agent.status === 'online') {
          const existing = pendingCommands.get(id) || [];
          existing.push(restartCmd);
          pendingCommands.set(id, existing);
          queued.push(id);
        }
      }
      res.json({ status: 'ok', queued });
    }
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Agent download — serves agent/ directory as a zip
app.get('/api/download-agent', (req, res) => {
  // Try multiple possible locations for agent files
  // In Docker: /app/agent/ (COPY agent/ ./agent/)
  // Dev mode: ../agent/ (relative to backend/)
  const candidates = [
    path.join(__dirname, 'agent'),        // /app/agent/ in Docker
    path.join(__dirname, '..', 'agent'),  // ../agent/ in dev
  ];
  const agentDir = candidates.find(d => fs.existsSync(d));
  if (!agentDir) {
    console.error(`Agent files not found. Checked: ${candidates.join(', ')}`);
    return res.status(404).json({ error: 'Agent files not found on server' });
  }
  console.log(`[${new Date().toISOString()}] Serving agent zip from: ${agentDir}`);

  const archiver = require('archiver');
  res.setHeader('Content-Type', 'application/zip');
  res.setHeader('Content-Disposition', 'attachment; filename="dohtar-agent.zip"');

  const archive = archiver('zip', { zlib: { level: 9 } });
  archive.on('error', (err) => res.status(500).json({ error: err.message }));
  archive.pipe(res);
  archive.directory(agentDir, 'dohtar-agent');
  archive.finalize();
});

// SPA fallback
app.get('*', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

httpServer.listen(PORT, () => {
  console.log(`[${new Date().toISOString()}] Dohtar Monitor Backend v2.0.0 listening on port ${PORT}`);
  console.log(`  API: http://localhost:${PORT}/api/`);
  console.log(`  WebSocket: ws://localhost:${PORT}/`);
  console.log(`  Discovery: UDP port ${DISCOVERY_PORT}`);
});

startWebSocketServer(httpServer);
startUdpDiscoveryListener();

process.on('SIGINT', () => {
  console.log(`\n[${new Date().toISOString()}] Shutting down gracefully...`);
  httpServer.close(() => {
    db.close();
    console.log('Server closed');
    process.exit(0);
  });
});

module.exports = { app, httpServer };
