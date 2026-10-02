#!/usr/bin/env python3
import asyncio
import sys

from twisted.internet import asyncioreactor
asyncioreactor.install()  # Needs to be before other stuff
from twisted.internet import reactor
from twisted.internet.defer import Deferred
from twisted.internet.protocol import Factory
from twisted.python import log
from twisted.web import pages, resource, server

import config, handlers, models
from tg_session import TelegramSession
from tw_web import MyResouce
from tw_irc import TelegramIRCFactory

session = TelegramSession()

# Wire routes
rss = MyResouce(session, handlers.retr_rss_user)
rss.putChild(b'i', MyResouce(session, handlers.retr_rss_id))
rss.putChild(b'favicon.ico', pages.notFound())

media = MyResouce(session, handlers.retr_media)
media.putChild(b'i', MyResouce(session, handlers.resolve_peer_with_media))

root = resource.Resource()
root.putChild(b'rss', rss)
root.putChild(b'media', media)
root.putChild(b'msg', MyResouce(session, handlers.retr_msg))
root.putChild(b'profile', MyResouce(session, handlers.retr_avatar))


# #################### Init
if __name__ == '__main__':
  asyncio.get_event_loop().run_until_complete(models.init())
  asyncio.get_event_loop().run_until_complete(session.startup())
  log.startLogging(sys.stdout)
  reactor.addSystemEventTrigger(
    'before', 'shutdown',
    lambda: Deferred.fromFuture(asyncio.ensure_future(session.cleanup())))
  reactor.addSystemEventTrigger(
    'before', 'shutdown',
    lambda: Deferred.fromFuture(asyncio.ensure_future(models.close())))

  reactor.listenTCP(config.web_port, server.Site(root), interface=config.bind)
  reactor.listenTCP(config.irc_port, TelegramIRCFactory(session), interface=config.bind)

  reactor.run()
