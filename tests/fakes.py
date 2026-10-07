"""In-memory stand-ins for the Polymarket client (test use only)."""

from __future__ import annotations

from favorite_hunter.http import DataUnavailable
from favorite_hunter.models import parse_book


class FakeClient:
    def __init__(self, markets, books, fee=None, fail=False, closed_markets=None, histories=None):
        self.markets = markets
        self.histories = histories or {}
        self.books = books
        self.fee = fee
        self.fail = fail
        self.fee_calls = 0
        self.requested_tokens: list[str] = []
        # condition_id -> raw closed/resolved gamma market, for settlement tests
        self.closed_markets = closed_markets or {}

    def iter_markets(self, **kwargs):
        if self.fail:
            raise DataUnavailable("gamma", "blocked by network proxy (403 Forbidden)")
        extra = kwargs.get("extra") or {}
        if "condition_ids" in extra:
            for cid in extra["condition_ids"]:
                if cid in self.closed_markets:
                    yield self.closed_markets[cid]
            return
        yield from self.markets

    def get_markets_by_condition_ids(self, condition_ids, closed=None):
        return list(self.iter_markets(extra={"condition_ids": list(condition_ids)}))

    def get_books(self, token_ids, batch_size=50):
        if self.fail:
            raise DataUnavailable("clob", "blocked by network proxy (403 Forbidden)")
        self.requested_tokens = list(token_ids)
        return {t: parse_book(self.books[t]) for t in token_ids if t in self.books}

    def get_price_history(self, token_id, start, end, bucket_seconds=300):
        if token_id not in self.histories:
            raise DataUnavailable("data-api", "HTTP 404: no history")
        return [(ts, p) for ts, p in self.histories[token_id] if start <= ts <= end]

    def get_fee_schedule(self, condition_id):
        self.fee_calls += 1
        return self.fee
