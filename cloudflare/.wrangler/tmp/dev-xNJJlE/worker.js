var __defProp = Object.defineProperty;
var __name = (target, value) => __defProp(target, "name", { value, configurable: true });

// worker.js
var GOOGLE_URL = "https://translate.googleapis.com/translate_a/t";
var UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";
var started = 0;
var stats = { requests: 0, strings: 0, errors: 0 };
var json = /* @__PURE__ */ __name((obj, status = 200) => new Response(JSON.stringify(obj), {
  status,
  headers: { "content-type": "application/json; charset=utf-8" }
}), "json");
function health(env) {
  if (!started) started = Date.now();
  return {
    status: "ok",
    platform: "cloudflare",
    proxy: false,
    uptime: Math.floor((Date.now() - started) / 1e3),
    limits: { max_items: maxItems(env), max_chars: maxChars(env) },
    ...stats
  };
}
__name(health, "health");
var maxItems = /* @__PURE__ */ __name((env) => parseInt(env.MAX_ITEMS || "400", 10), "maxItems");
var maxChars = /* @__PURE__ */ __name((env) => parseInt(env.MAX_CHARS || "30000", 10), "maxChars");
var sleep = /* @__PURE__ */ __name((ms) => new Promise((r) => setTimeout(r, ms)), "sleep");
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
        body
      });
      if (resp.status === 200) {
        const data = await resp.json();
        if (texts.length === 1 && Array.isArray(data) && data.length) {
          const first = data[0];
          return [typeof first === "string" ? first : String(first[0])];
        }
        if (Array.isArray(data) && data.length === texts.length) {
          return data.map((x) => Array.isArray(x) ? String(x[0]) : String(x));
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
__name(google, "google");
var worker_default = {
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
  }
};

// ../../../../usr/local/lib/node_modules/wrangler/templates/middleware/middleware-ensure-req-body-drained.ts
var drainBody = /* @__PURE__ */ __name(async (request, env, _ctx, middlewareCtx) => {
  try {
    return await middlewareCtx.next(request, env);
  } finally {
    try {
      if (request.body !== null && !request.bodyUsed) {
        const reader = request.body.getReader();
        while (!(await reader.read()).done) {
        }
      }
    } catch (e) {
      console.error("Failed to drain the unused request body.", e);
    }
  }
}, "drainBody");
var middleware_ensure_req_body_drained_default = drainBody;

// ../../../../usr/local/lib/node_modules/wrangler/templates/middleware/middleware-miniflare3-json-error.ts
function reduceError(e) {
  return {
    name: e?.name,
    message: e?.message ?? String(e),
    stack: e?.stack,
    cause: e?.cause === void 0 ? void 0 : reduceError(e.cause)
  };
}
__name(reduceError, "reduceError");
var jsonError = /* @__PURE__ */ __name(async (request, env, _ctx, middlewareCtx) => {
  try {
    return await middlewareCtx.next(request, env);
  } catch (e) {
    const error = reduceError(e);
    const body = JSON.stringify(error);
    const headers = {
      "Content-Type": "application/json",
      "MF-Experimental-Error-Stack": "true"
    };
    const encoded = encodeURIComponent(body);
    if (encoded.length <= 8192) {
      headers["MF-Experimental-Error-Stack-Payload"] = encoded;
    }
    return new Response(body, { status: 500, headers });
  }
}, "jsonError");
var middleware_miniflare3_json_error_default = jsonError;

// .wrangler/tmp/bundle-r0EmEF/middleware-insertion-facade.js
var __INTERNAL_WRANGLER_MIDDLEWARE__ = [
  middleware_ensure_req_body_drained_default,
  middleware_miniflare3_json_error_default
];
var middleware_insertion_facade_default = worker_default;

// ../../../../usr/local/lib/node_modules/wrangler/templates/middleware/common.ts
var __facade_middleware__ = [];
function __facade_register__(...args) {
  __facade_middleware__.push(...args.flat());
}
__name(__facade_register__, "__facade_register__");
function __facade_invokeChain__(request, env, ctx, dispatch, middlewareChain) {
  const [head, ...tail] = middlewareChain;
  const middlewareCtx = {
    dispatch,
    next(newRequest, newEnv) {
      return __facade_invokeChain__(newRequest, newEnv, ctx, dispatch, tail);
    }
  };
  return head(request, env, ctx, middlewareCtx);
}
__name(__facade_invokeChain__, "__facade_invokeChain__");
function __facade_invoke__(request, env, ctx, dispatch, finalMiddleware) {
  return __facade_invokeChain__(request, env, ctx, dispatch, [
    ...__facade_middleware__,
    finalMiddleware
  ]);
}
__name(__facade_invoke__, "__facade_invoke__");

// .wrangler/tmp/bundle-r0EmEF/middleware-loader.entry.ts
var __Facade_ScheduledController__ = class ___Facade_ScheduledController__ {
  constructor(scheduledTime, cron, noRetry) {
    this.scheduledTime = scheduledTime;
    this.cron = cron;
    this.#noRetry = noRetry;
  }
  scheduledTime;
  cron;
  static {
    __name(this, "__Facade_ScheduledController__");
  }
  #noRetry;
  noRetry() {
    if (!(this instanceof ___Facade_ScheduledController__)) {
      throw new TypeError("Illegal invocation");
    }
    this.#noRetry();
  }
};
function wrapExportedHandler(worker) {
  if (__INTERNAL_WRANGLER_MIDDLEWARE__ === void 0 || __INTERNAL_WRANGLER_MIDDLEWARE__.length === 0) {
    return worker;
  }
  for (const middleware of __INTERNAL_WRANGLER_MIDDLEWARE__) {
    __facade_register__(middleware);
  }
  const fetchDispatcher = /* @__PURE__ */ __name(function(request, env, ctx) {
    if (worker.fetch === void 0) {
      throw new Error("Handler does not export a fetch() function.");
    }
    return worker.fetch(request, env, ctx);
  }, "fetchDispatcher");
  return {
    ...worker,
    fetch(request, env, ctx) {
      const dispatcher = /* @__PURE__ */ __name(function(type, init) {
        if (type === "scheduled" && worker.scheduled !== void 0) {
          const controller = new __Facade_ScheduledController__(
            Date.now(),
            init.cron ?? "",
            () => {
            }
          );
          return worker.scheduled(controller, env, ctx);
        }
      }, "dispatcher");
      return __facade_invoke__(request, env, ctx, dispatcher, fetchDispatcher);
    }
  };
}
__name(wrapExportedHandler, "wrapExportedHandler");
function wrapWorkerEntrypoint(klass) {
  if (__INTERNAL_WRANGLER_MIDDLEWARE__ === void 0 || __INTERNAL_WRANGLER_MIDDLEWARE__.length === 0) {
    return klass;
  }
  for (const middleware of __INTERNAL_WRANGLER_MIDDLEWARE__) {
    __facade_register__(middleware);
  }
  return class extends klass {
    #fetchDispatcher = /* @__PURE__ */ __name((request, env, ctx) => {
      this.env = env;
      this.ctx = ctx;
      if (super.fetch === void 0) {
        throw new Error("Entrypoint class does not define a fetch() function.");
      }
      return super.fetch(request);
    }, "#fetchDispatcher");
    #dispatcher = /* @__PURE__ */ __name((type, init) => {
      if (type === "scheduled" && super.scheduled !== void 0) {
        const controller = new __Facade_ScheduledController__(
          Date.now(),
          init.cron ?? "",
          () => {
          }
        );
        return super.scheduled(controller);
      }
    }, "#dispatcher");
    fetch(request) {
      return __facade_invoke__(
        request,
        this.env,
        this.ctx,
        this.#dispatcher,
        this.#fetchDispatcher
      );
    }
  };
}
__name(wrapWorkerEntrypoint, "wrapWorkerEntrypoint");
var WRAPPED_ENTRY;
if (typeof middleware_insertion_facade_default === "object") {
  WRAPPED_ENTRY = wrapExportedHandler(middleware_insertion_facade_default);
} else if (typeof middleware_insertion_facade_default === "function") {
  WRAPPED_ENTRY = wrapWorkerEntrypoint(middleware_insertion_facade_default);
}
var middleware_loader_entry_default = WRAPPED_ENTRY;
export {
  __INTERNAL_WRANGLER_MIDDLEWARE__,
  middleware_loader_entry_default as default
};
//# sourceMappingURL=worker.js.map
