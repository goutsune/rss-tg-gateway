''' A Telegram session singleton, I intend to store here raw client and a few helpers, mostly to resolve
between display names, aliases and Peer objects.
'''
from slugify import slugify
from telethon import TelegramClient, utils
from telethon.errors.rpcerrorlist import ChannelPrivateError
from telethon.tl.functions.messages import GetForumTopicsByIDRequest, GetForumTopicsRequest
from telethon.tl.types import (
  Channel, Chat, ForumTopicDeleted, MessageReplyHeader, PeerChannel, PeerChat, PeerUser, User)

import config
from models import Peer, PeerKind


async def create_aliased(base, taken, **fields):
  ''' Create a Peer aliased base, or base2, base3... while the taken queryset already has that
  alias in any case. A leading digit gets an underscore in front, IRC names can't start with one
  '''
  if base[:1].isdigit():
    base = f'_{base}'
  alias, suffix = base, 1
  while await taken.filter(alias__iexact=alias).exists():
    suffix += 1
    alias = f'{base}{suffix}'
  return await Peer.create(alias=alias, **fields)


class TelegramSession:

  def __init__(self):

    self.client = TelegramClient(
      config.user, config.api_id, config.api_hash, sequential_updates=True)
    self.client.parse_mode = 'html'  # hmm, is this configurable on the fly I wonder

  async def resolve_peer(self, peer, force=False):
    ''' A helper funtion to avoid re-requesting user names when
    resolving them from peer identifier objects. Currently it just
    stores rendered name never updating it, which might be a problem.
    '''
    record = await self.resolve_peer_record(peer, force)
    return record and record.display_name

  async def resolve_peer_record(self, peer, force=False):
    # Telethon hands over entities, Peer* objects or marked ids, resolve_id splits the latter
    entity_id, peer_type = utils.resolve_id(utils.get_peer_id(peer))
    kind = {PeerUser: PeerKind.USER, PeerChat: PeerKind.CHAT, PeerChannel: PeerKind.CHANNEL}
    key = dict(kind=kind[peer_type], entity_id=entity_id, topic_id=None)
    record = await Peer.get_or_none(**key)
    if record and not force:
      return record

    try:
      if isinstance(peer, (User, Chat, Channel)):
        entity = peer
      else:
        entity = await self.client.get_entity(peer)

    except ChannelPrivateError:
      return False

    # Channel and chat titles go into first_name
    if isinstance(entity, User):
      first_name, last_name = entity.first_name, entity.last_name
      display_name = utils.get_display_name(entity)
      if entity.username:
        display_name += f' ({entity.username})'
    else:
      first_name, last_name = entity.title, None
      display_name = entity.title

    username = getattr(entity, 'username', None)
    fields = dict(
      first_name=first_name,
      last_name=last_name,
      username=username,
      display_name=display_name)

    if record:
      await record.update_from_dict(fields).save()
      return record

    if username:
      base = username.lower()
    else:
      base = slugify(display_name, max_length=20, separator='_') or f'i{entity_id}'
    return await create_aliased(base, Peer.exclude(kind=PeerKind.TOPIC), **key, **fields)

  async def resolve_conversation_record(self, message):
    ''' Record of the chat a message went to, or of its forum topic. Topics other than General
    flag their messages, General ones stay with the chat itself
    '''
    chat = await self.resolve_peer_record(message.peer_id)
    reply_to = message.reply_to
    if not (isinstance(reply_to, MessageReplyHeader) and reply_to.forum_topic):
      return chat

    return await self.resolve_topic_record(
      chat, reply_to.reply_to_top_id or reply_to.reply_to_msg_id)

  async def resolve_forum_topics(self, chat):
    ''' Topic records of a forum chat; General (id 1) is the chat itself, deleted topics are gone
    '''
    result = await self.client(GetForumTopicsRequest(
      chat.telegram_peer, offset_date=None, offset_id=0, offset_topic=0, limit=100))
    return [await self.resolve_topic_record(chat, topic.id, topic.title)
            for topic in result.topics
            if topic.id != 1 and not isinstance(topic, ForumTopicDeleted)]

  async def resolve_topic_record(self, chat, topic_id, title=None):
    key = dict(kind=PeerKind.TOPIC, entity_id=chat.entity_id, topic_id=topic_id)
    if record := await Peer.filter(**key).select_related('forum').get_or_none():
      return record

    if title is None:
      result = await self.client(GetForumTopicsByIDRequest(chat.telegram_peer, [topic_id]))
      title = result.topics[0].title
    base = slugify(title, max_length=20, separator='_') or f't{topic_id}'
    taken = Peer.filter(kind=PeerKind.TOPIC, entity_id=chat.entity_id)
    return await create_aliased(
      base, taken, **key, forum=chat, first_name=title, display_name=title)

  async def startup(self):
    print("Connecting...")
    await self.client.start()
    print("Updating dialogs...")
    await self.client.get_dialogs()
    print("OK!")

  async def cleanup(self):
    print("Disconnecting...")
    await self.client.disconnect()
    print("OK!")

  async def conn_check(self):
    if not self.client.is_connected():
      print("Not connected, reconnecting...")
      await self.startup()
