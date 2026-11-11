"""Exchanges recorded between the driver and .NET WCF stand-ins for the controller and watchdog.

Each JSON file in this folder holds the bytes that passed a TCP relay between a .NET client and
.NET Framework WCF services declaring the controller's full contract, one entry per connection
(the sizes of the pieces each side sent, and all bytes zlib-compressed in base64), and the list
of calls made on them: for each call what the service received and what it answered, written as
canonical text (:func:`render`), and the callbacks it sent before answering.

The canonical text of a value depends only on its wire meaning, so the .NET side and Python write
the same text for the same value:

==============  ===============================================================================
nil             ``null``
bool, int       ``true``, ``false``, ``42``
float           ``f:`` and the 8 hex digits of its IEEE single-precision bits; ``f:nan`` for NaN
decimal         ``d:`` and the digits with the scale kept (``d:12.50``)
string          in double quotes; backslash, quote and every character outside printable ASCII
                escaped as ``\\uXXXX`` (UTF-16 code units)
DateTime        ``dt:<ticks>:<kind>``, the ticks of the UTC instant for kind Local
TimeSpan, Guid  ``ts:<ticks>``, ``g:<guid>``
enum            ``<Enum>.<value>``
Version         ``v:<major>.<minor>.<build>.<revision>``
list, dict      ``[a,b]``, ``{key:value}``
data object     ``<Contract>{<Member>=<value>,...}``, members in wire order
==============  ===============================================================================
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import pathlib
import struct
import uuid
import zlib
from typing import Any, Dict, List

from pylabrobot.sartorius.ique.driver.wcf import (
  BOOL,
  DATETIME,
  DECIMAL,
  FLOAT,
  GUID,
  INT,
  STRING,
  TIMESPAN,
  UINT,
  VERSION,
  DataContract,
  DataObject,
  DateTime,
  DictionaryOf,
  EnumType,
  ListOf,
  Nullable,
  TimeSpan,
  XmlType,
)
from pylabrobot.sartorius.ique.driver.wcf.recordings import Connection

FOLDER = pathlib.Path(__file__).parent

_KINDS = {"utc": "Utc", "local": "Local", "unspecified": "Unspecified"}


def _string(text: str) -> str:
  """Return the canonical text of a string."""
  units = struct.unpack(f"<{len(text.encode('utf-16-le')) // 2}H", text.encode("utf-16-le"))
  out = []
  for unit in units:
    if unit in (0x22, 0x5C):
      out.append("\\" + chr(unit))
    elif 0x20 <= unit <= 0x7E:
      out.append(chr(unit))
    else:
      out.append(f"\\u{unit:04x}")
  return '"' + "".join(out) + '"'


def _float(value: float) -> str:
  """Return the canonical text of a single-precision float."""
  if math.isnan(value):
    return "f:nan"
  (bits,) = struct.unpack("<I", struct.pack("<f", value))
  return f"f:{bits:08x}"


def render(xml_type: XmlType, value: Any) -> str:
  """Return the canonical text of a value as the wcf package reads and writes it.

  Args:
    xml_type: The declared wire type of the value.
    value: The value: a data object for a class, a tuple for a version, None for nil.

  Raises:
    TypeError: The type is not one the controller's contract uses.
  """
  if value is None:
    return "null"
  if isinstance(xml_type, Nullable):
    return render(xml_type.inner, value)
  if isinstance(xml_type, EnumType):
    return f"{xml_type.name}.{value}"
  if xml_type is VERSION:
    parts = tuple(value) + (-1,) * (4 - len(value))
    return "v:" + ".".join(str(part) for part in parts)
  if isinstance(xml_type, DataContract):
    if not isinstance(value, DataObject):
      raise TypeError(f"expected a data object of {xml_type.name}, got {type(value).__name__}")
    members = value.contract.members()
    texts = [f"{name}={render(kind, value.values[name])}" for name, kind, _ in members]
    return f"{value.contract.name}{{{','.join(texts)}}}"
  if isinstance(xml_type, DictionaryOf):
    entries = [f"{render(xml_type.key, k)}:{render(xml_type.value, v)}" for k, v in value.items()]
    return "{" + ",".join(entries) + "}"
  if isinstance(xml_type, ListOf):
    return "[" + ",".join(render(xml_type.item, item) for item in value) + "]"
  if xml_type is BOOL:
    return "true" if value else "false"
  if xml_type is INT or xml_type is UINT:
    return str(value)
  if xml_type is FLOAT:
    return _float(value)
  if xml_type is DECIMAL:
    return "d:" + format(value, "f")
  if xml_type is STRING:
    return _string(value)
  if xml_type is DATETIME:
    exact = DateTime.from_datetime(value)
    return f"dt:{exact.ticks}:{_KINDS[exact.kind]}"
  if xml_type is TIMESPAN:
    return f"ts:{TimeSpan.from_timedelta(value).ticks}"
  if xml_type is GUID:
    return f"g:{uuid.UUID(int=value.int)}"
  raise TypeError(f"no canonical text for {xml_type.name}")


def pattern(length: int, seed: int) -> bytes:
  """Return the bytes of the stand-in services' recorded streams: a fixed pattern per seed."""
  return bytes((i * 31 + (i >> 8) * 7 + (i >> 16) * 3 + seed) & 0xFF for i in range(length))


def describe_bytes(data: bytes) -> str:
  """Return the canonical text of a stream: its length and the start of its SHA-256."""
  return f"bytes:{len(data)}:{hashlib.sha256(data).hexdigest()[:16]}"


def _connection(item: Dict[str, Any]) -> Connection:
  """Rebuild a recorded connection from its stored form."""
  data = zlib.decompress(base64.b64decode(item["data"]))
  events = []
  offset = 0
  for size in item["sizes"].split():
    side, length = size[0], int(size[1:])
    events.append((side, data[offset : offset + length]))
    offset += length
  if offset != len(data):
    raise ValueError(f"{item['address']}: the sizes cover {offset} of {len(data)} bytes")
  return Connection(item["address"], tuple(events))


def load(name: str) -> Dict[str, Any]:
  """Load the recording stored as ``<name>.json`` in this folder.

  Returns:
    The recording: ``description``, ``connections`` as replayable :class:`Connection` objects
    and ``calls`` as stored.
  """
  with open(FOLDER / f"{name}.json", encoding="utf-8") as file:
    content: Dict[str, Any] = json.load(file)
  connections: List[Connection] = [_connection(item) for item in content["connections"]]
  content["connections"] = connections
  return content
