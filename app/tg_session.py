''' A Telegram session singleton, I intend to store here raw client and a few helpers, mostly to resolve
between display names, aliases and Peer objects.
'''
from slugify import slugify
from telethon import TelegramClient, utils
from telethon.errors.rpcerrorlist import ChannelPrivateError
from telethon.tl.types import User

import config
from models import Peer


class TelegramSession:

  def __init__(self):

    self.client = TelegramClient(config.user, config.api_id, config.api_hash)
    self.client.parse_mode = 'html'  # hmm, is this configurable on the fly I wonder

  async def resolve_peer(self, peer, force=False):
    ''' A helper funtion to avoid re-requesting user names when
    resolving them from peer identifier objects. Currently it just
    stores rendered name never updating it, which might be a problem.
    '''
    peer_id = utils.get_peer_id(peer)
    record = await Peer.get_or_none(id=peer_id)
    if record and not force:
      return record.display_name

    try:
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
    record = await Peer.create(
      id=peer_id, first_name=first_name, last_name=last_name,
      username=username, display_name=display_name,
      alias=username or slugify(display_name, max_length=20, separator='_'))
    return record.display_name

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
