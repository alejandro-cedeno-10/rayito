/**
 * La tabla sin bordes de `rich` (`Table(box=None, pad_edge=False,
 * show_edge=False)`) que usa la CLI de Python, portada para que las dos CLI
 * impriman las mismas líneas: encabezado, columnas separadas por dos
 * espacios, cada celda rellena hasta el ancho de su columna y, si la tabla no
 * cabe en `maxWidth`, las columnas más anchas se estrechan (`_collapse_widths`)
 * y su texto se pliega por palabras o, si una palabra no cabe, por caracteres
 * (`overflow="fold"`); el encabezado se alinea abajo y el cuerpo arriba. Los vectores compartidos
 * (`testdata/cli/format-vectors.json`) fijan que el resultado es el de Python.
 */

import { cellText } from "./format.js";

const COLUMN_PADDING = 1;
const DOUBLE_WIDTH = 2;
const WORD_PATTERN = /\s*\S+\s*/y;
const TRAILING_WHITESPACE = /\s+$/;
const EVEN_RATIO = 1;
const HALF = 0.5;

/** Rangos de caracteres de ancho doble en una terminal (CJK, emoji y formas anchas). */
const WIDE_RANGES: ReadonlyArray<readonly [number, number]> = [
  [0x1100, 0x115f],
  [0x2e80, 0xa4cf],
  [0xac00, 0xd7a3],
  [0xf900, 0xfaff],
  [0xfe30, 0xfe6f],
  [0xff00, 0xff60],
  [0xffe0, 0xffe6],
  [0x1f300, 0x1f64f],
  [0x1f900, 0x1f9ff],
  [0x20000, 0x3fffd],
];

function characterWidth(character: string): number {
  const code = character.codePointAt(0) ?? 0;
  return WIDE_RANGES.some(([first, last]) => code >= first && code <= last) ? DOUBLE_WIDTH : 1;
}

export function displayWidth(text: string): number {
  let width = 0;
  for (const character of text) {
    width += characterWidth(character);
  }
  return width;
}

/** `round()` de Python: los empates van al par. */
function roundHalfEven(value: number): number {
  const floor = Math.floor(value);
  const difference = value - floor;
  if (difference !== HALF) {
    return Math.round(value);
  }
  return floor % 2 === 0 ? floor : floor + 1;
}

/** `rich.helpers.ratio_reduce`: reparte `total` entre las columnas con ratio, sin pasar de su máximo. */
function ratioReduce(
  total: number,
  ratios: readonly number[],
  maximums: readonly number[],
  values: readonly number[],
): number[] {
  const effective = ratios.map((ratio, index) => ((maximums[index] ?? 0) > 0 ? ratio : 0));
  let totalRatio = effective.reduce((sum, ratio) => sum + ratio, 0);
  if (totalRatio === 0) {
    return [...values];
  }
  let remaining = total;
  return values.map((value, index) => {
    const ratio = effective[index] ?? 0;
    if (ratio === 0 || totalRatio <= 0) {
      return value;
    }
    const distributed = Math.min(
      maximums[index] ?? 0,
      roundHalfEven((ratio * remaining) / totalRatio),
    );
    remaining -= distributed;
    totalRatio -= ratio;
    return value - distributed;
  });
}

/** `Table._collapse_widths`: estrecha primero las columnas más anchas hasta la segunda más ancha. */
function collapseWidths(widths: readonly number[], maxWidth: number): number[] {
  let current = [...widths];
  let total = current.reduce((sum, width) => sum + width, 0);
  let excess = total - maxWidth;
  while (total > 0 && excess > 0) {
    const widest = Math.max(...current);
    const second = Math.max(...current.map((width) => (width !== widest ? width : 0)));
    const difference = widest - second;
    const ratios = current.map((width) => (width === widest ? 1 : 0));
    if (!ratios.some(Boolean) || difference === 0) {
      break;
    }
    const maximums = current.map(() => Math.min(excess, difference));
    current = ratioReduce(excess, ratios, maximums, current);
    total = current.reduce((sum, width) => sum + width, 0);
    excess = total - maxWidth;
  }
  return current;
}

function chopCells(text: string, width: number): string[] {
  const lines: string[][] = [[]];
  let total = 0;
  for (const character of text) {
    const size = characterWidth(character);
    if (total + size > width) {
      lines.push([character]);
      total = size;
    } else {
      lines[lines.length - 1]?.push(character);
      total += size;
    }
  }
  return lines.map((line) => line.join(""));
}

interface Word {
  readonly start: number;
  readonly text: string;
}

function wordsOf(text: string): Word[] {
  const words: Word[] = [];
  WORD_PATTERN.lastIndex = 0;
  for (let match = WORD_PATTERN.exec(text); match !== null; match = WORD_PATTERN.exec(text)) {
    words.push({ start: match.index, text: match[0] });
  }
  return words;
}

/** `rich._wrap.divide_line` con `fold=True`: dónde se parte `text` para que cada línea quepa en `width`. */
function breakPositions(text: string, width: number): number[] {
  const breaks: number[] = [];
  let offset = 0;
  for (const word of wordsOf(text)) {
    const wordWidth = displayWidth(word.text.trimEnd());
    if (width - offset >= wordWidth) {
      offset += displayWidth(word.text);
      continue;
    }
    if (wordWidth > width) {
      const chunks = chopCells(word.text, width);
      let start = word.start;
      chunks.forEach((chunk, index) => {
        if (start > 0) {
          breaks.push(start);
        }
        if (index === chunks.length - 1) {
          offset = displayWidth(chunk);
        } else {
          start += chunk.length;
        }
      });
    } else if (offset > 0 && word.start > 0) {
      breaks.push(word.start);
      offset = displayWidth(word.text);
    }
  }
  return breaks;
}

function splitAt(text: string, positions: readonly number[]): string[] {
  const cuts = [0, ...positions, text.length];
  return cuts.slice(0, -1).map((start, index) => text.slice(start, cuts[index + 1]));
}

/** `Text.rstrip_end`: quita el blanco final que sobrepasa `width` y el resto del blanco antes de rellenar. */
function trimmedLine(line: string, width: number): string {
  const excess = line.length - width;
  const trailing = TRAILING_WHITESPACE.exec(line)?.[0].length ?? 0;
  const cropped =
    excess > 0 && trailing > 0 ? line.slice(0, line.length - Math.min(trailing, excess)) : line;
  return cropped.trimEnd();
}

function foldedLines(text: string, width: number): string[] {
  return text
    .split("\n")
    .flatMap((line) =>
      splitAt(line, breakPositions(line, width)).map((piece) => trimmedLine(piece, width)),
    );
}

function padCell(text: string, width: number): string {
  return text + " ".repeat(Math.max(0, width - displayWidth(text)));
}

function measure(texts: readonly string[]): number {
  return Math.max(0, ...texts.flatMap((text) => text.split("\n").map(displayWidth)));
}

/** Los anchos de columna (con su relleno) que `rich` calcula para `maxWidth`. */
function columnWidths(
  columns: readonly string[],
  cells: readonly (readonly string[])[],
  maxWidth: number,
): number[] {
  const last = columns.length - 1;
  const padding = (index: number): number =>
    (index === 0 ? 0 : COLUMN_PADDING) + (index === last ? 0 : COLUMN_PADDING);
  const measured = columns.map((_, index) =>
    Math.max(1, measure(cells.map((row) => row[index] ?? "")) + padding(index)),
  );
  if (measured.reduce((sum, width) => sum + width, 0) <= maxWidth) {
    return measured;
  }
  const collapsed = collapseWidths(measured, maxWidth);
  const total = collapsed.reduce((sum, width) => sum + width, 0);
  if (total <= maxWidth) {
    return collapsed;
  }
  const evenly = collapsed.map(() => EVEN_RATIO);
  return ratioReduce(total - maxWidth, evenly, collapsed, collapsed);
}

/** La tabla como texto, con las celdas más anchas que `maxWidth` plegadas. */
export function renderTable(
  columns: readonly string[],
  rows: Iterable<readonly unknown[]>,
  maxWidth: number,
): string {
  const cells = [
    [...columns],
    ...[...rows].map((row) => columns.map((_, index) => cellText(row[index]))),
  ];
  const widths = columnWidths(columns, cells, maxWidth);
  const last = columns.length - 1;
  const lines: string[] = [];
  cells.forEach((row, rowIndex) => {
    const folded = row.map((text, index) => {
      const left = index === 0 ? 0 : COLUMN_PADDING;
      const right = index === last ? 0 : COLUMN_PADDING;
      const contentWidth = Math.max(1, (widths[index] ?? 1) - left - right);
      return foldedLines(text, contentWidth).map(
        (line) => " ".repeat(left) + padCell(line, contentWidth) + " ".repeat(right),
      );
    });
    const height = Math.max(...folded.map((cell) => cell.length));
    const isHeader = rowIndex === 0;
    for (let index = 0; index < height; index += 1) {
      lines.push(
        folded
          .map((cell, column) => {
            const offset = isHeader ? height - cell.length : 0;
            return cell[index - offset] ?? " ".repeat(widths[column] ?? 0);
          })
          .join(""),
      );
    }
  });
  return lines.join("\n");
}
