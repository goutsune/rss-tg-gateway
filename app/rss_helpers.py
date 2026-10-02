from html import escape

from telethon.tl.types import MessageService, \
                                MessageActionPinMessage, \
                                MessageActionChatEditPhoto, \
                                MessageActionChannelCreate, \
                                PageBlockParagraph, \
                                PageBlockPhoto, \
                                TextPlain, \
                                TextConcat, \
                                TextUrl, \
                                TextBold, \
                                TextItalic, \
                                TextUnderline, \
                                TextStrike, \
                                TextFixed, \
                                TextMarked, \
                                TextSubscript, \
                                TextSuperscript

import config

rich_tags = {
  TextBold: 'b',
  TextItalic: 'i',
  TextUnderline: 'u',
  TextStrike: 's',
  TextFixed: 'code',
  TextMarked: 'mark',
  TextSubscript: 'sub',
  TextSuperscript: 'sup',
}


def get_filename(attributes):

  for attribute in attributes:
    if hasattr(attribute, 'file_name'):
      return attribute.file_name


def render_rich_text(text, html=True):
  if type(text) == TextPlain:
    return escape(text.text)
  if type(text) == TextConcat:
    return ''.join(render_rich_text(t, html=html) for t in text.texts)
  # TextEmpty and other leaves without nested text
  if not hasattr(text, 'text'):
    return ''

  inner = render_rich_text(text.text, html=html)

  if html:
    if type(text) == TextUrl:
      return f'<a href="{escape(text.url)}">{inner}</a>'
    if type(text) in rich_tags:
      tag = rich_tags[type(text)]
      return f'<{tag}>{inner}</{tag}>'

  return inner


def render_rich_message(rich, media_url):
  text = ''
  for block in rich.blocks:
    if type(block) == PageBlockParagraph:
      text += '<p style="white-space: pre-line">'\
              f'{render_rich_text(block.text)}</p>'
    elif type(block) == PageBlockPhoto:
      text += f'<img src="{media_url}?file_id={block.photo_id}" />'
  return text


async def get_name_from_msg(session, message):

  # Post attibute makes this is a channel message, let's add more info
  if message.post:
    channel = await session.resolve_peer(message.peer_id)
    if message.post_author:
      return f'{channel} ({message.post_author})'
    else:
      return f'{channel}'

  # Otherwise it is a normal message from a chat
  elif message.from_id:
    return await session.resolve_peer(message.from_id)

  else:
    return 'FIXME (unknown message type)'


async def render_msg(session, peer_info, m):
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
  msg['author'] = await get_name_from_msg(session, m)
  msg['guid'] = f'{peer_info.id}/{m.id}'

  if private:
    msg['link'] = f'https://t.me/c/{peer_info.id}/{m.id}'
    media_base = f'{config.host}/media/i'
  else:
    msg['link'] = f'https://t.me/{peer_info.username}/{m.id}'
    media_base = f'{config.host}/media'

  # Actual post text
  if m.text:
    msg['text'] += f'<p style="white-space: pre-line">{m.text}</p>'

  # Blog-like message
  if m.rich_message:
    msg['text'] += render_rich_message(
      m.rich_message, f'{media_base}/{peer}/{m.id}')
    # Find first text block object in message, use that to render title
    for block in m.rich_message.blocks:
      if type(block) == PageBlockParagraph:
        msg['title'] = render_rich_text(block.text, html=False)
        if len(msg['title']) > 60:
          msg['title'] = msg['title'][0:60] + '…'
        break
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
    f'<p style="white-space: pre-line"><b>{m.web_preview.site_name}</b> {preview_author}<br/>' \
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
      name = get_filename(m.document.attributes)
      msg['text'] += f'<p><a href="{media_base}/{peer}/{m.id}">{name}</a><p>'

  # =================== Forwarded message
  # Forwarded message should blockquote all of above
  if m.fwd_from:
    # We have a peer to read from
    if m.fwd_from.from_id:
      # Try resolving that peer id and make a link
      name = await session.resolve_peer(m.fwd_from.from_id)
      if name != False:
        user = await session.client.get_entity(m.fwd_from.from_id)
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
          f'<a href="https://t.me/{peer_info.username}/{m.reply_to_msg_id}">a message</a>.</p>'
      else:
        msg['text'] += f'<p>{msg["author"]} pinned '\
          f'<a href="https://t.me/c/{peer_info.id}/{m.reply_to_msg_id}">a message</a>.</p>'
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
