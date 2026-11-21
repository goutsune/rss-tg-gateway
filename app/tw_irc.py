''' IRC server side: line framing on Twisted, one active client connection
'''
import asyncio, inspect
from collections import defaultdict, deque
from datetime import datetime, timezone
from functools import partial

from telethon import events
from telethon.errors import RPCError
from telethon.tl.types import MessageReplyHeader
from twisted.internet.defer import Deferred
from twisted.internet.protocol import ServerFactory
from twisted.protocols.basic import LineOnlyReceiver
from twisted.python import log
from twisted.python.failure import Failure
from twisted.words.protocols import irc as irc_protocol

import config, irc_commands, irc_message
from models import PeerKind
from irc_helpers import (
  RECENT_MESSAGES, Autojoin, SERVER, RecentMessage, fetch_names, fetch_topic, find_peer, incoming_mentions,
  format_multiline_msg, irc_name, iter_text_lines, outgoing_mention, peer_hostmask, render_forward,
  render_media, render_reply)


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
    self.pending_choices = None
    self.compose = {}
    self.parted = set()

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

  async def join_chat(self, conversation):
    channel = irc_name(conversation)
    if conversation.kind == PeerKind.TOPIC:
      chat = conversation.forum
      topic = f'{conversation.display_name} // {await self.factory.chat_topic(chat)}'
    else:
      chat = conversation
      topic = await self.factory.chat_topic(chat)
    try:
      names = await fetch_names(self.factory.session, chat)
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

  def relay(self, prefix, target, text, date):
    tags = None
    if 'server-time' in self.caps:
      tags = {'time': date.strftime('%Y-%m-%dT%H:%M:%S.000Z')}
    rem_lines = 510 - len(f':{prefix} PRIVMSG {target} :'.encode())
    for line in format_multiline_msg(text, rem_lines):
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
    self.parted.add(channel.lower())
    self.part(channel)

  async def irc_JOIN(self, channels, *_):
    for name in channels.split(','):
      chat = await find_peer(name) if name.startswith('#') else None
      if not chat:
        self.reply(irc_protocol.ERR_NOSUCHCHANNEL, name, 'No such channel')
        continue
      self.parted.discard(name.lower())
      if irc_name(chat).lower() not in self.channels:
        await self.join_chat(chat)

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
      self.relay(self.hostmask, target, text, datetime.now(timezone.utc))

    composed = self.compose.pop(target.lower(), '')
    if text.endswith(('^', '|')):
      composed += text[:-1] + (' ' if text.endswith('^') else '\n')
      self.compose[target.lower()] = composed
      buffered = composed[:-1]
      irc_commands.notice(
        self, peer, f'{len(buffered)} chars, {buffered.count("\n") + 1} lines buffered')
      return
    if composed:
      text = composed[:-1] if text == '.' else composed + text

    is_choice = irc_commands.CHOICE.fullmatch(text.removeprefix(irc_commands.PREFIX))
    if self.pending_choices and not is_choice:
      irc_commands.notice(self, self.pending_choices[0], 'Reply choices dropped')
      self.pending_choices = None

    prefix = irc_commands.PREFIX
    if not peer or text.startswith(prefix) and not text.startswith(prefix * 2):
      await irc_commands.run(self, peer, text.removeprefix(prefix))
      return
    await self.send_to_telegram(peer, text.removeprefix(prefix))

  async def send_to_telegram(self, peer, text, reply_to=None):
    if peer.kind == PeerKind.TOPIC and reply_to is None:
      reply_to = peer.topic_id
    client = self.factory.session.client
    text, entities = await outgoing_mention(client, text)
    message = await client.send_message(
      peer.telegram_peer, text, parse_mode=None, formatting_entities=entities, reply_to=reply_to)
    self.factory.recent[peer.id].append(RecentMessage(message.id, None, text))
    await client.send_read_acknowledge(peer.telegram_peer)


class TelegramIRCFactory(ServerFactory):
  protocol = IRCConnection

  def __init__(self, session):
    self.session = session
    self.active = None
    self.topics = {}
    self.recent = defaultdict(partial(deque, maxlen=RECENT_MESSAGES))
    self.autojoin = Autojoin.MENTIONS
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

  async def render_message(self, message, chat, sender):
    recent = self.recent[chat.id]
    text = await incoming_mentions(self.session, message)
    rendered = render_media(message, await render_forward(self.session, message, text))
    rendered = await render_reply(message, recent, rendered)
    recent.append(RecentMessage(message.id, sender and sender.id, text))
    return rendered

  # #################### Telegram events
  async def on_private_message(self, event):
    peer = await self.session.resolve_peer_record(event.message.peer_id)
    text = await self.render_message(event.message, peer, None if event.out else peer)
    connection = self.active
    if not connection:
      return
    if not event.out:
      connection.relay(peer_hostmask(peer), connection.nick, text, event.date)
    elif 'echo-message' in connection.caps:
      connection.relay(connection.hostmask, peer.alias, text, event.date)
    else:
      connection.relay(peer_hostmask(peer), connection.nick, f'*me: {text}', event.date)

  async def on_chat_message(self, event):
    chat = await self.session.resolve_peer_record(event.message.peer_id)
    sender = None if event.out else await self.session.resolve_peer_record(event.message.from_id)
    # Forum topics other than General flag their messages; General stays in the chat channel
    reply_to = event.message.reply_to
    forum_topic = None
    if isinstance(reply_to, MessageReplyHeader) and reply_to.forum_topic:
      topic_id = reply_to.reply_to_top_id or reply_to.reply_to_msg_id
      forum_topic = await self.session.resolve_topic_record(chat, topic_id)
    conversation = forum_topic or chat
    text = await self.render_message(event.message, conversation, sender)
    connection = self.active
    if not connection:
      return
    channel = irc_name(conversation)
    if channel.lower() not in connection.channels:
      if channel.lower() in connection.parted or self.autojoin == Autojoin.NEVER or \
          self.autojoin == Autojoin.MENTIONS and not event.mentioned:
        return
      await connection.join_chat(conversation)
    prefix = connection.hostmask if event.out else peer_hostmask(sender)
    connection.relay(prefix, channel, text, event.date)
