/** `process.argv` empieza con el ejecutable de Node y la ruta del script. */
const NODE_ARGV_PREFIX = 2;

/** Los argumentos que escribió la persona, sin el ejecutable ni el script. */
export function userArguments(argv: readonly string[] = process.argv): string[] {
  return argv.slice(NODE_ARGV_PREFIX);
}
