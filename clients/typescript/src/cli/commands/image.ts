/**
 * `rayito image list | sizes`: las imágenes de MicroVM de la cuenta y qué
 * tamaño del catálogo cerrado está publicado. `publish`, `prune` y `zip` están
 * en `image-tools.ts`.
 */

import {
  DEFAULT_IMAGE_NAMES,
  IMAGE_VARIANTS,
  type ImageVariant,
} from "../../images/publish-domain.js";
import {
  applySizeSuffix,
  BASELINE_MEMORY_MIB,
  NAME_TO_MEMORY_MIB,
  resolveSize,
  SIZE_NAMES,
} from "../../sizing/sizing.js";
import { argument, defineCommand, defineGroup, option } from "../command.js";
import { UsageError } from "../failures.js";
import type { CliServices } from "../services.js";

const IMAGE_COLUMNS = [
  "name",
  "state",
  "latestActiveImageVersion",
  "latestFailedImageVersion",
  "createdAt",
] as const;
const VERSION_COLUMNS = [
  "imageVersion",
  "state",
  "status",
  "baseImageVersion",
  "artifact",
  "createdAt",
] as const;
const SIZES_COLUMNS = [
  "size",
  "name",
  "published",
  "imageArn",
  "state",
  "sameArtifact",
  "createdAt",
] as const;
const DEFAULT_VARIANT = "full";

const BASELINE_SIZE_NAME = SIZE_NAMES.find(
  (name) => NAME_TO_MEMORY_MIB[name] === BASELINE_MEMORY_MIB,
);

export const imageGroup = defineGroup({
  path: ["image"],
  help: "Imágenes de MicroVM: publish, list, sizes, prune, zip.",
});

export function validateVariant(variant: string): ImageVariant {
  const known = IMAGE_VARIANTS.find((candidate) => candidate === variant);
  if (known === undefined) {
    throw new UsageError(`--variant: admitidas: ${IMAGE_VARIANTS.join(", ")}`);
  }
  return known;
}

async function listedImages(
  services: CliServices,
  nameFilter: string | undefined,
): Promise<Record<string, unknown>[]> {
  const images = await services.images.listImages(nameFilter === undefined ? {} : { nameFilter });
  return images.map((image) => ({
    name: image.name,
    imageArn: image.imageArn,
    state: image.state,
    latestActiveImageVersion: image.latestActiveVersion ?? null,
    latestFailedImageVersion: image.latestFailedVersion ?? null,
    createdAt: image.createdAt,
  }));
}

async function listedVersions(
  services: CliServices,
  arn: string,
): Promise<Record<string, unknown>[]> {
  const versions = await services.images.listVersions(arn);
  return versions.map((version) => ({
    imageVersion: version.imageVersion,
    state: version.state,
    status: version.status,
    baseImageVersion: version.baseImageVersion ?? null,
    artifact: version.artifact ?? null,
    createdAt: version.createdAt,
  }));
}

export const listCommand = defineCommand({
  path: ["image", "list"],
  help: "Sin NAME, las imágenes de la cuenta; con NAME, sus versiones (la más nueva primero).",
  parameters: [
    argument("name", { help: "Nombre o ARN de una imagen: lista sus versiones." }),
    option(["--name-filter"], { help: "Filtro de nombre de list-microvm-images." }),
  ],
  run: async (context) => {
    const name = context.values.string("name");
    const nameFilter = context.values.string("name_filter");
    if (name !== undefined && nameFilter !== undefined) {
      throw new UsageError("--name-filter: --name-filter sólo sin NAME");
    }
    const services = await context.services();
    const columns: readonly string[] = name === undefined ? IMAGE_COLUMNS : VERSION_COLUMNS;
    const rows =
      name === undefined
        ? await listedImages(services, nameFilter)
        : await listedVersions(services, await services.controlPlane.resolveTemplateArn(name));
    if (context.globals.json) {
      context.console.emitJson(rows);
      return undefined;
    }
    context.console.table(
      columns,
      rows.map((row) => columns.map((column) => row[column])),
    );
    return undefined;
  },
});

function sizeImageNames(baseName: string): Array<readonly [string, string]> {
  return SIZE_NAMES.map((size) => [size, applySizeSuffix(baseName, resolveSize(size))] as const);
}

async function activeArtifact(
  services: CliServices,
  image: Record<string, unknown> | undefined,
): Promise<string | undefined> {
  if (image === undefined || image.latestActiveImageVersion === null) {
    return undefined;
  }
  return services.imageArtifacts.activeArtifact(
    String(image.imageArn),
    String(image.latestActiveImageVersion),
  );
}

export const sizesCommand = defineCommand({
  path: ["image", "sizes"],
  help: "Qué tamaño del catálogo cerrado ya está publicado para esta variante (nunca construye, publica ni lanza nada).",
  parameters: [
    option(["--variant"], { default: DEFAULT_VARIANT, help: "full, slim o poly." }),
    option(["--image-name"], {
      help: "Nombre base de `image publish --image-name`; sustituye al de la variante.",
    }),
  ],
  run: async (context) => {
    const variant = validateVariant(context.values.string("variant") ?? DEFAULT_VARIANT);
    const baseName = context.values.string("image_name") ?? DEFAULT_IMAGE_NAMES[variant];
    const services = await context.services();
    const published = new Map(
      (await listedImages(services, baseName)).map((image) => [String(image.name), image]),
    );
    const names = sizeImageNames(baseName);
    const othersPublished = names.some(
      ([size, name]) => size !== BASELINE_SIZE_NAME && published.has(name),
    );
    const baselineName = names.find(([size]) => size === BASELINE_SIZE_NAME)?.[1] ?? "";
    const baselineArtifact = othersPublished
      ? await activeArtifact(services, published.get(baselineName))
      : undefined;
    const rows: Record<string, unknown>[] = [];
    for (const [size, name] of names) {
      const image = published.get(name);
      let sameArtifact: boolean | null = null;
      if (size !== BASELINE_SIZE_NAME && othersPublished) {
        const artifact = await activeArtifact(services, image);
        if (baselineArtifact !== undefined && artifact !== undefined) {
          sameArtifact = artifact === baselineArtifact;
        }
      }
      rows.push({
        size,
        name,
        published: image !== undefined,
        imageArn: image === undefined ? null : image.imageArn,
        state: image === undefined ? null : image.state,
        sameArtifact,
        createdAt: image === undefined ? null : image.createdAt,
      });
    }
    if (context.globals.json) {
      context.console.emitJson(rows);
      return undefined;
    }
    context.console.table(
      SIZES_COLUMNS,
      rows.map((row) => SIZES_COLUMNS.map((column) => row[column])),
    );
    return undefined;
  },
});
