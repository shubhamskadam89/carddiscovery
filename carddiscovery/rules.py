"""The rules bundle: bank list + classification rules + scoring weights as data.

Source files (edited by developers):
    data/banks.toml   sender domains and product aliases
    data/rules.json   classification patterns, scoring weights and thresholds

A bundle is both, combined into one JSON document and signed with Ed25519:
    carddiscovery bundle keygen          one-time signing key
    carddiscovery bundle build           -> dist/bundle.json + dist/bundle.sig
    carddiscovery discover --bundle dist/bundle.json

An app (the mobile client) ships the public key and refuses any bundle whose
signature doesn't verify, so nobody can push rules that misread mail. Without
--bundle, the source files are used directly (development).
"""

from __future__ import annotations

import base64
import json
import os
import tomllib
from importlib import resources
from pathlib import Path

ENGINE_VERSION = 1
DEFAULT_KEY = Path(os.environ.get("CARDDISCOVERY_HOME", Path.home() / ".carddiscovery")) / "bundle_signing_key.pem"


class BundleError(RuntimeError):
    pass


# ---------------------------------------------------------------- loading

def _data(name: str) -> bytes:
    return resources.files("carddiscovery.data").joinpath(name).read_bytes()


def source_bundle() -> dict:
    """Bundle built from the source files in data/ (not signed)."""
    rules = json.loads(_data("rules.json"))
    banks = tomllib.loads(_data("banks.toml").decode())["banks"]
    return {**rules, "banks": banks}


def canonical(bundle: dict) -> bytes:
    """Stable bytes to sign: sorted keys, no whitespace, UTF-8."""
    return json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def load_bundle(path: Path, public_key_pem: bytes | None = None) -> dict:
    """Load a bundle and verify its signature (path + '.sig' next to it, or
    bundle.sig in the same folder). Raises BundleError if anything is off."""
    path = Path(path)
    sig_path = path.with_suffix(".sig")
    if not sig_path.exists():
        raise BundleError(f"no signature file next to the bundle ({sig_path.name})")
    bundle = json.loads(path.read_bytes())
    signature = base64.b64decode(sig_path.read_text().strip())
    verify(canonical(bundle), signature, public_key_pem or _data("bundle_public.pem"))
    if bundle.get("min_engine", 1) > ENGINE_VERSION:
        raise BundleError(f"bundle needs engine {bundle['min_engine']}, this is {ENGINE_VERSION}")
    return bundle


# ---------------------------------------------------------------- signing

def verify(payload: bytes, signature: bytes, public_key_pem: bytes) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    key = load_pem_public_key(public_key_pem)
    try:
        key.verify(signature, payload)
    except InvalidSignature:
        raise BundleError("bundle signature does not verify: refusing to use these rules") from None


def keygen(private_path: Path = DEFAULT_KEY, public_path: Path | None = None) -> Path:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if private_path.exists():
        raise BundleError(f"{private_path} already exists; not overwriting a signing key")
    key = Ed25519PrivateKey.generate()
    private_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    os.chmod(private_path, 0o600)
    public_path = public_path or Path(__file__).parent / "data" / "bundle_public.pem"
    public_path.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return public_path


def build(out_dir: Path, private_path: Path = DEFAULT_KEY) -> tuple[Path, Path]:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    if not private_path.exists():
        raise BundleError(f"no signing key at {private_path}; run `carddiscovery bundle keygen` first")
    bundle = source_bundle()
    key = load_pem_private_key(private_path.read_bytes(), password=None)
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle_path, sig_path = out_dir / "bundle.json", out_dir / "bundle.sig"
    bundle_path.write_text(json.dumps(bundle, indent=2, ensure_ascii=False) + "\n")
    sig_path.write_text(base64.b64encode(key.sign(canonical(bundle))).decode() + "\n")
    return bundle_path, sig_path


# ---------------------------------------------------------------- activation

def activate(bundle: dict) -> None:
    """Make the classifier and scorer use this bundle's rules and weights."""
    from . import classify, score
    classify.configure(bundle["classify"])
    score.configure(bundle["score"])
