/**
 * Adaptadores del SDK de AWS v3 de los puertos del panel de gestión: Service
 * Quotas y Cost Explorer (peers opcionales, importados en la primera
 * llamada) y el catálogo de imágenes de `lambda-microvms`. Construirlos no
 * llama a AWS. Los parámetros de AWS usados aquí están en `AWS_API_NOTES.md`
 * §30 y §6. Un error se traduce por **código** (nunca por texto) y el
 * mensaje sólo lleva ese código: el de AWS puede nombrar recursos de la
 * cuenta. Espejo de `rayito._dashboard._aws`.
 */

import {
  LambdaMicrovmsClient,
  ListMicrovmImagesCommand,
  ListMicrovmImageVersionsCommand,
} from "@aws-sdk/client-lambda-microvms";
import { type AwsClientSettings, clientConfig } from "../aws/control-plane.js";
import { awsCode, loadOptionalSdkClient } from "../aws/optional-client.js";
import { sanitizeAwsError } from "../aws/sanitize.js";
import { AuthenticationError, DashboardError, RateLimitError } from "../errors.js";
import { COST_EXPLORER_REGION, COST_EXPLORER_SERVICE_NAME, QUOTA_SERVICE_CODE } from "../limits.js";
import type {
  AppliedQuota,
  CostGroup,
  CostPeriod,
  CostQueryResult,
  ImageSummary,
  ImageVersionSummary,
} from "./domain.js";
import type { CostQuery, CostSource, ImageCatalog, QuotaSource } from "./ports.js";

type Credentials = AwsClientSettings["credentials"];

/**
 * Las cuotas de MicroVMs son las de `lambda` cuyo nombre lo menciona (el
 * mismo criterio que `rayito doctor`).
 */
export const MICROVM_QUOTA_NAME_MARKER = "microvm";
export const ACCESS_DENIED_CODE = "AccessDeniedException";
/**
 * Throttling de Service Quotas (`TooManyRequestsException`), de Cost
 * Explorer (`LimitExceededException`) y de lambda-microvms
 * (`ThrottlingException`).
 */
export const THROTTLING_CODES: ReadonlySet<string> = new Set([
  "TooManyRequestsException",
  "LimitExceededException",
  "ThrottlingException",
]);
export const COST_METRICS = ["UnblendedCost", "UsageQuantity"] as const;
export const COST_GROUP_KEY = "USAGE_TYPE";
export const SERVICE_DIMENSION = "SERVICE";
export const REGION_DIMENSION = "REGION";

const SERVICE_QUOTAS_PEER = "@aws-sdk/client-service-quotas";
const COST_EXPLORER_PEER = "@aws-sdk/client-cost-explorer";

/** Un cliente del SDK de AWS que sabe mandar un comando (`send`). */
export interface Sender {
  send(command: unknown): Promise<unknown>;
}

/** Entrega el cliente en la primera llamada, nunca al construir el adaptador. */
export type SenderProvider = () => Promise<Sender>;

/** Un error del SDK de AWS v3 lleva `$metadata` o `$fault`; un bug propio, no. */
function isAwsSdkError(error: unknown): boolean {
  return typeof error === "object" && error !== null && ("$metadata" in error || "$fault" in error);
}

/**
 * Memoiza la construcción del cliente: una promesa compartida entre llamadas
 * y, si falla (p. ej. falta el peer), la siguiente llamada reintenta.
 */
function memoizedClient<T>(build: () => Promise<T>): () => Promise<T> {
  let pending: Promise<T> | undefined;
  return () => {
    if (pending === undefined) {
      const current = build();
      current.catch(() => {
        if (pending === current) {
          pending = undefined;
        }
      });
      pending = current;
    }
    return pending;
  };
}

/**
 * Lanza un error propio con sólo el código de AWS: ni el mensaje ni `cause`
 * guardan el error original del SDK (`sanitizeAwsError` sin mensaje).
 */
export async function dashboardCall<T>(operation: string, invoke: () => Promise<T>): Promise<T> {
  try {
    return await invoke();
  } catch (error) {
    if (!isAwsSdkError(error)) {
      throw error;
    }
    const cause = sanitizeAwsError(error, { includeMessage: false });
    const code = awsCode(error);
    const message = `${operation}: AWS respondió ${cause.name}`;
    if (code === ACCESS_DENIED_CODE) {
      throw new AuthenticationError(message, { awsCode: code, cause });
    }
    if (code !== undefined && THROTTLING_CODES.has(code)) {
      throw new RateLimitError(message, { awsCode: code, cause });
    }
    throw new DashboardError(message, { awsCode: code, cause });
  }
}

interface QuotaRow {
  readonly QuotaCode?: string;
  readonly QuotaName?: string;
  readonly Value?: number;
  readonly Adjustable?: boolean;
}

export interface QuotaPage {
  readonly Quotas?: readonly QuotaRow[];
  readonly NextToken?: string;
}

interface ServiceQuotasModule {
  readonly ServiceQuotasClient: new (config: object) => Sender;
  readonly ListServiceQuotasCommand: new (input: object) => unknown;
}

/**
 * Las cuotas de MicroVMs de `lambda` de un cliente de Service Quotas ya
 * construido, sin traducir errores.
 */
export async function listMicrovmQuotas(
  send: (input: { ServiceCode: string; NextToken?: string }) => Promise<QuotaPage>,
): Promise<AppliedQuota[]> {
  const applied: AppliedQuota[] = [];
  let token: string | undefined;
  do {
    const page = await send({
      ServiceCode: QUOTA_SERVICE_CODE,
      ...(token === undefined ? {} : { NextToken: token }),
    });
    for (const quota of page.Quotas ?? []) {
      const name = String(quota.QuotaName ?? "");
      if (name.toLowerCase().includes(MICROVM_QUOTA_NAME_MARKER)) {
        applied.push({
          code: String(quota.QuotaCode),
          name,
          value: Number(quota.Value),
          adjustable: quota.Adjustable ?? false,
        });
      }
    }
    token = page.NextToken;
  } while (token !== undefined);
  return applied;
}

/** Una página de `ListServiceQuotas`: lo que el adaptador pide a su cliente. */
export type QuotaPager = (input: { ServiceCode: string; NextToken?: string }) => Promise<QuotaPage>;

/**
 * `QuotaSource` sobre Service Quotas (`servicequotas:ListServiceQuotas`):
 * `pager` entrega el cliente en la primera llamada, nunca al construir.
 */
export function serviceQuotasSource(pager: () => Promise<QuotaPager>): QuotaSource {
  return {
    listAppliedQuotas: async () => {
      const send = await pager();
      return dashboardCall("Quotas.listServiceQuotas", () => listMicrovmQuotas(send));
    },
  };
}

/** El adaptador real: importa el peer `@aws-sdk/client-service-quotas` en la primera llamada. */
export function sdkServiceQuotasSource(region: string, credentials: Credentials): QuotaSource {
  return serviceQuotasSource(
    memoizedClient(async () => {
      const { sdk, send } = await loadOptionalSdkClient<ServiceQuotasModule>(
        SERVICE_QUOTAS_PEER,
        "cuotas del panel de gestión (Quotas)",
        (module) => module.ServiceQuotasClient,
        region,
        credentials,
      );
      return (input) => send<QuotaPage>(new sdk.ListServiceQuotasCommand(input));
    }),
  );
}

interface CostMetric {
  readonly Amount?: string;
  readonly Unit?: string;
}

interface CostGroupRow {
  readonly Keys?: readonly string[];
  readonly Metrics?: Readonly<Record<string, CostMetric>>;
}

interface CostResultRow {
  readonly TimePeriod?: { readonly Start?: string; readonly End?: string };
  readonly Estimated?: boolean;
  readonly Groups?: readonly CostGroupRow[];
}

export interface CostPage {
  readonly ResultsByTime?: readonly CostResultRow[];
  readonly NextPageToken?: string;
}

interface CostExplorerModule {
  readonly CostExplorerClient: new (config: object) => Sender;
  readonly GetCostAndUsageCommand: new (input: object) => unknown;
}

export function costFilter(region: string | undefined): Record<string, unknown> {
  const service = { Dimensions: { Key: SERVICE_DIMENSION, Values: [COST_EXPLORER_SERVICE_NAME] } };
  if (region === undefined) {
    return service;
  }
  return { And: [service, { Dimensions: { Key: REGION_DIMENSION, Values: [region] } }] };
}

function costGroup(group: CostGroupRow): CostGroup {
  const metrics = group.Metrics ?? {};
  return {
    usageType: String(group.Keys?.[0]),
    costUsd: Number(metrics.UnblendedCost?.Amount),
    quantity: Number(metrics.UsageQuantity?.Amount),
    unit: String(metrics.UsageQuantity?.Unit ?? ""),
  };
}

function costPeriod(result: CostResultRow): CostPeriod {
  return {
    start: String(result.TimePeriod?.Start),
    end: String(result.TimePeriod?.End),
    estimated: result.Estimated ?? false,
    groups: (result.Groups ?? []).map(costGroup),
  };
}

/**
 * Consulta `GetCostAndUsage` siguiendo `NextPageToken`: el servicio `AWS
 * Lambda` agrupado por `USAGE_TYPE`, como el comando verificado en
 * `AWS_API_NOTES.md` §12. Cada página cuenta como una petición.
 */
export async function queryCostAndUsage(
  send: (input: Record<string, unknown>) => Promise<CostPage>,
  query: CostQuery,
): Promise<CostQueryResult> {
  const params: Record<string, unknown> = {
    TimePeriod: { Start: query.start, End: query.end },
    Granularity: query.granularity,
    Metrics: [...COST_METRICS],
    Filter: costFilter(query.region),
    GroupBy: [{ Type: "DIMENSION", Key: COST_GROUP_KEY }],
  };
  const periods: CostPeriod[] = [];
  let requests = 0;
  for (;;) {
    const page = await send(params);
    requests += 1;
    periods.push(...(page.ResultsByTime ?? []).map(costPeriod));
    if (!page.NextPageToken) {
      return { periods, requests };
    }
    params.NextPageToken = page.NextPageToken;
  }
}

/** Una página de `GetCostAndUsage`: lo que el adaptador pide a su cliente. */
export type CostPager = (input: Record<string, unknown>) => Promise<CostPage>;

/**
 * `CostSource` sobre Cost Explorer (`ce:GetCostAndUsage`, $0,01 por
 * petición): `pager` entrega el cliente en la primera llamada.
 */
export function costExplorerSource(pager: () => Promise<CostPager>): CostSource {
  return {
    getCostAndUsage: async (query) => {
      const send = await pager();
      return dashboardCall("UsageCost.getCostAndUsage", () => queryCostAndUsage(send, query));
    },
  };
}

/**
 * El adaptador real: importa el peer `@aws-sdk/client-cost-explorer` en la
 * primera llamada y habla con us-east-1, donde vive el servicio.
 */
export function sdkCostExplorerSource(credentials: Credentials): CostSource {
  return costExplorerSource(
    memoizedClient(async () => {
      const { sdk, send } = await loadOptionalSdkClient<CostExplorerModule>(
        COST_EXPLORER_PEER,
        "coste facturado del panel de gestión (UsageCost.billed)",
        (module) => module.CostExplorerClient,
        COST_EXPLORER_REGION,
        credentials,
      );
      return (input) => send<CostPage>(new sdk.GetCostAndUsageCommand(input));
    }),
  );
}

interface ImageRow {
  readonly name?: string;
  readonly imageArn?: string;
  readonly state?: string;
  readonly latestActiveImageVersion?: string;
  readonly latestFailedImageVersion?: string;
  readonly createdAt?: Date;
}

interface VersionRow {
  readonly imageVersion?: string;
  readonly state?: string;
  readonly status?: string;
  readonly baseImageVersion?: string;
  readonly codeArtifact?: { readonly uri?: string };
  readonly resources?: ReadonlyArray<{ readonly minimumMemoryInMiB?: number }>;
  readonly createdAt?: Date;
}

/**
 * El nombre del fichero del artefacto de código de una imagen o versión
 * (nunca el bucket ni la ruta), o `undefined` si no lo trae.
 */
export function artifactBasename(item: {
  readonly codeArtifact?: { readonly uri?: string } | undefined;
}): string | undefined {
  const uri = item.codeArtifact?.uri;
  return uri === undefined ? undefined : uri.slice(uri.lastIndexOf("/") + 1);
}

function createdAtMs(item: ImageVersionSummary): number {
  return item.createdAt.getTime();
}

/** Las imágenes de la cuenta, sin traducir errores. */
export async function listImageSummaries(
  client: Sender,
  nameFilter?: string | undefined,
): Promise<ImageSummary[]> {
  const images: ImageSummary[] = [];
  let token: string | undefined;
  do {
    const page = (await client.send(
      new ListMicrovmImagesCommand({
        ...(nameFilter ? { nameFilter } : {}),
        ...(token === undefined ? {} : { nextToken: token }),
      }),
    )) as { items?: readonly ImageRow[]; nextToken?: string };
    for (const item of page.items ?? []) {
      images.push({
        name: String(item.name),
        imageArn: String(item.imageArn),
        state: String(item.state),
        latestActiveVersion: item.latestActiveImageVersion,
        latestFailedVersion: item.latestFailedImageVersion,
        createdAt: item.createdAt as Date,
      });
    }
    token = page.nextToken;
  } while (token !== undefined);
  return images;
}

/** Las versiones de una imagen, la más nueva primero, sin traducir errores. */
export async function listImageVersionSummaries(
  client: Sender,
  imageArn: string,
): Promise<ImageVersionSummary[]> {
  const versions: ImageVersionSummary[] = [];
  let token: string | undefined;
  do {
    const page = (await client.send(
      new ListMicrovmImageVersionsCommand({
        imageIdentifier: imageArn,
        ...(token === undefined ? {} : { nextToken: token }),
      }),
    )) as { items?: readonly VersionRow[]; nextToken?: string };
    for (const item of page.items ?? []) {
      versions.push({
        imageVersion: String(item.imageVersion),
        state: String(item.state),
        status: String(item.status),
        baseImageVersion: item.baseImageVersion,
        artifact: artifactBasename(item),
        memoryMib: item.resources?.[0]?.minimumMemoryInMiB,
        createdAt: item.createdAt as Date,
      });
    }
    token = page.nextToken;
  } while (token !== undefined);
  return versions.sort((a, b) => createdAtMs(b) - createdAtMs(a));
}

/** `ImageCatalog` sobre `lambda-microvms` (`ListMicrovmImages`, `ListMicrovmImageVersions`). */
export class LambdaMicrovmsImageCatalog implements ImageCatalog {
  readonly #client: () => Sender;

  constructor(client: () => Sender) {
    this.#client = client;
  }

  listImages(options: { readonly nameFilter?: string | undefined } = {}): Promise<ImageSummary[]> {
    return dashboardCall("Fleet.listImages", () =>
      listImageSummaries(this.#client(), options.nameFilter),
    );
  }

  listVersions(imageArn: string): Promise<ImageVersionSummary[]> {
    return dashboardCall("Fleet.listImageVersions", () =>
      listImageVersionSummaries(this.#client(), imageArn),
    );
  }
}

/** El catálogo real: construye el cliente de `lambda-microvms` en el primer uso. */
export function lambdaMicrovmsImageCatalog(region: string, credentials: Credentials): ImageCatalog {
  let client: LambdaMicrovmsClient | undefined;
  return new LambdaMicrovmsImageCatalog(() => {
    client ??= new LambdaMicrovmsClient(
      clientConfig(region, credentials === undefined ? {} : { credentials }),
    );
    return client as unknown as Sender;
  });
}
