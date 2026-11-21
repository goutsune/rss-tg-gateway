''' IRC server side: line framing on Twisted, one active client connection
'''
from twisted.internet.protocol import ServerFactory
from twisted.protocols.basic import LineOnlyReceiver
from twisted.python import log
from twisted.words.protocols import irc as irc_protocol

import config, irc_message

SERVER = 'telegram'


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
      handler(*params)
    except Exception as e:
      log.err(None, f'IRC {command} handler failed')
      self.send('NOTICE', self.nick or '*', f'{command} failed: {e!r}', prefix=SERVER)

  # #################### Connection helpers
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


class TelegramIRCFactory(ServerFactory):
  protocol = IRCConnection

  def __init__(self, session):
    self.session = session
    self.active = None

  def attach(self, connection):
    if self.active:
      self.active.error('Replaced by a new connection')
    self.active = connection
