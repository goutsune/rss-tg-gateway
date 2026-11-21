''' A Telegram session singleton, I intend to store here raw client and a few helpers, mostly to resolve
between display names, aliases and Peer objects.
'''
from slugify import slugify
from telethon import TelegramClient, utils
from telethon.errors.rpcerrorlist import ChannelPrivateError
from telethon.tl.types import Channel, Chat, PeerChannel, PeerChat, PeerUser, User
from tortoise.exceptions import IntegrityError

import config
from models import Peer, PeerKind


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

    slug = slugify(display_name, max_length=20, separator='_')
    if username:
      base = username.lower()
    elif slug[:1].isdigit():
      base = f'_{slug}'
    else:
      base = slug or f'i{key["entity_id"]}'
    alias, suffix = base, 1
    while True:
      try:
        return await Peer.create(alias=alias, **key, **fields)
      except IntegrityError:
        suffix += 1
        alias = f'{base}{suffix}'

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
