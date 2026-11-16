from __future__ import annotations

import email

from cdserver.sim.generate import TODAY, build


def test_inbox_is_deterministic_dated_in_the_past_and_well_formed():
    a, truth = build("tester@example.com", total=10_000, seed=7)
    b, _ = build("tester@example.com", total=10_000, seed=7)
    assert len(a) == 10_000 and [m.key for m in a] == [m.key for m in b]
    assert all(m.when.date() <= TODAY for m in a)
    assert len({m.key for m in a}) == len(a)
    msg = email.message_from_bytes(next(m for m in a if m.attachments).raw("tester@example.com"))
    assert msg["Authentication-Results"].startswith("mx.google.com;")
    assert any(p.get_content_type() == "application/pdf" for p in msg.walk())
    assert {c["last4"] for c in truth["cards"]} >= {"2846", "6666", "7777", "1005"}
