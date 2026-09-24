"""``check_pins.py`` over crafted text and a temporary tree: the spellings the
old denylist grep missed (``@1.2.3``, ``@latest``, ``@release-v2`` and a
truncated ``@ab12cd34``) are findings, a full SHA with or without its version
comment and a local ``./`` action are not, every ``uvx`` without ``==`` is a
finding in both the bare and the ``--from`` form, every ``curl`` of a
``Dockerfile`` needs a pinned ``<NAME>_SHA256=`` checked by ``sha256sum -c``
and no floating release, a ``dnf install`` of a package in
``PINNED_DNF_PACKAGES`` needs ``-<version>-<release>``, ``main`` reports every
file and exits 1 — and the real repository is clean and ships the pinned
``git-core``."""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_pins

REPO_ROOT = SCRIPTS.parent
FULL_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
DENO_SHA256 = "c832298b1ad4422481334855f6003e0f54145762c5a134f20a489511d2f65bbf"
DENO_LAYER = f"""RUN if [ "$(cat /opt/rayito/sidecar/kernels_variant 2>/dev/null)" = "poly" ]; then \\
      python3 -m pip install --no-cache-dir --break-system-packages \\
           -r /opt/rayito/sidecar/requirements-poly.txt \\
      && python3 -m pip check \\
      && su user -c "python3 -c 'import bash_kernel'" \\
      && test -f /opt/rayito/sidecar/jupyter/kernels/rayito-bash/kernel.json \\
      && DENO_VERSION=2.9.7 \\
      && DENO_SHA256={DENO_SHA256} \\
      && curl -fsSL --retry 3 -o /tmp/deno.zip \\
           "https://github.com/denoland/deno/releases/download/v${{DENO_VERSION}}/deno-aarch64-unknown-linux-gnu.zip" \\
      && echo "${{DENO_SHA256}}  /tmp/deno.zip" | sha256sum -c - \\
      && mkdir -p /opt/rayito/deno \\
      && python3 -c "import zipfile; zipfile.ZipFile('/tmp/deno.zip').extract('deno', '/opt/rayito/deno')" \\
      && rm -f /tmp/deno.zip \\
      && chown -R root:root /opt/rayito/deno \\
      && chmod 0755 /opt/rayito/deno /opt/rayito/deno/deno \\
      && su user -c "DENO_NO_UPDATE_CHECK=1 NO_COLOR=1 /opt/rayito/deno/deno --version" | grep -q "^deno ${{DENO_VERSION}} " \\
      && test -f /opt/rayito/sidecar/jupyter/kernels/rayito-javascript/kernel.json \\
      && test -f /opt/rayito/sidecar/jupyter/kernels/rayito-typescript/kernel.json; \\
    fi"""
GIT_CORE_NEVRA = "git-core-2.50.1-1.amzn2023.0.1"


def test_tag_alias_branch_and_short_sha_are_unpinned() -> None:
    text = """      - uses: actions/checkout@v7
      - uses: owner/action@main
      - uses: owner/action@master
      - uses: owner/action@release/1.0
      - uses: owner/action@1.2.3
      - uses: owner/action@latest
      - uses: owner/action@release-v2
      - uses: owner/action@ab12cd34"""

    findings = check_pins.unpinned_actions(text)

    assert [number for number, _, _ in findings] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert all(reason == check_pins.ACTION_REASON for _, _, reason in findings)


def test_full_sha_local_action_and_quoted_uses_are_pinned() -> None:
    text = f"""      - uses: actions/checkout@{FULL_SHA} # v7.0.1
      - uses: actions/checkout@{FULL_SHA}
      - uses: github/codeql-action/upload-sarif@{FULL_SHA} # v4.31.2
      - uses: ./.github/actions/local
      # - uses: foo@v1
      - name: no unpinned actions (every `uses:` is a commit SHA)
        run: grep -nE 'uses: [^@]+@v[0-9]' .github/workflows/*.yml"""

    assert check_pins.unpinned_actions(text) == []


def test_uvx_without_a_version_is_a_finding() -> None:
    text = """\tuvx twine check dist/*
\tuvx cfn-lint --version
          uvx ruff check .
          uvx --from "rayito[mcp]" rayito-mcp"""

    findings = check_pins.unpinned_uvx(text)

    assert [number for number, _, _ in findings] == [1, 2, 3, 4]
    assert all(reason == check_pins.UVX_REASON for _, _, reason in findings)


def test_pinned_uvx_invocations_pass() -> None:
    text = """        run: uvx pip-audit==2.10.1 -r req.txt --no-deps --strict
\tuvx twine==7.0.0 check dist/*
\tuvx ruff==0.16.7 format --check .
\tuvx cfn-lint==1.56.3 -- $(EGRESS_TEMPLATE) $(IAM_TEMPLATE)
          uvx --from pkg==1.0 tool
# uvx twine check dist/*"""

    assert check_pins.unpinned_uvx(text) == []


def test_unverified_downloads_are_findings() -> None:
    text = f"""RUN curl -fsSL -o /tmp/tool.tgz https://example.com/tool.tgz
RUN TOOL_SHA256={DENO_SHA256} \\
    && curl -fsSL -o /tmp/tool.tgz https://example.com/tool.tgz
RUN TOOL_SHA256={DENO_SHA256[:63]} \\
    && curl -fsSL -o /tmp/tool.tgz https://example.com/tool.tgz \\
    && echo "${{TOOL_SHA256}}  /tmp/tool.tgz" | sha256sum -c -
RUN TOOL_SHA256={DENO_SHA256} \\
    && curl -fsSL -o /tmp/tool.tgz https://github.com/o/r/releases/latest/download/tool.tgz \\
    && echo "${{TOOL_SHA256}}  /tmp/tool.tgz" | sha256sum -c -"""

    findings = check_pins.unpinned_downloads(text)

    assert [number for number, _, _ in findings] == [1, 2, 4, 7]
    assert all(reason == check_pins.DOWNLOAD_REASON for _, _, reason in findings)
    assert findings[1][1] == f"RUN TOOL_SHA256={DENO_SHA256} \\"


def test_the_deno_layer_and_commented_curls_pass() -> None:
    text = f"""#   sin Docker:  TOKEN=$(curl -fsSL "https://example.com/token" | jq -r .token)
#                curl -fsSI -H "Authorization: Bearer $TOKEN" \\
FROM scratch

{DENO_LAYER}
RUN echo done"""

    assert check_pins.unpinned_downloads(text) == []


def test_a_second_download_in_a_verified_instruction_is_a_finding() -> None:
    text = f"""RUN TOOL_SHA256={DENO_SHA256} \\
    && curl -fsSL -o /tmp/tool.tgz https://example.com/tool.tgz \\
    && echo "${{TOOL_SHA256}}  /tmp/tool.tgz" | sha256sum -c - \\
    && curl -fsSL -o /tmp/other.tgz https://example.com/other.tgz"""

    findings = check_pins.unpinned_downloads(text)

    assert [number for number, _, _ in findings] == [1]


def test_a_download_piped_or_written_to_stdout_is_a_finding() -> None:
    text = f"""RUN TOOL_SHA256={DENO_SHA256} \\
    && curl -fsSL -o /tmp/tool.tgz https://example.com/tool.tgz \\
    && echo "${{TOOL_SHA256}}  /tmp/tool.tgz" | sha256sum -c - \\
    && curl -fsSL https://example.com/install.sh | sh
RUN TOOL_SHA256={DENO_SHA256} \\
    && curl -fsSL https://example.com/tool.tgz > /tmp/tool.tgz \\
    && echo "${{TOOL_SHA256}}  /tmp/tool.tgz" | sha256sum -c -"""

    findings = check_pins.unpinned_downloads(text)

    assert [number for number, _, _ in findings] == [1, 5]


def test_the_checked_path_must_be_the_downloaded_path() -> None:
    text = f"""RUN TOOL_SHA256={DENO_SHA256} \\
    && curl -fsSL -o /tmp/tool.tgz https://example.com/tool.tgz \\
    && echo "${{TOOL_SHA256}}  /tmp/elsewhere.tgz" | sha256sum -c -"""

    findings = check_pins.unpinned_downloads(text)

    assert [number for number, _, _ in findings] == [1]


def test_wget_and_remote_add_are_findings() -> None:
    text = f"""RUN TOOL_SHA256={DENO_SHA256} \\
    && wget -O /tmp/tool.tgz https://example.com/tool.tgz \\
    && echo "${{TOOL_SHA256}}  /tmp/tool.tgz" | sha256sum -c -
ADD https://example.com/tool.tgz /tmp/tool.tgz
ADD --checksum=sha256:{DENO_SHA256} https://example.com/tool.tgz /tmp/tool.tgz
ADD kernel-sidecar/ /opt/rayito/sidecar/"""

    findings = check_pins.unpinned_downloads(text)

    assert [number for number, _, _ in findings] == [1, 4, 5]


def test_only_files_named_dockerfile_get_the_download_gate(tmp_path: Path) -> None:
    unpinned = "RUN curl -fsSL -o /tmp/x https://example.com/x\n"
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(unpinned, encoding="utf-8")
    makefile = tmp_path / "Makefile"
    makefile.write_text(unpinned, encoding="utf-8")

    assert check_pins.findings_for(dockerfile) == [
        (1, unpinned.strip(), check_pins.DOWNLOAD_REASON)
    ]
    assert check_pins.findings_for(makefile) == []


def test_main_checks_the_image_dockerfile(tmp_path: Path) -> None:
    image = tmp_path / "image"
    image.mkdir()
    dockerfile = image / "Dockerfile"
    dockerfile.write_text(
        "FROM scratch\nRUN curl -fsSL https://example.com/x | sh\n", encoding="utf-8"
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = check_pins.main([], root=tmp_path)

    assert code == 1
    assert "KO image/Dockerfile:2: " + check_pins.DOWNLOAD_REASON in stdout.getvalue()

    dockerfile.write_text(f"FROM scratch\n{DENO_LAYER}\n", encoding="utf-8")
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = check_pins.main([], root=tmp_path)

    assert code == 0
    assert "OK 1 fichero(s)" in stdout.getvalue()
    assert "image/Dockerfile" in stdout.getvalue()


def test_only_a_bare_git_core_is_a_dnf_finding() -> None:
    text = f"""RUN dnf install -y --setopt=install_weak_deps=0 \\
      python3.12 tar git-core \\
    && dnf clean all
RUN dnf install -y --setopt=install_weak_deps=0 \\
      python3.12 tar {GIT_CORE_NEVRA} \\
    && dnf clean all
RUN dnf install -y --setopt=install_weak_deps=0 jq \\
    && dnf clean all"""

    findings = check_pins.unpinned_dnf_packages(text)

    assert findings == [(1, "git-core", check_pins.DNF_REASON)]


def test_a_version_without_release_is_a_dnf_finding() -> None:
    text = """# RUN dnf install -y git-core
RUN dnf -y install git-core-2.50.1 && dnf clean all
RUN echo git-core"""

    assert check_pins.unpinned_dnf_packages(text) == [
        (2, "git-core-2.50.1", check_pins.DNF_REASON)
    ]


def test_main_reports_a_bare_git_core_in_the_image_dockerfile(tmp_path: Path) -> None:
    image = tmp_path / "image"
    image.mkdir()
    (image / "Dockerfile").write_text(
        "FROM scratch\nRUN dnf install -y \\\n      tar git-core \\\n    && dnf clean all\n",
        encoding="utf-8",
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = check_pins.main([], root=tmp_path)

    assert code == 1
    assert "KO image/Dockerfile:2: " + check_pins.DNF_REASON in stdout.getvalue()
    assert "    git-core" in stdout.getvalue()


def test_the_image_installs_the_pinned_git_core_and_checks_git() -> None:
    text = (REPO_ROOT / "image" / "Dockerfile").read_text(encoding="utf-8")
    instructions = check_pins.dockerfile_instructions(text)
    first_dnf = next(i.text for i in instructions if "dnf install" in i.text)
    tool_check = next(
        i.text for i in instructions if i.text.startswith("RUN for tool in ")
    )
    checked_tools = tool_check.removeprefix("RUN for tool in ").split(";")[0].split()

    assert GIT_CORE_NEVRA in check_pins.dnf_install_packages(first_dnf)
    assert "git" in checked_tools


def test_main_reports_every_finding_and_exits_one(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  check:\n    steps:\n      - uses: actions/checkout@v7\n",
        encoding="utf-8",
    )
    (tmp_path / "Makefile").write_text(
        "lint:\n\tuvx ruff check scripts\n", encoding="utf-8"
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = check_pins.main([], root=tmp_path)

    printed = stdout.getvalue()
    assert code == 1
    assert ".github/workflows/ci.yml:4" in printed
    assert "Makefile:2" in printed


def test_main_accepts_a_clean_tree(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        f"jobs:\n  check:\n    steps:\n      - uses: actions/checkout@{FULL_SHA} # v7.0.1\n",
        encoding="utf-8",
    )
    (tmp_path / "Makefile").write_text(
        "lint:\n\tuvx ruff==0.16.7 check scripts\n", encoding="utf-8"
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = check_pins.main([], root=tmp_path)

    assert code == 0
    assert "OK 2 fichero(s)" in stdout.getvalue()


def test_the_repository_itself_is_clean() -> None:
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = check_pins.main([], root=REPO_ROOT)

    assert code == 0, stdout.getvalue()
    assert stdout.getvalue().startswith("OK ")
    assert "image/Dockerfile" in stdout.getvalue()
