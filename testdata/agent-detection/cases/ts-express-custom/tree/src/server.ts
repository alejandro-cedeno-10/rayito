import express from "express";

const app = express();
app.use(express.json());

app.get("/health", (_req, res) => res.json({ ok: true }));
app.post("/chat", (req, res) => res.json({ text: `eco: ${req.body.message}` }));

app.listen(process.env.PORT || 3000);
