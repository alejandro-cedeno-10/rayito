/**
 * `shlex.join` de Python: cada argumento entre comillas simples salvo que
 * sólo lleve caracteres seguros. `rayito sandbox exec` envía el comando al
 * agente como una sola cadena de shell; los vectores compartidos
 * (`testdata/cli/shell-join-vectors.json`) fijan que las dos CLI producen la
 * misma.
 */

const SAFE_CHARACTERS = /^[A-Za-z0-9_@%+=:,./-]+$/;

function quote(argument: string): string {
  if (argument === "") {
    return "''";
  }
  if (SAFE_CHARACTERS.test(argument)) {
    return argument;
  }
  return `'${argument.replaceAll("'", `'"'"'`)}'`;
}

export function shellJoin(words: readonly string[]): string {
  return words.map(quote).join(" ");
}
