/**
 * EPUB Translator — Worker node for **Cloudflare Workers** (free plan, no card).
 *
 * Same HTTP contract as app.py so the bot can use it interchangeably:
 *   GET  /            -> health JSON
 *   GET  /health      -> health JSON
 *   POST /translate   {"text_list":[...], "lang":"hi", "source":"auto"}
 *                     -> {"success":true,"translated":[...]}
 *
 * Optional secret: set `WORKER_SECRET` (wrangler secret put WORKER_SECRET) and the
 * bot must send header  X-Worker-Key: <secret>.
 *
 * Deploy:   cd cloudflare && npx wrangler login && npx wrangler deploy
 * URL:      https://epub-translate-worker.<your-subdomain>.workers.dev
 *
 * Free plan limits: 100 000 requests/day, 10 ms CPU per request (I/O wait is free —
 * the Google call is pure network time, so a batch costs ~1–3 ms CPU).
 */

const GOOGLE_URL = "https://translate.googleapis.com/translate_a/t";
const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";

// Date.now() is frozen at 0 during module evaluation on Workers — set on first request.
let started = 0;
const stats = { requests: 0, strings: 0, errors: 0 };

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });

function health(env) {
  if (!started) started = Date.now();
  return {
    status: "ok",
    platform: "cloudflare",
    proxy: false,
    uptime: Math.floor((Date.now() - started) / 1000),
    limits: { max_items: maxItems(env), max_chars: maxChars(env) },
    ...stats,
  };
}

const maxItems = (env) => parseInt(env.MAX_ITEMS || "400", 10);
const maxChars = (env) => parseInt(env.MAX_CHARS || "30000", 10);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function google(texts, lang, source) {
  const params = new URLSearchParams({ client: "gtx", sl: source || "auto", tl: lang });
  const body = new URLSearchParams();
  for (const t of texts) body.append("q", t);

  let lastErr = "unknown";
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const resp = await fetch(`${GOOGLE_URL}?${params}`, {
        method: "POST",
        headers: { "user-agent": UA, "content-type": "application/x-www-form-urlencoded" },
        body,
      });
      if (resp.status === 200) {
        const data = await resp.json();
        if (texts.length === 1 && Array.isArray(data) && data.length) {
          const first = data[0];
          return [typeof first === "string" ? first : String(first[0])];
        }
        if (Array.isArray(data) && data.length === texts.length) {
          return data.map((x) => (Array.isArray(x) ? String(x[0]) : String(x)));
        }
        lastErr = "unexpected response shape";
      } else if (resp.status === 429) {
        lastErr = "rate limited";
        await sleep(1500 * (attempt + 1));
        continue;
      } else {
        lastErr = `HTTP ${resp.status}`;
      }
    } catch (e) {
      lastErr = String(e && e.message ? e.message : e);
    }
    await sleep(500 * (attempt + 1));
  }
  throw new Error(lastErr);
}

export default {
  async fetch(request, env) {
    if (!started) started = Date.now();
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (request.method === "HEAD" && path === "/") return new Response(null, { status: 200 });
    if (request.method === "GET" && (path === "/" || path === "/health")) return json(health(env));

    if (request.method === "POST" && path === "/translate") {
      const secret = env.WORKER_SECRET || "";
      if (secret && request.headers.get("x-worker-key") !== secret) {
        return json({ detail: "unauthorized" }, 401);
      }
      let body;
      try {
        body = await request.json();
      } catch {
        return json({ detail: "invalid json" }, 422);
      }
      const texts = Array.isArray(body.text_list) ? body.text_list.map(String) : [];
      const lang = body.lang || "hi";
      const source = body.source || "auto";
      if (!texts.length) return json({ success: true, translated: [] });
      const total = texts.reduce((n, t) => n + t.length, 0);
      if (texts.length > maxItems(env) || total > maxChars(env)) {
        return json({ detail: "batch too large" }, 413);
      }
      stats.requests++;
      stats.strings += texts.length;
      try {
        const translated = await google(texts, lang, source);
        return json({ success: true, translated });
      } catch (e) {
        stats.errors++;
        return json({ success: false, error: String(e.message || e) });
      }
    }

    return json({ detail: "not found" }, 404);
  },
};
