import re

from telethon import helpers, utils
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.types import (
  InputMessageEntityMentionName, InputPeerChannel, MessageEntityMention,
  MessageEntityMentionName)

from models import Peer

SERVER = 'telegram'
LEADING_MENTION = re.compile(r'([^\s:]+)(::?) ')


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


async def outgoing_mention(client, text):
  match = LEADING_MENTION.match(text)
  if not match:
    return text, None
  rest = text[match.end(2):]
  if match[2] == '::':
    return f'{match[1]}:{rest}', None
  peer = await find_peer(match[1])
  if not peer:
    return text, None

  name = ' '.join(filter(None, (peer.first_name, peer.last_name)))
  user = utils.get_input_user(await client.get_input_entity(peer.id))
  mention = InputMessageEntityMentionName(0, len(name.encode('utf-16-le')) // 2, user)
  return f'{name}:{rest}', [mention]


async def incoming_mentions(session, message):
  text = helpers.add_surrogate(message.message)
  for entity in reversed(message.entities or []):
    if isinstance(entity, MessageEntityMentionName):
      peer = await session.resolve_peer_record(entity.user_id)
    elif isinstance(entity, MessageEntityMention):
      username = text[entity.offset + 1:entity.offset + entity.length]
      peer = await Peer.get_or_none(username__iexact=username)
    else:
      continue
    if peer:
      text = text[:entity.offset] + peer.alias + text[entity.offset + entity.length:]
  return helpers.del_surrogate(text)


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
