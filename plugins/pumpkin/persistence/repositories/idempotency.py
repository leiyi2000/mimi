from __future__ import annotations

from typing import Any

from tortoise.exceptions import IntegrityError

from persistence.models import IdempotencyRow


class IdempotencyRepositoryMixin:
    async def idempotency_get(self, key: str) -> dict[str, Any] | None:
        row = await IdempotencyRow.get_or_none(key=key)
        return row.result if row else None

    async def idempotency_reserve(
        self,
        key: str,
        *,
        reservation: dict[str, Any] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        initial = {"_status": "in_progress", **(reservation or {})}
        try:
            row, created = await IdempotencyRow.get_or_create(
                key=key,
                defaults={"result": initial},
            )
        except IntegrityError:
            row = await IdempotencyRow.get(key=key)
            created = False
        return created, dict(row.result)

    async def idempotency_release(self, key: str) -> None:
        row = await IdempotencyRow.get_or_none(key=key)
        if row is not None and row.result.get("_status") == "in_progress":
            await row.delete()

    async def idempotency_put(
        self,
        key: str,
        result: dict[str, Any],
        *,
        using_db: Any = None,
    ) -> None:
        await IdempotencyRow.update_or_create(
            key=key,
            using_db=using_db,
            defaults={"result": result},
        )
