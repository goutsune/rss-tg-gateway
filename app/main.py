#!/usr/bin/env python3
import asyncio
import sys

from twisted.internet import asyncioreactor
asyncioreactor.install()  # Needs to be before other stuff
from twisted.internet import reactor
from twisted.internet.defer import Deferred
from twisted.python import log
from twisted.web import pages, resource, server

import config, handlers
from tg_session import TelegramSession
from tw_web import MyResouce

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
root.putChild(b'profile', MyResouce(session, handlers.retr_avatar))


# #################### Init
if __name__ == '__main__':
  asyncio.get_event_loop().run_until_complete(session.startup())
  log.startLogging(sys.stdout)
  reactor.addSystemEventTrigger(
    'before', 'shutdown',
    lambda: Deferred.fromFuture(asyncio.ensure_future(session.cleanup())))
  reactor.listenTCP(config.port, server.Site(root), interface=config.bind)
  reactor.run()
