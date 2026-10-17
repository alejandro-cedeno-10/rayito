import { serve } from "@hono/node-server";
import { Hono } from "hono";

const app = new Hono();
app.get("/status", (c) => c.json({ ok: true }));
app.post("/invoke", async (c) => c.json({ response: (await c.req.json()).prompt }));

serve({ fetch: app.fetch, port: 3000 });
