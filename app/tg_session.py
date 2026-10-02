''' A Telegram session singleton, I intend to store here raw client and a few helpers, mostly to resolve
between display names, aliases and Peer objects.
'''
from telethon import TelegramClient
from telethon.errors.rpcerrorlist import ChannelPrivateError

import config


class TelegramSession:

  def __init__(self):

    self.client = TelegramClient(config.user, config.api_id, config.api_hash)
    self.client.parse_mode = 'html'  # hmm, is this configurable on the fly I wonder
    # Cache for resolving peers, move to sqlite
    self.users = {}
    # Cache for author names when retrieving channel messages
    self.author_names = {}

  async def resolve_peer(self, peer):
    ''' A helper funtion to avoid re-requesting user names when
    resolving them from peer identifier objects. Currently it just
    stores rendered name never updating it, which might be a problem.
    '''
    try:
      # This processes normal users, preserving their usernames along
      # with full name if possible
      if hasattr(peer, 'user_id'):
        uid = peer.user_id
        if uid not in self.users:
          entity = await self.client.get_entity(peer)
          if entity.last_name:
            if entity.username:
              self.users[uid] = f'{entity.first_name} '\
                                f'{entity.last_name} ({entity.username})'
            else:
              self.users[uid] = f'{entity.first_name} {entity.last_name}'
          elif entity.first_name:
            if entity.username:
              self.users[uid] = f'{entity.first_name} ({entity.username})'
            else:
              self.users[uid] = f'{entity.first_name}'

      # This gets channel names
      if hasattr(peer, 'channel_id'):
        uid = peer.channel_id
        if uid not in self.users:
          channel = await self.client.get_entity(peer)
          # TODO: confirm ID pool is shared betweeen channels and users
          self.users[uid] = channel.title

    except ChannelPrivateError:
      return False

    return self.users[uid]

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
