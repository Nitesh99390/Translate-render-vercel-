/**
 * EPUB Translator — Worker node for **Deno Deploy** (free plan, no card).
 * Re-uses the Cloudflare module verbatim: Deno speaks the same `fetch(request, env)` shape.
 *
 * Deploy:  https://dash.deno.com → New Project → link this GitHub repo →
 *          entry point: deno_worker.ts   (env var WORKER_SECRET optional)
 * Local:   deno run --allow-net --allow-env deno_worker.ts
 */
// @ts-ignore — plain JS module, no types
import worker from "./cloudflare/worker.js";

const env = {
  WORKER_SECRET: Deno.env.get("WORKER_SECRET") ?? "",
  MAX_ITEMS: Deno.env.get("MAX_ITEMS") ?? "400",
  MAX_CHARS: Deno.env.get("MAX_CHARS") ?? "30000",
};

Deno.serve(
  { port: Number(Deno.env.get("PORT") ?? "8000") },
  (req: Request) => worker.fetch(req, env),
);
