"""Rules bundle: rules live in data, bundles are signed, tampering is refused."""

from __future__ import annotations

import json

import pytest

from carddiscovery import rules
from carddiscovery.registry import Registry


@pytest.fixture
def signed(tmp_path):
    key, pub = tmp_path / "key.pem", tmp_path / "pub.pem"
    rules.keygen(private_path=key, public_path=pub)
    bundle_path, _ = rules.build(tmp_path / "dist", private_path=key)
    return bundle_path, pub.read_bytes()


def test_source_bundle_has_banks_rules_and_weights():
    b = rules.source_bundle()
    assert "hdfc" in b["banks"] and b["classify"]["rules"] and b["score"]["weights"]


def test_signed_bundle_verifies_and_activates(signed):
    path, pub = signed
    bundle = rules.load_bundle(path, public_key_pem=pub)
    rules.activate(bundle)
    reg = Registry.from_banks(bundle["banks"])
    assert reg.bank_for_sender("alerts@hdfcbank.net") == "hdfc"


def test_tampered_bundle_is_refused(signed):
    path, pub = signed
    data = json.loads(path.read_text())
    data["score"]["owned_threshold"] = 0.01  # someone tries to make every guess "owned"
    path.write_text(json.dumps(data))
    with pytest.raises(rules.BundleError, match="does not verify"):
        rules.load_bundle(path, public_key_pem=pub)


def test_bundle_signed_with_another_key_is_refused(signed, tmp_path):
    path, _ = signed
    other_pub = tmp_path / "other_pub.pem"
    rules.keygen(private_path=tmp_path / "other_key.pem", public_path=other_pub)
    with pytest.raises(rules.BundleError):
        rules.load_bundle(path, public_key_pem=other_pub.read_bytes())


def test_bundle_needing_newer_engine_is_refused(tmp_path):
    key, pub = tmp_path / "key.pem", tmp_path / "pub.pem"
    rules.keygen(private_path=key, public_path=pub)
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    import base64
    bundle = {**rules.source_bundle(), "min_engine": rules.ENGINE_VERSION + 1}
    (tmp_path / "bundle.json").write_text(json.dumps(bundle))
    sig = load_pem_private_key(key.read_bytes(), None).sign(rules.canonical(bundle))
    (tmp_path / "bundle.sig").write_text(base64.b64encode(sig).decode())
    with pytest.raises(rules.BundleError, match="needs engine"):
        rules.load_bundle(tmp_path / "bundle.json", public_key_pem=pub.read_bytes())


def test_signing_key_is_never_overwritten(tmp_path):
    key = tmp_path / "key.pem"
    rules.keygen(private_path=key, public_path=tmp_path / "pub.pem")
    with pytest.raises(rules.BundleError, match="already exists"):
        rules.keygen(private_path=key, public_path=tmp_path / "pub2.pem")
