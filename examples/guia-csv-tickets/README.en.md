# Guide: a tickets CSV in a sandbox, with the CLI, Python and TypeScript

[Español](README.md)

Complete lab for the Spanish guide «Un sandbox en tu cuenta de AWS en
minutos: Rayito con la CLI, Python y TypeScript»: the synthetic CSV, the
contract that defines the correct result, one script per step in Python and
TypeScript, the agent configuration and the lockfiles. It is pinned to
**Rayito 0.15.0** in every piece: `rayito[cli]==0.15.0` from PyPI,
`rayito@0.15.0` from npm, the CLI they ship and `rayd` 0.15.0 in the images.
See "Versions" at the end. File names, scripts and output are in Spanish, as
in the guide.

To get exactly what the guide uses, clone the repository and check out the
commit its link points to:

```bash
git clone https://github.com/alejandro-cedeno-10/rayito.git
cd rayito && git checkout <commit-from-the-link>
cd examples/guia-csv-tickets
```

Everything these scripts create is a resource in your AWS account and shows
up on your bill. Read "Costs and cleanup" before running steps 06 to 10.

## Layout

```
datos/tickets.csv          the synthetic CSV (7 tickets)
contrato/esperado.json     the expected result per category
contrato/verificar.py      deterministic computation, standard library only
app/resumen.py             the app that Template.copy("app/", ...) copies
python/NN_*.py             one runnable script per example (+ _comun.py)
typescript/NN_*.ts         the same in TypeScript (+ comun.ts)
agente-triaje/             option B: rayito-agent.toml + AGENTS.md
opencode-sin-toml/         minimal reproduction of the OpenCode detection
cli/comandos.sh            the guide's CLI commands, commented
mi_template.py             the 06 template as a variable, for `rayito template build`
pyproject.toml, uv.lock    Python dependencies (uv)
package.json, pnpm-lock.yaml, pnpm-workspace.yaml, tsconfig.json   TypeScript (pnpm)
```

Run everything **from this folder**: the scripts read `datos/` and
`contrato/`, and the template copies `app/` relative to it. In the guide the
option B project is called `./triaje`; here it is `agente-triaje/`.

## Requirements

- Python 3.11 or later and [uv](https://docs.astral.sh/uv/) (tested with
  Python 3.12.14 and uv 0.12.18). pip works too:
  `pip install "rayito[cli]==0.15.0" "aws-bedrock-token-generator==1.1.0"`.
- Node 20 or later and pnpm (tested with Node 22.23.2 and pnpm 12.14.0).
  TypeScript 5.2 or later for `await using`; the lockfile pins
  `typescript@7.0.2`, `tsx@4.23.15` and `@types/node@26.6.5`.
- An AWS account prepared once with `rayito setup` (admin credentials only
  for that step) in a Region with Lambda MicroVMs, tested in `us-east-1`.
  The exact steps are in section 1 of `cli/comandos.sh` and in
  [Set up AWS](https://alejandro-cedeno-10.github.io/rayito/en/primeros-pasos/configurar-aws/);
  running the examples only needs a role with `SandboxLauncherPolicy`.
- For 09 and 10, access to the Amazon Bedrock model they use (Claude Haiku
  4.5 through the `us.` inference profile).

```bash
uv sync                          # creates .venv with rayito 0.15.0
pnpm install --frozen-lockfile   # installs rayito@0.15.0 and the tools
python3 contrato/verificar.py    # the contract, no AWS
pnpm run typecheck               # type check, no AWS
```

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `AWS_PROFILE`, `AWS_REGION` | credentials and Region | your environment's |
| `RAYITO_TEMPLATE` | sandbox image | `rayito-base` |
| `RAYITO_BUCKET` | artifacts bucket (printed by `rayito setup`) | none; required by 06 and the image CLI |
| `TEMPLATE_NAME` | image name for example 06 | `tickets-demo` |
| `AGENT_TEMPLATE` | image with the agent | `rayito-agent` |
| `MODEL_SECRET` | secret name (stored as `rayito/<name>`) | `bedrock-key` |
| `RAYITO_APP_TAG` | value of the `metadata.app` tag | `tickets-demo` |

No variable holds a secret value except `AWS_BEARER_TOKEN_BEDROCK`, which
only `08_crear_secreto` reads and which you must never write to a file.

## The examples

| Step | Python | TypeScript | What it checks |
|---|---|---|---|
| 01 | `uv run python/01_primer.py` | `pnpm exec tsx typescript/01_primer.ts` | command, file and stateful cell (`hola`, `contenido`, `42`) |
| 02 | `uv run python/02_tickets.py` | `pnpm exec tsx typescript/02_tickets.ts` | per-category summary compared with `contrato/esperado.json`; exits 1 if a cell fails, the contract does not hold or there is no PNG |
| 03 | `uv run python/03_error_intencional.py` | `pnpm exec tsx typescript/03_error_intencional.ts` | a deliberate `KeyError` arrives as `execution.error` |
| 04 | `uv run python/04_reconectar.py` | `pnpm exec tsx typescript/04_reconectar.ts` | a second handle reads what the first one wrote |
| 05 | `uv run python/05_shim_e2b.py` | `pnpm exec tsx typescript/05_shim_e2b.ts` | the E2B shim returns `42` |
| 06 | `uv run python/06_template.py` | `pnpm exec tsx typescript/06_template.ts` | custom image: `tabulate` 0.9.0, a server on port 8000 and `app/resumen.py` |
| 07 | `uv run python/07_pool.py` | `pnpm exec tsx typescript/07_pool.ts` | `create()` versus taking a prepared slot, with hit or fallback |
| 08 | `uv run python/08_crear_secreto.py [--generar]` | `pnpm exec tsx typescript/08_crear_secreto.ts` | stores `Bearer <key>` in `rayito/$MODEL_SECRET` without printing it |
| 09 | `uv run python/09_agente.py` | `pnpm exec tsx typescript/09_agente.ts` | the agent uses `python3` (per the trace) and its counts match the contract |
| 10 | `uv run python/10_agente_negativo.py` | `pnpm exec tsx typescript/10_agente_negativo.ts` | optional negative test: with everything denied, the requested file does not appear even if the model says it created it |
| 99 | `uv run python/99_limpieza_secreto.py [--forzar]` | `pnpm exec tsx typescript/99_limpieza_secreto.ts [--forzar]` | deletes the secret (with a recovery window by default) |

Both languages do the same in 10 and 99. In the guide's lab, 10 ran in
Python; the TypeScript versions of 10 and 99 were checked with `tsc`, not
against AWS.

Expected result of 02 (the contract):

| category | tickets | mean hours |
|---|---|---|
| acceso | 2 | 1.5 |
| facturacion | 3 | 4.33 |
| rendimiento | 2 | 8.0 |
| **total** | **7** | |

The chart PNG is not part of the contract. In the guide's runs it came out
byte-identical between Python and TypeScript with the same image version,
but that depends on the matplotlib version and its fonts; do not use it as a
check.

### Before step 09

The agent needs three things that cost money and that the scripts do not
create on their own (they are in `cli/comandos.sh`):

1. the `rayito-base-caps` image (`rayito image publish ... --os-capabilities ALL`);
2. the `rayito-agent` image (`rayito agent template build`);
3. the secret with the model key (`08_crear_secreto`).

`09_agente` does not trust the model's final answer: it looks in
`result.tool_calls` for a completed `bash` call whose command contains
`python3` and compares the counts in the answer with the contract. It is
still a limited result: another model, runtime version or prompt wording can
change it, and the script will say so by exiting 1.

The instruction «No modifiques ficheros» is a request to the model. The
permission configuration blocks the edit tools and restricts the `bash` tool
to commands that match `python3 *`. That does not make the environment
read-only: Python can still modify files the process can reach. Preventing
that takes additional file system restrictions or a tool that runs only one
predefined operation. Permissions are not a security boundary either: the
boundary is the MicroVM with Internet egress closed.

`10_agente_negativo` detects an unsupported answer: the model may claim it
created the file, and the script checks the trace and the sandbox's file
system. No tool calls does not, on its own, prove that a specific permission
rule was applied.

### OpenCode detection without a Rayito file

`opencode-sin-toml/` has only an `opencode.json` (denying `edit`, `bash` and
`webfetch`) and an `AGENTS.md`. To see which project the detection resolves,
without calling AWS, copy it to a temporary directory and run
`AWS_REGION=us-east-1 rayito agent detect --write` inside it; look at the
`agent` section of the resulting `rayito-agent.json`. With 0.15.0 it is
missing (no instructions, no permissions); with 0.17.0 both are there.

## Costs and cleanup

AWS list prices in `us-east-1` (USD) as collected in the Rayito 0.15.0 docs
([Pricing](https://alejandro-cedeno-10.github.io/rayito/en/cost/)), not an
observed bill. For what you actually paid, check Cost Explorer a day or two
later.

- 01 to 05 and 07: seconds of MicroVM; cents in total.
- 06: one new image version per run, ≈ 0.04 USD per week with a one-week
  minimum even if you delete it earlier.
- `rayito-base-caps` and `rayito-agent`: one version each (the agent one,
  ≈ 0.056 USD per week).
- 08: one secret, 0.40 USD per month prorated by the hour, plus 0.05 USD per
  10,000 calls.
- 09 and 10: model tokens, billed separately by Bedrock.

Cleanup: `rayito sandbox list`, `rayito image prune` and the commands at the
end of `cli/comandos.sh`. `99_limpieza_secreto` schedules deletion with a
7-day recovery window; `--forzar` deletes without recovery and only makes
sense for a secret created for this test. Terminating a sandbox does not undo
what its code did outside it (API calls, writes to other services).

Be careful with `rayito sandbox kill --all`: it terminates every MicroVM in
the account and Region, including other people's; if you share the account,
kill by id.

## Versions

Everything was tested with 0.15.0. Newer versions have been released since
([releases](https://github.com/alejandro-cedeno-10/rayito/releases)); these
examples have not been repeated with them and their results are not
attributed to any version other than 0.15.0. The only known difference that
affects this lab is the OpenCode detection without a Rayito file
(`opencode-sin-toml/`), fixed in 0.17.0. The exact 0.15.0 documentation is in
the [tagged source](https://github.com/alejandro-cedeno-10/rayito/tree/python-v0.15.0/docs/site/docs).
This folder's dependencies are pinned by its lockfiles on purpose, and the
folder is not in the Dependabot configuration.
