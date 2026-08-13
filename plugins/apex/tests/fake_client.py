from napcat import Message


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[str | list[Message] | Message] = []

    async def send_private_msg(self, user_id, message):
        self.sent.append(message)
        return {"message_id": len(self.sent)}
