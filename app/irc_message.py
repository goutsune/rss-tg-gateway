''' Minimal IRC message codec, strings in and out, no framing
'''
import re

LINE_BREAKING_CHARS = re.compile(r'[\r\n\0]')


def decode(line):
  prefix = None
  if line.startswith(':'):
    prefix, _, line = line[1:].partition(' ')
  middle, sep, trailing = line.partition(' :')
  params = middle.split()
  if sep:
    params.append(trailing)
  command = params.pop(0).upper() if params else ''
  return prefix, command, params


def encode(command, *params, prefix=None, tags=None):
  params = [LINE_BREAKING_CHARS.sub('', str(p)) for p in params]
  if params and (not params[-1] or ' ' in params[-1] or params[-1].startswith(':')):
    params[-1] = ':' + params[-1]

  parts = []
  if tags:
    parts.append('@' + ';'.join(f'{k}={v}' for k, v in tags.items()))
  if prefix:
    parts.append(':' + LINE_BREAKING_CHARS.sub('', prefix))
  parts.append(command)
  return ' '.join(parts + params)
