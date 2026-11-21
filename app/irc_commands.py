''' TelegramServ and in-conversation commands, operating on Peer records
'''
import re

from slugify import slugify
from telethon import utils
from tortoise.expressions import Q

from irc_helpers import SERVER, find_peer, irc_name
from models import Peer

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
  'R partial|body: reply to a recent message containing partial, R1, R2... pick a match',
  'r [alias:] body: reply to the latest message of alias, of the peer in private chats',
  f'In a conversation {PREFIX}command runs a command, {PREFIX * 2}text sends {PREFIX}text',
]


def notice(connection, context, text):
  target = irc_name(context) if context and context.id < 0 else connection.nick
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
  if await Peer.filter(alias__iexact=new).exclude(id=peer.id).exists():
    notice(connection, context, f'Alias {new} is taken')
    return

  old_name = irc_name(peer)
  old_hostmask = f'{peer.alias}!{peer.id}@{SERVER}'
  peer.alias = new
  await peer.save()

  if peer.id > 0:
    connection.send('NICK', new, prefix=old_hostmask)
  elif old_name.lower() in connection.channels:
    connection.part(old_name)
    await connection.join_chat(peer)
  notice(connection, peer if peer.id < 0 else context, f'{old_name} is now {irc_name(peer)}')


async def cmd_aliases(connection, context, rest):
  text = rest.strip()
  peers = await Peer.filter(
    Q(alias__icontains=text) | Q(display_name__icontains=text)).order_by('alias')
  for peer in peers:
    peer_type = utils.resolve_id(peer.id)[1].__name__
    notice(connection, context, f'{irc_name(peer)} ({peer_type}): {peer.display_name}')


async def cmd_whois(connection, context, rest):
  names = rest.split()
  peer = await find_peer(names[0]) if names else context
  if not peer:
    notice(connection, context, f'No such nick/channel {names[0]}')
    return
  peer_type = utils.resolve_id(peer.id)[1].__name__
  notice(connection, context, f'{irc_name(peer)} ({peer_type}): {peer.display_name}')
  notice(connection, context, f'id {peer.id}, username {peer.username}')


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
      sender = await Peer.get_or_none(id=match.sender_id)
      name = sender.alias if sender else match.sender_id
      notice(connection, context, f'{PREFIX}R{number} {name}: {" ".join(match.text.split())}')


async def cmd_reply_latest(connection, context, rest):
  if context.id < 0:
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


COMMANDS = {
  'help': cmd_help,
  'alias': cmd_alias,
  'aliases': cmd_aliases,
  'whois': cmd_whois,
  'flush': cmd_flush,
  'R': cmd_reply_search,
  'r': cmd_reply_latest,
}
