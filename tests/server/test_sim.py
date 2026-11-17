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


def test_card_aware_full_scan_finds_the_same_cards_for_far_fewer_units(c, account):
    """Same simulated inbox, both full-scan strategies: at least equal accuracy, far fewer Gmail units."""
    from carddiscovery.registry import Registry
    from cdserver import cards
    from cdserver.engine import EvidenceStore, card_aware_scan, full_plan, quick_plan, scan
    from cdserver.gmailx import Meter
    from cdserver.sim.dryrun import LocalBox
    from cdserver.sim.evaluate import evaluate

    mails, truth = build("tester@example.com", total=4_000, seed=11)
    reg = Registry.load()
    results = {}
    for strategy in ("read_all", "card_aware"):
        a = account(f"sim-{strategy}")
        store = EvidenceStore(c, a)
        scan(LocalBox(mails, "tester@example.com", Meter(1_000, per_minute=10**9)), reg, store,
             quick_plan(reg, TODAY), names=["Shubham Kadam"], max_reads=40, allow_full_reads=False, interleave=True)
        m = Meter(10**6, per_minute=10**9)
        box = LocalBox(mails, "tester@example.com", m)
        if strategy == "read_all":
            scan(box, reg, store, full_plan(reg, TODAY, 3), names=["Shubham Kadam"])
        else:
            card_aware_scan(box, reg, store, TODAY, 3, names=["Shubham Kadam"])
        cards.rebuild(c, a, reg)
        rep = evaluate(c, a, truth, 3, reg)["score"]
        results[strategy] = (rep["correct"], rep["false_positives_owned"], m.units)
    assert results["card_aware"][0] >= results["read_all"][0]           # at least as many cards right
    assert results["card_aware"][1] == 0                                  # nothing wrongly owned
    assert results["card_aware"][2] < results["read_all"][2] * 0.5       # at least half the units
