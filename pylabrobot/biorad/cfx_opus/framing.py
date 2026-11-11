"""Message framing of the Bio-Rad CFX command link.

Every message in either direction is a 5-byte header followed by its payload:

====  =========================================================================
Byte  Contents
====  =========================================================================
0     ``handle << 2 | channel``
1     ``newLine << 6 | charTimeout << 3 | msgTimeout``
2     ``ascii << 3 | textPayload << 2 | passThrough << 1 | dummyAdded``
3-4   payload length, big-endian ``uint16``
====  =========================================================================

Only the first USB packet of a message carries the header; the payload continues in the packets
that follow, so a receiver accumulates bytes until it has the announced length. Channel 1 carries
the ASCII command language. The instrument sets ``passThrough`` when the payload is raw bytes
rather than text.
"""

from dataclasses import dataclass
from typing import List, Optional

HEADER_LENGTH = 5

#: The largest payload one message carries. A longer reply arrives as several messages.
MAX_PAYLOAD_LENGTH = 65530

COMMAND_CHANNEL = 1

_NEW_LINE = 1
_CHAR_TIMEOUT = 7
_MESSAGE_TIMEOUT = 7
_ASCII = 1 << 3
_TEXT_PAYLOAD = 1 << 2
_PASS_THROUGH = 1 << 1


class CFXProtocolError(Exception):
  """The instrument sent bytes that do not fit the command link's framing or reply shape."""


class CFXCommandError(Exception):
  """The instrument answered a command with a non-zero result code."""

  def __init__(self, command: str, code: str, value: Optional[str]):
    super().__init__(
      f"'{command}' failed with result code {code}" + (f": {value}" if value else "")
    )
    self.command = command
    self.code = code
    self.value = value


@dataclass(frozen=True)
class Message:
  """One complete message read from the instrument."""

  channel: int
  handle: int
  pass_through: bool
  payload: bytes


def encode_command(command: str, channel: int = COMMAND_CHANNEL) -> bytes:
  """Frame one command of the ASCII command language for sending.

  Args:
    command: The command text without its line ending, e.g. ``"*IDN?"``.
    channel: The logical channel. Commands go on channel 1.

  Returns:
    The header followed by the command and ``\\r\\n``.
  """

  if "\r" in command or "\n" in command:
    raise ValueError(f"A command must be a single line: {command!r}")
  payload = (command + "\r\n").encode("ascii")
  if len(payload) > MAX_PAYLOAD_LENGTH:
    raise ValueError(f"Command is {len(payload)} bytes, more than one message holds")
  return (
    bytes(
      [
        channel & 0x03,
        (_NEW_LINE << 6) | (_CHAR_TIMEOUT << 3) | _MESSAGE_TIMEOUT,
        _ASCII | _TEXT_PAYLOAD,
      ]
    )
    + len(payload).to_bytes(2, "big")
    + payload
  )


class MessageReader:
  """Reassembles messages from the bytes read off the instrument's IN endpoint.

  USB reads can split a message or, in principle, end partway into the next one, so bytes are
  buffered across calls to :meth:`feed`.
  """

  def __init__(self) -> None:
    self._buffer = bytearray()

  def feed(self, data: bytes) -> List[Message]:
    """Add bytes from one read and return every message they complete, in arrival order."""

    self._buffer += data
    messages: List[Message] = []
    while len(self._buffer) >= HEADER_LENGTH:
      length = int.from_bytes(self._buffer[3:5], "big")
      end = HEADER_LENGTH + length
      if len(self._buffer) < end:
        break
      first = self._buffer[0]
      messages.append(
        Message(
          channel=first & 0x03,
          handle=first >> 2,
          pass_through=bool(self._buffer[2] & _PASS_THROUGH),
          payload=bytes(self._buffer[HEADER_LENGTH:end]),
        )
      )
      del self._buffer[:end]
    return messages

  @property
  def pending(self) -> int:
    """Bytes received that do not yet make up a whole message."""

    return len(self._buffer)

  def clear(self) -> None:
    """Drop any partial message."""

    self._buffer.clear()


@dataclass(frozen=True)
class Reply:
  """A text reply: the value it reports, if it has a value slot, and its result code."""

  value: Optional[str]
  code: str


def parse_reply(payload: bytes) -> Reply:
  """Split a text reply into its value and its 4-digit result code.

  Commands that report something answer ``<value>;<code>``, with an empty value still followed by
  the semicolon. Pure actions answer the bare ``<code>``.
  """

  try:
    text = payload.decode("ascii")
  except UnicodeDecodeError as error:
    raise CFXProtocolError(f"Reply is not ASCII text: {payload!r}") from error
  text = text.rstrip("\r\n\x00")
  value, separator, code = text.rpartition(";")
  if len(code) != 4 or not code.isdigit():
    raise CFXProtocolError(f"Reply has no 4-digit result code: {text!r}")
  return Reply(value=value if separator else None, code=code)
