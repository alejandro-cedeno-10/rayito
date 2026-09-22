"""``check_auditable.py`` on a synthetic ELF64 built here (no binary fixture
committed): the ``.dep-v0`` section is found by name, decompressed and
verified; a binary without it, a wrong root or a missing crate exit 1."""

from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_auditable

ELF_HEADER_SIZE = 64
SECTION_HEADER_SIZE = 64
AARCH64 = 0xB7


def packages(
    root_version: str = "0.1.0", drop: str | None = None
) -> list[dict[str, object]]:
    graph: list[dict[str, object]] = [
        {
            "name": "rayd",
            "version": root_version,
            "root": True,
            "dependencies": [1, 2, 3, 4],
        },
        {"name": "tonic", "version": "0.14.6", "source": "registry"},
        {"name": "tokio", "version": "1.53.1", "source": "registry"},
        {"name": "axum", "version": "0.8.9", "source": "registry"},
        {"name": "nix", "version": "0.31.3", "source": "registry"},
        {"name": "rayd-core", "version": root_version, "source": "local"},
    ]
    return [package for package in graph if package["name"] != drop]


def synthetic_elf(sections: dict[str, bytes]) -> bytes:
    """ELF64 little-endian aarch64 with a null section, ``.shstrtab`` and the
    given sections, section data first and the header table last."""
    names = [".shstrtab", *sections]
    names_table = b"\0" + b"".join(name.encode() + b"\0" for name in names)
    name_offsets = {}
    cursor = 1
    for name in names:
        name_offsets[name] = cursor
        cursor += len(name) + 1
    payloads: list[tuple[str, bytes]] = [(".shstrtab", names_table), *sections.items()]
    data_offset = ELF_HEADER_SIZE
    body = b""
    headers = [struct.pack("<IIQQQQIIQQ", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)]
    for name, payload in payloads:
        offset = data_offset + len(body)
        headers.append(
            struct.pack(
                "<IIQQQQIIQQ",
                name_offsets[name],
                1,
                0,
                0,
                offset,
                len(payload),
                0,
                0,
                1,
                0,
            )
        )
        body += payload
    table_offset = data_offset + len(body)
    header = struct.pack(
        "<4sBBBBB7sHHIQQQIHHHHHH",
        b"\x7fELF",
        2,
        1,
        1,
        0,
        0,
        b"\0" * 7,
        2,
        AARCH64,
        1,
        0,
        0,
        table_offset,
        0,
        ELF_HEADER_SIZE,
        0,
        0,
        SECTION_HEADER_SIZE,
        len(headers),
        1,
    )
    return header + body + b"".join(headers)


def auditable_elf(graph: list[dict[str, object]]) -> bytes:
    payload = zlib.compress(json.dumps({"packages": graph}).encode())
    return synthetic_elf({".note.gnu.build-id": b"\x01\x02", ".dep-v0": payload})


def test_reads_dep_v0_and_reports_root_and_count(tmp_path: Path) -> None:
    binary = tmp_path / "rayd"
    binary.write_bytes(auditable_elf(packages()))
    report = check_auditable.check_binary(binary, "0.1.0")
    assert report == check_auditable.DependencyReport(6, "rayd", "0.1.0")


def test_main_prints_count_and_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binary = tmp_path / "rayd"
    binary.write_bytes(auditable_elf(packages()))
    assert check_auditable.main([str(binary), "--expected-version", "0.1.0"]) == 0
    assert capsys.readouterr().out.strip() == ".dep-v0: 6 packages, root rayd 0.1.0"


def test_default_expected_version_is_the_workspace_version(tmp_path: Path) -> None:
    expected = check_auditable.workspace_version()
    binary = tmp_path / "rayd"
    binary.write_bytes(auditable_elf(packages(root_version=expected)))
    assert check_auditable.main([str(binary)]) == 0


def test_binary_without_section_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binary = tmp_path / "rayd"
    binary.write_bytes(synthetic_elf({".text": b"\x00" * 8}))
    assert check_auditable.main([str(binary), "--expected-version", "0.1.0"]) == 1
    assert "no .dep-v0 section" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("graph", "expected", "message"),
    [
        (packages(root_version="0.2.0"), "0.1.0", "expected '0.1.0'"),
        (packages(drop="nix"), "0.1.0", "required package 'nix' is missing"),
        (
            [{"name": "other", "version": "1.0.0", "root": True}],
            "1.0.0",
            "root package is 'other'",
        ),
    ],
)
def test_wrong_graph_exits_1_naming_the_item(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    graph: list[dict[str, object]],
    expected: str,
    message: str,
) -> None:
    binary = tmp_path / "rayd"
    binary.write_bytes(auditable_elf(graph))
    assert check_auditable.main([str(binary), "--expected-version", expected]) == 1
    assert message in capsys.readouterr().err


def test_not_an_elf_and_missing_file_exit_1(tmp_path: Path) -> None:
    junk = tmp_path / "junk"
    junk.write_bytes(b"MZ" + b"\0" * 100)
    assert check_auditable.main([str(junk), "--expected-version", "0.1.0"]) == 1
    assert (
        check_auditable.main([str(tmp_path / "absent"), "--expected-version", "0.1.0"])
        == 1
    )
