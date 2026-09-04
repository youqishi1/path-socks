import { connect } from "cloudflare:sockets";

const WS_OPEN = 1;
const CONNECT_TIMEOUT_MS = 8000;
const HANDSHAKE_TIMEOUT_MS = 8000;
const MAX_PATH_LENGTH = 2048;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if ((request.headers.get("Upgrade") || "").toLowerCase() !== "websocket") {
      if (url.pathname === "/health") {
        return new Response("ok\n", { headers: { "cache-control": "no-store" } });
      }
      return new Response("Not Found\n", { status: 404 });
    }

    const prefix = "/proxyip=";
    if (!url.pathname.startsWith(prefix) || url.pathname.length > MAX_PATH_LENGTH) {
      return new Response("Invalid WebSocket path\n", { status: 400 });
    }

    let rawProxy;
    try {
      rawProxy = decodeURIComponent(url.pathname.slice(prefix.length));
    } catch {
      return new Response("Invalid path encoding\n", { status: 400 });
    }

    const proxy = parseSocks5(rawProxy);
    if (!proxy) {
      return new Response("SOCKS5 path required\n", { status: 400 });
    }

    return handleVlessWebSocket(request, env.UUIDS, proxy);
  },
};

function parseSocks5(value) {
  if (typeof value !== "string" || !/^socks5?:\/\//i.test(value)) return null;
  const schemeLength = value.indexOf("://") + 3;
  const rest = value.slice(schemeLength);
  const at = rest.lastIndexOf("@");
  let auth = "";
  let hostPort = rest;
  if (at >= 0) {
    auth = rest.slice(0, at);
    hostPort = rest.slice(at + 1);
  }

  let username = "";
  let password = "";
  if (auth) {
    const colon = auth.indexOf(":");
    username = colon >= 0 ? auth.slice(0, colon) : auth;
    password = colon >= 0 ? auth.slice(colon + 1) : "";
    try { username = decodeURIComponent(username); } catch {}
    try { password = decodeURIComponent(password); } catch {}
  }

  let host = "";
  let portText = "";
  if (hostPort.startsWith("[")) {
    const end = hostPort.indexOf("]");
    if (end < 0 || hostPort[end + 1] !== ":") return null;
    host = hostPort.slice(1, end);
    portText = hostPort.slice(end + 2);
  } else {
    const colon = hostPort.lastIndexOf(":");
    if (colon <= 0) return null;
    host = hostPort.slice(0, colon);
    portText = hostPort.slice(colon + 1);
  }

  const port = Number(portText);
  if (!host || !Number.isInteger(port) || port < 1 || port > 65535) return null;
  if (new TextEncoder().encode(username).length > 255) return null;
  if (new TextEncoder().encode(password).length > 255) return null;
  return { host, port, username, password };
}

async function handleVlessWebSocket(request, expectedUuids, proxy) {
  const pair = new WebSocketPair();
  const client = pair[0];
  const server = pair[1];
  server.accept();
  server.binaryType = "arraybuffer";

  const state = { remote: null, initialized: false, pending: new Uint8Array(0) };
  const earlyData = decodeEarlyData(request.headers.get("sec-websocket-protocol") || "");

  const input = new ReadableStream({
    start(controller) {
      server.addEventListener("message", async (event) => {
        try {
          let data = event.data;
          if (data instanceof Blob) data = await data.arrayBuffer();
          controller.enqueue(new Uint8Array(data));
        } catch (error) {
          controller.error(error);
        }
      });
      server.addEventListener("close", () => {
        try { controller.close(); } catch {}
        closeRemote(state.remote);
      });
      server.addEventListener("error", () => {
        try { controller.error(new Error("WebSocket error")); } catch {}
      });
      if (earlyData.byteLength) controller.enqueue(earlyData);
    },
    cancel() {
      closeRemote(state.remote);
      closeWebSocket(server);
    },
  });

  input.pipeTo(new WritableStream({
    async write(chunk) {
      if (state.initialized) {
        const writer = state.remote.writable.getWriter();
        try { await writer.write(chunk); } finally { writer.releaseLock(); }
        return;
      }

      state.pending = concatBytes(state.pending, chunk);
      const parsed = parseVlessHeader(state.pending, expectedUuids);
      if (parsed.incomplete) return;
      if (parsed.error) throw new Error(parsed.error);
      if (parsed.command !== 1) throw new Error("Only TCP is supported");

      const initialPayload = state.pending.slice(parsed.payloadOffset);
      state.pending = new Uint8Array(0);
      state.remote = await connectThroughSocks5(proxy, parsed.host, parsed.port, initialPayload);
      state.initialized = true;
      pipeRemoteToWebSocket(state.remote, server, new Uint8Array([parsed.version, 0]));
    },
    close() { closeRemote(state.remote); },
    abort() { closeRemote(state.remote); },
  })).catch(() => {
    closeRemote(state.remote);
    closeWebSocket(server);
  });

  return new Response(null, { status: 101, webSocket: client });
}

function parseVlessHeader(bytes, expectedUuids) {
  if (bytes.length < 18) return { incomplete: true };
  const version = bytes[0];
  const uuid = bytesToUuid(bytes.slice(1, 17));
  const allowed = String(expectedUuids).toLowerCase().split(",").map((item) => item.trim());
  if (!allowed.includes(uuid.toLowerCase())) return { error: "Invalid UUID" };
  const optionLength = bytes[17];
  const commandIndex = 18 + optionLength;
  if (bytes.length < commandIndex + 4) return { incomplete: true };
  const command = bytes[commandIndex];
  const port = (bytes[commandIndex + 1] << 8) | bytes[commandIndex + 2];
  const addressType = bytes[commandIndex + 3];
  let index = commandIndex + 4;
  let host;

  if (addressType === 1) {
    if (bytes.length < index + 4) return { incomplete: true };
    host = Array.from(bytes.slice(index, index + 4)).join(".");
    index += 4;
  } else if (addressType === 2) {
    if (bytes.length < index + 1) return { incomplete: true };
    const length = bytes[index++];
    if (bytes.length < index + length) return { incomplete: true };
    host = new TextDecoder().decode(bytes.slice(index, index + length));
    index += length;
  } else if (addressType === 3) {
    if (bytes.length < index + 16) return { incomplete: true };
    const groups = [];
    for (let i = 0; i < 16; i += 2) groups.push(((bytes[index + i] << 8) | bytes[index + i + 1]).toString(16));
    host = groups.join(":");
    index += 16;
  } else {
    return { error: "Invalid address type" };
  }

  if (!host || !port) return { error: "Invalid destination" };
  return { version, command, port, host, payloadOffset: index };
}

async function connectThroughSocks5(proxy, targetHost, targetPort, initialPayload) {
  const socket = connect({ hostname: proxy.host, port: proxy.port });
  await withTimeout(socket.opened, CONNECT_TIMEOUT_MS, "SOCKS5 connect timeout");
  const writer = socket.writable.getWriter();
  const reader = socket.readable.getReader();
  let buffered = new Uint8Array(0);
  const readExactly = async (length) => {
    while (buffered.length < length) {
      const result = await withTimeout(reader.read(), HANDSHAKE_TIMEOUT_MS, "SOCKS5 handshake timeout");
      if (result.done) throw new Error("SOCKS5 closed during handshake");
      buffered = concatBytes(buffered, new Uint8Array(result.value));
    }
    const output = buffered.slice(0, length);
    buffered = buffered.slice(length);
    return output;
  };
  try {
    const needsAuth = Boolean(proxy.username || proxy.password);
    await writer.write(needsAuth ? new Uint8Array([5, 2, 0, 2]) : new Uint8Array([5, 1, 0]));
    const method = await readExactly(2);
    if (method[0] !== 5 || method[1] === 255) throw new Error("SOCKS5 method rejected");

    if (method[1] === 2) {
      const user = new TextEncoder().encode(proxy.username);
      const pass = new TextEncoder().encode(proxy.password);
      const auth = new Uint8Array(3 + user.length + pass.length);
      auth[0] = 1;
      auth[1] = user.length;
      auth.set(user, 2);
      auth[2 + user.length] = pass.length;
      auth.set(pass, 3 + user.length);
      await writer.write(auth);
      const authReply = await readExactly(2);
      if (authReply[1] !== 0) throw new Error("SOCKS5 authentication failed");
    } else if (method[1] !== 0) {
      throw new Error("Unsupported SOCKS5 authentication");
    }

    await writer.write(buildSocksConnectRequest(targetHost, targetPort));
    const head = await readExactly(4);
    if (head[0] !== 5 || head[1] !== 0) throw new Error("SOCKS5 destination refused");
    let remain;
    if (head[3] === 1) remain = 6;
    else if (head[3] === 4) remain = 18;
    else if (head[3] === 3) {
      const lengthByte = await readExactly(1);
      remain = lengthByte[0] + 2;
    } else throw new Error("Invalid SOCKS5 reply");
    await readExactly(remain);

    if (initialPayload.byteLength) await writer.write(initialPayload);
    return socket;
  } catch (error) {
    try { socket.close(); } catch {}
    throw error;
  } finally {
    try { reader.releaseLock(); } catch {}
    try { writer.releaseLock(); } catch {}
  }
}

function buildSocksConnectRequest(host, port) {
  let address;
  let atyp;
  if (/^(?:\d{1,3}\.){3}\d{1,3}$/.test(host)) {
    const parts = host.split(".").map(Number);
    if (parts.some((value) => value < 0 || value > 255)) throw new Error("Invalid IPv4");
    atyp = 1;
    address = new Uint8Array(parts);
  } else if (host.includes(":")) {
    atyp = 4;
    address = ipv6ToBytes(host);
  } else {
    atyp = 3;
    const encoded = new TextEncoder().encode(host);
    if (!encoded.length || encoded.length > 255) throw new Error("Invalid domain");
    address = concatBytes(new Uint8Array([encoded.length]), encoded);
  }
  const packet = new Uint8Array(4 + address.length + 2);
  packet.set([5, 1, 0, atyp], 0);
  packet.set(address, 4);
  packet[packet.length - 2] = (port >> 8) & 255;
  packet[packet.length - 1] = port & 255;
  return packet;
}

function ipv6ToBytes(input) {
  const halves = input.split("::");
  if (halves.length > 2) throw new Error("Invalid IPv6");
  const left = halves[0] ? halves[0].split(":") : [];
  const right = halves.length === 2 && halves[1] ? halves[1].split(":") : [];
  const missing = 8 - left.length - right.length;
  if (missing < 0 || (halves.length === 1 && missing !== 0)) throw new Error("Invalid IPv6");
  const groups = halves.length === 2 ? [...left, ...Array(missing).fill("0"), ...right] : left;
  const result = new Uint8Array(16);
  groups.forEach((group, i) => {
    if (!/^[0-9a-f]{1,4}$/i.test(group || "0")) throw new Error("Invalid IPv6");
    const value = parseInt(group || "0", 16);
    result[i * 2] = value >> 8;
    result[i * 2 + 1] = value & 255;
  });
  return result;
}

async function pipeRemoteToWebSocket(socket, webSocket, responseHeader) {
  let first = true;
  try {
    await socket.readable.pipeTo(new WritableStream({
      write(chunk) {
        if (webSocket.readyState !== WS_OPEN) throw new Error("WebSocket closed");
        const bytes = new Uint8Array(chunk);
        if (first) {
          first = false;
          webSocket.send(concatBytes(responseHeader, bytes).buffer);
        } else {
          webSocket.send(bytes.buffer);
        }
      },
    }));
  } catch {}
  closeRemote(socket);
  closeWebSocket(webSocket);
}

function decodeEarlyData(value) {
  if (!value) return new Uint8Array(0);
  try {
    const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
    const binary = atob(normalized);
    return Uint8Array.from(binary, (char) => char.charCodeAt(0));
  } catch {
    return new Uint8Array(0);
  }
}

function bytesToUuid(bytes) {
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function concatBytes(a, b) {
  const result = new Uint8Array(a.length + b.length);
  result.set(a, 0);
  result.set(b, a.length);
  return result;
}

function withTimeout(promise, timeoutMs, message) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(message)), timeoutMs); }),
  ]).finally(() => clearTimeout(timer));
}

function closeRemote(socket) {
  try { socket?.close(); } catch {}
}

function closeWebSocket(socket) {
  try { if (socket.readyState === 1 || socket.readyState === 2) socket.close(); } catch {}
}
