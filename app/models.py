''' Tortoise ORM models and database lifecycle
'''
from enum import Enum

from telethon.tl.types import PeerChannel, PeerChat, PeerUser
from tortoise import Tortoise, fields
from tortoise.models import Model

import config


class PeerKind(Enum):
  USER = 'user'
  CHAT = 'chat'
  CHANNEL = 'channel'
  TOPIC = 'topic'


class Peer(Model):
  ''' Anything with an alias: users, chats, channels and forum topics. entity_id is the bare
  Telegram id, of the forum channel for topics, topic_id is set for topics only
  '''
  id = fields.IntField(primary_key=True)
  kind = fields.CharEnumField(PeerKind)
  entity_id = fields.BigIntField()
  topic_id = fields.IntField(null=True)
  first_name = fields.TextField(null=True)
  last_name = fields.TextField(null=True)
  username = fields.TextField(null=True)
  display_name = fields.TextField()
  alias = fields.CharField(max_length=32, unique=True)
  updated_at = fields.DatetimeField(auto_now=True)

  class Meta:
    unique_together = (('kind', 'entity_id', 'topic_id'),)

  @property
  def telegram_peer(self):
    if self.kind == PeerKind.USER:
      return PeerUser(self.entity_id)
    if self.kind == PeerKind.CHAT:
      return PeerChat(self.entity_id)
    return PeerChannel(self.entity_id)


async def init():
  await Tortoise.init(
    db_url=config.db_url, modules={'models': ['models']}, _enable_global_fallback=True)
  await Tortoise.generate_schemas()


async def close():
  await Tortoise.close_connections()
