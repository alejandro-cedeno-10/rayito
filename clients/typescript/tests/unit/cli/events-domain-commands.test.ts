/**
 * `rayito events ...` y `rayito domain ...`: las órdenes sólo parsean
 * argumentos y delegan en `LifecycleEvents`/`CustomDomain`, que tienen sus
 * propios tests contra fakes; aquí se comprueba el cableado, la salida y los
 * códigos de salida (`test_m15_events_webhooks_cli.py` y `test_m15_domain_cli.py`).
 */

import { describe, expect, it } from "vitest";
import type { EventRecord } from "../../../src/lifecycle-events/domain.js";
import { COMPONENT as CUSTOM_DOMAIN } from "../../../src/stacks/components/custom-domain.js";
import { buildWorld, type CaseWorld } from "./case-world.js";
import { FakeDomain, FakeEvents } from "./ops-fakes.js";
import { runWithServices } from "./run-case.js";

const DEPLOY_ARGS = [
  "events",
  "deploy",
  "--artifact-bucket",
  "mi-bucket",
  "--log-group-name",
  "/rayito/rayito-base",
];
const STACK_NAME = "rayito-events-webhooks";

async function invokeEvents(events: FakeEvents, args: string[], stdin?: string) {
  const built = buildWorld({});
  const requested: string[] = [];
  const outcome = await runWithServices({
    args,
    stdin,
    services: {
      ...built.services,
      lifecycleEvents: (stackName) => {
        requested.push(stackName);
        return events;
      },
    },
  });
  return { ...outcome, requested };
}

describe("events deploy", () => {
  it("reenvía sus argumentos, imprime el coste y el estado", async () => {
    const events = new FakeEvents();
    const outcome = await invokeEvents(events, [...DEPLOY_ARGS, "--yes"]);
    expect(outcome.exitCode).toBe(0);
    expect(events.calls).toEqual([
      [
        "deploy",
        {
          artifactBucket: "mi-bucket",
          logGroupName: "/rayito/rayito-base",
          reconcilerIntervalMinutes: 5,
          tags: {},
        },
      ],
    ]);
    expect(outcome.requested).toEqual([STACK_NAME]);
    expect(outcome.stdout).toContain("Coste y activación de events-webhooks");
    expect(outcome.stdout).toContain("events-webhooks: CREATE_COMPLETE");
  });

  it("reenvía las etiquetas repetidas", async () => {
    const events = new FakeEvents();
    await invokeEvents(events, [
      ...DEPLOY_ARGS,
      "--tag",
      "Owner=equipo",
      "--tag",
      "Environment=testing",
      "--yes",
    ]);
    expect(events.calls[0]?.[1]).toMatchObject({
      tags: { Owner: "equipo", Environment: "testing" },
    });
  });

  it("una etiqueta sin = es un error de uso y no despliega", async () => {
    const events = new FakeEvents();
    const outcome = await invokeEvents(events, [...DEPLOY_ARGS, "--tag", "x", "--yes"]);
    expect(outcome.exitCode).toBe(2);
    expect(outcome.stderr).toContain("--tag");
    expect(events.calls).toEqual([]);
  });

  it("pide confirmación y, si se declina, sale con 1 sin desplegar", async () => {
    const events = new FakeEvents();
    const outcome = await invokeEvents(events, DEPLOY_ARGS, "n\n");
    expect(outcome.exitCode).toBe(1);
    expect(outcome.stdout).toContain("¿Desplegar esta pila? [y/N]: ");
    expect(events.calls).toEqual([]);
  });

  it("--json es el estado de la pila y no imprime el coste ni pregunta", async () => {
    const events = new FakeEvents();
    const outcome = await invokeEvents(events, ["--json", ...DEPLOY_ARGS, "--stack-name", "otra"]);
    expect(JSON.parse(outcome.stdout)).toEqual({
      name: "rayito-events-webhooks",
      state: "CREATE_COMPLETE",
      outputs: {},
    });
    expect(outcome.requested).toEqual(["otra"]);
  });
});

describe("events status y destroy", () => {
  it("status distingue no desplegado de un estado", async () => {
    const events = new FakeEvents();
    expect((await invokeEvents(events, ["events", "status"])).stdout).toBe(
      "events-webhooks: no desplegado\n",
    );
    events.current = { name: STACK_NAME, state: "UPDATE_COMPLETE", outputs: {} };
    expect((await invokeEvents(events, ["events", "status"])).stdout).toBe(
      "events-webhooks: UPDATE_COMPLETE\n",
    );
    expect((await invokeEvents(new FakeEvents(), ["--json", "events", "status"])).stdout).toBe(
      "null\n",
    );
  });

  it("destroy dice qué se borra y confirma", async () => {
    const events = new FakeEvents();
    const outcome = await invokeEvents(events, ["events", "destroy", "--yes"]);
    expect(outcome.stdout).toContain("Al borrar: ");
    expect(outcome.stdout).toContain("events-webhooks: borrado");
    expect(events.calls).toEqual([["destroy", undefined]]);
    const declined = await invokeEvents(new FakeEvents(), ["events", "destroy"], "no\n");
    expect(declined.exitCode).toBe(1);
  });

  it("destroy --json", async () => {
    const outcome = await invokeEvents(new FakeEvents(), ["--json", "events", "destroy"]);
    expect(JSON.parse(outcome.stdout)).toEqual({ name: "events-webhooks", state: "destroyed" });
  });
});

describe("events list", () => {
  const record: EventRecord = {
    eventId: "e1",
    sandboxId: "microvm-a",
    kind: "killed",
    killReason: "timeout",
    generation: 2,
    occurredAtMs: 1_790_000_000_000,
    imageArn: "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base",
    imageVersion: "1.0",
  } as EventRecord;

  it("reenvía los filtros y imprime una tabla", async () => {
    const events = new FakeEvents();
    events.records = [record];
    const outcome = await invokeEvents(events, [
      "events",
      "list",
      "--sandbox-id",
      "microvm-a",
      "--type",
      "sandbox.lifecycle.killed",
      "--limit",
      "10",
      "--order",
      "asc",
    ]);
    expect(events.calls).toEqual([
      [
        "getEvents",
        { sandboxId: "microvm-a", types: ["sandbox.lifecycle.killed"], limit: 10, order: "asc" },
      ],
    ]);
    expect(outcome.stdout).toContain("event_id");
    expect(outcome.stdout).toContain("sandbox.lifecycle.killed");
    expect(outcome.stdout).toContain("1790000000000");
  });

  it("sin filtros no manda ni sandboxId ni types, y --json lleva killReason y generation", async () => {
    const events = new FakeEvents();
    events.records = [record];
    const outcome = await invokeEvents(events, ["--json", "events", "list"]);
    expect(events.calls).toEqual([["getEvents", { limit: 100, order: "desc" }]]);
    expect(JSON.parse(outcome.stdout)).toEqual([
      {
        eventId: "e1",
        sandboxId: "microvm-a",
        type: "sandbox.lifecycle.killed",
        killReason: "timeout",
        generation: 2,
        occurredAtMs: 1_790_000_000_000,
      },
    ]);
  });
});

describe("events webhook", () => {
  it("add registra con el secreto y los tipos", async () => {
    const events = new FakeEvents();
    const outcome = await invokeEvents(events, [
      "events",
      "webhook",
      "add",
      "https://hooks.example.com/x",
      "--secret-name",
      "mi-webhook",
      "--type",
      "sandbox.lifecycle.killed",
      "--type",
      "sandbox.lifecycle.paused",
    ]);
    expect(events.calls).toEqual([
      [
        "registerWebhook",
        "https://hooks.example.com/x",
        {
          secretName: "mi-webhook",
          types: ["sandbox.lifecycle.killed", "sandbox.lifecycle.paused"],
        },
      ],
    ]);
    expect(outcome.stdout).toBe(
      "webhook abc123: https://hooks.example.com/x (sandbox.lifecycle.killed, sandbox.lifecycle.paused)\n",
    );
  });

  it("add exige --secret-name y al menos un --type (salida 2)", async () => {
    const outcome = await invokeEvents(new FakeEvents(), [
      "events",
      "webhook",
      "add",
      "https://h.example.com",
    ]);
    expect(outcome.exitCode).toBe(2);
  });

  it("list imprime la tabla y --json la lista", async () => {
    const events = new FakeEvents();
    events.webhooks = [
      { webhookId: "w1", url: "https://h.example.com", types: ["sandbox.lifecycle.killed"] },
    ];
    expect((await invokeEvents(events, ["events", "webhook", "list"])).stdout).toContain("w1");
    expect(
      JSON.parse((await invokeEvents(events, ["--json", "events", "webhook", "list"])).stdout),
    ).toEqual([
      { webhookId: "w1", url: "https://h.example.com", types: ["sandbox.lifecycle.killed"] },
    ]);
  });

  it("remove da de baja por id", async () => {
    const events = new FakeEvents();
    expect((await invokeEvents(events, ["events", "webhook", "remove", "w1"])).stdout).toBe(
      "webhook w1: borrado\n",
    );
    expect(events.calls).toEqual([["deleteWebhook", "w1"]]);
    expect(
      JSON.parse(
        (await invokeEvents(events, ["--json", "events", "webhook", "remove", "w1"])).stdout,
      ),
    ).toEqual({ webhookId: "w1", state: "deleted" });
  });
});

describe("domain", () => {
  async function invokeDomain(
    args: string[],
    options: { world?: CaseWorld; stdin?: string; domain?: FakeDomain } = {},
  ) {
    const built = buildWorld(options.world ?? {});
    const requested: Array<{ publicDomain: string; stackName: string | undefined }> = [];
    const outcome = await runWithServices({
      args,
      stdin: options.stdin,
      services: {
        ...built.services,
        customDomain: (request) => {
          requested.push(request);
          return options.domain ?? new FakeDomain(request.publicDomain);
        },
      },
    });
    return { ...built, ...outcome, requested };
  }

  const Deploy = [
    "domain",
    "deploy",
    "--public-domain",
    "sbx.example.com",
    "--certificate-arn",
    "arn:aws:acm:us-east-1:123456789012:certificate/abc",
  ];

  it("deploy imprime el coste, despliega y dice a dónde apuntar el CNAME", async () => {
    const domain = new FakeDomain("sbx.example.com");
    domain.deployed = {
      name: "rayito-custom-domain",
      state: "CREATE_COMPLETE",
      outputs: { DistributionDomainName: "d111.cloudfront.net" },
    };
    const outcome = await invokeDomain([...Deploy, "--yes"], { domain });
    expect(outcome.exitCode).toBe(0);
    expect(outcome.stdout).toContain("Coste y activación de custom-domain");
    expect(outcome.stdout).toContain("Cómo apagarla: rayito domain destroy (");
    expect(outcome.stdout).toContain("custom-domain: CREATE_COMPLETE");
    expect(outcome.stdout).toContain(
      "  DNS: apunta un CNAME/alias de *.sbx.example.com a d111.cloudfront.net",
    );
    expect(domain.calls).toEqual([
      [
        "deploy",
        {
          certificateArn: "arn:aws:acm:us-east-1:123456789012:certificate/abc",
          alternateDomainNames: undefined,
        },
      ],
    ]);
    expect(outcome.requested).toEqual([{ publicDomain: "sbx.example.com", stackName: undefined }]);
  });

  it("con nombres alternativos los usa en lugar del comodín", async () => {
    const domain = new FakeDomain("sbx.example.com");
    domain.deployed = {
      name: "n",
      state: "UPDATE_COMPLETE",
      outputs: { DistributionDomainName: "d111.cloudfront.net" },
    };
    const outcome = await invokeDomain(
      [
        ...Deploy,
        "--alternate-domain-name",
        "8000-a.sbx.example.com",
        "--alternate-domain-name",
        "8000-b.sbx.example.com",
        "--stack-name",
        "mi-dominio",
        "--yes",
      ],
      { domain },
    );
    expect(outcome.stdout).toContain(
      "CNAME/alias de 8000-a.sbx.example.com, 8000-b.sbx.example.com a d111.cloudfront.net",
    );
    expect(domain.calls[0]?.[1]).toMatchObject({
      alternateDomainNames: ["8000-a.sbx.example.com", "8000-b.sbx.example.com"],
    });
    expect(outcome.requested[0]?.stackName).toBe("mi-dominio");
  });

  it("pide confirmación; declinar sale con 1 sin desplegar", async () => {
    const domain = new FakeDomain("sbx.example.com");
    const outcome = await invokeDomain(Deploy, { domain, stdin: "n\n" });
    expect(outcome.exitCode).toBe(1);
    expect(domain.calls).toEqual([]);
  });

  it("--json no imprime coste ni pregunta", async () => {
    const outcome = await invokeDomain(["--json", ...Deploy]);
    expect(JSON.parse(outcome.stdout)).toEqual({
      name: "rayito-custom-domain",
      state: "CREATE_COMPLETE",
      outputs: {},
    });
  });

  it("status sobre la pila real del mundo, sin pasar por CustomDomain", async () => {
    const world: CaseWorld = {
      stacks: {
        "rayito-custom-domain": {
          state: "CREATE_COMPLETE",
          outputs: { DistributionDomainName: "d111.cloudfront.net" },
        },
      },
    };
    const outcome = await invokeDomain(["domain", "status"], { world });
    expect(outcome.stdout).toBe(
      "custom-domain: CREATE_COMPLETE\n  DistributionDomainName = d111.cloudfront.net\n",
    );
    expect(outcome.requested).toEqual([]);
    expect((await invokeDomain(["domain", "status"])).stdout).toBe(
      "custom-domain: no desplegado\n",
    );
    expect((await invokeDomain(["--json", "domain", "status"])).stdout).toBe("null\n");
  });

  it("destroy dice qué se borra, espera lo que tarda CloudFront y confirma", async () => {
    const world: CaseWorld = { stacks: { "rayito-custom-domain": { state: "CREATE_COMPLETE" } } };
    const outcome = await invokeDomain(["domain", "destroy", "--yes"], { world });
    expect(outcome.stdout).toContain(`Al borrar: ${CUSTOM_DOMAIN.cost.removal}`);
    expect(outcome.stdout).toContain("custom-domain: borrado");
    expect(outcome.provisioner.waitTimeoutsMs).toEqual([1_800_000]);
    const declined = await invokeDomain(["domain", "destroy"], { world, stdin: "no\n" });
    expect(declined.exitCode).toBe(1);
    const json = await invokeDomain(["--json", "domain", "destroy"], { world });
    expect(JSON.parse(json.stdout)).toEqual({ name: "custom-domain", state: "destroyed" });
  });
});
