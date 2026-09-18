import logging
from collections.abc import Awaitable, Callable

from napcat import Text, MessageEvent


log = logging.getLogger(__name__)

Handler = Callable[[MessageEvent], Awaitable[None]]
_COMMANDS: dict[str, Handler] = {}


def command(*keywords: str) -> Callable[[Handler], Handler]:
    def decorator(func: Handler) -> Handler:
        for kw in keywords:
            if any(registered.casefold() == kw.casefold() for registered in _COMMANDS):
                raise ValueError(f"duplicate command: {kw!r}")
            _COMMANDS[kw] = func
            log.debug("registered command %r -> %s", kw, func.__name__)
        return func

    return decorator


def commands() -> dict[str, Handler]:
    return dict(_COMMANDS)


def _resolve(text: str) -> tuple[Handler, str] | None:
    text = text.strip()
    for kw in sorted(_COMMANDS, key=len, reverse=True):
        prefix = text[: len(kw)]
        if prefix.casefold() != kw.casefold():
            continue
        return _COMMANDS[kw], text[len(kw) :].strip()
    return None


def argument(event: MessageEvent) -> str:
    match event:
        case MessageEvent(message=[Text(text=text)]):
            resolved = _resolve(text)
            if resolved is not None:
                return resolved[1]
    return ""


async def dispatch(event: MessageEvent) -> bool:
    match event:
        case MessageEvent(message=[Text(text=text)]):
            resolved = _resolve(text)
            if resolved is None:
                return False
            handler, _ = resolved
            await handler(event)
            return True
    return False
