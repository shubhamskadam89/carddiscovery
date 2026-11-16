"""A metered stand-in for MeteredGmail over an in-memory list of messages (test double only)."""

from __future__ import annotations

from tests.test_run import FakeGmail


class MeteredFake(FakeGmail):
    def __init__(self, msgs, meter):
        super().__init__(msgs)
        self.meter = meter

    def user_addresses(self):
        self.meter.charge("profile")
        return super().user_addresses()

    def search_many(self, queries):
        self.meter.charge("search", len(queries))
        return [self.search(q) for q in queries]

    def get_metadata(self, ids):
        self.meter.charge("metadata", len(ids))
        return super().get_metadata(ids)

    def fill_bodies(self, msgs):
        self.meter.charge("full", len(msgs))
        for m in msgs:
            self.fill_body(m)

    def get_full(self, ids):
        self.meter.charge("full", len(ids))
        metas = super().get_metadata(ids)
        for m in metas:
            self.fill_body(m)
        return metas
