## 1. Host (`agent_host`)

- [x] 1.1 `RestartPolicy`, `RestartBudget` and `Supervisor` in `process.py`;
      `http-app` and `hermes` use them (`availability()`, `app_crashloop`).
- [x] 1.2 `Service.acquire()` queue, `queue_max` and `queue_timeout_seconds` in
      `host.json`, `/ping` and `503` states.
- [x] 1.3 `json` protocol of `http-app` (`request_field`, `response_field`,
      `port_env`) and forwarding of the other routes.
- [x] 1.4 `drivers/node_callable.py`, `strands-ts` alias, `node_bridge.mjs`
      adapters (`strands`, `langgraph`, `vercel-ai`, `callable`), history store,
      HTTP/1.1 for the AWS SDK.

## 2. SDKs

- [x] 2.1 `_install_plan.py` and `install-plan.ts`; pins in `limits.json`.
- [x] 2.2 `CustomRuntime` (`package_manager`, `python_version`, `node_version`,
      `adapter`, `json` fields), `StrandsRuntime` Node options, `DeepAgents`
      `requirements` and `package_manager`, Hermes on the shared CPython.
- [x] 2.3 Registry option keys, project-file schema, shared vectors
      (`install-plans`, `node-adapters`, `framework-*`).
- [x] 2.4 CHANGELOG of both packages.

## 3. Verification

- [x] 3.1 Python and TypeScript unit tests; the host against a real Node.
- [x] 3.2 Real LangGraph.js, deepagents-js and AI SDK graphs through the bridge.
- [ ] 3.3 AWS acceptance on the release tree (outside this change's branch).
