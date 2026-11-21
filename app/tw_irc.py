''' IRC server side: line framing on Twisted, one active client connection
'''
import asyncio, inspect

from telethon import events
from twisted.internet.defer import Deferred
from twisted.internet.protocol import ServerFactory
from twisted.protocols.basic import LineOnlyReceiver
from twisted.python import log
from twisted.python.failure import Failure
from twisted.words.protocols import irc as irc_protocol

import config, irc_message
from irc_helpers import SERVER, peer_hostmask, render_lines
from models import Peer


class IRCConnection(LineOnlyReceiver):
  delimiter = b'\n'
  MAX_LENGTH = 8192

  # #################### LineOnlyReceiver / Protocol overrides
  def connectionMade(self):
    self.nick = None
    self.user = None
    self.password = None
    self.registered = False

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
    if not self.registered and command not in {'PASS', 'NICK', 'USER', 'PING', 'QUIT'}:
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
    if self.registered or not (self.nick and self.user):
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

  # #################### IRC commands
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

  def irc_MODE(self, target, *_):
    if target.lower() == self.nick.lower():
      self.reply(irc_protocol.RPL_UMODEIS, '+')

  def irc_USERHOST(self, *nicks):
    own = [f'{self.nick}=+{self.user}@{SERVER}' for n in nicks if n.lower() == self.nick.lower()]
    self.reply(irc_protocol.RPL_USERHOST, ' '.join(own))

  def irc_WHO(self, mask='*', *_):
    self.reply(irc_protocol.RPL_ENDOFWHO, mask, 'End of WHO')

  async def irc_PRIVMSG(self, target, text):
    peer = await Peer.get_or_none(alias=target)
    if not peer:
      self.reply(irc_protocol.ERR_NOSUCHNICK, target, 'No such nick')
      return
    client = self.factory.session.client
    await client.send_message(peer.id, text, parse_mode=None)
    await client.send_read_acknowledge(peer.id)


class TelegramIRCFactory(ServerFactory):
  protocol = IRCConnection

  def __init__(self, session):
    self.session = session
    self.active = None
    session.client.add_event_handler(
      self.on_private_message, events.NewMessage(incoming=True, func=lambda e: e.is_private))

  def attach(self, connection):
    if self.active:
      self.active.error('Replaced by a new connection')
    self.active = connection

  # #################### Telegram events
  async def on_private_message(self, event):
    sender = await self.session.resolve_peer_record(event.sender_id)
    if not self.active:
      return
    for line in render_lines(event.message):
      self.active.send('PRIVMSG', self.active.nick, line, prefix=peer_hostmask(sender))
