/**
 * Versiones de intérprete de un proyecto (ADR-029): lee una restricción de
 * Python (PEP 440: `>=3.11,<3.14`, `~=3.12`, `==3.12.*`, `^3.11`) o de Node
 * (semver: `>=20`, `^22`, `22.x`, `20 || 22`) y elige entre las compilaciones
 * fijadas de `limits.json`. Espejo de `rayito._agent._detect_versions`, puro y
 * sin dependencias: los dos SDK implementan lo mismo y lo comprueban con
 * `testdata/agent-detection/versions.json`.
 */

export type Version = readonly number[];

export const SPECIFIER_KIND = "specifier";
export const SEMVER_KIND = "semver";
export const MINOR_KIND = "minor";
export const MAJOR_KIND = "major";
const CLAUSE = /^(~=|==|!=|>=|<=|>|<|\^|~)?\s*v?(\d+(?:\.\d+)*)(\.\*)?$/;
const COMPARATOR_SOURCE =
  "(>=|<=|>|<|=|\\^|~)?\\s*v?(\\d+|[xX*])(?:\\.(\\d+|[xX*]))?(?:\\.(\\d+|[xX*]))?";
const WILDCARDS = new Set(["x", "X", "*"]);
const OR = "||";
const MINOR_COMPONENTS = 2;
const SEMVER_COMPONENTS = 3;

export function parseVersion(text: string): Version {
  return text.split(".").map((part) => Number.parseInt(part, 10));
}

function pad(version: Version, size: number): number[] {
  return [...version, ...new Array<number>(Math.max(0, size - version.length)).fill(0)];
}

export function compare(left: Version, right: Version): number {
  const size = Math.max(left.length, right.length);
  const a = pad(left, size);
  const b = pad(right, size);
  for (let index = 0; index < size; index += 1) {
    const difference = (a[index] as number) - (b[index] as number);
    if (difference !== 0) {
      return difference < 0 ? -1 : 1;
    }
  }
  return 0;
}

/** `version` con el componente `index` aumentado y los siguientes a cero. */
function bump(version: Version, index: number): Version {
  return [
    ...version.slice(0, index),
    (version[index] as number) + 1,
    ...new Array<number>(version.length - index - 1).fill(0),
  ];
}

/** El primer `major.minor.patch` por encima de una versión parcial (`22` ->
 * `23.0.0`, `22.1` -> `22.2.0`). */
function nextPartial(numbers: Version): Version {
  return pad(bump(numbers, numbers.length - 1), SEMVER_COMPONENTS);
}

function caretUpper(version: Version): Version {
  const padded = pad(version, SEMVER_COMPONENTS);
  const first = padded.findIndex((part) => part !== 0);
  return bump(padded, first === -1 ? padded.length - 1 : first);
}

function tildeUpper(version: Version): Version {
  return bump(version, version.length === 1 ? 0 : 1);
}

function startsWith(candidate: Version, prefix: Version): boolean {
  return prefix.every((part, index) => candidate[index] === part);
}

function clauseSatisfied(
  operator: string,
  version: Version,
  wildcard: boolean,
  candidate: Version,
): boolean {
  switch (operator) {
    case "":
    case "==":
      return wildcard ? startsWith(candidate, version) : compare(candidate, version) === 0;
    case "!=":
      return wildcard ? !startsWith(candidate, version) : compare(candidate, version) !== 0;
    case ">=":
      return compare(candidate, version) >= 0;
    case ">":
      return compare(candidate, version) > 0;
    case "<=":
      return compare(candidate, version) <= 0;
    case "<":
      return compare(candidate, version) < 0;
    case "~=":
      return (
        version.length >= MINOR_COMPONENTS &&
        compare(candidate, version) >= 0 &&
        startsWith(candidate, version.slice(0, -1))
      );
    case "^":
      return compare(candidate, version) >= 0 && compare(candidate, caretUpper(version)) < 0;
    default:
      return compare(candidate, version) >= 0 && compare(candidate, tildeUpper(version)) < 0;
  }
}

/** `version` cumple la restricción de Python `specifier` (cláusulas separadas
 * por comas), o `undefined` si no se entiende. */
export function specifierSatisfied(specifier: string, version: string): boolean | undefined {
  const candidate = parseVersion(version);
  for (const raw of specifier.split(",")) {
    const match = CLAUSE.exec(raw.trim());
    if (match === null) {
      return undefined;
    }
    const operator = match[1] ?? "";
    const wildcard = match[3] !== undefined;
    if (!clauseSatisfied(operator, parseVersion(match[2] as string), wildcard, candidate)) {
      return false;
    }
  }
  return true;
}

type SimpleClause = readonly [string, Version];

/** Un comparador de semver como pares `(operador, versión)` simples. */
function expandComparator(operator: string, parts: readonly string[]): SimpleClause[] | undefined {
  const numbers = parts
    .filter((part) => !WILDCARDS.has(part))
    .map((part) => Number.parseInt(part, 10));
  const exact = numbers.length === SEMVER_COMPONENTS && parts.length === SEMVER_COMPONENTS;
  if (numbers.length === 0) {
    return ["", "=", "^", "~"].includes(operator) ? [] : undefined;
  }
  const base = pad(numbers, SEMVER_COMPONENTS);
  if (operator === "" || operator === "=") {
    return exact
      ? [["==", base]]
      : [
          [">=", base],
          ["<", nextPartial(numbers)],
        ];
  }
  if (operator === "^" || operator === "~") {
    const upper = operator === "^" ? caretUpper(base) : tildeUpper(numbers);
    return [
      [">=", base],
      ["<", pad(upper, SEMVER_COMPONENTS)],
    ];
  }
  if (exact || operator === ">=" || operator === "<") {
    return [[operator, base]];
  }
  const next = nextPartial(numbers);
  return operator === ">" ? [[">=", next]] : [["<", next]];
}

function groupSatisfied(group: string, candidate: Version): boolean | undefined {
  const comparator = new RegExp(COMPARATOR_SOURCE, "g");
  const comparators = [...group.matchAll(comparator)];
  const leftover = group.replace(new RegExp(COMPARATOR_SOURCE, "g"), "").replaceAll(",", "").trim();
  if (comparators.length === 0 || leftover !== "") {
    return undefined;
  }
  for (const match of comparators) {
    const parts = [match[2], match[3], match[4]].filter(
      (part): part is string => part !== undefined,
    );
    const clauses = expandComparator(match[1] ?? "", parts);
    if (clauses === undefined) {
      return undefined;
    }
    for (const [operator, version] of clauses) {
      if (!clauseSatisfied(operator, version, false, candidate)) {
        return false;
      }
    }
  }
  return true;
}

/** `version` cumple el rango de semver `spec` (grupos con `||`), o
 * `undefined` si no se entiende. */
export function semverSatisfied(spec: string, version: string): boolean | undefined {
  const candidate = parseVersion(version);
  let outcome = false;
  for (const group of spec.split(OR)) {
    const result = groupSatisfied(group.trim(), candidate);
    if (result === undefined) {
      return undefined;
    }
    if (result) {
      outcome = true;
    }
  }
  return outcome;
}

/** Despacha por tipo de restricción: `specifier` (PEP 440), `semver`, o
 * `minor` / `major` (el prefijo de una versión fijada en un fichero). */
export function satisfied(kind: string, constraint: string, version: string): boolean | undefined {
  if (kind === SPECIFIER_KIND) {
    return specifierSatisfied(constraint, version);
  }
  if (kind === SEMVER_KIND) {
    return semverSatisfied(constraint, version);
  }
  return startsWith(parseVersion(version), parseVersion(constraint));
}

/** El resultado de elegir una compilación: su clave (`"3.12"`, `"22"`) o
 * `undefined` si ninguna cumple, y si la restricción se entendió. */
export interface Resolution {
  readonly key: string | undefined;
  readonly understood: boolean;
}

/** La compilación fijada que cumple `constraint`: la de por defecto si cumple
 * (sin descarga) y, si no, la más alta que cumpla. */
export function resolveBuild(
  kind: string,
  constraint: string,
  builds: Readonly<Record<string, string>>,
  defaultKey: string,
): Resolution {
  const check = (key: string): boolean | undefined =>
    satisfied(kind, constraint, builds[key] as string);
  const first = check(defaultKey);
  if (first === undefined) {
    return { key: undefined, understood: false };
  }
  if (first) {
    return { key: defaultKey, understood: true };
  }
  const descending = Object.keys(builds).sort((left, right) =>
    compare(parseVersion(right), parseVersion(left)),
  );
  return { key: descending.find((key) => check(key) === true), understood: true };
}
