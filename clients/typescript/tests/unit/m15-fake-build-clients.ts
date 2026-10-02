/**
 * `BuildClients` falso en memoria (m15-templates): sin AWS SDK, sin red.
 * Espejo de `tests/unit/fake_templates.py`.
 */

import type { BuildClients } from "../../src/templates/build.js";
import { writeZip } from "../../src/templates/zip-node.js";

export interface FakeVersion {
  readonly state: string;
  readonly status: string;
  readonly imageVersion: string;
  readonly createdAt: string;
  readonly stateReason?: string;
  readonly [key: string]: unknown;
}

/** Un zip base real (bytes), con `Dockerfile` y cualquier fichero extra. */
export function makeBaseZip(
  dockerfile: string,
  extra: Record<string, Uint8Array> = {},
): Uint8Array {
  const entries: Array<[string, Uint8Array]> = [
    ["Dockerfile", new TextEncoder().encode(dockerfile)],
  ];
  for (const [name, content] of Object.entries(extra)) {
    entries.push([name, content]);
  }
  return writeZip(entries);
}

export class FakeBuildClients implements BuildClients {
  readonly region = "us-east-1";
  readonly accountIdValue = "123456789012";
  readonly images = new Map<string, { state: string }>();
  readonly versions = new Map<string, FakeVersion>();
  readonly objects = new Map<string, Uint8Array>();
  logLines: string[] = [];
  nextBuildVersion = "1";
  /** Nombre del error del SDK que create/update-microvm-image lanzan en
   * vez de aceptar el build (p. ej. `ServiceQuotaExceededException`, Q83). */
  submitErrorName: string | undefined;
  readonly calls: Array<[string, ...unknown[]]> = [];

  private key(bucket: string, objectKey: string): string {
    return `${bucket}/${objectKey}`;
  }

  private versionKey(arn: string, version: string): string {
    return `${arn}#${version}`;
  }

  async accountId(): Promise<string> {
    return this.accountIdValue;
  }

  async getMicrovmImage(arn: string): Promise<{ state: string } | undefined> {
    this.calls.push(["getMicrovmImage", arn]);
    return this.images.get(arn);
  }

  async getMicrovmImageVersion(
    arn: string,
    version: string,
  ): Promise<Record<string, unknown> | undefined> {
    this.calls.push(["getMicrovmImageVersion", arn, version]);
    return this.versions.get(this.versionKey(arn, version));
  }

  async listMicrovmImageVersions(arn: string): Promise<Array<Record<string, unknown>>> {
    this.calls.push(["listMicrovmImageVersions", arn]);
    return [...this.versions.entries()]
      .filter(([key]) => key.startsWith(`${arn}#`))
      .map(([, item]) => item);
  }

  async getObject(bucket: string, objectKey: string): Promise<Uint8Array | undefined> {
    this.calls.push(["getObject", bucket, objectKey]);
    return this.objects.get(this.key(bucket, objectKey));
  }

  async headObject(bucket: string, objectKey: string): Promise<boolean> {
    this.calls.push(["headObject", bucket, objectKey]);
    return this.objects.has(this.key(bucket, objectKey));
  }

  async putObject(bucket: string, objectKey: string, body: Uint8Array): Promise<void> {
    this.calls.push(["putObject", bucket, objectKey]);
    this.objects.set(this.key(bucket, objectKey), body);
  }

  async createMicrovmImage(
    name: string,
    request: Record<string, unknown>,
  ): Promise<{ imageArn: string; imageVersion: string }> {
    this.calls.push(["createMicrovmImage", name]);
    this.throwSubmitError();
    const arn = `arn:aws:lambda:${this.region}:${this.accountIdValue}:microvm-image:${name}`;
    const version = this.nextBuildVersion;
    this.images.set(arn, { state: "CREATED" });
    this.versions.set(this.versionKey(arn, version), {
      state: "SUCCESSFUL",
      status: "ACTIVE",
      imageVersion: version,
      createdAt: new Date(1).toISOString(),
      ...request,
    });
    return { imageArn: arn, imageVersion: version };
  }

  async updateMicrovmImage(
    arn: string,
    request: Record<string, unknown>,
  ): Promise<{ imageArn: string; imageVersion: string }> {
    this.calls.push(["updateMicrovmImage", arn]);
    this.throwSubmitError();
    const version = this.nextBuildVersion;
    this.versions.set(this.versionKey(arn, version), {
      state: "SUCCESSFUL",
      status: "ACTIVE",
      imageVersion: version,
      createdAt: new Date(2).toISOString(),
      ...request,
    });
    return { imageArn: arn, imageVersion: version };
  }

  private throwSubmitError(): void {
    if (this.submitErrorName !== undefined) {
      const error = new Error(this.submitErrorName);
      error.name = this.submitErrorName;
      throw error;
    }
  }

  async readBuildLogs(_logGroup: string): Promise<string[]> {
    this.calls.push(["readBuildLogs"]);
    return this.logLines;
  }

  /** Programa una versión FALLIDA de `name` cuando se la construya
   * (sustituye `createMicrovmImage` por esta respuesta fija). */
  failNextBuildWith(stateReason: string): void {
    this.createMicrovmImage = async (name, request) => {
      const arn = `arn:aws:lambda:${this.region}:${this.accountIdValue}:microvm-image:${name}`;
      const version = this.nextBuildVersion;
      this.images.set(arn, { state: "CREATE_FAILED" });
      this.versions.set(this.versionKey(arn, version), {
        state: "FAILED",
        status: "INACTIVE",
        imageVersion: version,
        createdAt: new Date(3).toISOString(),
        stateReason,
        ...request,
      });
      return { imageArn: arn, imageVersion: version };
    };
  }
}
