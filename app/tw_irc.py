''' IRC server side: line framing on Twisted, one active client connection
'''
import asyncio, inspect
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import partial

from telethon import events
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest, ReadDiscussionRequest
from twisted.internet.defer import Deferred
from twisted.internet.protocol import ServerFactory
from twisted.internet.task import LoopingCall
from twisted.protocols.basic import LineOnlyReceiver
from twisted.python import log
from twisted.python.failure import Failure
from twisted.words.protocols import irc as irc_protocol

import config, irc_commands, irc_message
from irc_helpers import (
  SERVER, Autojoin, RecentMessage, find_peer, format_edit, format_multiline_msg,
  incoming_mentions, irc_name, iter_text_lines, outgoing_mention, peer_hostmask, render_forward,
  render_link_preview, render_media, render_reply)
from models import Peer, PeerKind


class IRCConnection(LineOnlyReceiver):
  delimiter = b'\n'
  MAX_LENGTH = config.irc_max_line_length
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
    self.present = {}
    self.idle_sweep = LoopingCall(self.part_idle)
    self.idle_sweep.start(config.irc_presence_check_interval, now=False)

  def connectionLost(self, reason):
    self.idle_sweep.stop()
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

  async def join_chat(self, peer):
    channel = irc_name(peer)
    if peer.kind == PeerKind.FORUM:
      chat = peer.chat
      topic = f'{chat.display_name} // {peer.display_name}'
    else:
      chat = peer
      topic = await self.factory.chat_topic(chat)
    # Present are those who wrote here within the presence timeout, as far as the buffer knows
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=config.irc_presence_timeout)
    last_active = {m.sender_id: m.date for m in self.factory.recent[peer.id]
                   if m.sender_id and m.date > cutoff}
    members = await Peer.filter(id__in=list(last_active))
    self.present[peer.id] = (peer, {m.id: (m, last_active[m.id]) for m in members})

    self.channels.add(channel.lower())
    self.send('JOIN', channel, prefix=self.hostmask)
    if topic:
      self.reply(irc_protocol.RPL_TOPIC, channel, topic)
    names = [self.nick, *(member.alias for member in members)]
    for names_line in iter_text_lines(names, 400):
      self.reply(irc_protocol.RPL_NAMREPLY, '=', channel, names_line)
    self.reply(irc_protocol.RPL_ENDOFNAMES, channel, 'End of NAMES')

  def part(self, channel):
    self.channels.discard(channel.lower())
    self.send('PART', channel, prefix=self.hostmask)

  def relay(self, prefix, target, text, date, action=False):
    tags = None
    if 'server-time' in self.caps:
      tags = {'time': date.strftime('%Y-%m-%dT%H:%M:%S.000Z')}
    rem_lines = 510 - len(f':{prefix} PRIVMSG {target} :'.encode())
    if action:
      rem_lines -= len('\x01ACTION \x01')
    for line in format_multiline_msg(text, rem_lines):
      self.send('PRIVMSG', target, f'\x01ACTION {line}\x01' if action else line,
                prefix=prefix, tags=tags)

  def relay_message(self, peer, sender, text, date):
    ''' Relay a Telegram message into its query or channel; sender is None for own messages,
    which a query can only show with echo-message, otherwise as *me: from the peer
    '''
    if peer.kind != PeerKind.USER:
      if sender:
        self.mark_present(peer, sender, date)
      prefix = peer_hostmask(sender) if sender else self.hostmask
      self.relay(prefix, irc_name(peer), text, date)
    elif sender:
      self.relay(peer_hostmask(sender), self.nick, text, date)
    elif 'echo-message' in self.caps:
      self.relay(self.hostmask, peer.alias, text, date)
    else:
      self.relay(peer_hostmask(peer), self.nick, f'*me: {text}', date)

  def mark_present(self, peer, member, date):
    if (datetime.now(timezone.utc) - date).total_seconds() > config.irc_presence_timeout:
      return
    _, members = self.present.setdefault(peer.id, (peer, {}))
    if member.id in members:
      date = max(date, members[member.id][1])
    else:
      self.send('JOIN', irc_name(peer), prefix=peer_hostmask(member))
    members[member.id] = (member, date)

  def mark_absent(self, peer, member):
    _, members = self.present.setdefault(peer.id, (peer, {}))
    if members.pop(member.id, None):
      self.send('PART', irc_name(peer), prefix=peer_hostmask(member))

  def part_idle(self):
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=config.irc_presence_timeout)
    for peer, members in self.present.values():
      channel = irc_name(peer)
      for member_id, (member, last_active) in list(members.items()):
        if last_active < cutoff:
          del members[member_id]
          if channel.lower() in self.channels:
            self.send('PART', channel, prefix=peer_hostmask(member))

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

  async def irc_WHOIS(self, *targets):
    nick = targets[-1]
    peer = await find_peer(nick)
    if not peer or peer.kind != PeerKind.USER:
      self.reply(irc_protocol.ERR_NOSUCHNICK, nick, 'No such nick')
      self.reply(irc_protocol.RPL_ENDOFWHOIS, nick, 'End of WHOIS')
      return

    name = ' '.join(filter(None, (peer.first_name, peer.last_name)))
    username = f'@{peer.username}' if peer.username else 'no username'
    self.reply(irc_protocol.RPL_WHOISUSER, peer.alias, peer.entity_id, SERVER, '*', name)
    self.reply(irc_protocol.RPL_WHOISSERVER, peer.alias, SERVER, username)
    if seen := self.factory.last_seen.get(peer.id):
      idle = int((datetime.now(timezone.utc) - seen).total_seconds())
      self.reply(irc_protocol.RPL_WHOISIDLE, peer.alias, idle, int(seen.timestamp()),
                 'seconds idle, signon time')
    self.reply(irc_protocol.RPL_ENDOFWHOIS, peer.alias, 'End of WHOIS')

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
      if not dialog.is_group:
        continue
      size = getattr(dialog.entity, 'participants_count', None) or 0
      topic = await self.factory.chat_topic(peer)
      self.reply(irc_protocol.RPL_LIST, irc_name(peer), size, topic)
      if getattr(dialog.entity, 'forum', False):
        for forum in await session.resolve_forums(peer):
          self.reply(irc_protocol.RPL_LIST, irc_name(forum), size,
                     f'{peer.display_name} // {forum.display_name}')
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
    if text.endswith(('\\', '|')):
      composed += text[:-1] + (' ' if text.endswith('\\') else '\n')
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
    if peer.kind == PeerKind.FORUM and reply_to is None:
      reply_to = peer.forum_id
    client = self.factory.session.client
    text, entities = await outgoing_mention(client, text)
    telegram_peer = peer.telegram_peer
    message = await client.send_message(
      telegram_peer, text, parse_mode=None, formatting_entities=entities, reply_to=reply_to)
    self.factory.recent[peer.id].append(RecentMessage(message.id, None, text, message.date))
    # Without max_id Telethon marks the whole chat read, every forum included
    if peer.kind == PeerKind.FORUM:
      await client(ReadDiscussionRequest(telegram_peer, peer.forum_id, message.id))
    else:
      await client.send_read_acknowledge(telegram_peer, max_id=message.id)


class TelegramIRCFactory(ServerFactory):
  protocol = IRCConnection

  def __init__(self, session):
    self.session = session
    self.active = None
    self.topics = {}
    self.recent = defaultdict(partial(deque, maxlen=config.irc_recent_messages))
    self.autojoin = Autojoin.MENTIONS
    self.last_seen = {}
    session.client.add_event_handler(
      self.on_private_message, events.NewMessage(func=lambda e: e.is_private))
    session.client.add_event_handler(
      self.on_chat_message, events.NewMessage(func=lambda e: e.is_group))
    session.client.add_event_handler(
      self.on_edit,
      events.MessageEdited(incoming=True, func=lambda e: e.is_private or e.is_group))
    session.client.add_event_handler(
      self.on_chat_action, events.ChatAction(func=lambda e: e.is_group))

  def attach(self, connection):
    if self.active:
      self.active.error('Replaced by a new connection')
    self.active = connection

  async def chat_topic(self, chat):
    if chat.id in self.topics:
      return self.topics[chat.id]
    client = self.session.client
    if chat.kind == PeerKind.CHANNEL:
      full = await client(GetFullChannelRequest(chat.telegram_peer))
    else:
      full = await client(GetFullChatRequest(chat.entity_id))
    description = ' :: '.join(line for line in full.full_chat.about.splitlines() if line.strip())
    self.topics[chat.id] = f'{chat.display_name} // {description}' if description \
      else chat.display_name
    return self.topics[chat.id]

  async def render_message(self, message, peer, sender):
    recent = self.recent[peer.id]
    text = await incoming_mentions(self.session, message)
    rendered = render_media(message, await render_forward(self.session, message, text))
    own_nick = self.active.nick if self.active else None
    rendered = await render_reply(
      self.session, message, recent, render_link_preview(message, rendered), own_nick)
    if all(m.id != message.id for m in recent):
      recent.append(RecentMessage(message.id, sender and sender.id, text, message.date))
    if sender:
      self.last_seen[sender.id] = max(message.date, self.last_seen.get(sender.id, message.date))
    return rendered

  # #################### Telegram events
  async def on_private_message(self, event):
    peer = await self.session.resolve_peer_record(event.message.peer_id)
    sender = None if event.out else peer
    text = await self.render_message(event.message, peer, sender)
    if self.active:
      self.active.relay_message(peer, sender, text, event.date)

  async def on_chat_message(self, event):
    peer = await self.session.resolve_message_peer_record(event.message)
    sender = None if event.out else await self.session.resolve_peer_record(event.message.from_id)
    text = await self.render_message(event.message, peer, sender)
    connection = self.active
    if not connection:
      return
    channel = irc_name(peer)
    if channel.lower() not in connection.channels:
      if channel.lower() in connection.parted or self.autojoin == Autojoin.NEVER or \
          self.autojoin == Autojoin.MENTIONS and not event.mentioned:
        return
      await connection.join_chat(peer)
    connection.relay_message(peer, sender, text, event.date)

  async def on_edit(self, event):
    peer = await self.session.resolve_message_peer_record(event.message)
    recent = self.recent[peer.id]
    index = next((i for i, m in enumerate(recent) if m.id == event.id), None)
    text = await incoming_mentions(self.session, event.message)
    old = None
    if index is not None:
      old = recent[index].text
      recent[index] = recent[index]._replace(text=text)

    connection = self.active
    if not connection:
      return
    if peer.kind == PeerKind.USER:
      sender, target = peer, connection.nick
    elif irc_name(peer).lower() in connection.channels:
      sender = await self.session.resolve_peer_record(event.message.from_id)
      target = irc_name(peer)
    else:
      return
    for line in format_edit(old, text):
      connection.relay(peer_hostmask(sender), target, line, event.message.edit_date)

  async def on_chat_action(self, event):
    if event.action_message:
      peer = await self.session.resolve_message_peer_record(event.action_message)
    else:
      peer = await self.session.resolve_peer_record(event.chat_id)
    connection = self.active
    if not connection or irc_name(peer).lower() not in connection.channels:
      return

    if event.new_pin:
      pinned = await event.get_pinned_message()
      pinner = await self.session.resolve_peer_record(event.action_message.from_id)
      body = ' '.join(render_media(pinned, pinned.message or '').split())
      connection.relay(peer_hostmask(pinner), irc_name(peer), f'Pinned: {body}',
                       event.action_message.date, action=True)
      return
    for user in await event.get_users():
      member = await self.session.resolve_peer_record(user)
      if event.user_joined or event.user_added:
        connection.mark_present(peer, member, datetime.now(timezone.utc))
      elif event.user_left or event.user_kicked:
        connection.mark_absent(peer, member)
