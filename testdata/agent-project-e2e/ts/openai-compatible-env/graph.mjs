// Grafo del e2e de «modelo elegido por tu código» (ADR-033 D2), espejo del de
// Python: el único nodo no llama a ningún modelo y responde con booleanos y
// nombres, nunca con los valores de su configuración.
import { END, MessagesAnnotation, START, StateGraph } from "@langchain/langgraph";

// El `upstream` del proyecto: si la URL que ve el código no lo nombra, pasa por
// la pasarela de secretos de Rayito (MicroVM); en AgentCore es él mismo.
const UPSTREAM_HOST = "llm-gateway.example.com";
// El marcador de la clave que pone Rayito (`modelCredentialPlaceholder`).
const PLACEHOLDER_PREFIX = "placeholder";

// El host de la URL, comparado entero (no como subcadena); "" si no es una URL.
function hostOf(url) {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

function checks() {
  const baseUrl = process.env.LLM_BASE_URL ?? "";
  const apiKey = process.env.LLM_API_KEY ?? "";
  return {
    base_url_set: baseUrl !== "",
    base_url_is_gateway: baseUrl !== "" && hostOf(baseUrl) !== UPSTREAM_HOST,
    api_key_is_placeholder: apiKey.toLowerCase().startsWith(PLACEHOLDER_PREFIX),
    model_env: "LLM_MODEL",
  };
}

export const graph = new StateGraph(MessagesAnnotation)
  .addNode("answer", () => ({
    messages: [{ role: "assistant", content: JSON.stringify(checks()) }],
  }))
  .addEdge(START, "answer")
  .addEdge("answer", END)
  .compile();
