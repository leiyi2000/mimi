from tortoise import fields, Model


class GroupMessage(Model):
    id = fields.IntField(pk=True)
    message_id = fields.IntField(unique=True, index=True)
    group_id = fields.BigIntField(index=True)
    user_id = fields.BigIntField(index=True)
    nickname = fields.CharField(max_length=255)
    card = fields.CharField(max_length=255, null=True)
    role = fields.CharField(max_length=50, null=True)
    raw_message = fields.TextField(null=True)
    local_image_paths = fields.JSONField(null=True)
    time = fields.BigIntField()
    self_id = fields.BigIntField()
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "group_messages"