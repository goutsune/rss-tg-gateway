import os
from datetime import datetime

from jinja2 import Environment, FileSystemLoader, select_autoescape
from telethon import utils
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import InputPeerChannel, InputPeerUser
from telethon.errors.rpcerrorlist import ChannelPrivateError

import config
from rss_helpers import render_msg

# Preconfigure jinja2 template env
templates = Environment(
  loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), 'templates')),
  autoescape=select_autoescape())


async def retr_rss_user(session, request, user, offset=0, limit=25):
  try:
    peer = await session.client.get_input_entity(user)
  except ValueError:
    return f'Unknown user {user}', 404

  try:
    if hasattr(peer, 'channel_id'):
      peer = peer.channel_id
    elif hasattr(peer, 'user_id'):
      peer = peer.user_id
  except ChannelPrivateError:
    return 'Private channel, sorry!', 403

  return await retr_rss(session, request, user, offset, limit)


async def retr_rss_id(session, request, peer, offset=0, limit=25):
  try:
    peer = int(peer)
  except ValueError as e:
    return str(e), 400

  return await retr_rss(session, request, peer, offset, limit)


async def retr_rss(session, request, peer, offset=0, limit=25):
  # Abort shortly on HEAD request to save time
  if request.method == b'HEAD':
    return 'OK'

  try:
    msgs = await session.client.get_messages(peer,
                                             limit=int(limit),
                                             add_offset=int(offset))
  except ChannelPrivateError:
    return 'Private channel, sorry!', 403
  except ValueError as e:
    return str(e), 400

  if not msgs:
    return 'Bad Request', 400

  # Fetch 10 more messages if last message fetched is part of a group
  if msgs[-1].grouped_id:
    extra_msgs = await session.client.get_messages(peer, limit=10, max_id=msgs[-1].id)
    # And add matching ones to processed group
    for msg in extra_msgs:
      if msg.grouped_id == msgs[-1].grouped_id:
        msgs.append(msg)

  input_peer = await session.client.get_input_entity(peer)
  if type(input_peer) == InputPeerChannel:
    peer_info = await session.client(GetFullChannelRequest(input_peer))
    info = peer_info.full_chat.about
    peer_info = peer_info.chats[0]
  elif type(input_peer) == InputPeerUser:
    peer_info = await session.client(GetFullUserRequest(input_peer))
    info = peer_info.full_user.about
    peer_info = peer_info.users[0]
  else:
    peer_info = await session.client.get_entity(peer)
    info = ''

  if peer_info.username:
    link = f'https://t.me/{peer_info.username}'
    avatar = f'{config.host}/profile/{peer_info.username}'
  else:
    link = f'https://t.me/c/{peer}'
    avatar = f'{config.host}/profile/{peer}'

  title = utils.get_display_name(peer_info)
  # date = datetime.today().strftime(r'%a, %d %b %Y %H:%M:%S %z')
  date = datetime.today().strftime(r'%Y-%m-%dT%H:%M:%S%z')
  build = date

  content = []
  res = []
  # Fetch all messages for now
  for m in msgs:
    msg = await render_msg(session, peer_info, m)
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

  return templates.get_template('rss.html').render(
    contents=res, peer=peer, info=info, title=title,
    link=link, avatar=avatar, date=date, build=build, offset=offset)


async def resolve_peer_with_media(session, request, peer_id, msg, size=None):
  try:
    input_peer = await session.client.get_input_entity(int(peer_id))
  except ValueError as e:
    return str(e), 400

  return await retr_media(session, request, input_peer, msg, size)


async def retr_media(session, request, peer, msg, size=None):
  try:
    msg = int(msg)
    m = await session.client.get_messages(peer, ids=msg)
  except ValueError as e:
    return str(e), 400

  if not m:
    return f'Unable to fetch message {msg} from {peer}', 404

  if not m.media and not m.action:
    return f'Unable to fetch media from {m}', 400

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
    return f'Unknown media type {m.media}', 400

  if size is not None:
    size = int(size)
    return await m.download_media(file=bytes, thumb=size), {
     'Content-Type': mime_type,
     'Cache-Control': 'no-cache',
     'Content-Disposition': f'inline; filename={name}'}

  send_media = (
    i async for i in
    session.client.iter_download(
      file=source,
      request_size=32768))

  return send_media, {
    'Content-Type': mime_type,
    'Cache-Control': 'no-cache',
    'Content-Disposition': f'inline; filename={name}'}


async def retr_avatar(session, request, peer, icon=None):

  try:
    input_peer = await session.client.get_input_entity(peer)
  except ValueError as e:
    return str(e), 400

  return await session.client.download_profile_photo(input_peer, file=bytes), {
    'Content-Type': 'image/jpeg',
    'Cache-Control': 'no-cache',
    'Content-Disposition': f'inline; filename={peer}'}
