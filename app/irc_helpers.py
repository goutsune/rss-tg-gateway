from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.types import InputPeerChannel

SERVER = 'telegram'


def peer_hostmask(peer):
  return f'{peer.alias}!{peer.id}@{SERVER}'


def render_lines(message):
  return message.raw_text.split('\n')


async def fetch_topic(client, chat_id):
  chat = await client.get_input_entity(chat_id)
  if isinstance(chat, InputPeerChannel):
    full = await client(GetFullChannelRequest(chat))
  else:
    full = await client(GetFullChatRequest(chat.chat_id))
  return ' :: '.join(line for line in full.full_chat.about.splitlines() if line.strip())


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
