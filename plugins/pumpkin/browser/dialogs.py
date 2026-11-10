import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .protocol import WorkerErrorCode
from .sdk_protocol import SdkError


@dataclass(slots=True)
class BrowserDialogs:
    items: list[tuple[str, Any]] = field(default_factory=list)
    opened: asyncio.Event = field(default_factory=asyncio.Event)

    def open(self, dialog: Any) -> None:
        self.items.append((uuid4().hex, dialog))
        self.opened.set()

    def payload(self) -> dict[str, Any] | None:
        if not self.items:
            return None
        dialog_id, dialog = self.items[0]
        return {
            "dialog_id": dialog_id, "type": dialog.type,
            "message": dialog.message[:2_000], "default_value": dialog.default_value[:2_000],
            "truncated": len(dialog.message) > 2_000 or len(dialog.default_value) > 2_000,
        }

    async def resolve(self, dialog_id: str, accept: bool, prompt_text: str | None = None) -> None:
        if not self.items or self.items[0][0] != dialog_id:
            raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "dialog ID is no longer current", field="dialog_id")
        if type(accept) is not bool or (
            prompt_text is not None and (not isinstance(prompt_text, str) or len(prompt_text) > 8_000)
        ):
            raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "invalid dialog choice")
        dialog = self.items[0][1]
        if prompt_text is not None and (dialog.type != "prompt" or not accept):
            raise SdkError(WorkerErrorCode.ARGUMENT_INVALID, "prompt_text requires accepting a prompt")
        self.items.pop(0)
        if not self.items:
            self.opened.clear()
        if accept:
            await dialog.accept(**({"prompt_text": prompt_text} if prompt_text is not None else {}))
        else:
            await dialog.dismiss()

    async def wait(self, action: asyncio.Task) -> bool:
        waiting = asyncio.create_task(self.opened.wait())
        try:
            await asyncio.wait((action, waiting), return_when=asyncio.FIRST_COMPLETED)
            return action.done() and not self.items
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    async def close(self) -> None:
        await asyncio.gather(*(dialog.dismiss() for _, dialog in self.items), return_exceptions=True)
        self.items.clear()
        self.opened.clear()
