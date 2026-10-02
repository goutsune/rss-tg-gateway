''' Tortoise ORM models and database lifecycle
'''
from tortoise import Tortoise, fields
from tortoise.models import Model

import config


class Peer(Model):
  id = fields.BigIntField(primary_key=True)
  first_name = fields.TextField(null=True)
  last_name = fields.TextField(null=True)
  username = fields.TextField(null=True)
  display_name = fields.TextField()
  alias = fields.CharField(max_length=32)
  updated_at = fields.DatetimeField(auto_now=True)


async def init():
  await Tortoise.init(
    db_url=config.db_url, modules={'models': ['models']}, _enable_global_fallback=True)
  await Tortoise.generate_schemas()


async def close():
  await Tortoise.close_connections()
