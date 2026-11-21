''' TelegramServ and in-conversation commands, operating on Peer records
'''
import re

from telethon import utils
from tortoise.expressions import Q

from irc_helpers import SERVER, find_peer, irc_name
from models import Peer

SERVICE = 'TelegramServ'
SERVICE_HOSTMASK = f'{SERVICE}!service@{SERVER}'
PREFIX = ','
VALID_ALIAS = re.compile(r'[A-Za-z_\[\]\\^{}|][A-Za-z0-9_\-\[\]\\^{}|]*')

HELP = [
  'help: this list',
  'alias [name] <new>: rename a user or #chat, name defaults to the current conversation',
  'aliases [filter]: aliases with filter in alias or display name',
  'whois [name]: peer record, name defaults to the current conversation',
  'flush: drop cached chat topics',
  f'In a conversation {PREFIX}command runs a command, {PREFIX * 2}text sends {PREFIX}text',
]


def notice(connection, context, text):
  target = irc_name(context) if context and context.id < 0 else connection.nick
  connection.send('NOTICE', target, text, prefix=SERVICE_HOSTMASK)


async def run(connection, context, text):
  name, *args = text.split()
  command = COMMANDS.get(name.lower())
  if not command:
    notice(connection, context, f'Unknown command {name}, see help')
    return
  await command(connection, context, *args)


async def cmd_help(connection, context):
  for line in HELP:
    notice(connection, context, line)


async def cmd_alias(connection, context, *args):
  *names, new = args
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


async def cmd_aliases(connection, context, text=''):
  peers = await Peer.filter(
    Q(alias__icontains=text) | Q(display_name__icontains=text)).order_by('alias')
  for peer in peers:
    peer_type = utils.resolve_id(peer.id)[1].__name__
    notice(connection, context, f'{irc_name(peer)} ({peer_type}): {peer.display_name}')


async def cmd_whois(connection, context, *names):
  peer = await find_peer(names[0]) if names else context
  if not peer:
    notice(connection, context, f'No such nick/channel {names[0]}')
    return
  peer_type = utils.resolve_id(peer.id)[1].__name__
  notice(connection, context, f'{irc_name(peer)} ({peer_type}): {peer.display_name}')
  notice(connection, context, f'id {peer.id}, username {peer.username}')


async def cmd_flush(connection, context):
  connection.factory.topics.clear()
  notice(connection, context, 'Topic cache dropped')


COMMANDS = {
  'help': cmd_help,
  'alias': cmd_alias,
  'aliases': cmd_aliases,
  'whois': cmd_whois,
  'flush': cmd_flush,
}
