import asyncio

from twisted.internet.interfaces import IPushProducer
from twisted.python.failure import Failure
from twisted.web import resource, server
from zope.interface import implementer


# Workaround for slow telegram data fetches
@implementer(IPushProducer)
class Throttle:
  '''
  '''

  def __init__(self):
    self.ready = asyncio.Event()
    self.ready.set()

  def pauseProducing(self):
    self.ready.clear()

  def resumeProducing(self):
    self.ready.set()

  def stopProducing(self):
    pass


class Endpoint(resource.Resource):
  isLeaf = True

  def __init__(self, session, handler, peer):
    super().__init__()
    self.session = session
    self.handler = handler
    self.peer = peer

  def render_GET(self, request):
    task = asyncio.ensure_future(self.respond(request))
    request.notifyFinish().addErrback(lambda _: task.cancel())
    return server.NOT_DONE_YET

  async def respond(self, request):
    try:
      await self.session.conn_check()

      result = await self.handler(
        self.session, request, self.peer,
        *[s.decode() for s in request.postpath])

      request.setHeader('Content-Type', 'text/html; charset=utf-8')

      # Some ghetto hacking to keep response shapes same as in Quart
      body, extra = result \
        if isinstance(result, tuple) \
        else (result, None)

      # int for return code
      if isinstance(extra, int):
        request.setResponseCode(extra)
      # dict for headers
      elif isinstance(extra, dict):
        for name, value in extra.items():
          request.setHeader(name, value)
      elif extra is not None:
        raise ValueError(f'What are you feeding into response meta? Hint: {extra}')

      # Need to manually encode body, sheesh
      if isinstance(body, str):
        request.write(body.encode())
      # I think only avatar uses that
      elif isinstance(body, bytes):
        request.write(body)
      # Streamed responses
      else:
        throttle = Throttle()
        request.registerProducer(throttle, True)
        async for chunk in body:
          request.write(chunk)
          await throttle.ready.wait()
        request.unregisterProducer()

      request.finish()

    except Exception as e:  # Let's do some generic wapping here
      request.processingFailed(Failure())


# Generic to quickly inject handler function without writing whole class
class MyResouce(resource.Resource):

  def __init__(self, session, handler):
    super().__init__()
    self.session = session
    self.handler = handler

  def getChild(self, name, request):
    return Endpoint(self.session, self.handler, name.decode())
