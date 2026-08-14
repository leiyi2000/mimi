from tortoise import fields, Model


class EABinding(Model):
    user_id = fields.BigIntField(pk=True)
    ea_id = fields.CharField(max_length=255)
    updated_at = fields.DatetimeField(auto_now=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "ea_bindings"
