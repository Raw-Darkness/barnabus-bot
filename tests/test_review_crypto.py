import base64
import json
import os
import time

import pytest

from barnabus import review_crypto as crypto


@pytest.fixture(scope="module")
def keypair(tmp_path_factory):
    root = tmp_path_factory.mktemp("review-keys")
    private_path, public_path = root / "private.pem", root / "public.pem"
    crypto.generate_keypair(private_path, public_path)
    return private_path, public_path


def test_round_trip_uses_random_encryption_and_hides_report(keypair):
    private_path, public_path = keypair
    report = {"schema": 1, "threads": [{"title": "secret report title", "messages": ["résumé"]}]}
    expires = time.time() + 3600
    first = crypto.seal(report, public_path, expires)
    second = crypto.seal(report, public_path, expires)
    assert set(first) == {"schema", "algorithm", "expires_at", "wrapped_key", "nonce", "ciphertext"}
    assert first["algorithm"] == "RSA-OAEP-SHA256+AES-256-GCM"
    assert first["ciphertext"] != second["ciphertext"]
    assert first["wrapped_key"] != second["wrapped_key"]
    assert "secret report title" not in json.dumps(first)
    assert crypto.unseal(first, private_path) == report


def test_wrong_key_and_tampered_header_or_ciphertext_are_rejected(keypair, tmp_path):
    private_path, public_path = keypair
    other_private, other_public = tmp_path / "other-private.pem", tmp_path / "other-public.pem"
    crypto.generate_keypair(other_private, other_public)
    envelope = crypto.seal({"secret": "private finding"}, public_path, time.time() + 3600)
    with pytest.raises(ValueError, match="Invalid review envelope"):
        crypto.unseal(envelope, other_private)
    mutations = [
        {"schema": 2},
        {"algorithm": "AES-256-GCM"},
        {"expires_at": envelope["expires_at"] + 1},
        {"wrapped_key": crypto._encode(b"x" * 384)},
        {"nonce": crypto._encode(b"x" * 12)},
        {"ciphertext": crypto._encode(b"x" * 32)},
    ]
    for mutation in mutations:
        with pytest.raises(ValueError) as exc:
            crypto.unseal({**envelope, **mutation}, private_path)
        assert "private finding" not in str(exc.value)


@pytest.mark.parametrize("bad_expiry", [float("nan"), float("inf"), -float("inf"), "tomorrow", True, 10**1000])
def test_invalid_expiry_is_rejected_on_seal_and_unseal(keypair, bad_expiry):
    private_path, public_path = keypair
    with pytest.raises(ValueError):
        crypto.seal({}, public_path, bad_expiry)
    envelope = crypto.seal({}, public_path, time.time() + 3600)
    with pytest.raises(ValueError):
        crypto.unseal({**envelope, "expires_at": bad_expiry}, private_path)


def test_expiry_is_enforced_and_cannot_be_refreshed(keypair):
    private_path, public_path = keypair
    start = time.time()
    envelope = crypto.seal({"secret": "private finding"}, public_path, start + 60)
    with pytest.raises(ValueError):
        crypto.unseal(envelope, private_path, now=start + 60)
    with pytest.raises(ValueError):
        crypto.unseal({**envelope, "expires_at": start + 120}, private_path, now=start + 61)
    with pytest.raises(ValueError):
        crypto.seal({}, public_path, start + crypto.MAX_AGE_SECONDS + 60)


def test_malformed_and_oversized_envelopes_are_rejected(keypair, monkeypatch):
    private_path, public_path = keypair
    envelope = crypto.seal({}, public_path, time.time() + 3600)
    bad = [
        None,
        {**envelope, "extra": 1},
        {key: value for key, value in envelope.items() if key != "nonce"},
        {**envelope, "schema": True},
        {**envelope, "nonce": "!!"},
        {**envelope, "nonce": base64.b64encode(b"short").decode()},
        {**envelope, "ciphertext": base64.b64encode(b"short").decode()},
    ]
    for candidate in bad:
        with pytest.raises(ValueError):
            crypto.unseal(candidate, private_path)
    monkeypatch.setattr(crypto, "MAX_CIPHERTEXT_BYTES", 16)
    with pytest.raises(ValueError):
        crypto.unseal(envelope, private_path)
    with pytest.raises(ValueError):
        crypto.seal({"long": "x" * 20}, public_path, time.time() + 60)


def test_key_generation_never_overwrites_existing_files(tmp_path, keypair):
    private_path, public_path = keypair
    before_private, before_public = private_path.read_bytes(), public_path.read_bytes()
    with pytest.raises(FileExistsError):
        crypto.generate_keypair(private_path, public_path)
    assert private_path.read_bytes() == before_private
    assert public_path.read_bytes() == before_public
    occupied = tmp_path / "occupied.pem"
    occupied.write_text("existing")
    new_private = tmp_path / "new-private.pem"
    with pytest.raises(FileExistsError):
        crypto.generate_keypair(new_private, occupied)
    assert not new_private.exists()
    assert occupied.read_text() == "existing"
    if os.name != "nt":
        assert private_path.stat().st_mode & 0o777 == 0o600
