/**
 * Los agentes de ejemplo de TypeScript (`testdata/agent-samples/`), uno de
 * Python y OpenCode desplegados en AWS real sin tocar su código
 * (`agent-zero-config`), cada uno dos veces: sin opciones y con las propias
 * (4096 MiB, 2 sesiones por VM, cola de 2, de 1 a 2 VMs con 1 caliente).
 * Espejo de `clients/python/tests/e2e/test_agent_zero_config.py`: lo que sólo
 * existe en este SDK (la detección, `deployAgent`, `AgentFleet`) se prueba
 * aquí; el guest y el host se prueban una vez, en Python.
 *
 * Variables, coste y tope: cabecera de `agent_zero_config_support.py`.
 */

import { randomBytes } from "node:crypto";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { decodeHandle } from "../../src/agent/scaling-domain.js";
import { AgentError, AgentFleet, AgentProject, deployAgent, Sandbox } from "../../src/index.js";
import { modelCredential, sandboxExposesKey } from "./agent-project-support.js";
import {
  BASE,
  BEDROCK_REGION,
  BEDROCK_SECRET,
  BUCKET,
  EXPECTED_ANSWER,
  GATEWAY_NAME,
  guard,
  LONG_PROMPT,
  ledgerAdd,
  PARALLEL_CALLS,
  POLL_MS,
  PROMPT,
  projectName,
  RECOVERY_TIMEOUT_MS,
  RUN_ID,
  type Sample,
  SUSPEND_WAIT_MS,
  selectedSamples,
  stagedSample,
  TEST_TIMEOUT_MS,
} from "./agent-zero-config-support.js";
import { e2eEnabled } from "./helpers.js";

const READY = e2eEnabled() && Boolean(BEDROCK_SECRET && BUCKET && BASE && RUN_ID);
const SAMPLES = selectedSamples();
const DEDICATED_SAMPLE = "typescript-strands-ts";
const CHILDREN_SCRIPT = [
  "import os",
  "def cmdline(pid):",
  "    try:",
  '        return open("/proc/%s/cmdline" % pid, "rb").read()',
  "    except OSError:",
  '        return b""',
  'host = [p for p in os.listdir("/proc") if p.isdigit() and b"agent_host" in cmdline(p)]',
  "kids = []",
  'for pid in os.listdir("/proc"):',
  "    if not pid.isdigit():",
  "        continue",
  "    try:",
  '        fields = open("/proc/%s/stat" % pid).read().rsplit(")", 1)[1].split()',
  "    except OSError:",
  "        continue",
  "    if fields[1] in host:",
  "        kids.append(pid)",
  'print(" ".join(kids))',
].join("\n");
const PING_SCRIPT =
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8088/ping', timeout=5).read().decode())";

function newToken(): string {
  return randomBytes(32).toString("base64url");
}

function vmOf(handle: string | undefined): string {
  const route = decodeHandle(handle ?? "");
  expect(route, String(handle)).toBeDefined();
  return (route as { sandboxId: string }).sandboxId;
}

function checkAnswer(sample: Sample, text: string): void {
  expect(text.trim()).not.toBe("");
  if (sample.toolUse) {
    expect(text).toContain(EXPECTED_ANSWER);
  }
}

describe.runIf(READY)("agent-zero-config en AWS real (Bedrock)", () => {
  const fleets: AgentFleet[] = [];
  let startedAt = Date.now();

  afterEach(async () => {
    const seconds = (Date.now() - startedAt) / 1000;
    for (const fleet of fleets.splice(0)) {
      ledgerAdd({
        kind: "vm",
        seconds: seconds * Math.max(1, fleet.sandboxIds.length),
        memory_mib: 2048,
      });
      await fleet.stop().catch(() => undefined);
    }
    startedAt = Date.now();
  });

  async function deploy(directory: string): Promise<[AgentFleet, string]> {
    guard();
    ledgerAdd({ kind: "build" });
    const accessToken = newToken();
    const fleet = await deployAgent(directory, {
      options: { bucket: BUCKET, secretNames: { [GATEWAY_NAME]: BEDROCK_SECRET }, accessToken },
      region: BEDROCK_REGION,
    });
    fleets.push(fleet);
    return [fleet, accessToken];
  }

  async function liveOf(name: string): Promise<string[]> {
    const live: string[] = [];
    for await (const item of Sandbox.list({ template: name, region: BEDROCK_REGION })) {
      live.push(item.sandboxId);
    }
    return live;
  }

  it.for(SAMPLES.map((sample) => [sample.id, sample] as const))(
    "sin opciones: %s",
    { timeout: TEST_TIMEOUT_MS },
    async ([, sample]) => {
      const root = join(tmpdir(), `zc-ts-${randomBytes(4).toString("hex")}`);
      const directory = stagedSample(sample, root);
      const [, report] = await AgentProject.detect(directory, undefined, {
        region: BEDROCK_REGION,
      });
      expect(report).toBeDefined();
      expect([report?.framework, report?.kind, report?.protocol]).toEqual([
        sample.expected.framework,
        sample.expected.kind,
        sample.expected.protocol,
      ]);
      const [fleet, accessToken] = await deploy(directory);
      expect((await fleet.status()).healthy).toBe(1);
      checkAnswer(sample, (await fleet.invoke(PROMPT)).text);
      const sandbox = await Sandbox.connect(fleet.sandboxIds[0] as string, {
        accessToken,
        region: BEDROCK_REGION,
      });
      expect(await sandboxExposesKey(sandbox, await modelCredential(BEDROCK_REGION))).toEqual([]);
      await fleet.stop();
      fleets.splice(fleets.indexOf(fleet), 1);
      expect(await liveOf(projectName(sample, ""))).toEqual([]);
    },
  );

  it.for(SAMPLES.map((sample) => [sample.id, sample] as const))(
    "con opciones propias: %s",
    { timeout: TEST_TIMEOUT_MS },
    async ([, sample]) => {
      const root = join(tmpdir(), `zc-ts-${randomBytes(4).toString("hex")}`);
      const [fleet, accessToken] = await deploy(stagedSample(sample, root, "c"));
      const status = await fleet.status();
      expect([status.minVms, status.maxVms, status.warmVms]).toEqual([1, 2, 1]);
      const first = await fleet.invoke(PROMPT);
      checkAnswer(sample, first.text);

      const replies = await Promise.all(
        Array.from({ length: PARALLEL_CALLS }, () => fleet.invoke(LONG_PROMPT)),
      );
      expect(replies.every((reply) => reply.text.trim() !== "")).toBe(true);
      expect(new Set(replies.map((reply) => vmOf(reply.sessionId))).size).toBeGreaterThanOrEqual(2);

      const direct = fleet.deployments[0];
      const outcomes = await Promise.all(
        Array.from({ length: PARALLEL_CALLS }, () =>
          direct?.invoke(LONG_PROMPT).then(
            () => "text",
            (error: unknown) => {
              if (error instanceof AgentError && error.reason === "busy") return "busy";
              throw error;
            },
          ),
        ),
      );
      expect(outcomes).toContain("busy");
      expect(outcomes).toContain("text");

      const rebuilt = await AgentFleet.connect(fleet.name, {
        accessToken,
        connection: { region: BEDROCK_REGION },
      });
      const again = await rebuilt.invoke(PROMPT, { sessionId: first.sessionId });
      expect(vmOf(again.sessionId)).toBe(vmOf(first.sessionId));

      await assertRestartsADeadApp(fleet, accessToken, sample);
      await assertSuspendsAndResumes(fleet, sample);
      await fleet.stop();
      fleets.splice(fleets.indexOf(fleet), 1);
      expect(await liveOf(projectName(sample, "c"))).toEqual([]);
    },
  );

  it("las sesiones dedicadas tienen su propio VM", {
    timeout: TEST_TIMEOUT_MS,
  }, async (context) => {
    const sample = SAMPLES.find((entry) => entry.id === DEDICATED_SAMPLE);
    if (sample === undefined) {
      context.skip();
      return;
    }
    const root = join(tmpdir(), `zc-ts-${randomBytes(4).toString("hex")}`);
    const [fleet] = await deploy(stagedSample(sample, root, "d"));
    const first = await fleet.invoke(PROMPT);
    const second = await fleet.invoke(PROMPT);
    expect(vmOf(first.sessionId)).not.toBe(vmOf(second.sessionId));
    const again = await fleet.invoke("repite", { sessionId: first.sessionId });
    expect(vmOf(again.sessionId)).toBe(vmOf(first.sessionId));
  });

  async function assertRestartsADeadApp(
    fleet: AgentFleet,
    accessToken: string,
    sample: Sample,
  ): Promise<void> {
    const sandbox = await Sandbox.connect(fleet.sandboxIds[0] as string, {
      accessToken,
      region: BEDROCK_REGION,
    });
    const found = await sandbox.commands.run(`python3 -c ${JSON.stringify(CHILDREN_SCRIPT)}`);
    const children = found.stdout.split(/\s+/).filter(Boolean);
    if (children.length === 0) {
      return;
    }
    for (const pid of children) {
      await sandbox.commands.run(`python3 -c 'import os,signal; os.kill(${pid}, signal.SIGKILL)'`);
    }
    const deadline = Date.now() + RECOVERY_TIMEOUT_MS;
    let healthy = false;
    while (!healthy && Date.now() < deadline) {
      const ping = await sandbox.commands
        .run(`python3 -c ${JSON.stringify(PING_SCRIPT)}`)
        .catch(() => undefined);
      healthy = ping?.stdout.includes('"Healthy"') ?? false;
      if (!healthy) {
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
      }
    }
    expect(healthy).toBe(true);
    checkAnswer(sample, (await fleet.invoke(PROMPT)).text);
  }

  async function assertSuspendsAndResumes(fleet: AgentFleet, sample: Sample): Promise<void> {
    await new Promise((resolve) => setTimeout(resolve, SUSPEND_WAIT_MS));
    const states: string[] = [];
    for await (const item of Sandbox.list({ template: fleet.name, region: BEDROCK_REGION })) {
      states.push(item.state);
    }
    expect(states).toContain("SUSPENDED");
    const started = Date.now();
    const reply = await fleet.invoke(PROMPT);
    console.log(
      `[${sample.id}] reanudar y primera llamada: ${((Date.now() - started) / 1000).toFixed(1)} s`,
    );
    checkAnswer(sample, reply.text);
  }
});
