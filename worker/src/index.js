// Slack Events API 受信 → 署名検証 → GitHub App installation token 取得 → repository_dispatch
//
// 環境変数 (wrangler secret put で設定):
//   SLACK_SIGNING_SECRET  — Slack App の Signing Secret
//   GH_APP_PRIVATE_KEY    — GitHub App の Private Key (PEM 全体、BEGIN/END含む)
// 環境変数 (wrangler.toml [vars] で設定):
//   GH_REPO               — "mrmt/slacken"
//   GH_APP_ID             — GitHub App の App ID (数値文字列)
//   GH_INSTALLATION_ID    — GitHub App の Installation ID (数値文字列)
//   SLACK_CHANNEL_ID      — 対象チャンネルID (C...)。空文字なら全チャンネル通す

const encoder = new TextEncoder();

// ---------- 汎用暗号ユーティリティ ----------

async function hmacSha256Hex(secret, message) {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, encoder.encode(message));
  return [...new Uint8Array(sig)]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function base64UrlEncode(input) {
  // input: string または Uint8Array
  let bytes;
  if (typeof input === "string") {
    bytes = encoder.encode(input);
  } else {
    bytes = input;
  }
  let str = "";
  for (const b of bytes) str += String.fromCharCode(b);
  return btoa(str).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function pemToArrayBuffer(pem) {
  const b64 = pem
    .replace(/-----BEGIN [^-]+-----/g, "")
    .replace(/-----END [^-]+-----/g, "")
    .replace(/\s+/g, "");
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

// ---------- Slack 署名検証 ----------

async function verifySlackSignature(request, rawBody, signingSecret) {
  const ts = request.headers.get("x-slack-request-timestamp");
  const sig = request.headers.get("x-slack-signature");
  if (!ts || !sig) return false;
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - Number(ts)) > 300) return false;
  const base = `v0:${ts}:${rawBody}`;
  const mySig = "v0=" + (await hmacSha256Hex(signingSecret, base));
  return timingSafeEqual(mySig, sig);
}

// ---------- GitHub App JWT & installation token ----------

// Isolate ローカルキャッシュ (Worker isolate は複数走るが、各isolateで再利用できるだけで十分)
let cachedInstallationToken = null; // { token: string, expiresAt: number (unix sec) }

let cachedSigningKey = null; // CryptoKey

async function loadPrivateKey(pem) {
  if (cachedSigningKey) return cachedSigningKey;
  const keyBuf = pemToArrayBuffer(pem);
  cachedSigningKey = await crypto.subtle.importKey(
    "pkcs8",
    keyBuf,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return cachedSigningKey;
}

async function makeAppJwt(env) {
  const key = await loadPrivateKey(env.GH_APP_PRIVATE_KEY);
  const now = Math.floor(Date.now() / 1000);
  const header = base64UrlEncode(JSON.stringify({ alg: "RS256", typ: "JWT" }));
  // GitHub は iat の時計ずれを許容するため -60 秒、exp は最大10分だが余裕を持って 9 分
  const payload = base64UrlEncode(
    JSON.stringify({ iat: now - 60, exp: now + 9 * 60, iss: env.GH_APP_ID }),
  );
  const signingInput = `${header}.${payload}`;
  const sig = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    key,
    encoder.encode(signingInput),
  );
  return `${signingInput}.${base64UrlEncode(new Uint8Array(sig))}`;
}

async function getInstallationToken(env) {
  const now = Math.floor(Date.now() / 1000);
  // 30秒バッファで使い回す
  if (cachedInstallationToken && cachedInstallationToken.expiresAt - 30 > now) {
    return cachedInstallationToken.token;
  }
  const jwt = await makeAppJwt(env);
  const res = await fetch(
    `https://api.github.com/app/installations/${env.GH_INSTALLATION_ID}/access_tokens`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${jwt}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "slacken-worker",
      },
    },
  );
  if (!res.ok) {
    const body = await res.text();
    cachedInstallationToken = null;
    throw new Error(`installation token fetch failed: ${res.status} ${body}`);
  }
  const body = await res.json();
  cachedInstallationToken = {
    token: body.token,
    expiresAt: Math.floor(new Date(body.expires_at).getTime() / 1000),
  };
  return cachedInstallationToken.token;
}

// ---------- Dispatch ----------

async function dispatchGithub(env, event) {
  let token;
  try {
    token = await getInstallationToken(env);
  } catch (e) {
    console.error("get installation token failed", e.message);
    return;
  }
  const res = await fetch(
    `https://api.github.com/repos/${env.GH_REPO}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "slacken-worker",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        event_type: "slack_message",
        client_payload: { event },
      }),
    },
  );
  if (!res.ok) {
    console.error("gh dispatch failed", res.status, await res.text());
  }
}

// ---------- Event フィルタ ----------

function shouldDispatch(env, event) {
  if (!event || event.type !== "message") return false;
  const skipSubtypes = new Set([
    "channel_join",
    "channel_leave",
    "bot_message",
    "message_deleted",
  ]);
  if (event.subtype && skipSubtypes.has(event.subtype)) return false;
  if (event.bot_id) return false;
  if (env.SLACK_CHANNEL_ID && event.channel !== env.SLACK_CHANNEL_ID) return false;
  return true;
}

// ---------- fetch handler ----------

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("method not allowed", { status: 405 });
    }
    const rawBody = await request.text();

    if (!env.SLACK_SIGNING_SECRET) {
      return new Response("misconfigured", { status: 500 });
    }
    const ok = await verifySlackSignature(request, rawBody, env.SLACK_SIGNING_SECRET);
    if (!ok) return new Response("invalid signature", { status: 401 });

    let body;
    try {
      body = JSON.parse(rawBody);
    } catch {
      return new Response("bad json", { status: 400 });
    }

    if (body.type === "url_verification") {
      return new Response(body.challenge, {
        headers: { "content-type": "text/plain" },
      });
    }

    // Slack再送も処理する。初回配送が Worker cold start 等で3秒超え→retry扱いで
    // 握り潰されると当該メッセージが完全に取りこぼされる (Notion側は permalink upsert +
    // on-slack-event の concurrency: slacken-msg-${ts} で冪等・単一実行が担保されているため
    // 二重発火しても副作用なし)。ログだけ残す。
    const retryNum = request.headers.get("x-slack-retry-num");
    if (retryNum) {
      console.log("processing slack retry", retryNum, request.headers.get("x-slack-retry-reason"));
    }

    if (body.type === "event_callback" && shouldDispatch(env, body.event)) {
      // 3秒SLA遵守のため待たずに即200を返す
      ctx.waitUntil(dispatchGithub(env, body.event));
    }

    return new Response("ok");
  },
};
