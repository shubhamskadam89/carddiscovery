"""Phone OTP login and sessions."""

from __future__ import annotations

import pytest

from carddiscovery.accounts import (OTP_MAX_ATTEMPTS, OTP_MAX_PER_HOUR, OTP_RESEND_AFTER, OTP_TTL, SESSION_TTL,
                                    AccountStore, ConsoleSender, OtpError, normalize_phone)


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def store(tmp_path):
    clock = Clock()
    sender = ConsoleSender()
    return AccountStore(tmp_path / "a.db", sender=sender, now=clock), sender, clock


@pytest.mark.parametrize("raw", ["9876543210", "98765 43210", "+91 98765 43210", "+91-9876543210", "09876543210"])
def test_phone_normalised(raw):
    assert normalize_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", ["12345", "5876543210", "98765432101", "abc", ""])
def test_bad_phone_rejected(raw):
    with pytest.raises(OtpError):
        normalize_phone(raw)


def test_login_creates_account_and_session(store):
    s, sender, clock = store
    s.send_otp("9876543210")
    acc, token = s.verify_otp("9876543210", sender.last["+919876543210"])
    assert acc.phone == "+919876543210" and s.session(token).id == acc.id
    clock.t += OTP_RESEND_AFTER + 1
    s.send_otp("+91 98765 43210")  # same number, different formatting
    acc2, token2 = s.verify_otp("98765 43210", sender.last["+919876543210"])
    assert acc2.id == acc.id and token2 != token


def test_code_is_stored_hashed_not_plain(store):
    s, sender, _ = store
    s.send_otp("9876543210")
    code = sender.last["+919876543210"]
    raw = s.db.execute("SELECT code_hmac FROM otps").fetchone()[0]
    assert code not in raw and len(raw) == 64


def test_code_is_single_use(store):
    s, sender, _ = store
    s.send_otp("9876543210")
    code = sender.last["+919876543210"]
    s.verify_otp("9876543210", code)
    with pytest.raises(OtpError, match="Request a code first"):
        s.verify_otp("9876543210", code)


def test_code_expires(store):
    s, sender, clock = store
    s.send_otp("9876543210")
    clock.t += OTP_TTL + 1
    with pytest.raises(OtpError, match="expired"):
        s.verify_otp("9876543210", sender.last["+919876543210"])


def test_wrong_tries_burn_the_code(store):
    s, sender, _ = store
    s.send_otp("9876543210")
    good = sender.last["+919876543210"]
    bad = "000000" if good != "000000" else "111111"
    for _ in range(OTP_MAX_ATTEMPTS):
        with pytest.raises(OtpError):
            s.verify_otp("9876543210", bad)
    with pytest.raises(OtpError):
        s.verify_otp("9876543210", good)  # even the right code is dead now


def test_resend_cooldown_and_hourly_cap(store):
    s, _, clock = store
    s.send_otp("9876543210")
    with pytest.raises(OtpError, match="wait"):
        s.send_otp("9876543210")
    for _ in range(OTP_MAX_PER_HOUR - 1):
        clock.t += OTP_RESEND_AFTER + 1
        s.send_otp("9876543210")
    clock.t += OTP_RESEND_AFTER + 1
    with pytest.raises(OtpError, match="Too many codes"):
        s.send_otp("9876543210")
    clock.t += 3600
    s.send_otp("9876543210")


def test_sessions_expire_and_logout(store):
    s, sender, clock = store
    s.send_otp("9876543210")
    _, token = s.verify_otp("9876543210", sender.last["+919876543210"])
    s.logout(token)
    assert s.session(token) is None
    clock.t += OTP_RESEND_AFTER + 1
    s.send_otp("9876543210")
    _, token = s.verify_otp("9876543210", sender.last["+919876543210"])
    clock.t += SESSION_TTL + 1
    assert s.session(token) is None


def test_cards_saved_per_account_and_deleted(store):
    s, sender, _ = store
    s.send_otp("9876543210")
    acc, _ = s.verify_otp("9876543210", sender.last["+919876543210"])
    s.save_cards(acc.id, [{"key": "hdfc:1111", "title": "HDFC ••1111", "group": "owned"}])
    assert [c["key"] for c in s.cards(acc.id)] == ["hdfc:1111"] and s.cards(acc.id)[0]["source"] == "gmail"
    s.delete_account(acc.id)
    assert s.cards(acc.id) == []
