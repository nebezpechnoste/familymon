"""CRX3 packaging of the lesson-block extension: layout, signature, ids."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from familymon.policy.crx import (
    CRX_NAME,
    EXTENSION_SRC,
    ID_NAME,
    UPDATE_NAME,
    VERSION_NAME,
    ExtensionRef,
    _field,
    build_extension,
    crx_id_bytes,
    extension_id,
    generate_key,
    load_extension_ref,
    pack_crx,
    parse_crx,
    public_key_der,
    update_manifest,
    version_from_timestamp,
    zip_source,
)


@pytest.fixture(scope="module")
def key(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("key") / "ext.pem"
    generate_key(path)
    return path


def _verify(key_pem: Path, data: bytes, signature: bytes) -> bool:
    pub = subprocess.run(
        ["openssl", "rsa", "-in", str(key_pem), "-pubout"], check=True, capture_output=True
    ).stdout
    d = key_pem.parent
    (d / "pub.pem").write_bytes(pub)
    (d / "data.bin").write_bytes(data)
    (d / "sig.bin").write_bytes(signature)
    r = subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            str(d / "pub.pem"),
            "-signature",
            str(d / "sig.bin"),
            str(d / "data.bin"),
        ],
        capture_output=True,
    )
    return r.returncode == 0


# ---- ids ----------------------------------------------------------------------


def test_key_is_private_and_id_well_formed(key):
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    ext_id = extension_id(public_key_der(key))
    assert len(ext_id) == 32
    assert all("a" <= c <= "p" for c in ext_id)


def test_extension_id_is_chromes_formula():
    # Chrome: hex of sha256(SubjectPublicKeyInfo)[:16], digits 0-f -> a-p.
    import hashlib

    spki = b"not a real key but any bytes hash the same way"
    expected = (
        hashlib.sha256(spki)
        .hexdigest()[:32]
        .translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))
    )
    assert extension_id(spki) == expected
    assert crx_id_bytes(spki) == hashlib.sha256(spki).digest()[:16]


# ---- archive ----------------------------------------------------------------


def test_zip_contains_the_source_with_version_patched():
    archive = zip_source(EXTENSION_SRC, "1.2.3.4")
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
    expected = {p.relative_to(EXTENSION_SRC).as_posix() for p in EXTENSION_SRC.iterdir()}
    assert names == expected
    assert {"manifest.json", "background.js", "rules.js", "blocked.html", "schema.json"} <= names
    assert manifest["version"] == "1.2.3.4"
    assert manifest["manifest_version"] == 3
    assert manifest["storage"]["managed_schema"] == "schema.json"


def test_zip_is_deterministic():
    assert zip_source(EXTENSION_SRC, "1.0") == zip_source(EXTENSION_SRC, "1.0")
    assert zip_source(EXTENSION_SRC, "1.0") != zip_source(EXTENSION_SRC, "1.1")


# ---- crx ----------------------------------------------------------------------


def test_pack_layout_and_signature_verify(key):
    data = pack_crx(EXTENSION_SRC, key, "1.0.0")
    parsed = parse_crx(data)
    spki = public_key_der(key)
    assert data[:4] == b"Cr24"
    assert parsed.public_key == spki
    assert parsed.signed_header_data == _field(1, crx_id_bytes(spki))
    assert parsed.archive == zip_source(EXTENSION_SRC, "1.0.0")
    assert _verify(key, parsed.signed_bytes, parsed.signature)


def test_pack_is_deterministic(key):
    assert pack_crx(EXTENSION_SRC, key, "1.0.0") == pack_crx(EXTENSION_SRC, key, "1.0.0")


def test_tampered_archive_fails_verification(key):
    data = bytearray(pack_crx(EXTENSION_SRC, key, "1.0.0"))
    data[-1] ^= 0xFF
    parsed = parse_crx(bytes(data))
    assert not _verify(key, parsed.signed_bytes, parsed.signature)


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        parse_crx(b"PK\x03\x04 definitely a zip, not a crx")


# ---- manifest + build -----------------------------------------------------


def test_version_from_timestamp_has_no_leading_zeros():
    assert version_from_timestamp(datetime(2026, 10, 5, 9, 5)) == "1.2610.5.905"
    assert version_from_timestamp(datetime(2026, 10, 13, 19, 5)) == "1.2610.13.1905"
    assert version_from_timestamp(datetime(2027, 1, 1, 0, 0)) == "1.2701.1.000"
    parts = version_from_timestamp(datetime(2026, 10, 13, 19, 5)).split(".")
    assert all(int(p) < 65536 and (p == "0" or not p.startswith("0")) for p in parts)


def test_update_manifest_points_at_the_crx():
    xml = update_manifest("a" * 32, "file:///x/familymon.crx", "1.2.3")
    assert "appid='" + "a" * 32 + "'" in xml
    assert "codebase='file:///x/familymon.crx'" in xml
    assert "version='1.2.3'" in xml


def test_build_writes_artefacts_and_load_ref_reads_them(key, tmp_path):
    out = tmp_path / "out"
    deployed = Path("/usr/share/familymon/chrome-extension")
    ref = build_extension(key, out, "1.2610.13.1905", deployed_dir=deployed)
    assert ref == ExtensionRef(
        id=extension_id(public_key_der(key)),
        update_url=f"file://{deployed}/{UPDATE_NAME}",
        version="1.2610.13.1905",
    )
    assert ref.forcelist_entry == f"{ref.id};file://{deployed}/{UPDATE_NAME}"
    for name in (CRX_NAME, UPDATE_NAME, ID_NAME, VERSION_NAME):
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o644
    assert (out / ID_NAME).read_text().strip() == ref.id
    assert f"codebase='file://{deployed}/{CRX_NAME}'" in (out / UPDATE_NAME).read_text()
    assert parse_crx((out / CRX_NAME).read_bytes()).public_key == public_key_der(key)
    # The policy writer reads the deployed dir back into an equivalent ref,
    # with URLs rooted wherever it found the files.
    back = load_extension_ref(out)
    assert back is not None
    assert (back.id, back.version) == (ref.id, ref.version)
    assert back.update_url == f"file://{out}/{UPDATE_NAME}"


def test_build_generates_a_key_when_missing(tmp_path):
    key = tmp_path / "fresh" / "key.pem"
    ref = build_extension(key, tmp_path / "out", "1.0", deployed_dir=tmp_path / "out")
    assert key.exists()
    assert len(ref.id) == 32


def test_load_ref_absent_or_corrupt(tmp_path):
    assert load_extension_ref(tmp_path / "nope") is None
    (tmp_path / ID_NAME).write_text("not-an-id\n")
    (tmp_path / VERSION_NAME).write_text("1.0\n")
    assert load_extension_ref(tmp_path) is None
    (tmp_path / ID_NAME).write_text("a" * 32 + "\n")
    assert load_extension_ref(tmp_path) is None  # crx + update.xml missing
    (tmp_path / CRX_NAME).write_bytes(b"x")
    (tmp_path / UPDATE_NAME).write_text("x")
    assert load_extension_ref(tmp_path) is not None
    os.chmod(tmp_path, 0o755)
