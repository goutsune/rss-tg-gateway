''' Misc message processing tools used for both RSS and IRC message renderers
'''
import re, string

FNAME_REGEX = re.compile(r'[^-a-zA-Z0-9.]+')
CHARS = string.ascii_letters
BASE = len(CHARS)

def get_filename(attributes):
  for attribute in attributes:
    if hasattr(attribute, 'file_name'):
      return attribute.file_name


def to_alpha(n):
    if n == 0: return CHARS[0]
    sign = "-" if n < 0 else ""
    n = abs(n)
    s = ""
    while n:
        s = CHARS[n % BASE] + s
        n //= BASE
    return sign + s

def from_alpha(s):
    sign = -1 if s[0] == "-" else 1
    if sign < 0: s = s[1:]
    n = 0
    for c in s:
        n = n * BASE + CHARS.index(c)
    return str(sign * n)  # I'm feeding this back into requests, so strings only

