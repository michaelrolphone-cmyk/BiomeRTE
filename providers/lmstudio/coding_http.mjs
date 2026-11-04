// Authenticated localhost MCP transport for LM Studio's native agent loop.
// Filesystem operations use the official server; terminal operations retain
// BiomeRTE's durable command identity and completion evidence.
import { createServer as createHttpServer } from 'node:http';
import { readFileSync, realpathSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { timingSafeEqual } from 'node:crypto';
import { Readable } from 'node:stream';
import { finished } from 'node:stream/promises';
import { createRequire } from 'node:module';
import { WebStandardStreamableHTTPServerTransport } from './coding-deps/node_modules/@modelcontextprotocol/server/dist/index.mjs';
import { createServer as createFileServer } from './coding-deps/node_modules/@modelcontextprotocol/server-filesystem/dist/server.js';
const filesystemRequire = createRequire(new URL('./coding-deps/node_modules/@modelcontextprotocol/server-filesystem/package.json', import.meta.url));
const { z } = filesystemRequire('zod');

const root = join(homedir(), '.local/share/biome-lmstudio');
const cfg = JSON.parse(readFileSync(join(root, 'coding-config.json'), 'utf8'));
const workspace = realpathSync(cfg.workspace);
const port = cfg.mcp_port;
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid MCP port');
const authority = `127.0.0.1:${port}`;
const expectedAuth = Buffer.from('Bearer ' + readFileSync(join(root, 'mcp-token'), 'utf8').trim());

function terminal(name, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(join(homedir(), 'miniconda3/bin/python3'), [
      '-S', '-c', 'import json,sys; from coding_mcp import dispatch; print(json.dumps(dispatch(json.load(sys.stdin))))'
    ], { cwd: root, stdio: ['pipe', 'pipe', 'inherit'] });
    let output = '';
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', chunk => { output += chunk; });
    child.once('error', reject);
    child.once('close', code => {
      try {
        if (code !== 0) throw new Error(`Terminal bridge exited ${code}; inspect durable command records before retrying`);
        resolve(JSON.parse(output));
      } catch (error) { reject(error); }
    });
    child.stdin.end(JSON.stringify({ method: 'tools/call', params: { name, arguments: args } }));
  });
}

function createCodingServer() {
  const server = createFileServer([workspace]);
  server.registerTool('start_command', {
    description: 'Start a shell command in the coding workspace through BiomeRTE. Returns a durable operation ID. Use command_status until settled and inspect the real exit_code. Do not commit or push unless the task explicitly authorizes it.',
    inputSchema: z.object({ command: z.string().min(1), directory: z.string().optional().describe('Directory relative to the coding workspace, default .') })
  }, args => terminal('start_command', args));
  server.registerTool('command_status', {
    description: 'Read or advance a started command. Queued, claimed, uncertain, or nonzero exit is not success. Preserve this operation ID when retrying.',
    inputSchema: z.object({ operation: z.string() })
  }, args => terminal('command_status', args));
  return server;
}

const http = createHttpServer(async (req, res) => {
  const deny = (code, message) => { res.writeHead(code, { 'Content-Type': 'text/plain' }); res.end(message); };
  if (req.headers.host !== authority || (req.headers.origin && req.headers.origin !== `http://${authority}`)) return deny(403, 'Forbidden origin');
  const auth = Buffer.from(req.headers.authorization || '');
  if (auth.length !== expectedAuth.length || !timingSafeEqual(auth, expectedAuth)) return deny(401, 'Authentication required');
  if (req.url !== '/mcp') return deny(404, 'Not found');
  if (!['POST', 'GET', 'DELETE'].includes(req.method)) return deny(405, 'Method not allowed');
  // Stateless requests use a fresh SDK transport, so no stale session survives
  // a reconnect. Commands themselves remain durable in BiomeRTE.
  const server = createCodingServer();
  const transport = new WebStandardStreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
  try {
    await server.connect(transport);
    const body = req.method === 'POST' ? Readable.toWeb(req) : undefined;
    const request = new Request(`http://${authority}/mcp`, { method: req.method, headers: req.headers, body, ...(body ? { duplex: 'half' } : {}) });
    const response = await transport.handleRequest(request);
    res.writeHead(response.status, Object.fromEntries(response.headers));
    if (response.body) {
      Readable.fromWeb(response.body).pipe(res);
      await finished(res);
    } else res.end();
  } catch (error) {
    console.error('MCP request failed:', error.message);
    if (!res.headersSent) deny(500, 'MCP request failed; inspect command records before retrying');
    else res.destroy();
  } finally { await server.close(); }
});
http.listen(port, '127.0.0.1', () => console.log(`Biome coding MCP listening on http://${authority}/mcp`));
