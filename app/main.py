from datetime import datetime
from quart import Quart, Response, render_template, request, abort
import hypercorn.asyncio
from telethon import TelegramClient
from telethon import utils
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import InputPeerChannel, \
                                InputPeerUser, \
                                MessageService, \
                                MessageActionPinMessage, \
                                MessageActionChatEditPhoto, \
                                MessageActionChannelCreate
from telethon.errors.rpcerrorlist import ChannelPrivateError
from config import api_id, api_hash, user

app = Quart(__name__)
host = "http://127.0.0.1:9504"


class MadMachine:

  def __init__(self, session, api_id, api_hash):

    self.client = TelegramClient(user, api_id, api_hash)
    self.client.parse_mode = 'html'
    # Cache for resolving peers
    self.users = {}
    # Cache for author names when retrieving channel messages
    self.author_names = {}

  def get_filename(self, attributes):

    for attribute in attributes:
      if hasattr(attribute, 'file_name'):
        return attribute.file_name

  async def resolve_peer(self, peer):
    ''' A helper funtion to avoid re-requesting user names when
    resolving them from peer identifier objects. Currently it just
    stores rendered name never updating it, which might be a problem.
    '''
    try:
      # This processes normal users, preserving their usernames along
      # with full name if possible
      if hasattr(peer, 'user_id'):
        uid = peer.user_id
        if uid not in self.users:
          entity = await self.client.get_entity(peer)
          if entity.last_name:
            if entity.username:
              self.users[uid] = f'{entity.first_name} '\
                                f'{entity.last_name} ({entity.username})'
            else:
              self.users[uid] = f'{entity.first_name} {entity.last_name}'
          elif entity.first_name:
            if entity.username:
              self.users[uid] = f'{entity.first_name} ({entity.username})'
            else:
              self.users[uid] = f'{entity.first_name}'

      # This gets channel names
      if hasattr(peer, 'channel_id'):
        uid = peer.channel_id
        if uid not in self.users:
          channel = await self.client.get_entity(peer)
          # TODO: confirm ID pool is shared betweeen channels and users
          self.users[uid] = channel.title

    except ChannelPrivateError:
      return False

    return self.users[uid]

  async def get_name_from_msg(self, message):

    # Post attibute makes this is a channel message, let's add more info
    if message.post:
      channel = await self.resolve_peer(message.peer_id)
      if message.post_author:
        return f'{channel} ({message.post_author})'
      else:
        return f'{channel}'

    # Otherwise it is a normal message from a chat
    elif message.from_id:
      return await self.resolve_peer(message.from_id)

    else:
      return 'FIXME (unknown message type)'

  async def render_msg(self, peer_info, m):
    private = True  # Switch to generate private URLs instead of public ones

    if peer_info.username:
      peer = peer_info.username
      private = False
    else:
      peer = peer_info.id
    msg = {}
    msg['id'] = m.id
    msg['text'] = ''
    # msg['date'] = m.date.strftime(r'%a, %d %b %Y %H:%M:%S %z')
    msg['date'] = m.date.strftime(r'%Y-%m-%dT%H:%M:%S%z')
    if m.message and len(m.message) > 60:
      msg['title'] = m.message[0:60] + '…'
    else:
      msg['title'] = m.message
    if not msg['title']:
      msg['title'] = m.date.strftime(r'%d %b %Y %H:%M:%S')
    msg['author'] = await c.get_name_from_msg(m)
    msg['guid'] = f'{peer_info.id}/{m.id}'

    if private:
      msg['link'] = f'https://t.me/c/{peer_info.id}/{m.id}'
      media_base = f'{host}/media/i'
    else:
      msg['link'] = f'https://t.me/{peer_info.username}/{m.id}'
      media_base = f'{host}/media'

    # Actual post text
    if m.text:
      msg['text'] += f'<p style="white-space: pre-line">{m.text}</p>'

    # ################### Processing attachments
    # =================== Photo
    if (m.photo and not m.web_preview) or m.sticker:
      if private:
        msg['text'] += f'<img src="{media_base}/{peer}/{m.id}" />'
      else:
        msg['text'] += f'<img src="{media_base}/{peer}/{m.id}" />'

    # =================== Video/GIF
    if m.gif:
      mime = m.gif.mime_type
      w = m.gif.attributes[0].w
      # h = m.gif.attributes[0].h

      msg['text'] += \
        f'<video width="{w}" height="auto" poster="{media_base}/{peer}/{m.id}/1" loop autoplay>'\
        f'<source src="{media_base}/{peer}/{m.id}" type="{mime}" />'\
        '</video>'
    # =================== Video/moov
    if m.video:
      mime = m.video.mime_type
      w = min(m.video.attributes[0].w, 640)
      # h = min(m.video.attributes[0].h, 640)

      msg['text'] += \
        f'<video width="{w}" height="auto" poster="{media_base}/{peer}/{m.id}/1" controls=1>'\
        f'<source src="{media_base}/{peer}/{m.id}" type="{mime}" />'\
        '</video>'
    # =================== Link
    if m.web_preview:
      preview_author = \
        f'({m.web_preview.author})' if m.web_preview.author else ''
      msg['text'] += '<blockquote>' \
      f'<p><b>{m.web_preview.site_name}</b> {preview_author}<br/>' \
      f'{m.web_preview.title}<br/>' \
      f'{m.web_preview.description}</p>'
      if  m.web_preview.photo:
        msg['text'] += f'<br/><img src="{media_base}/{peer}/{m.id}" /></blockquote>'
      else:
        msg['text'] += '</blockquote>'
    # =================== Octet-stream
    if m.document:
      # Try to show as image anything that matches image mime-type
      if 'image/' in m.document.mime_type:
        msg['text'] += f'<img src="{media_base}/{peer}/{m.id}" />'
      # Otherwise show <a> link with title for now.
      else:
        name = c.get_filename(m.document.attributes)
        msg['text'] += f'<p><a href="{media_base}/{peer}/{m.id}">{name}</a><p>'

    # =================== Forwarded message
    # Forwarded message should blockquote all of above
    if m.fwd_from:
      # We have a peer to read from
      if m.fwd_from.from_id:
        # Try resolving that peer id and make a link
        name = await c.resolve_peer(m.fwd_from.from_id)
        if name != False:
          user = await c.client.get_entity(m.fwd_from.from_id)
          user = user.username
          post = m.fwd_from.channel_post
          if post:
            name = f'<a href="https://t.me/{user}/{post}">{name}</a>'
          else:
            name = f'<a href="https://t.me/{user}">{name}</a>'


        elif m.fwd_from.from_name:
            # We got ChannelPrivate exception, let's note that
            name = f'{m.fwd_from.from_name} (Private channel)'
        else:
            # Can this happen?
            name = f'Private channel'

      elif m.fwd_from.from_name:
        # No peer, but we have simple name to insert e.g. channel signs
        name = m.fwd_from.from_name
      else:
        # Can this happen?
        name = "??????"
      msg['text'] = f"<p>Forwarded from {name}:</p>"\
                    f"<blockquote>{msg['text']}</blockquote>"

    # =================== Service Messages
    if type(m) == MessageService:
      # ================= Message Pin
      if (type(m.action)) == MessageActionPinMessage:
        if peer_info.username:
          msg['text'] += f'<p>{msg["author"]} pinned '\
                         f'<a href="https://t.me/{peer_info.username}/'\
                         f'{m.reply_to_msg_id}">a message</a>.</p>'
        else:
          msg['text'] += f'<p>{msg["author"]} pinned '\
                         f'<a href="https://t.me/c/{peer_info.id}/'\
                         f'{m.reply_to_msg_id}">a message</a>.</p>'
        msg['title'] = f'{msg["author"]} pinned a message'
      # ================= Channel photo edit
      if (type(m.action)) == MessageActionChatEditPhoto:
        msg['text'] += f'<p>Channel photo updated.</p>'
        msg['title'] = 'Channel photo updated'
      # ================= Channel created
      if (type(m.action)) == MessageActionChannelCreate:
        msg['text'] += f'<p>Channel created.</p>'
        msg['title'] = 'Channel created'
    return msg


# ###################### Quart setup
@app.route('/rss/<user>')
@app.route('/rss/<user>/<int:offset>')
async def retr_rss_user(user, offset=0):
  try:
    peer = await c.client.get_input_entity(user)
  except ValueError:
    return f'Unknown user {user}', 404

  try:
    if hasattr(peer, 'channel_id'):
      peer = peer.channel_id
    elif hasattr(peer, 'user_id'):
      peer = peer.user_id
  except ChannelPrivateError:
    return 'Private channel, sorry!', 403

  return await retr_rss(user, offset)


@app.route('/rss/i/<int:peer>')
@app.route('/rss/i/<int:peer>/<int:offset>')
async def retr_rss(peer, offset=0):
  # Abort shortly on HEAD request to save time
  if request.method == 'HEAD':
    return "OK"

  limit = int(request.args.get('limit', '25'))
  try:
    msgs = await c.client.get_messages(peer,
                                       limit=limit,
                                       add_offset=offset)
  except ChannelPrivateError:
    return 'Private channel, sorry!', 403

  if not msgs:
    abort(400)

  # Fetch 10 more messages if last message fetched is part of a group
  if msgs[-1].grouped_id:
    extra_msgs = await c.client.get_messages(peer, limit=10, max_id=msgs[-1].id)
    # And add matching ones to processed group
    for msg in extra_msgs:
      if msg.grouped_id == msgs[-1].grouped_id:
        msgs.append(msg)

  input_peer = await c.client.get_input_entity(peer)
  if type(input_peer) == InputPeerChannel:
    peer_info = await c.client(GetFullChannelRequest(input_peer))
    info = peer_info.full_chat.about
    peer_info = peer_info.chats[0]
  elif type(input_peer) == InputPeerUser:
    peer_info = await c.client(GetFullUserRequest(input_peer))
    info = peer_info.full_user.about
    peer_info = peer_info.users[0]
  else:
    peer_info = await c.client.get_entity(peer)
    info = ''

  if peer_info.username:
    link = f'https://t.me/{peer_info.username}'
    avatar = f'{host}/profile/{peer_info.username}'
  else:
    link = f'https://t.me/c/{peer}'
    avatar = f'{host}/profile/{peer}'

  title = utils.get_display_name(peer_info)
  # date = datetime.today().strftime(r'%a, %d %b %Y %H:%M:%S %z')
  date = datetime.today().strftime(r'%Y-%m-%dT%H:%M:%S%z')
  build = date

  content = []
  res = []
  # Fetch all messages for now
  for m in msgs:
    msg = await c.render_msg(peer_info, m)
    msg['group'] = m.grouped_id
    content.insert(0, msg)

  fin = {}
  for m in content:
    if not m['group']:
      res.append(m)
    else:
      if not m['group'] in fin:
        fin[m['group']] = m
      else:
        fin[m['group']]['text'] += m['text']

  res.extend(fin.values())

  return await render_template(
    'rss.html', contents=res, peer=peer, info=info, title=title,
    link=link, avatar=avatar, date=date, build=build, offset=offset)


@app.route('/')
@app.route('/rss/favicon.ico')
async def retr_404():
  return 'Error', 404

@app.route('/media/<peer>/<int:msg>/<size>')
@app.route('/media/<peer>/<int:msg>')
async def retr_media(peer, msg, size=None):
  m = await c.client.get_messages(peer, ids=msg)

  if not m:
    return(f'Unable to fetch message {msg} from {peer}', 404)

  if not m.media and not m.action:
    return(f'Unable to fetch media from {m}', 400)

  if type(peer) != str:
    peer = peer.access_hash

  mime_type = 'application/octet-stream'
  name = f'{peer}_{msg}.bin'
  source = None
  if m.document:
    source = m.document
    mime_type = m.document.mime_type
    for attribute in m.document.attributes:
      if hasattr(attribute, 'file_name'):
        name = attribute.file_name
  elif m.action and hasattr(m.action, 'photo'):
    source = m.action.photo
    mime_type = 'image/jpeg'
    name = f'{peer}_{msg}.jpg'
  elif m.photo:
    source = m.photo
    mime_type = 'image/jpeg'
    name = f'{peer}_{msg}.jpg'
  else:
    return(f'Unknown media type {m.media}', 400)

  if size is not None:
    size = int(size)
    return await m.download_media(file=bytes, thumb=size), {
       'Content-Type': mime_type,
       'Cache-Control': 'no-cache',
       'Content-Disposition': f'inline; filename={name}'}

  send_media = (
    i async for i in
    c.client.iter_download(
      file=source,
      request_size=32768))

  return send_media, {
    'Content-Type': mime_type,
    'Cache-Control': 'no-cache',
    'Transfer-Encoding': 'chunked',
    'Content-Disposition': f'inline; filename={name}'}


@app.route('/profile/<peer>')
@app.route('/profile/<peer>/icon.jpg')
@app.route('/profile/<peer>/favicon.ico')
async def retr_avatar(peer):
  input_peer = await c.client.get_input_entity(peer)
  return await c.client.download_profile_photo(input_peer, file=bytes), {
        'Content-Type': 'image/jpeg',
        'Cache-Control': 'no-cache',
        'Content-Disposition': f'inline; filename={peer}'}


@app.before_serving
async def startup():
  print("Connecting...")
  await c.client.start()
  print("Updating dialogs...")
  await c.client.get_dialogs()
  print("OK!")


@app.after_serving
async def cleanup():
  print("Disconnecting...")
  await c.client.disconnect()
  print("OK!")

@app.before_request
async def conn_check():
  if not c.client.is_connected():
    print("Not connected, reconnecting...")
    await startup()

# #################### Init
async def main():
  config = hypercorn.Config()
  config.bind = ["localhost:9504"]
  await hypercorn.asyncio.serve(app, config)


c = MadMachine(user, api_id, api_hash)
if __name__ == '__main__':
  c.client.loop.run_until_complete(main())
