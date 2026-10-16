/**
 * Un grupo que también corre solo (`rayito dashboard --port 8080`, el
 * `invoke_without_command` de Typer): se resuelve a su comando raíz, la ayuda
 * lo describe y una variable de entorno de una opción repetible se lee como
 * entradas separadas por espacios.
 */

import { describe, expect, it } from "vitest";
import { defineCommand, defineGroup, option } from "../../../src/cli/command.js";
import { helpFor } from "../../../src/cli/help.js";
import { type CommandTree, parseInvocation } from "../../../src/cli/parse.js";

const root = defineCommand({
  path: ["panel"],
  help: "El panel.",
  parameters: [
    option(["--port"], { type: "int", default: 0, help: "Puerto." }),
    option(["--account"], { multiple: true, envvar: "PANEL_ACCOUNTS", help: "Cuentas." }),
  ],
  run: async () => undefined,
});
const child = defineCommand({
  path: ["panel", "sub"],
  help: "Un subcomando.",
  parameters: [],
  run: async () => undefined,
});
const tree: CommandTree = {
  groups: [defineGroup({ path: ["panel"], help: "Grupo.", root })],
  commands: [child],
};

describe("group with a root command", () => {
  it("runs the root without a subcommand, with its options", () => {
    const invocation = parseInvocation(["panel", "--port", "8080"], tree, {});
    expect(invocation.kind).toBe("run");
    if (invocation.kind === "run") {
      expect(invocation.command).toBe(root);
      expect(invocation.values.number("port")).toBe(8080);
    }
  });

  it("runs the root with no arguments at all after the group", () => {
    const invocation = parseInvocation(["panel"], tree, {});
    expect(invocation.kind === "run" && invocation.command === root).toBe(true);
  });

  it("still resolves a subcommand", () => {
    const invocation = parseInvocation(["panel", "sub"], tree, {});
    expect(invocation.kind === "run" && invocation.command === child).toBe(true);
  });

  it("an unknown word is an unexpected argument of the root", () => {
    expect(() => parseInvocation(["panel", "nope"], tree, {})).toThrow(
      "argumentos sobrantes: nope",
    );
  });

  it("--help describes the root and lists the subcommands", () => {
    const invocation = parseInvocation(["panel", "--help"], tree, {});
    expect(invocation).toEqual({ kind: "help", path: ["panel"], bare: false });
    const help = helpFor(["panel"], tree);
    expect(help).toContain("--port");
    expect(help).toContain("El panel.");
    expect(help).toContain("Comandos:");
    expect(help).toContain("sub");
  });

  it("a repeatable option reads its variable as space-separated entries", () => {
    const invocation = parseInvocation(["panel"], tree, { PANEL_ACCOUNTS: "uno=a  dos=b\ttres=c" });
    expect(invocation.kind === "run" && invocation.values.strings("account")).toEqual([
      "uno=a",
      "dos=b",
      "tres=c",
    ]);
  });

  it("the command line wins over the variable", () => {
    const invocation = parseInvocation(["panel", "--account", "x=y"], tree, {
      PANEL_ACCOUNTS: "uno=a",
    });
    expect(invocation.kind === "run" && invocation.values.strings("account")).toEqual(["x=y"]);
  });
});
