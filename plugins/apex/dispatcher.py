import logging
from collections.abc import Awaitable, Callable

from napcat import Text, MessageEvent


log = logging.getLogger(__name__)

Handler = Callable[[MessageEvent], Awaitable[None]]
_COMMANDS: dict[str, Handler] = {}


def command(*keywords: str) -> Callable[[Handler], Handler]:
    def decorator(func: Handler) -> Handler:
        for kw in keywords:
            if kw in _COMMANDS:
                raise ValueError(f"duplicate command: {kw!r}")
            _COMMANDS[kw] = func
            log.debug("registered command %r -> %s", kw, func.__name__)
        return func

    return decorator


def commands() -> dict[str, Handler]:
    return dict(_COMMANDS)


async def dispatch(event: MessageEvent) -> bool:
    match event:
        case MessageEvent(message=[Text(text=text)]):
            handler = _COMMANDS.get(text.strip())
            if handler is None:
                return False
            await handler(event)
            return True
    return False
