from tortoise import fields, Model


class LolBinding(Model):
    """A sender's own LOL nickname/ID, so 战绩 needs no argument."""

    user_id = fields.BigIntField(primary_key=True)  # QQ number
    nickname = fields.CharField(max_length=128)  # 联盟昵称 / 游戏 ID
    updated_at = fields.DatetimeField(auto_now=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "lol_bindings"


class MobileLolBinding(Model):
    """A sender's preferred Wild Rift role name."""

    user_id = fields.BigIntField(primary_key=True)
    nickname = fields.CharField(max_length=128)
    updated_at = fields.DatetimeField(auto_now=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "mobile_lol_bindings"


class MlolSession(Model):
    """The plugin-wide mlol login state, stored at the fixed primary key 1."""

    user_id = fields.BigIntField(primary_key=True)
    openid = fields.CharField(max_length=128)
    access_token = fields.TextField()
    ct = fields.TextField()
    ctt = fields.TextField(null=True)
    wt = fields.TextField(null=True)
    sk = fields.TextField(null=True)
    uin = fields.CharField(max_length=32, null=True)
    tid = fields.CharField(max_length=128, null=True)
    mlol_user_id = fields.CharField(max_length=64, null=True)
    uuid = fields.CharField(max_length=64, null=True)
    scene = fields.CharField(max_length=256, null=True)
    area_id = fields.IntField(null=True)
    area_name = fields.CharField(max_length=64, null=True)
    ct_refresh_at = fields.DatetimeField(null=True)
    wt_refresh_at = fields.DatetimeField(null=True)
    expired = fields.BooleanField(default=False)
    updated_at = fields.DatetimeField(auto_now=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "mlol_sessions"
