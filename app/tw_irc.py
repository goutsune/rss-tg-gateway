from twisted.internet.protocol import ServerFactory
from twisted.python import log
from twisted.words.protocols import irc


class TelegramIRC(irc.IRC):
    def connectionMade(self):
        session = self.factory.session

        self.sendLine(":server 001 grug :Welcome to dummy server")
        self.sendLine(":server 376 grug :End of MOTD")

    def lineReceived(self, line):
        log.msg(f"IRC: {line.decode('utf-8', 'ignore')}")
        if line.startswith("PING "):
            self.sendLine("PONG " + line[5:])


class TelegramIRCFactory(ServerFactory):
    protocol = TelegramIRC

    def __init__(self, session):
        self.session = session
