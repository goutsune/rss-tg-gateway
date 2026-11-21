SERVER = 'telegram'


def peer_hostmask(peer):
  return f'{peer.alias}!{peer.id}@{SERVER}'


def render_lines(message):
  return message.raw_text.split('\n')
