// Network observations extend the registered controller row. The authenticated
// node and existing CONTROLLER_NODE_MAP remain the identity/routing authority.
const networkSchemas = new WeakMap();
const object = value => value && typeof value === 'object' && !Array.isArray(value);
const invalid = () => Object.assign(Error('Invalid controller network report'), {status:400});
const interfaceName = value => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,80}$/.test(value);

export async function controllerNetworkSchema(db) {
 let pending = networkSchemas.get(db);
 if (pending) return pending;
 pending = (async () => {
  const columns = await db.prepare('PRAGMA table_info(controller_nodes)').all();
  if (columns.results.some(column => column.name === 'network')) return;
  try {
   await db.prepare("ALTER TABLE controller_nodes ADD COLUMN network TEXT NOT NULL DEFAULT '{}'").run();
  } catch (error) {
   // Another Worker may have installed the additive column concurrently.
   const current = await db.prepare('PRAGMA table_info(controller_nodes)').all();
   if (!current.results.some(column => column.name === 'network')) throw error;
  }
 })();
 networkSchemas.set(db, pending);
 try { await pending; } catch (error) { networkSchemas.delete(db); throw error; }
}

function ipv4Scope(bytes) {
 const [a,b,c] = bytes;
 if (a === 10 || a === 172 && b >= 16 && b <= 31 || a === 192 && b === 168) return 'private';
 if (a === 169 && b === 254) return 'link_local';
 if (a === 100 && b >= 64 && b <= 127) return 'shared';
 if (a === 0 || a >= 240 || a === 192 && b === 0 && (c === 2 || c === 0 && ![9,10].includes(bytes[3])) ||
     a === 192 && b === 88 && c === 99 || a === 198 && (b === 18 || b === 19 || b === 51 && c === 100) ||
     a === 203 && b === 0 && c === 113) return 'special';
 return 'public';
}
function parseAddress(value) {
 if (typeof value !== 'string' || !value || value.length > 120 || value !== value.trim()) return null;
 const split = value.split('%');
 if (split.length > 2 || split.length === 2 && !interfaceName(split[1])) return null;
 const [bare,zone] = split;
 if (!bare.includes(':')) {
  if (zone || !/^(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}$/.test(bare)) return null;
  const bytes = bare.split('.').map(Number);
  if (bytes.some(byte => byte > 255) || bytes.every(byte => byte === 0) || bytes[0] === 127 ||
      bytes[0] >= 224 && bytes[0] <= 239 || bytes.every(byte => byte === 255)) return null;
  return {address:bytes.join('.'),family:'ipv4',scope:ipv4Scope(bytes),words:bytes,zone};
 }
 if (!/^[0-9a-fA-F:.]+$/.test(bare)) return null;
 let address;
 try {
  const url = new URL('http://[' + bare + ']/');
  if (!url.hostname.startsWith('[') || url.pathname !== '/') return null;
  address = url.hostname.slice(1,-1);
 } catch { return null; }
 const halves = address.split('::'), left = halves[0] ? halves[0].split(':') : [],
  right = halves.length > 1 && halves[1] ? halves[1].split(':') : [];
 const words = [...left,...Array(8-left.length-right.length).fill('0'),...right].map(word => parseInt(word,16));
 if (words.length !== 8 || words.every(word => word === 0) ||
     words.slice(0,7).every(word => word === 0) && words[7] === 1 || words[0] >>> 8 === 255) return null;
 let scope;
 if ((words[0] & 0xffc0) === 0xfe80) scope = 'link_local';
 else if ((words[0] & 0xfe00) === 0xfc00) scope = 'private';
 else if (words.slice(0,5).every(word => word === 0) && words[5] === 0xffff) {
  const mapped = [words[6] >>> 8,words[6] & 255,words[7] >>> 8,words[7] & 255];
  if (!parseAddress(mapped.join('.'))) return null;
  scope = ipv4Scope(mapped);
 } else {
  // IANA special-purpose registry (2025-10-09). More-specific public
  // exceptions override protocol space; documentation/transition space does
  // not become an ordinary public locator merely because it starts with 2/3.
  const publicException = words[0] === 0x64 && words[1] === 0xff9b && words.slice(2,6).every(word => word === 0) ||
   words[0] === 0x2001 && (words[1] === 1 && words.slice(2,7).every(word => word === 0) && [1,2,3].includes(words[7]) ||
    words[1] === 3 || words[1] === 4 && words[2] === 0x112 || [0x20,0x30].includes(words[1] & 0xfff0));
  const special = (words[0] & 0xe000) !== 0x2000 || words[0] === 0x2001 && (words[1] === 0xdb8 || words[1] < 0x200) ||
   words[0] === 0x2002 || words[0] === 0x3fff && words[1] < 0x1000;
  scope = publicException || !special ? 'public' : 'special';
 }
 if (zone && scope !== 'link_local') return null;
 return {address,family:'ipv6',scope,words,zone};
}
function prefixNetwork(ip, prefix) {
 const bits = ip.family === 'ipv4' ? 8 : 16;
 let remaining = prefix;
 const words = ip.words.map(word => {
  const kept = Math.min(bits,Math.max(0,remaining)); remaining -= bits;
  return kept === 0 ? 0 : word & ((2 ** bits - 1) << (bits-kept));
 });
 const address = ip.family === 'ipv4' ? words.join('.') :
  new URL('http://[' + words.map(word => word.toString(16)).join(':') + ']/').hostname.slice(1,-1);
 return address + '/' + prefix;
}
function snapshot(report) {
 if (!object(report) || report.version !== 1 || Object.keys(report).some(key => !['version','addresses'].includes(key)) ||
     !Array.isArray(report.addresses) || report.addresses.length > 128) throw invalid();
 const addresses = new Map();
 for (const row of report.addresses) {
  if (!object(row) || Object.keys(row).some(key => !['address','family','interface','scope','prefix_length','network','scope_id'].includes(key)) ||
      !interfaceName(row.interface)) throw invalid();
  const ip = parseAddress(row.address);
  if (!ip || row.family !== ip.family || ip.zone && ip.zone !== row.interface ||
      row.scope !== undefined && !['private','public','link_local','shared','special'].includes(row.scope)) throw invalid();
  const normalized = {address:ip.address + (ip.family === 'ipv6' && ip.scope === 'link_local' ? '%' + row.interface : ''),
   family:ip.family,interface:row.interface,scope:ip.scope};
  if (row.prefix_length !== undefined) {
   if (!Number.isInteger(row.prefix_length) || row.prefix_length < 0 || row.prefix_length > (ip.family === 'ipv4' ? 32 : 128)) throw invalid();
   normalized.prefix_length = row.prefix_length;
   normalized.network = prefixNetwork(ip,row.prefix_length);
  }
  if (row.network !== undefined && (typeof row.network !== 'string' || row.network.length > 128 || row.prefix_length === undefined)) throw invalid();
  if (row.scope_id !== undefined) {
   if (!Number.isInteger(row.scope_id) || row.scope_id < 1 || row.scope_id > 4294967295 ||
       ip.family !== 'ipv6' || ip.scope !== 'link_local') throw invalid();
   normalized.scope_id = row.scope_id;
  }
  addresses.set(normalized.interface + '|' + normalized.family + '|' + normalized.address, normalized);
 }
 return [...addresses.entries()].sort(([a],[b]) => a.localeCompare(b)).map(([,row]) => row);
}

// Return an expression/parameters for the same SQL UPDATE as heartbeat seen.
// Omitted metadata is preserved in SQL, not copied from a potentially stale
// authentication read. Unknown source headers never freshen an old observation.
export function controllerNetworkUpdate(req, payload, now) {
 let expression = "COALESCE(network,'{}')";
 const parameters = [];
 const observed = parseAddress(req.headers.get('CF-Connecting-IP'));
 if (observed && !observed.zone) {
  expression = "json_set(" + expression + ",'$.gateway_observed',json(?))";
  const proxy = observed.address === '2a06:98c0:3600::103' || !!req.headers.get('CF-Worker');
  parameters.push(JSON.stringify({address:proxy ? null : observed.address,
   ...(proxy ? {proxy_address:observed.address,via:'cloudflare_worker'} : {}),
   family:observed.family,scope:observed.scope,source:'CF-Connecting-IP',observed_at:now,
   purpose:proxy ? 'proxy_observation' : 'egress_observation',
   attribution:proxy ? 'unknown_controller_egress' : 'unverified_egress'}));
 }
 if (Object.hasOwn(payload,'network')) {
  if (payload.network === null) {
   expression = "json_set(" + expression + ",'$.self_report.available',json('false'))";
  } else {
   const addresses = snapshot(payload.network);
   expression = "json_set(" + expression + ",'$.self_report',json(?))";
   parameters.push(JSON.stringify({available:true,observed_at:now,addresses}));
  }
 }
 return {expression,parameters};
}

export function controllerNetworkRegistration(env, node, heartbeatSeconds, now=Date.now()/1000, routingEnabled=true) {
 let saved = {};
 try { const parsed = JSON.parse(node.network ?? '{}'); if (object(parsed)) saved = parsed; } catch {}
 const ttl = Number.isFinite(heartbeatSeconds) && heartbeatSeconds > 0 ? heartbeatSeconds : 0;
 const fresh = observed => !!(node.enabled && Number.isFinite(observed) && observed > 0 && ttl > 0 && observed > now-ttl && observed <= now);
 const self = object(saved.self_report) ? saved.self_report : {};
 const known = Array.isArray(self.addresses) ? self.addresses : [];
 const current = self.available === true && fresh(self.observed_at);
 const selfReport = {status:current ? 'current' : self.available === false ? 'unavailable' :
  Number.isFinite(self.observed_at) ? 'stale' : 'unreported',current,
  observed_at:Number.isFinite(self.observed_at) ? self.observed_at : null,
  expires_at:Number.isFinite(self.observed_at) && ttl ? self.observed_at+ttl : null,
  addresses:known};
 let observed = null;
 if (object(saved.gateway_observed) && parseAddress(saved.gateway_observed.address ?? saved.gateway_observed.proxy_address)) {
  const value = saved.gateway_observed;
  observed = {...value,current:fresh(value.observed_at),
   expires_at:Number.isFinite(value.observed_at) && ttl ? value.observed_at+ttl : null,
   inbound_reachable:null};
 }
 let identity = null;
 try {
  const matches = Object.entries(JSON.parse(env.CONTROLLER_NODE_MAP ?? '{}')).filter(([id,label]) =>
   /^[a-f0-9]{64}$/.test(id) && label === node.id);
  if (matches.length === 1) identity = matches[0][0];
 } catch {}
 let route = identity ? {controller:identity,transport:'outbound_gateway',
  ready:!!(routingEnabled && node.ready && fresh(node.seen))} : null;
 if (route && env.BIOME_OAUTH_ORIGIN) {
  try {
   const origin = new URL(env.BIOME_OAUTH_ORIGIN);
   if (origin.protocol === 'https:' && !origin.username && !origin.password && origin.pathname === '/' && !origin.search && !origin.hash)
    route = {...route,native_call:origin.origin + '/v1/call'};
  } catch {}
 }
 return {version:1,self_report:selfReport,gateway_observed:observed,route};
}
