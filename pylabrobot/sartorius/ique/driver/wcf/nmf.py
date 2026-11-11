"""The .NET Message Framing Protocol ([MC-NMF]): how SOAP messages travel over ``net.tcp``.

A connection opens with a preamble that names the protocol version, the mode, the address
the messages are for and their encoding, which the receiver acknowledges. After that each
message is an envelope record:

- In duplex mode both sides send any number of sized envelopes, each a size and that many
  bytes, until each sends an End record.
- In singleton unsized mode ("streamed") one message goes each way. A message is an
  unsized envelope: chunks, each a size and that many bytes, up to a zero size, and then
  an End record.

The functions here build the records and read them from any source of bytes, given as a
function that returns exactly the number of bytes asked for.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Awaitable, Callable, Dict, Iterator, List, Optional, Tuple

from pylabrobot.sartorius.ique.driver.wcf.errors import (
  FramingError,
  FramingFault,
  NbfxError,
  NbfxIncompleteError,
)
from pylabrobot.sartorius.ique.driver.wcf.nbfx import decode_int31, encode_int31


class FramingRecord(IntEnum):
  """Record type octets ([MC-NMF] 2.2.1)."""

  VERSION = 0x00
  MODE = 0x01
  VIA = 0x02
  KNOWN_ENCODING = 0x03
  EXTENSIBLE_ENCODING = 0x04
  UNSIZED_ENVELOPE = 0x05
  SIZED_ENVELOPE = 0x06
  END = 0x07
  FAULT = 0x08
  UPGRADE_REQUEST = 0x09
  UPGRADE_RESPONSE = 0x0A
  PREAMBLE_ACK = 0x0B
  PREAMBLE_END = 0x0C


class FramingMode(IntEnum):
  """Communication modes (2.2.3.2)."""

  SINGLETON_UNSIZED = 0x01
  DUPLEX = 0x02
  SIMPLEX = 0x03
  SINGLETON_SIZED = 0x04


class EnvelopeEncoding(IntEnum):
  """Known envelope encodings (2.2.3.4.1)."""

  SOAP11_UTF8 = 0x00
  SOAP11_UTF16 = 0x01
  SOAP11_UNICODE_LE = 0x02
  SOAP12_UTF8 = 0x03
  SOAP12_UTF16 = 0x04
  SOAP12_UNICODE_LE = 0x05
  SOAP12_MTOM = 0x06
  SOAP12_BINARY = 0x07
  SOAP12_BINARY_SESSION = 0x08


PROTOCOL_VERSION = (1, 0)
FAULT_NAMESPACE = "http://schemas.microsoft.com/ws/2006/05/framing/faults/"

# The fault names of section 2.2.5, each sent as FAULT_NAMESPACE followed by the name.
FAULTS: Dict[str, str] = {
  "ConnectionDispatchFailed": "the endpoint exists, but dispatching the message to it failed",
  "ContentTypeInvalid": "the endpoint does not support the envelope encoding",
  "ContentTypeTooLong": "the envelope encoding record exceeds the receiver's limit",
  "EndpointAccessDenied": "the endpoint cannot be accessed",
  "EndpointNotFound": "no endpoint listens at the address of the Via record",
  "EndpointPaused": "the endpoint is paused",
  "EndpointUnavailable": "the endpoint is unavailable",
  "InvalidRecordSequence": "the records do not follow the protocol's order",
  "MaxMessageSizeExceededFault": "the message exceeds the receiver's size limit",
  "ServerTooBusy": "the endpoint lacks the resources to process the connection",
  "ServiceActivationFailed": "the endpoint's process cannot be activated",
  "UnsupportedMode": "the receiver does not support the mode",
  "UnsupportedVersion": "the receiver does not support the protocol version",
  "UpgradeInvalid": "the receiver does not support the requested upgrade",
  "ViaTooLong": "the Via record exceeds the receiver's limit",
}

UNSIZED_ENVELOPE_START = bytes((FramingRecord.UNSIZED_ENVELOPE,))
CHUNKS_END = b"\x00"
END = bytes((FramingRecord.END,))

# The largest envelope or chunk a .NET peer sends: its sizes are signed 32-bit numbers.
MAX_SIZE = 2**31 - 1

ReadExact = Callable[[int], Awaitable[bytes]]


def encode_size(size: int) -> bytes:
  """Encode the size of a variable-sized record: seven bits per byte, lowest first (2.2.2)."""
  try:
    return encode_int31(size)
  except NbfxError as error:
    raise FramingError(f"record size {size} cannot be encoded") from error


def preamble(mode: FramingMode, via: str, encoding: EnvelopeEncoding) -> bytes:
  """Build what an initiator sends first: the preamble message and a Preamble End record.

  Args:
    mode: The communication mode.
    via: The URI the messages are bound for, such as ``net.tcp://host:9002/Controller``.
    encoding: The encoding of the envelopes that follow.
  """
  address = via.encode("utf-8")
  if not address:
    raise FramingError("the Via of a preamble cannot be empty")
  return (
    bytes((FramingRecord.VERSION, *PROTOCOL_VERSION, FramingRecord.MODE, mode, FramingRecord.VIA))
    + encode_size(len(address))
    + address
    + bytes((FramingRecord.KNOWN_ENCODING, encoding, FramingRecord.PREAMBLE_END))
  )


def sized_envelope(payload: bytes) -> bytes:
  """Build a Sized Envelope record (2.2.4.1)."""
  if not payload:
    raise FramingError("an envelope cannot be empty")
  return bytes((FramingRecord.SIZED_ENVELOPE,)) + encode_size(len(payload)) + payload


def chunk(data: bytes) -> bytes:
  """Build one data chunk of an unsized envelope (2.2.4.2)."""
  if not data:
    raise FramingError("a data chunk cannot be empty")
  return encode_size(len(data)) + data


def fault_record(fault: str) -> bytes:
  """Build a Fault record (2.2.5) for a fault URI."""
  data = fault.encode("utf-8")
  return bytes((FramingRecord.FAULT,)) + encode_size(len(data)) + data


async def read_size(read: ReadExact, limit: int = MAX_SIZE) -> int:
  """Read the size of a variable-sized record (3.1.4.1).

  Raises:
    FramingError: The size is malformed or above ``limit``.
  """
  size = 0
  for index in range(5):
    byte = (await read(1))[0]
    size |= (byte & 0x7F) << (7 * index)
    if not byte & 0x80:
      if size > limit:
        raise FramingError(f"record size {size} exceeds the limit of {limit}")
      return size
  raise FramingError("record size is longer than five bytes")


async def _read_fault(read: ReadExact) -> FramingFault:
  """Read the rest of a Fault record and return it as an error."""
  data = await read(await read_size(read, limit=65536))
  return FramingFault(data.decode("utf-8", errors="replace"))


async def _unexpected(read: ReadExact, record: int, expected: str) -> FramingError:
  """Return the error for a record that is not the expected one."""
  if record == FramingRecord.FAULT:
    return await _read_fault(read)
  return FramingError(f"expected {expected}, received framing record {record:#04x}")


async def read_preamble_ack(read: ReadExact) -> None:
  """Read the receiver's answer to a preamble.

  Raises:
    FramingFault: The receiver refused the connection, for example because no endpoint
      listens at the Via address.
    FramingError: The receiver sent anything else than a Preamble Ack record.
  """
  record = (await read(1))[0]
  if record != FramingRecord.PREAMBLE_ACK:
    raise await _unexpected(read, record, "a Preamble Ack record")


async def read_duplex_record(read: ReadExact, limit: int = MAX_SIZE) -> Optional[bytes]:
  """Read the next record of a duplex session.

  Returns:
    The payload of a Sized Envelope record, or None for the End record.

  Raises:
    FramingFault: The peer sent a Fault record.
    FramingError: The peer sent another record, or an envelope above ``limit`` bytes.
  """
  record = (await read(1))[0]
  if record == FramingRecord.END:
    return None
  if record != FramingRecord.SIZED_ENVELOPE:
    raise await _unexpected(read, record, "a Sized Envelope or End record")
  size = await read_size(read, limit)
  if size == 0:
    raise FramingError("received an empty envelope")
  return await read(size)


async def read_unsized_start(read: ReadExact) -> None:
  """Read the start of an Unsized Envelope record.

  Raises:
    FramingFault: The peer sent a Fault record.
    FramingError: The peer sent another record.
  """
  record = (await read(1))[0]
  if record != FramingRecord.UNSIZED_ENVELOPE:
    raise await _unexpected(read, record, "an Unsized Envelope record")


async def read_chunk(read: ReadExact, limit: int = MAX_SIZE) -> bytes:
  """Read the next data chunk of an unsized envelope; empty bytes mark its end."""
  size = await read_size(read, limit)
  return await read(size) if size else b""


async def read_end(read: ReadExact) -> None:
  """Read the End record that follows a streamed message.

  Raises:
    FramingFault: The peer sent a Fault record.
    FramingError: The peer sent another record.
  """
  record = (await read(1))[0]
  if record != FramingRecord.END:
    raise await _unexpected(read, record, "an End record")


@dataclass(frozen=True)
class Record:
  """One framing record of a recorded byte stream.

  Attributes:
    type: The record type.
    payload: The record's data. For an unsized envelope, its chunks joined.
    chunks: The sizes of the chunks of an unsized envelope.
  """

  type: FramingRecord
  payload: bytes = b""
  chunks: Tuple[int, ...] = ()


_FIXED_SIZES = {FramingRecord.VERSION: 2, FramingRecord.MODE: 1, FramingRecord.KNOWN_ENCODING: 1}
_SIZED = (
  FramingRecord.VIA,
  FramingRecord.EXTENSIBLE_ENCODING,
  FramingRecord.SIZED_ENVELOPE,
  FramingRecord.FAULT,
  FramingRecord.UPGRADE_REQUEST,
)


def _take(data: bytes, position: int, size: int) -> bytes:
  """Return ``size`` bytes of recorded data at ``position``.

  Raises:
    FramingError: The data ends before them.
  """
  if position + size > len(data):
    raise FramingError("recorded data ends inside a framing record")
  return data[position : position + size]


def split_records(data: bytes) -> Iterator[Record]:
  """Split everything one side sent over a connection into its framing records.

  Raises:
    FramingError: The data is not a whole number of well-formed records. The records
      before the malformed one are given first; that one and those after it are not.
  """
  position = 0
  try:
    while position < len(data):
      try:
        kind = FramingRecord(data[position])
      except ValueError as error:
        raise FramingError(f"unknown framing record {data[position]:#04x}") from error
      position += 1
      if kind in _SIZED:
        size, position = decode_int31(data, position)
        payload = _take(data, position, size)
        position += size
        yield Record(kind, payload)
      elif kind == FramingRecord.UNSIZED_ENVELOPE:
        pieces: List[bytes] = []
        while True:
          size, position = decode_int31(data, position)
          if size == 0:
            break
          pieces.append(_take(data, position, size))
          position += size
        yield Record(kind, b"".join(pieces), tuple(len(piece) for piece in pieces))
      else:
        size = _FIXED_SIZES.get(kind, 0)
        payload = _take(data, position, size)
        position += size
        yield Record(kind, payload)
  except NbfxIncompleteError as error:
    raise FramingError("recorded data ends inside a record size") from error
