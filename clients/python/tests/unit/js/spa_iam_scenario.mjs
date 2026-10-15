// Escenario de `test_dashboard_spa_iam.py`: `api.js` contra un `fetch` falso.
import { webcrypto } from "node:crypto";
const calls = [];
const store = new Map();
globalThis.sessionStorage = {
  getItem: (k) => store.get(k) ?? null,
  setItem: (k, v) => store.set(k, v),
  removeItem: (k) => store.delete(k),
};
globalThis.location = { origin: "https://pagepage.lambda-url.us-east-1.on.aws", search: "?code=c&state=s", hash: "", pathname: "/" };
globalThis.history = { replaceState: () => {} };
store.set("rayito-dashboard-pkce", JSON.stringify({ verifier: "v", state: "s", hash: "" }));
const json = (status, body) => ({
  ok: status < 400, status, json: async () => body, clone() { return this; },
});
globalThis.fetch = async (url, init = {}) => {
  calls.push({
    url: String(url), headers: init.headers ?? {}, credentials: init.credentials,
    method: init.method ?? "GET", body: String(init.body ?? "").startsWith("{") ? JSON.parse(init.body) : null,
  });
  const target = init.headers?.["X-Amz-Target"];
  if (String(url) === "/config.json") {
    return json(200, { auth: { type: "cognito", domain: "https://login.example.com", clientId: "client-1", scope: "openid",
      [process.env.IAM_BLOCK ?? "iam"]: { region: "us-east-1", providerName: "cognito-idp.us-east-1.amazonaws.com/us-east-1_AbCdEfGhI",
        identityPoolId: "us-east-1:00000000-0000-0000-0000-000000000001", apiUrl: "https://apiapi.lambda-url.us-east-1.on.aws" } } });
  }
  if (String(url).endsWith("/oauth2/token")) {
    return json(200, { access_token: "ACCESS", id_token: "IDTOKEN", refresh_token: "R", expires_in: 3600 });
  }
  if (target === "AWSCognitoIdentityService.GetId") return json(200, { IdentityId: "us-east-1:id" });
  if (target === "AWSCognitoIdentityService.GetCredentialsForIdentity") {
    return json(200, { Credentials: { AccessKeyId: "ASIA012345678EXAMPLE", SecretKey: "secret", SessionToken: "tok", Expiration: Date.now() / 1000 + 3600 } });
  }
  const apiCalls = calls.filter((c) => c.url.includes("/api/v1")).length;
  if (process.env.FORBIDDEN === "iam" && apiCalls === 1) return json(403, { Message: "Forbidden" });
  if (process.env.FORBIDDEN === "api") return json(403, { error: { code: "forbidden", message: "Prohibido" } });
  return json(200, { sandboxes: [] });
};
const { detectMode } = await import(process.env.AUTH_MODULE);
const api = await import(process.env.API_MODULE);
await detectMode();
api.setAccount("Mi cuenta");
let failure = null;
try {
  await api.get("/sandboxes", { limit: 5 });
  await api.post("/sandboxes/microvm-1/kill", { confirm: true });
} catch (error) {
  failure = { name: error.constructor.name, status: error.status, code: error.code };
}
console.log(JSON.stringify({ calls, failure }));
