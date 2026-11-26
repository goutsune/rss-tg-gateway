import os, re
from collections import namedtuple
from difflib import SequenceMatcher
from enum import Enum

from slugify import slugify
from telethon import helpers, utils
from telethon.tl.functions.stories import GetStoriesByIDRequest
from telethon.tl.types import (
  InputMessageEntityMentionName, MessageEntityMention, MessageEntityMentionName,
  MessageReplyStoryHeader, PeerUser)

import config
from models import Peer, PeerKind
from util import to_alpha

SERVER = 'telegram'
LEADING_MENTION = re.compile(r'([^\s:]+)(::?) ')

RecentMessage = namedtuple('RecentMessage', 'id sender_id text date')


class Autojoin(Enum):
  NEVER = 'never'
  MENTIONS = 'mentions'
  ON_EVENT = 'on_event'


def truncate_on_word(text, limit):
  ''' Cut text to limit with …, keeping the word that crosses limit when it ends within
  irc_cut_threshold more characters, cutting before it otherwise
  '''
  if len(text) <= limit:
    return text
  word_end = text.find(' ', limit)
  if word_end == -1:
    word_end = len(text)
  if word_end - limit <= config.irc_cut_threshold:
    return text if word_end == len(text) else text[:word_end] + '…'
  word_start = text.rfind(' ', 0, limit)
  return text[:word_start if word_start > 0 else limit] + '…'


def peer_hostmask(peer):
  return f'{peer.alias}!{peer.entity_id}@{SERVER}'


def irc_name(peer):
  if peer.kind == PeerKind.USER:
    return peer.alias
  if peer.kind == PeerKind.FORUM:
    return f'#{peer.chat.alias}/{peer.alias}'
  return f'#{peer.alias}'


async def find_peer(name):
  if not name.startswith('#'):
    return await Peer.get_or_none(alias__iexact=name, kind=PeerKind.USER)
  chat_alias, _, forum_alias = name[1:].partition('/')
  if forum_alias:
    forums = Peer.filter(
      kind=PeerKind.FORUM, alias__iexact=forum_alias, chat__alias__iexact=chat_alias)
    return await forums.select_related('chat').get_or_none()
  return await Peer.get_or_none(
    alias__iexact=chat_alias, kind__in=(PeerKind.CHAT, PeerKind.CHANNEL))


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
  if peer.username:
    return f'@{peer.username},{rest}', None

  name = ' '.join(filter(None, (peer.first_name, peer.last_name)))
  user = utils.get_input_user(await client.get_input_entity(peer.telegram_peer))
  mention = InputMessageEntityMentionName(0, len(name.encode('utf-16-le')) // 2, user)
  return f'{name},{rest}', [mention]


async def incoming_mentions(session, message):
  text = helpers.add_surrogate(message.message)
  for entity in reversed(message.entities or []):
    if isinstance(entity, MessageEntityMentionName):
      peer = await session.resolve_peer_record(PeerUser(entity.user_id))
    elif isinstance(entity, MessageEntityMention):
      username = text[entity.offset + 1:entity.offset + entity.length]
      peer = await Peer.get_or_none(username__iexact=username)
    else:
      continue
    if peer:
      text = text[:entity.offset] + peer.alias + text[entity.offset + entity.length:]
  return helpers.del_surrogate(text)


def render_media(message, text):
  if message.web_preview or not message.file:
    return text
  # /a/ is the web server's short media route: peer and message ids alpha-encoded with
  # util.to_alpha, anything after the first . in the message part is only a filename hint
  name, _ = os.path.splitext(message.file.name or '')
  hint = f'.{slugify(name)}' if name else ''
  link = f'{config.host}/a/{to_alpha(message.chat_id)}/{to_alpha(message.id)}{hint}' \
    f'{message.file.ext or ""}'
  if message.sticker:
    link += f' {message.file.emoji}'
  return f'{text} {link}' if text else link


def render_link_preview(message, text):
  preview = message.web_preview
  if not preview:
    return text
  parts = (' '.join(part.split()) for part in (preview.title, preview.description) if part)
  rendered = ' :: '.join(part for part in parts if part)
  if not rendered:
    return text
  rendered = truncate_on_word(rendered, config.irc_link_preview_length)
  return f'{text}\n[{rendered}]'


async def render_forward(session, message, text):
  forward = message.forward
  if not forward:
    return text
  entity = forward.sender or forward.chat
  if entity:
    name = (await session.resolve_peer_record(entity)).alias
  else:
    name = slugify(forward.from_name, separator='_')
  return f'(F: {name}) {text}'


async def render_reply(session, message, recent, text, own_nick):
  ''' alias: text for replies to one of the last irc_near_reply_distance messages or to the
  author's latest one, alias: > quote | text otherwise
  '''
  reply_to = message.reply_to
  if isinstance(reply_to, MessageReplyStoryHeader):
    result = await message.client(GetStoriesByIDRequest(reply_to.peer, [reply_to.story_id]))
    captions = [story.caption for story in result.stories if getattr(story, 'caption', None)]
    return '> <{}> | {}'.format(' '.join(' '.join(captions).split()), text)
  if not message.reply_to_msg_id or reply_to.forum_topic and not reply_to.reply_to_top_id:
    return text

  index = next((i for i, m in enumerate(recent) if m.id == message.reply_to_msg_id), None)
  if index is not None:
    replied = recent[index]
    author = (await Peer.get(id=replied.sender_id)).alias if replied.sender_id else own_nick
    quote = replied.text
    later = [m for m in recent if replied.id < m.id < message.id]
    is_near = len(later) < config.irc_near_reply_distance or \
      all(m.sender_id != replied.sender_id for m in later)
  else:
    replied_message = await message.get_reply_message()
    if not replied_message:
      return text
    if replied_message.out:
      author = own_nick
    else:
      sender = replied_message.from_id or replied_message.peer_id
      author = (await session.resolve_peer_record(sender)).alias
    quote = replied_message.message or ''
    is_near = False

  if reply_to.quote_text:
    return '{}: > {} | {}'.format(author, ' '.join(reply_to.quote_text.split()), text)
  if is_near:
    return f'{author}: {text}'
  quote = ' '.join(quote.split())
  if index is None and any(c.isalnum() for c in quote):
    return f'{author}: > {quote} | {text}'
  quote = truncate_on_word(quote or f'[{to_alpha(message.reply_to_msg_id)}]',
                           config.irc_quote_length)
  return f'{author}: > {quote} | {text}'


def format_multiline_msg(text, rem_lines):
  for number, line in enumerate(text.split('\n')):
    if number:
      line = f'| {line}'
    while len(line.encode()) > rem_lines:
      head = line.encode()[:rem_lines - 1].decode(errors='ignore')
      space = head.rfind(' ')
      if space > 0:
        head = head[:space]
      yield f'{head}\\'
      line = '\\ ' + line[len(head):].removeprefix(' ')
    yield line


def format_line_edit(old, new):
  ''' %s/old/new/ for one changed run of words up to irc_edit_length, whole line otherwise. Pure
  inserts and deletes take the neighbouring word along, so the old part is never empty
  '''
  old_words, new_words = old.split(' '), new.split(' ')
  changes = [op for op in SequenceMatcher(None, old_words, new_words).get_opcodes()
             if op[0] != 'equal']
  if len(changes) == 1:
    _, i1, i2, j1, j2 = changes[0]
    if i1 == i2 or j1 == j2:
      if i1:
        i1, j1 = i1 - 1, j1 - 1
      else:
        i2, j2 = i2 + 1, j2 + 1
    before, after = ' '.join(old_words[i1:i2]), ' '.join(new_words[j1:j2])
    if max(len(before), len(after)) <= config.irc_edit_length:
      return f'%s/{before}/{after}/'
  return f'% {new}'


def format_edit(old, new):
  ''' Lines announcing an edit: one line changed -> format_line_edit, more lines changed or added
  -> each of them as %| line, unknown old text -> the whole new text. Removed lines are not shown
  '''
  new_lines = new.split('\n')
  if old is None:
    if len(new_lines) == 1:
      return [f'% {new}']
    return [f'%| {line}' for line in new_lines]

  old_lines = old.split('\n')
  changes = [op for op in SequenceMatcher(None, old_lines, new_lines).get_opcodes()
             if op[0] != 'equal']
  if len(changes) == 1 and changes[0][0] == 'replace':
    _, i1, i2, j1, j2 = changes[0]
    if i2 - i1 == 1 and j2 - j1 == 1:
      return [format_line_edit(old_lines[i1], new_lines[j1])]
  return [f'%| {line}' for _, _, _, j1, j2 in changes for line in new_lines[j1:j2]]


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
