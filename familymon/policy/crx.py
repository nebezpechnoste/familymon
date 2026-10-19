"""Package the lesson-block Chrome extension as a CRX3 and describe it to
Chrome's policy.

Chrome only force-installs extensions from a CRX reachable through an update
manifest, so the deb ships three generated files under EXTENSION_DIR: the
signed `familymon.crx`, the `update.xml` Chrome polls, and the extension `id`
(derived from the signing key). The policy writer (policy/webblock.py) reads
the id and points ExtensionInstallForcelist at the update manifest.

CRX3 layout (components/crx_file/crx3.proto in Chromium):

    "Cr24" | u32le 3 | u32le len(header) | header | zip

header = CrxFileHeader{ sha256_with_rsa(2) = AsymmetricKeyProof{public_key(1),
signature(2)}, signed_header_data(10000) = SignedData{crx_id(1)} } where
crx_id = sha256(SubjectPublicKeyInfo)[:16] and the RSA-PKCS1-SHA256 signature
covers "CRX3 SignedData\\0" | u32le len(signed_header_data) |
signed_header_data | zip. Signing uses the openssl CLI — no new Python
dependency on the kid's machine, which never builds anything anyway.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

EXTENSION_SRC = Path(__file__).resolve().parent.parent / "chrome_extension"
# Where the deb installs the generated artefacts (world-readable: Chrome
# fetches them as the kid's user).
EXTENSION_DIR = Path("/usr/share/familymon/chrome-extension")
CRX_NAME = "familymon.crx"
UPDATE_NAME = "update.xml"
ID_NAME = "id"
VERSION_NAME = "version"

_MAGIC = b"Cr24"
_SIGNATURE_CONTEXT = b"CRX3 SignedData\x00"
_ZIP_DATE = (2026, 1, 1, 0, 0, 0)  # fixed: the archive must not change with the clock


@dataclass(frozen=True)
class ExtensionRef:
    id: str
    update_url: str
    version: str

    @property
    def forcelist_entry(self) -> str:
        return f"{self.id};{self.update_url}"


# ---- keys and ids -------------------------------------------------------------


def generate_key(path: Path) -> None:
    """RSA-2048 private key, PEM, mode 0600. One per deployment; losing it
    only changes the extension id (Chrome swaps the old one out)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["openssl", "genrsa", "-out", str(path), "2048"],
        check=True,
        capture_output=True,
    )
    os.chmod(path, 0o600)


def public_key_der(key_pem: Path) -> bytes:
    """DER SubjectPublicKeyInfo for the private key — the bytes Chrome hashes."""
    return subprocess.run(
        ["openssl", "rsa", "-in", str(key_pem), "-pubout", "-outform", "DER"],
        check=True,
        capture_output=True,
    ).stdout


def crx_id_bytes(spki: bytes) -> bytes:
    return hashlib.sha256(spki).digest()[:16]


def extension_id(spki: bytes) -> str:
    """Chrome's 32-letter id: hex of the truncated hash, 0-9a-f mapped to a-p."""
    return "".join(chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 15)) for b in crx_id_bytes(spki))


# ---- protobuf wire encoding (length-delimited fields only) ---------------------


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while True:
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, pos
        shift += 7


def _parse_fields(buf: bytes) -> list[tuple[int, bytes]]:
    """Decode a message of length-delimited fields into (number, payload)."""
    out = []
    pos = 0
    while pos < len(buf):
        tag, pos = _read_varint(buf, pos)
        if tag & 7 != 2:
            raise ValueError(f"unexpected wire type {tag & 7}")
        size, pos = _read_varint(buf, pos)
        out.append((tag >> 3, buf[pos : pos + size]))
        pos += size
    return out


# ---- packing -----------------------------------------------------------------


def zip_source(src: Path, version: str) -> bytes:
    """Deterministic zip of the extension directory with manifest `version`
    patched in. Same input bytes in, same archive out."""
    manifest = json.loads((src / "manifest.json").read_text())
    manifest["version"] = version
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in src.rglob("*") if p.is_file()):
            rel = path.relative_to(src).as_posix()
            if rel.endswith((".pyc", "~")) or "__pycache__" in rel:
                continue
            info = zipfile.ZipInfo(rel, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            data = (
                (json.dumps(manifest, indent=2) + "\n").encode()
                if rel == "manifest.json"
                else path.read_bytes()
            )
            zf.writestr(info, data)
    return buf.getvalue()


def sign(key_pem: Path, data: bytes) -> bytes:
    """RSA PKCS#1 v1.5 over SHA-256 of `data`."""
    with tempfile.NamedTemporaryFile() as tmp:
        tmp.write(data)
        tmp.flush()
        return subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(key_pem), tmp.name],
            check=True,
            capture_output=True,
        ).stdout


def pack_crx(src: Path, key_pem: Path, version: str) -> bytes:
    archive = zip_source(src, version)
    spki = public_key_der(key_pem)
    signed_header_data = _field(1, crx_id_bytes(spki))
    signature = sign(
        key_pem,
        _SIGNATURE_CONTEXT
        + struct.pack("<I", len(signed_header_data))
        + signed_header_data
        + archive,
    )
    proof = _field(1, spki) + _field(2, signature)
    header = _field(2, proof) + _field(10000, signed_header_data)
    return _MAGIC + struct.pack("<I", 3) + struct.pack("<I", len(header)) + header + archive


@dataclass(frozen=True)
class ParsedCrx:
    public_key: bytes
    signature: bytes
    signed_header_data: bytes
    archive: bytes

    @property
    def signed_bytes(self) -> bytes:
        return (
            _SIGNATURE_CONTEXT
            + struct.pack("<I", len(self.signed_header_data))
            + self.signed_header_data
            + self.archive
        )


def parse_crx(data: bytes) -> ParsedCrx:
    """Inverse of pack_crx for verification; raises ValueError on bad layout."""
    if data[:4] != _MAGIC:
        raise ValueError("not a CRX (magic)")
    (version,) = struct.unpack("<I", data[4:8])
    if version != 3:
        raise ValueError(f"unsupported CRX version {version}")
    (header_len,) = struct.unpack("<I", data[8:12])
    header = data[12 : 12 + header_len]
    archive = data[12 + header_len :]
    fields = dict(_parse_fields(header))
    proof = dict(_parse_fields(fields[2]))
    return ParsedCrx(
        public_key=proof[1],
        signature=proof[2],
        signed_header_data=fields[10000],
        archive=archive,
    )


# ---- update manifest + build -----------------------------------------------


def update_manifest(ext_id: str, crx_url: str, version: str) -> str:
    return (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>\n"
        f"  <app appid='{ext_id}'>\n"
        f"    <updatecheck codebase='{crx_url}' version='{version}' />\n"
        "  </app>\n"
        "</gupdate>\n"
    )


def version_from_timestamp(when: datetime) -> str:
    """Chrome wants up to four integers, each < 65536, strictly increasing
    across builds and without leading zeros: 1.<yymm>.<d>.<hmm>."""
    return f"1.{when:%y%m}.{when.day}.{when.hour}{when:%M}"


def build_extension(
    key_pem: Path,
    out_dir: Path,
    version: str,
    *,
    src: Path = EXTENSION_SRC,
    deployed_dir: Path = EXTENSION_DIR,
) -> ExtensionRef:
    """Write crx + update.xml + id + version into `out_dir`. URLs inside the
    update manifest point at `deployed_dir`, where the deb installs them."""
    if not key_pem.exists():
        generate_key(key_pem)
    out_dir.mkdir(parents=True, exist_ok=True)
    ext_id = extension_id(public_key_der(key_pem))
    crx_url = f"file://{deployed_dir}/{CRX_NAME}"
    (out_dir / CRX_NAME).write_bytes(pack_crx(src, key_pem, version))
    (out_dir / UPDATE_NAME).write_text(update_manifest(ext_id, crx_url, version))
    (out_dir / ID_NAME).write_text(ext_id + "\n")
    (out_dir / VERSION_NAME).write_text(version + "\n")
    for name in (CRX_NAME, UPDATE_NAME, ID_NAME, VERSION_NAME):
        os.chmod(out_dir / name, 0o644)
    return ExtensionRef(
        id=ext_id, update_url=f"file://{deployed_dir}/{UPDATE_NAME}", version=version
    )


def load_extension_ref(directory: Path = EXTENSION_DIR) -> ExtensionRef | None:
    """The deployed extension, or None when the package did not ship one."""
    try:
        ext_id = (directory / ID_NAME).read_text().strip()
        version = (directory / VERSION_NAME).read_text().strip()
    except OSError:
        return None
    if len(ext_id) != 32 or not all("a" <= c <= "p" for c in ext_id):
        return None
    if not (directory / CRX_NAME).exists() or not (directory / UPDATE_NAME).exists():
        return None
    return ExtensionRef(id=ext_id, update_url=f"file://{directory}/{UPDATE_NAME}", version=version)
