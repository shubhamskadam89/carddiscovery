"""Mail provider adapters. Each adapter implements `MailProvider`."""

from __future__ import annotations

from typing import Iterable, Protocol

from ..models import MessageMeta
from ..queries import Query


class MailProvider(Protocol):
    def mailbox(self) -> str: ...

    def user_addresses(self) -> list[str]: ...

    def search(self, query: Query) -> list[str]: ...

    def get_metadata(self, ids: Iterable[str]) -> list[MessageMeta]: ...

    def fill_body(self, msg: MessageMeta) -> MessageMeta: ...
