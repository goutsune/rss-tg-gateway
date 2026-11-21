from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.types import InputPeerChannel

from models import Peer

SERVER = 'telegram'


def peer_hostmask(peer):
  return f'{peer.alias}!{peer.id}@{SERVER}'


def irc_name(peer):
  return f'#{peer.alias}' if peer.id < 0 else peer.alias


async def find_peer(name):
  if name.startswith('#'):
    return await Peer.get_or_none(alias__iexact=name[1:], id__lt=0)
  return await Peer.get_or_none(alias__iexact=name, id__gt=0)


async def fetch_topic(client, chat):
  input_chat = await client.get_input_entity(chat.id)
  if isinstance(input_chat, InputPeerChannel):
    full = await client(GetFullChannelRequest(input_chat))
  else:
    full = await client(GetFullChatRequest(input_chat.chat_id))
  description = ' :: '.join(line for line in full.full_chat.about.splitlines() if line.strip())
  return f'{chat.display_name} // {description}' if description else chat.display_name


async def fetch_names(session, chat_id):
  members = await session.client.get_participants(chat_id)
  return [(await session.resolve_peer_record(user)).alias for user in members if not user.is_self]


def iter_text_lines(words, limit):
  line = ''
  for word in words:
    if line and len(line) + 1 + len(word) > limit:
      yield line
      line = word
    else:
      line = f'{line} {word}' if line else word
  if line:
    yield line
