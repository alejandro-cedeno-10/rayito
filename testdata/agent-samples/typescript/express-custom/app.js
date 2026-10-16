import { BedrockRuntimeClient, ConverseCommand } from "@aws-sdk/client-bedrock-runtime";
import express from "express";

const DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0";

const DEFAULT_PORT = 3000;
const client = new BedrockRuntimeClient({ region: process.env.AWS_REGION });
const app = express();
app.use(express.json());

app.get("/healthz", (_req, res) => {
  res.json({ status: "ok" });
});

app.post("/chat", async (req, res) => {
  const reply = await client.send(
    new ConverseCommand({
      modelId: process.env.MODEL_ID ?? DEFAULT_MODEL_ID,
      system: [{ text: "You answer frequently asked questions in one short sentence." }],
      messages: [{ role: "user", content: [{ text: String(req.body.prompt) }] }],
    }),
  );
  res.json({ text: reply.output.message.content[0].text });
});

app.listen(Number(process.env.PORT ?? DEFAULT_PORT), "127.0.0.1");
