import os
import logging
import asyncio

from anthropic import AsyncAnthropic
from napcat import Text, At, GroupMessageEvent, NapCatClient

from settings import *


log = logging.getLogger(__name__)


PROMPT_PATH = os.path.join(os.path.dirname(__file__), "skills/prompt.md")
with open(PROMPT_PATH, "r", encoding="utf-8") as f:
    PROMPT = f.read().strip()


async def main():
    host = os.getenv("NAPCAT_HOST", "127.0.0.1")
    port = int(os.getenv("NAPCAT_PORT", "3001"))
    token = os.getenv("NAPCAT_TOKEN", None)

    anthropic_client = AsyncAnthropic(
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        base_url=os.getenv("ANTHROPIC_BASE_URL"),
    )

    while True:
        try:
            client = NapCatClient(f"ws://{host}:{port}/", token)
            async for event in client:
                match event:
                    case GroupMessageEvent(
                        message=[At(qq=at_qq), Text(text=text)],
                        self_id=self_id,
                    ) if at_qq == str(self_id):
                        response = await anthropic_client.messages.create(
                            model="MiniMax-M2.7",
                            max_tokens=1024,
                            messages=[
                                {"role": "system", "content": PROMPT},
                                {"role": "user", "content": text},
                            ],
                        )
                        reply = next(
                            block.text for block in response.content
                            if hasattr(block, "text")
                        )
                        await event.send_msg(Text(text=reply))
        except Exception:  # noqa: BLE001
            import traceback

            log.error(traceback.format_exc())
            await asyncio.sleep(5)


if __name__ == "__main__":
    log.info("niu plugin running")
    asyncio.run(main())
