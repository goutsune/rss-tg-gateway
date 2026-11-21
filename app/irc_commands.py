''' TelegramServ and in-conversation commands, operating on Peer records
'''
import re

from slugify import slugify
from telethon.tl.types import PeerChannel, PeerChat
from tortoise.expressions import Q

from irc_helpers import SERVER, Autojoin, find_peer, irc_name, peer_hostmask
from models import Peer, PeerKind

SERVICE = 'TelegramServ'
SERVICE_HOSTMASK = f'{SERVICE}!service@{SERVER}'
PREFIX = ','
CHOICE = re.compile(r'R(\d+)')
VALID_ALIAS = re.compile(r'[A-Za-z_\[\]\\^{}|][A-Za-z0-9_\-\[\]\\^{}|]*')

HELP = [
  'help: this list',
  'alias [name] <new>: rename a user or #chat, name defaults to the current conversation',
  'aliases [filter]: aliases with filter in alias or display name',
  'whois [name]: peer record, name defaults to the current conversation',
  'flush: drop cached chat topics',
  'autojoin [never|mentions|on_event]: show or set when a chat message joins you to its '
  'channel; mentions covers replies to you too, a parted channel waits for your JOIN',
  'idjoin <id>: join a chat by the id or t.me/c/ link copied from Telegram',
  'R partial|body: reply to a recent message containing partial, R1, R2... pick a match',
  'r [alias:] body: reply to the latest message of alias, of the peer in private chats',
  f'In a conversation {PREFIX}command runs a command, {PREFIX * 2}text sends {PREFIX}text',
  '',
  'Writing longer messages:',
  '  end a line with ^ to keep typing, the next line continues after a space',
  '  end a line with | to keep typing, the next line starts on a new line',
  '  type a lone . to send what you have, or just finish with a normal line',
  '  start with "foo: " to mention foo by name, "foo:: " keeps it as plain text',
  '',
  'Reading what comes in:',
  '  |text is the same message continuing on a new line',
  '  text^ and ^text are one long line cut in two',
  '  > quote… | text is a reply to that quote',
  '  > quote on its own line, then text, replies to an older message or quotes part of one',
  '  > <caption> | text is a reply to a story',
  '  (F: name) text was forwarded from name',
  '  *me: text in a private chat is something you sent from another Telegram app',
]


def notice(connection, context, text):
  target = irc_name(context) if context and context.kind != PeerKind.USER else connection.nick
  connection.send('NOTICE', target, text, prefix=SERVICE_HOSTMASK)


async def run(connection, context, text):
  name, _, rest = text.strip().partition(' ')
  # R1, R2... pick one of the matches a previous ambiguous R left pending; the reply goes to
  # the conversation R ran in. Any other message drops the pending choices (see irc_PRIVMSG)
  if match := CHOICE.fullmatch(name):
    pending_context, matches, body = connection.pending_choices
    connection.pending_choices = None
    reply_to = matches[int(match[1]) - 1].id
    await connection.send_to_telegram(pending_context, body, reply_to=reply_to)
    return

  command = COMMANDS.get(name)
  if not command:
    notice(connection, context, f'Unknown command {name}, see help')
    return
  await command(connection, context, rest)


async def cmd_help(connection, context, rest):
  for line in HELP:
    notice(connection, context, line)


async def cmd_alias(connection, context, rest):
  *names, new = rest.split()
  peer = await find_peer(names[0]) if names else context
  new = new.removeprefix('#')
  if not peer:
    notice(connection, context, f'No such nick/channel {names[0]}')
    return
  if not VALID_ALIAS.fullmatch(new):
    notice(connection, context, f'Invalid alias {new}')
    return
  if peer.kind == PeerKind.TOPIC:
    is_taken = Peer.filter(kind=PeerKind.TOPIC, forum_id=peer.forum_id)
  else:
    is_taken = Peer.exclude(kind=PeerKind.TOPIC)
  if await is_taken.exclude(id=peer.id).filter(alias__iexact=new).exists():
    notice(connection, context, f'Alias {new} is taken')
    return

  # A forum's topic channels carry its alias too, they get rejoined along with it
  conversations = [peer]
  if peer.kind in (PeerKind.CHAT, PeerKind.CHANNEL):
    topics = await Peer.filter(kind=PeerKind.TOPIC, forum=peer)
    for topic in topics:
      topic.forum = peer
    conversations += topics
  old_names = [irc_name(conversation) for conversation in conversations]
  old_hostmask = peer_hostmask(peer)
  peer.alias = new
  await peer.save()

  is_user = peer.kind == PeerKind.USER
  if is_user:
    connection.send('NICK', new, prefix=old_hostmask)
  for conversation, old_name in zip(conversations, old_names):
    if old_name.lower() in connection.channels:
      connection.part(old_name)
      await connection.join_chat(conversation)
  notice(connection, context if is_user else peer, f'{old_names[0]} is now {irc_name(peer)}')


async def cmd_aliases(connection, context, rest):
  text = rest.strip()
  peers = await Peer.filter(
    Q(alias__icontains=text) | Q(display_name__icontains=text)
  ).select_related('forum').order_by('alias')
  for peer in peers:
    notice(connection, context, f'{irc_name(peer)} ({peer.kind.value}): {peer.display_name}')


async def cmd_whois(connection, context, rest):
  names = rest.split()
  peer = await find_peer(names[0]) if names else context
  if not peer:
    notice(connection, context, f'No such nick/channel {names[0]}')
    return
  notice(connection, context, f'{irc_name(peer)} ({peer.kind.value}): {peer.display_name}')
  notice(connection, context, f'id {peer.entity_id}, username {peer.username}')


async def cmd_flush(connection, context, rest):
  connection.factory.topics.clear()
  notice(connection, context, 'Topic cache dropped')


async def cmd_reply_search(connection, context, rest):
  partial, _, body = rest.partition('|')
  partial, body = slugify(partial), body.strip()
  recent = connection.factory.recent[context.id]
  matches = [m for m in reversed(recent) if partial in slugify(m.text)]

  if not matches:
    notice(connection, context, f'No recent message matches {partial}')
  elif len(matches) == 1:
    await connection.send_to_telegram(context, body, reply_to=matches[0].id)
  else:
    connection.pending_choices = (context, matches, body)
    for number, match in enumerate(matches, 1):
      sender = match.sender_id and await Peer.get(id=match.sender_id)
      name = sender.alias if sender else connection.nick
      notice(connection, context, f'{PREFIX}R{number} {name}: {" ".join(match.text.split())}')


async def cmd_reply_latest(connection, context, rest):
  if context.kind != PeerKind.USER:
    alias, _, body = rest.partition(':')
    sender = await find_peer(alias.strip())
  else:
    sender, body = context, rest
  recent = connection.factory.recent[context.id]
  latest = next((m for m in reversed(recent) if sender and m.sender_id == sender.id), None)
  if not latest:
    notice(connection, context, 'No recent message from that user')
    return
  await connection.send_to_telegram(context, body.strip(), reply_to=latest.id)


async def cmd_autojoin(connection, context, rest):
  if mode := rest.strip():
    try:
      connection.factory.autojoin = Autojoin(mode)
    except ValueError:
      modes = ', '.join(m.value for m in Autojoin)
      notice(connection, context, f'Autojoin is one of {modes}')
      return
  notice(connection, context, f'Autojoin: {connection.factory.autojoin.value}')


async def cmd_idjoin(connection, context, rest):
  # Ids come copied from Telegram either marked (-100... channels, -... basic groups) or bare,
  # as clients and t.me/c/<id>/<msg> links show them. A bare id tries the channel form first
  # so a supergroup wins over a basic group with the same number
  number = int(re.search(r'-?\d+', rest)[0])
  candidates = [number] if number < 0 else [PeerChannel(number), PeerChat(number)]

  for candidate in candidates:
    try:
      chat = await connection.factory.session.resolve_peer_record(candidate)
    except ValueError:
      continue
    if chat:
      connection.parted.discard(irc_name(chat).lower())
      if irc_name(chat).lower() not in connection.channels:
        await connection.join_chat(chat)
      return
  notice(connection, context, f'No known chat with id {number}')


COMMANDS = {
  'help': cmd_help,
  'alias': cmd_alias,
  'aliases': cmd_aliases,
  'whois': cmd_whois,
  'flush': cmd_flush,
  'autojoin': cmd_autojoin,
  'idjoin': cmd_idjoin,
  'R': cmd_reply_search,
  'r': cmd_reply_latest,
}
