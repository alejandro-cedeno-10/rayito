/**
 * Lectura de los logs de `rayd` en CloudWatch para los e2e que comprueban la
 * higiene del log (espejo de `rayito.cli._logs` en Python): el grupo por
 * defecto es `/rayito/<nombre de la imagen>` y el stream acaba en
 * `]<sandboxId>` (`AWS_API_NOTES.md` §14). Las ingestas tardan, así que
 * `rayitoLogLines` espera a que haya eventos. Nunca imprime una línea.
 */

import {
  CloudWatchLogsClient,
  DescribeLogStreamsCommand,
  GetLogEventsCommand,
} from "@aws-sdk/client-cloudwatch-logs";
import type { Sandbox } from "../../src/index.js";
import { sleep } from "./helpers.js";

const LOG_GROUP_PREFIX = "/rayito";
const STREAM_PAGE_LIMIT = 50;
const LOGS_WAIT_MS = 90_000;
const LOGS_POLL_MS = 5_000;

async function streamNames(
  client: CloudWatchLogsClient,
  group: string,
  sandboxId: string,
): Promise<string[]> {
  try {
    const page = await client.send(
      new DescribeLogStreamsCommand({
        logGroupName: group,
        orderBy: "LastEventTime",
        descending: true,
        limit: STREAM_PAGE_LIMIT,
      }),
    );
    return (page.logStreams ?? [])
      .map((stream) => stream.logStreamName ?? "")
      .filter((name) => name.endsWith(`]${sandboxId}`));
  } catch (error) {
    if ((error as { name?: string }).name === "ResourceNotFoundException") {
      return [];
    }
    throw error;
  }
}

async function readOnce(
  client: CloudWatchLogsClient,
  group: string,
  sandboxId: string,
): Promise<string[]> {
  const lines: string[] = [];
  for (const logStreamName of await streamNames(client, group, sandboxId)) {
    const events = await client.send(
      new GetLogEventsCommand({ logGroupName: group, logStreamName, startFromHead: true }),
    );
    lines.push(...(events.events ?? []).map((event) => event.message ?? ""));
  }
  return lines;
}

/** Las líneas del log de `rayd` del sandbox; vacío sólo si nunca llegaron en `LOGS_WAIT_MS`. */
export async function rayitoLogLines(
  sandbox: Sandbox,
  region: string | undefined,
): Promise<string[]> {
  const client = new CloudWatchLogsClient(region === undefined ? {} : { region });
  const info = await sandbox.getInfo();
  const group = `${LOG_GROUP_PREFIX}/${info.templateName}`;
  const deadline = performance.now() + LOGS_WAIT_MS;
  let lines = await readOnce(client, group, sandbox.sandboxId);
  while (lines.length === 0 && performance.now() < deadline) {
    await sleep(LOGS_POLL_MS);
    lines = await readOnce(client, group, sandbox.sandboxId);
  }
  return lines;
}
