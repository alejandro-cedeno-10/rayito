"""Render the `DashboardBody` of `infra/dashboard-cloudwatch.yaml` from the
SDK's generator (`rayito._dashboard._cloudwatch_board`), so the stack and
`rayito dashboard cloudwatch` can never draw different dashboards.

The block between the two ``GENERATED dashboard-body`` markers is the only
part this script writes; the rest of the template is hand-written. It needs
the Python SDK's dependencies, so run it inside the client environment::

    cd clients/python
    uv run python ../../scripts/gen_cloudwatch_template.py           # rewrite
    uv run python ../../scripts/gen_cloudwatch_template.py --check   # exit 1 on drift

`scripts/tests/test_cloudwatch_template.py` runs the same comparison in CI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SDK_SOURCE = REPO_ROOT / "clients" / "python" / "src"
TEMPLATE = REPO_ROOT / "infra" / "dashboard-cloudwatch.yaml"
BEGIN = "# BEGIN GENERATED dashboard-body"
END = "# END GENERATED dashboard-body"
INDENT = " " * 6

if str(SDK_SOURCE) not in sys.path:
    sys.path.insert(0, str(SDK_SOURCE))

from rayito._dashboard._cloudwatch_board import template_parts


def scalar(text: str) -> str:
    """A YAML double-quoted scalar (JSON string syntax is valid YAML)."""
    return json.dumps(text, ensure_ascii=True)


REGION_PLACEHOLDER = "${AWS::Region}"


def with_region(text: str) -> str:
    """The widgets of an optional block as a CloudFormation expression.

    `Fn::Sub` replaces the variables of the outer string, not the ones inside
    the values it inserts, so a literal `${AWS::Region}` in `OtlpWidgets` or
    `QuotaWidgets` would reach the dashboard unreplaced. `Fn::Join` with a
    `Ref` does resolve it inside a variable value."""
    pieces = text.split(REGION_PLACEHOLDER)
    parts: list[str] = []
    for index, piece in enumerate(pieces):
        if index:
            parts.append('!Ref "AWS::Region"')
        parts.append(scalar(piece))
    return f'!Join ["", [{", ".join(parts)}]]'


def generated_block() -> str:
    body, otlp, quota = template_parts()
    return (
        f"{INDENT}DashboardBody: !Sub\n"
        f"{INDENT}  - {scalar(body)}\n"
        f'{INDENT}  - OtlpWidgets: !If [OtlpWidgets, {with_region(otlp)}, ""]\n'
        f'{INDENT}    QuotaWidgets: !If [QuotaWidgets, {with_region(quota)}, ""]\n'
    )


def render(current: str) -> str:
    head, _, rest = current.partition(f"{BEGIN}\n")
    _, _, tail = rest.partition(f"{END}\n")
    return f"{head}{BEGIN}\n{generated_block()}{END}\n{tail}"


def main(argv: list[str]) -> int:
    current = TEMPLATE.read_text(encoding="utf-8")
    expected = render(current)
    if "--check" in argv:
        if current == expected:
            return 0
        sys.stdout.write(
            "gen_cloudwatch_template: infra/dashboard-cloudwatch.yaml drifts from the SDK "
            "generator; run the script without --check\n"
        )
        return 1
    TEMPLATE.write_text(expected, encoding="utf-8")
    sys.stdout.write("gen_cloudwatch_template: wrote infra/dashboard-cloudwatch.yaml\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
