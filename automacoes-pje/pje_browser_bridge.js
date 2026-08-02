'use strict';

const fs = require('fs');
const net = require('net');
const os = require('os');
const path = require('path');
const {
  MAX_MESSAGE_BYTES,
  failure,
  success,
  validateRequest,
} = require('./pje/contracts');
const { createPJeOperations } = require('./pje/operations');

function resolveSocketPath(env = process.env) {
  const configured = String(env.PJE_BROWSER_BRIDGE_SOCKET || '').trim();
  if (!configured) {
    return path.join(os.tmpdir(), `pje-tjpa-browser-${process.getuid?.() ?? 'user'}.sock`);
  }
  if (!path.isAbsolute(configured)) {
    throw new Error('PJE_BROWSER_BRIDGE_SOCKET deve ser um caminho absoluto.');
  }
  const parent = path.dirname(configured);
  if (!fs.existsSync(parent) || !fs.statSync(parent).isDirectory()) {
    throw new Error(`Diretório do socket não existe: ${parent}`);
  }
  return configured;
}

const SOCKET_PATH = resolveSocketPath();

function safeRemoveStaleSocket(socketPath) {
  if (!fs.existsSync(socketPath)) return;
  const stat = fs.lstatSync(socketPath);
  if (!stat.isSocket() || (process.getuid && stat.uid !== process.getuid())) {
    throw new Error(`Recusando remover caminho não seguro: ${socketPath}`);
  }
  fs.unlinkSync(socketPath);
}

function createBridgeServer({
  execute = createPJeOperations(),
  socketPath = SOCKET_PATH,
} = {}) {
  const server = net.createServer((socket) => {
    socket.setEncoding('utf8');
    let buffer = '';

    socket.on('data', async (chunk) => {
      buffer += chunk;
      if (Buffer.byteLength(buffer, 'utf8') > MAX_MESSAGE_BYTES) {
        socket.end(`${JSON.stringify(failure('unknown', {
          code: 'INVALID_REQUEST',
          message: 'Mensagem excede 256 KiB.',
        }))}\n`);
        return;
      }
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        let requestId = 'unknown';
        let response;
        try {
          const raw = JSON.parse(line);
          requestId = raw.request_id || requestId;
          const request = validateRequest(raw);
          const data = await execute(request.command, request.payload, request.timeout_ms);
          response = success(request.request_id, data);
        } catch (error) {
          response = failure(requestId, error);
        }
        socket.write(`${JSON.stringify(response)}\n`);
      }
    });
  });

  async function start() {
    safeRemoveStaleSocket(socketPath);
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(socketPath, () => {
        server.off('error', reject);
        fs.chmodSync(socketPath, 0o600);
        resolve();
      });
    });
    return socketPath;
  }

  async function stop() {
    if (server.listening) {
      await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    }
    safeRemoveStaleSocket(socketPath);
  }

  return { server, socketPath, start, stop };
}

if (require.main === module) {
  const bridge = createBridgeServer();
  bridge.start().then((socketPath) => {
    console.error(`[pje-bridge] pronto em ${socketPath}`);
  }).catch((error) => {
    console.error(`[pje-bridge] falha: ${error.message}`);
    process.exit(1);
  });

  const shutdown = async () => {
    await bridge.stop().catch(() => {});
    process.exit(0);
  };
  process.once('SIGTERM', shutdown);
  process.once('SIGINT', shutdown);
}

module.exports = {
  SOCKET_PATH,
  createBridgeServer,
  resolveSocketPath,
  safeRemoveStaleSocket,
};
