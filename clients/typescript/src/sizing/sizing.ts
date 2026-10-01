/**
 * Catálogo cerrado de tamaños CPU/RAM de `size`/`SizeRequest` (M15,
 * sizes-catalog, §4 opción A de `docs/research/2026-10-e2b-out-of-scope.md`).
 * Espejo de `rayito._sizing`. Dominio puro: ninguna función de aquí hace
 * E/S de red ni de disco.
 *
 * Resuelve el tamaño pedido al primer valor publicado que lo cubra (nunca
 * por debajo de lo pedido) dentro del catálogo cerrado de `limits.ts`
 * (`SUPPORTED_MEMORY_MIB`): `create-microvm-image` sólo acepta 512/1024/
 * 2048/4096/8192 MiB (Q87, `ValidationException` síncrona sin crear nada
 * para cualquier otro valor). También calcula el sufijo de imagen
 * (`<variant>-<size>`, por ejemplo `rayito-base-4gb`).
 */

import { InvalidArgumentError } from "../errors.js";
import { SUPPORTED_MEMORY_MIB } from "../limits.js";

/** Nombres cortos de `size`, en el mismo orden que `SUPPORTED_MEMORY_MIB` (Q87). */
export const SIZE_NAMES = ["512mb", "1gb", "2gb", "4gb", "8gb"] as const;
export type SizeName = (typeof SIZE_NAMES)[number];

export const NAME_TO_MEMORY_MIB: Readonly<Record<SizeName, number>> = Object.freeze(
  Object.fromEntries(
    SIZE_NAMES.map((name, index) => [name, SUPPORTED_MEMORY_MIB[index]]),
  ) as Record<SizeName, number>,
);

const MEMORY_MIB_TO_NAME: ReadonlyMap<number, SizeName> = new Map(
  SIZE_NAMES.map((name) => [NAME_TO_MEMORY_MIB[name], name]),
);

/** DEFAULT_MEMORY_MIB de `cli/_publish.py`: la imagen sin sufijo de `rayito
 * image publish` (sin `--sizes`) es siempre ésta. */
export const BASELINE_MEMORY_MIB = 2048 as const;
export const MAX_SUPPORTED_MEMORY_MIB = Math.max(...SUPPORTED_MEMORY_MIB);

/**
 * RES-2 (Q88, `docs/research/2026-10-e2b-out-of-scope.md`; confirma el
 * punto suelto de Q68 en AWS_API_NOTES.md §4): el guest ve memoria/512
 * vCPU en los cinco tamaños del catálogo (512->1, 1024->2, 2048->4,
 * 4096->8, 8192->16), medido con `nproc`. Una proporción constante, no una
 * extrapolación.
 */
export const MIB_PER_VCPU_Q88 = 512 as const;

/** `size={ memoryMib: 3000 }`: una petición explícita en MiB, para cuando
 * ningún nombre corto encaja exactamente. Siempre se redondea hacia
 * arriba, nunca hacia abajo. */
export interface SizeRequest {
  readonly memoryMib: number;
}

export type SizeInput = SizeName | SizeRequest;

/** Un tamaño ya resuelto contra el catálogo cerrado. */
export interface ResolvedSize {
  readonly name: SizeName;
  readonly memoryMib: number;
  readonly requestedMib: number;
}

export function isRoundedUp(resolved: ResolvedSize): boolean {
  return resolved.memoryMib !== resolved.requestedMib;
}

export function isBaselineSize(resolved: ResolvedSize): boolean {
  return resolved.memoryMib === BASELINE_MEMORY_MIB;
}

/** vCPU del guest para `memoryMib`, medidos exactamente para los cinco
 * tamaños del catálogo (RES-2/Q88, ver `MIB_PER_VCPU_Q88`); al menos 1. */
export function baselineCpuFor(memoryMib: number): number {
  return Math.max(1, Math.floor(memoryMib / MIB_PER_VCPU_Q88));
}

function isSizeRequest(size: SizeInput): size is SizeRequest {
  return typeof size === "object" && size !== null;
}

function requestedMemoryMib(size: SizeInput): number {
  if (isSizeRequest(size)) {
    if (size.memoryMib <= 0) {
      throw new InvalidArgumentError(`size.memoryMib=${size.memoryMib}: tiene que ser positivo`);
    }
    return size.memoryMib;
  }
  const mib = NAME_TO_MEMORY_MIB[size];
  if (mib === undefined) {
    throw new InvalidArgumentError(
      `size=${JSON.stringify(size)}: admitidos ${SIZE_NAMES.join(", ")} o { memoryMib }`,
    );
  }
  return mib;
}

/**
 * Redondea `size` hacia arriba al primer valor de `SUPPORTED_MEMORY_MIB`
 * que lo cubra. `InvalidArgumentError` antes de cualquier llamada a AWS
 * tanto si el nombre/los MiB no son válidos como si ni siquiera
 * `MAX_SUPPORTED_MEMORY_MIB` alcanza (por ejemplo 16384, Q87).
 */
export function resolveSize(size: SizeInput): ResolvedSize {
  const requested = requestedMemoryMib(size);
  for (const published of SUPPORTED_MEMORY_MIB) {
    if (published >= requested) {
      const name = MEMORY_MIB_TO_NAME.get(published);
      if (name === undefined) {
        throw new InvalidArgumentError(`tamaño sin nombre en el catálogo: ${published}`);
      }
      return { name, memoryMib: published, requestedMib: requested };
    }
  }
  throw new InvalidArgumentError(
    `size=${JSON.stringify(size)}: pide ${requested} MiB, por encima del máximo publicado ` +
      `(${MAX_SUPPORTED_MEMORY_MIB} MiB, Q87)`,
  );
}

/**
 * `"rayito-base"` + tamaño `4gb` -> `"rayito-base-4gb"`. El baseline (2048
 * MiB) no añade sufijo. Un ARN no admite sufijo (la imagen la nombra su
 * ARN, no la convención `rayito-<variant>[-<size>]`): usa el nombre, no el
 * ARN, con `size`.
 */
export function applySizeSuffix(imageName: string, resolved: ResolvedSize): string {
  if (imageName.startsWith("arn:")) {
    throw new InvalidArgumentError(
      "size no se puede combinar con un template dado por ARN: publica una imagen con sufijo " +
        "de tamaño y pásala por su nombre, no por ARN",
    );
  }
  if (isBaselineSize(resolved)) {
    return imageName;
  }
  return `${imageName}-${resolved.name}`;
}

const COMPAT_WARNING_TYPE = "RayitoCompatWarning";

/** Un aviso `RayitoCompatWarning` cuando `size` no cae justo en el catálogo. */
export function warnIfRounded(resolved: ResolvedSize): void {
  if (!isRoundedUp(resolved)) {
    return;
  }
  process.emitWarning(
    `size pidió ${resolved.requestedMib} MiB; se redondea hacia arriba a ${resolved.memoryMib} ` +
      `MiB (${resolved.name})`,
    { type: COMPAT_WARNING_TYPE },
  );
}
