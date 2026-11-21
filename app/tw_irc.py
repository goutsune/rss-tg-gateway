''' IRC server side: line framing on Twisted, one active client connection
'''
import asyncio, inspect
from collections import defaultdict, deque
from datetime import datetime, timezone
from functools import partial

from telethon import events
from telethon.errors import RPCError
from twisted.internet.defer import Deferred
from twisted.internet.protocol import ServerFactory
from twisted.protocols.basic import LineOnlyReceiver
from twisted.python import log
from twisted.python.failure import Failure
from twisted.words.protocols import irc as irc_protocol

import config, irc_commands, irc_message
from irc_helpers import (
  RECENT_MESSAGES, SERVER, RecentMessage, fetch_names, fetch_topic, find_peer, incoming_mentions,
  irc_name, iter_text_lines, outgoing_mention, peer_hostmask, render_reply)


class IRCConnection(LineOnlyReceiver):
  delimiter = b'\n'
  MAX_LENGTH = 8192
  CAPABILITIES = ('echo-message', 'server-time')

  # #################### LineOnlyReceiver / Protocol overrides
  def connectionMade(self):
    self.nick = None
    self.user = None
    self.password = None
    self.registered = False
    self.negotiating = False
    self.caps = set()
    self.channels = set()

  def connectionLost(self, reason):
    if self.factory.active is self:
      self.factory.active = None

  def sendLine(self, line):
    return self.transport.writeSequence((line, b'\r\n'))

  def lineLengthExceeded(self, line):
    self.error('Line too long')

  def lineReceived(self, line):
    line = line.rstrip(b'\r').decode('utf-8', errors='replace')
    if not line:
      return
    _, command, params = irc_message.decode(line)

    handler = getattr(self, f'irc_{command}', None)
    if handler is None:
      self.reply(irc_protocol.ERR_UNKNOWNCOMMAND, command, 'Unknown command')
      return
    if not self.registered and command not in {'CAP', 'PASS', 'NICK', 'USER', 'PING', 'QUIT'}:
      self.reply(irc_protocol.ERR_NOTREGISTERED, 'Not registered')
      return

    try:
      result = handler(*params)
    except Exception:
      self.command_failed(Failure(), command)
      return
    if inspect.iscoroutine(result):
      Deferred.fromFuture(asyncio.ensure_future(result)).addErrback(self.command_failed, command)

  # #################### Connection helpers
  def command_failed(self, failure, command):
    log.err(failure, f'IRC {command} failed')
    self.send('NOTICE', self.nick or '*', f'{command} failed: {failure.value!r}', prefix=SERVER)

  @property
  def hostmask(self):
    return f'{self.nick}!{self.user}@{SERVER}'

  def send(self, command, *params, prefix=None, tags=None):
    self.sendLine(irc_message.encode(command, *params, prefix=prefix, tags=tags).encode())

  def reply(self, code, *params):
    self.send(code, self.nick or '*', *params, prefix=SERVER)

  def error(self, reason):
    self.send('ERROR', reason)
    self.transport.loseConnection()

  def register(self):
    if self.registered or self.negotiating or not (self.nick and self.user):
      return
    if self.password != config.irc_password:
      self.reply(irc_protocol.ERR_PASSWDMISMATCH, 'Password incorrect')
      self.error('Bad password')
      return

    self.registered = True
    self.factory.attach(self)
    self.reply(irc_protocol.RPL_WELCOME, self.hostmask)
    self.reply(irc_protocol.RPL_ISUPPORT,
               'CASEMAPPING=ascii', 'CHANTYPES=#', 'NETWORK=Telegram', 'are supported')
    self.reply(irc_protocol.ERR_NOMOTD, 'No MOTD')

  async def join_chat(self, chat):
    channel = irc_name(chat)
    topic = await self.factory.chat_topic(chat)
    try:
      names = await fetch_names(self.factory.session, chat.id)
    except RPCError as e:
      self.send('NOTICE', self.nick, f'{channel} names failed: {e!r}', prefix=SERVER)
      names = []

    self.channels.add(channel.lower())
    self.send('JOIN', channel, prefix=self.hostmask)
    if topic:
      self.reply(irc_protocol.RPL_TOPIC, channel, topic)
    for names_line in iter_text_lines([self.nick, *names], 400):
      self.reply(irc_protocol.RPL_NAMREPLY, '=', channel, names_line)
    self.reply(irc_protocol.RPL_ENDOFNAMES, channel, 'End of NAMES')

  def part(self, channel):
    self.channels.discard(channel.lower())
    self.send('PART', channel, prefix=self.hostmask)

  def relay(self, prefix, target, lines, date):
    tags = None
    if 'server-time' in self.caps:
      tags = {'time': date.strftime('%Y-%m-%dT%H:%M:%S.000Z')}
    for line in lines:
      self.send('PRIVMSG', target, line, prefix=prefix, tags=tags)

  # #################### IRC commands
  def irc_CAP(self, subcommand, *args):
    subcommand = subcommand.upper()
    if subcommand in ('LS', 'REQ') and not self.registered:
      self.negotiating = True

    if subcommand == 'LS':
      self.send('CAP', self.nick or '*', 'LS', ' '.join(self.CAPABILITIES), prefix=SERVER)
    elif subcommand == 'LIST':
      self.send('CAP', self.nick or '*', 'LIST', ' '.join(sorted(self.caps)), prefix=SERVER)
    elif subcommand == 'REQ':
      requested = args[0].split()
      if all(cap.removeprefix('-') in self.CAPABILITIES for cap in requested):
        for cap in requested:
          if cap.startswith('-'):
            self.caps.discard(cap[1:])
          else:
            self.caps.add(cap)
        self.send('CAP', self.nick or '*', 'ACK', ' '.join(requested), prefix=SERVER)
      else:
        self.send('CAP', self.nick or '*', 'NAK', ' '.join(requested), prefix=SERVER)
    elif subcommand == 'END':
      self.negotiating = False
      self.register()

  def irc_PASS(self, password, *_):
    self.password = password

  def irc_NICK(self, nick, *_):
    if self.registered:
      self.send('NICK', nick, prefix=self.hostmask)
    self.nick = nick
    self.register()

  def irc_USER(self, user, _mode, _unused, _realname):
    self.user = user
    self.register()

  def irc_PING(self, token, *_):
    self.send('PONG', SERVER, token, prefix=SERVER)

  def irc_QUIT(self, *_):
    self.error('Quit')

  def irc_MODE(self, target, *modes):
    # Stub: no modes exist. Channel answers exist only because irssi queries `MODE #chan` and
    # the ban list `MODE #chan b` on join, and keeps the channel unsynced until 324 and 368 arrive
    if target.startswith('#'):
      if modes and 'b' in modes[0]:
        self.reply(irc_protocol.RPL_ENDOFBANLIST, target, 'End of ban list')
      else:
        self.reply(irc_protocol.RPL_CHANNELMODEIS, target, '+')
    elif target.lower() == self.nick.lower():
      self.reply(irc_protocol.RPL_UMODEIS, '+')

  def irc_USERHOST(self, *nicks):
    own = [f'{self.nick}=+{self.user}@{SERVER}' for n in nicks if n.lower() == self.nick.lower()]
    self.reply(irc_protocol.RPL_USERHOST, ' '.join(own))

  def irc_WHO(self, mask='*', *_):
    self.reply(irc_protocol.RPL_ENDOFWHO, mask, 'End of WHO')

  def irc_PART(self, channel, *_):
    self.part(channel)

  async def irc_LIST(self, *_):
    session = self.factory.session
    async for dialog in session.client.iter_dialogs():
      peer = await session.resolve_peer_record(dialog.entity)
      if dialog.is_group:
        size = getattr(dialog.entity, 'participants_count', None) or 0
        topic = await self.factory.chat_topic(peer)
        self.reply(irc_protocol.RPL_LIST, irc_name(peer), size, topic)
    self.reply(irc_protocol.RPL_LISTEND, 'End of LIST')

  async def irc_PRIVMSG(self, target, text):
    peer = None
    if target.lower() != irc_commands.SERVICE.lower():
      peer = await find_peer(target)
      if not peer:
        self.reply(irc_protocol.ERR_NOSUCHNICK, target, 'No such nick/channel')
        return
    if 'echo-message' in self.caps:
      self.relay(self.hostmask, target, [text], datetime.now(timezone.utc))

    prefix = irc_commands.PREFIX
    if not peer or text.startswith(prefix) and not text.startswith(prefix * 2):
      await irc_commands.run(self, peer, text.removeprefix(prefix))
      return
    client = self.factory.session.client
    text, entities = await outgoing_mention(client, text.removeprefix(prefix))
    message = await client.send_message(
      peer.id, text, parse_mode=None, formatting_entities=entities)
    self.factory.recent[peer.id].append(RecentMessage(message.id, message.sender_id, text))
    await client.send_read_acknowledge(peer.id)


class TelegramIRCFactory(ServerFactory):
  protocol = IRCConnection

  def __init__(self, session):
    self.session = session
    self.active = None
    self.topics = {}
    self.recent = defaultdict(partial(deque, maxlen=RECENT_MESSAGES))
    session.client.add_event_handler(
      self.on_private_message, events.NewMessage(func=lambda e: e.is_private))
    session.client.add_event_handler(
      self.on_chat_message, events.NewMessage(func=lambda e: e.is_group))

  def attach(self, connection):
    if self.active:
      self.active.error('Replaced by a new connection')
    self.active = connection

  async def chat_topic(self, chat):
    if chat.id not in self.topics:
      self.topics[chat.id] = await fetch_topic(self.session.client, chat)
    return self.topics[chat.id]

  async def render_message(self, message):
    recent = self.recent[message.chat_id]
    text = await incoming_mentions(self.session, message)
    rendered = await render_reply(message, recent, text)
    recent.append(RecentMessage(message.id, message.sender_id, text))
    return rendered

  # #################### Telegram events
  async def on_private_message(self, event):
    peer = await self.session.resolve_peer_record(event.chat_id)
    text = await self.render_message(event.message)
    connection = self.active
    if not connection:
      return
    lines = text.split('\n')
    if not event.out:
      connection.relay(peer_hostmask(peer), connection.nick, lines, event.date)
    elif 'echo-message' in connection.caps:
      connection.relay(connection.hostmask, peer.alias, lines, event.date)
    else:
      connection.relay(
        peer_hostmask(peer), connection.nick, [f'*me: {line}' for line in lines], event.date)

  async def on_chat_message(self, event):
    chat = await self.session.resolve_peer_record(event.chat_id)
    sender = None if event.out else await self.session.resolve_peer_record(event.sender_id)
    text = await self.render_message(event.message)
    connection = self.active
    if not connection:
      return
    channel = irc_name(chat)
    if channel.lower() not in connection.channels:
      await connection.join_chat(chat)
    prefix = connection.hostmask if event.out else peer_hostmask(sender)
    connection.relay(prefix, channel, text.split('\n'), event.date)
