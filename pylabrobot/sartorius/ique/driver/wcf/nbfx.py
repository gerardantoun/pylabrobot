"""Reader and writer for the .NET Binary Format: XML Data Structure ([MC-NBFX]).

A document is a sequence of records: element, attribute and text records, plus comments and
arrays. :class:`Reader` turns bytes into the nodes of ``tree`` and :class:`Writer` does the
reverse.

The format refers to some strings by number ([MC-NBFX] 2.1.4) and leaves the numbering to
the layer above, so both classes take a function for it: see ``dictionary`` for the table
of [MC-NBFS] and the per-connection table of [MC-NBFSE].

A tree read from bytes is written back to the same bytes: every ``Text`` remembers the
record it came from. A tree built in code leaves ``Text.record`` unset, and the writer then
produces the bytes a .NET Framework writer (``XmlDictionaryWriter``) produces for the same
content: the same record for each value, and the same choice between merging a text record
with the end of its element (2.2.3.31) and writing that end as a record of its own.
"""

from __future__ import annotations

import array
import datetime
import decimal
import struct
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from pylabrobot.sartorius.ique.driver.wcf.errors import NbfxError, NbfxIncompleteError
from pylabrobot.sartorius.ique.driver.wcf.records import (
  ARRAY_ITEMS,
  BYTES,
  CHARS,
  COUNTED,
  FIRST_ATTRIBUTE,
  FIRST_ELEMENT,
  FIXED,
  LAST_ATTRIBUTE,
  LAST_ELEMENT,
  TEXT_RECORDS,
  UNICODE,
  RecordType,
)
from pylabrobot.sartorius.ique.driver.wcf.tree import (
  Array,
  Attribute,
  Comment,
  Element,
  Node,
  Text,
  Xmlns,
  declared,
  resolve,
)
from pylabrobot.sartorius.ique.driver.wcf.values import (
  DictionaryString,
  QName,
  Single,
  TextValue,
  TimeSpan,
  UniqueId,
  datetime_from_binary,
  datetime_to_binary,
  decimal_from_binary,
  decimal_to_binary,
  to_single,
)

_R = RecordType


class EndElement:
  """The reader event that closes the innermost open element."""


END_ELEMENT = EndElement()
Event = Union[Element, Text, Comment, Array, EndElement]
DictionaryLookup = Callable[[int], DictionaryString]
DictionaryKey = Callable[[DictionaryString], Optional[int]]


def encode_int31(value: int) -> bytes:
  """Encode an integer as a MultiByteInt31: seven bits per byte, lowest first (2.1.2)."""
  if not 0 <= value < 2**31:
    raise NbfxError(f"{value} does not fit a MultiByteInt31")
  out = bytearray()
  while value >= 0x80:
    out.append((value & 0x7F) | 0x80)
    value >>= 7
  out.append(value)
  return bytes(out)


def decode_int31(data: Union[bytes, bytearray], position: int) -> Tuple[int, int]:
  """Decode a MultiByteInt31 at ``position``; return the value and the position after it.

  Raises:
    NbfxIncompleteError: The data ends inside the integer.
    NbfxError: The integer takes more than 31 bits.
  """
  value = 0
  for index in range(5):
    if position + index >= len(data):
      raise NbfxIncompleteError("data ends inside a MultiByteInt31")
    byte = data[position + index]
    value |= (byte & 0x7F) << (7 * index)
    if not byte & 0x80:
      if value >= 2**31:
        raise NbfxError("MultiByteInt31 exceeds 31 bits")
      return value, position + index + 1
  raise NbfxError("MultiByteInt31 is longer than five bytes")


def _is_letter(prefix: str) -> bool:
  """Tell whether a prefix is one lowercase letter, which has its own record types."""
  return len(prefix) == 1 and "a" <= prefix <= "z"


def _swap(values: "array.array[Any]") -> "array.array[Any]":
  """Convert an array between little-endian wire order and this machine's order."""
  if sys.byteorder == "big":
    values.byteswap()
  return values


class Reader:
  """Incremental [MC-NBFX] reader.

  Feed it bytes with :meth:`feed`, then call :meth:`read` for one event at a time or
  :meth:`read_element` for a whole element. When the bytes fed so far end inside a record,
  both raise :class:`NbfxIncompleteError` and leave the reader where it was, so the call can
  be repeated after feeding more.
  """

  def __init__(self, lookup: Optional[DictionaryLookup] = None) -> None:
    """Create a reader.

    Args:
      lookup: Returns the string for a dictionary key. Without it a document that refers
        to a dictionary string is rejected.
    """
    self._lookup = lookup
    self._buffer = bytearray()
    self._position = 0
    self._scopes: List[Dict[str, str]] = []
    self._pending_end = False

  def feed(self, data: bytes) -> None:
    """Append bytes to read."""
    del self._buffer[: self._position]
    self._position = 0
    self._buffer += data

  @property
  def depth(self) -> int:
    """The number of elements that are open."""
    return len(self._scopes)

  @property
  def buffered(self) -> int:
    """The number of bytes fed and not read yet."""
    return len(self._buffer) - self._position

  def _take(self, count: int) -> bytes:
    """Consume ``count`` bytes."""
    end = self._position + count
    if end > len(self._buffer):
      raise NbfxIncompleteError(f"data ends {end - len(self._buffer)} bytes short of a record")
    data = bytes(self._buffer[self._position : end])
    self._position = end
    return data

  def _byte(self) -> int:
    """Consume one byte."""
    if self._position >= len(self._buffer):
      raise NbfxIncompleteError("data ends where a record is expected")
    self._position += 1
    return self._buffer[self._position - 1]

  def _int31(self) -> int:
    """Consume a MultiByteInt31."""
    value, self._position = decode_int31(self._buffer, self._position)
    return value

  def _decode(self, data: bytes, encoding: str) -> str:
    """Decode characters, reporting malformed ones as a format error."""
    try:
      return data.decode(encoding)
    except UnicodeDecodeError as error:
      raise NbfxError(f"malformed {encoding} characters: {error}") from error

  def _string(self) -> str:
    """Consume a String: a MultiByteInt31 length and that many UTF-8 bytes (2.1.3)."""
    return self._decode(self._take(self._int31()), "utf-8")

  def _name(self) -> str:
    """Consume a String that names something and so must not be empty."""
    name = self._string()
    if not name:
      raise NbfxError("empty element, attribute or prefix name")
    return name

  def _dictionary_string(self) -> DictionaryString:
    """Consume a DictionaryString: a MultiByteInt31 key (2.1.4)."""
    key = self._int31()
    if self._lookup is None:
      raise NbfxError(f"dictionary string {key} used, but no dictionary is in use")
    return self._lookup(key)

  def _unpack(self, fmt: str) -> Any:
    """Consume one fixed-size little-endian value."""
    return struct.unpack(fmt, self._take(struct.calcsize(fmt)))[0]

  def _counted(self, fmt: str) -> bytes:
    """Consume a length of the given width and that many bytes."""
    length = self._unpack(fmt)
    if length < 0:
      raise NbfxError(f"negative length {length} in a text record")
    return self._take(length)

  def _list(self) -> Tuple[Any, ...]:
    """Consume the text records of a list up to its EndListText record (2.2.3.19)."""
    items: List[Any] = []
    while True:
      record = self._byte()
      if record == _R.END_LIST_TEXT:
        return tuple(items)
      if record not in TEXT_RECORDS or record & 1 or record == _R.START_LIST_TEXT:
        raise NbfxError(f"record {record:#04x} is not allowed inside a list")
      items.append(self._text(record))

  def _structured(self, record: int) -> TextValue:
    """Consume the payload of a text record whose bytes have inner structure."""
    if record == _R.DECIMAL_TEXT:
      return decimal_from_binary(self._take(16))
    if record == _R.DATETIME_TEXT:
      return datetime_from_binary(self._unpack("<Q"))
    if record == _R.TIMESPAN_TEXT:
      return TimeSpan.from_ticks(self._unpack("<q"))
    if record == _R.UNIQUE_ID_TEXT:
      return UniqueId(bytes_le=self._take(16))
    if record == _R.UUID_TEXT:
      return uuid.UUID(bytes_le=self._take(16))
    raise NbfxError(f"record {record:#04x} is not a text record")

  def _text(self, record: int) -> TextValue:
    """Consume the payload of a text record given its type without the end-element bit."""
    if record in (_R.ZERO_TEXT, _R.ONE_TEXT):
      return int(record == _R.ONE_TEXT)
    if record in (_R.FALSE_TEXT, _R.TRUE_TEXT):
      return record == _R.TRUE_TEXT
    if record in FIXED:
      number: Union[int, float] = self._unpack(FIXED[record])
      return Single(number) if record == _R.FLOAT_TEXT else number
    if record == _R.EMPTY_TEXT:
      return ""
    if record in COUNTED:
      data = self._counted(COUNTED[record])
      if record <= _R.CHARS32_TEXT:
        return self._decode(data, "utf-8")
      return data if record <= _R.BYTES32_TEXT else self._decode(data, "utf-16-le")
    if record == _R.DICTIONARY_TEXT:
      return self._dictionary_string()
    if record == _R.START_LIST_TEXT:
      return self._list()
    if record == _R.BOOL_TEXT:
      flag = self._byte()
      if flag > 1:
        raise NbfxError(f"BoolText value {flag} is neither 0 nor 1")
      return bool(flag)
    if record == _R.QNAME_DICTIONARY_TEXT:
      letter = self._byte()
      if letter > 25:
        raise NbfxError(f"QNameDictionaryText prefix {letter} is not a letter")
      return QName(chr(ord("a") + letter), self._dictionary_string())
    try:
      return self._structured(record)
    except NbfxError:
      raise
    except ValueError as error:
      raise NbfxError(str(error)) from error

  def _named(self, record: int, first: int) -> Tuple[str, str]:
    """Consume the prefix and name of an element or attribute record.

    Args:
      record: The record type.
      first: The first code of the family, ShortElement or ShortAttribute. It is followed
        by the prefixed, ShortDictionary and Dictionary forms; the letter forms are apart.
    """
    form = record - first
    if 0 <= form <= 3:
      prefix = self._name() if form & 1 else ""
      return prefix, (self._dictionary_string() if form & 2 else self._name())
    letters = (
      (_R.PREFIX_DICTIONARY_ELEMENT_A, _R.PREFIX_ELEMENT_A)
      if first == _R.SHORT_ELEMENT
      else (_R.PREFIX_DICTIONARY_ATTRIBUTE_A, _R.PREFIX_ATTRIBUTE_A)
    )
    if letters[0] <= record < letters[0] + 26:
      return chr(ord("a") + record - letters[0]), self._dictionary_string()
    if letters[1] <= record < letters[1] + 26:
      return chr(ord("a") + record - letters[1]), self._name()
    raise NbfxError(f"record {record:#04x} is not an element or attribute record")

  def _attributes(self) -> List[Union[Attribute, Xmlns]]:
    """Consume the attribute records that follow an element record."""
    items: List[Union[Attribute, Xmlns]] = []
    while True:
      if self._position >= len(self._buffer):
        raise NbfxIncompleteError("data ends after an element or attribute record")
      record = self._buffer[self._position]
      if not FIRST_ATTRIBUTE <= record <= LAST_ATTRIBUTE:
        return items
      self._position += 1
      if record == _R.SHORT_XMLNS_ATTRIBUTE:
        items.append(Xmlns("", self._string()))
      elif record == _R.XMLNS_ATTRIBUTE:
        items.append(Xmlns(self._name(), self._string()))
      elif record == _R.SHORT_DICTIONARY_XMLNS_ATTRIBUTE:
        items.append(Xmlns("", self._dictionary_string()))
      elif record == _R.DICTIONARY_XMLNS_ATTRIBUTE:
        items.append(Xmlns(self._name(), self._dictionary_string()))
      else:
        prefix, name = self._named(record, _R.SHORT_ATTRIBUTE)
        value = self._byte()
        if value not in TEXT_RECORDS or value & 1:
          raise NbfxError(f"record {value:#04x} cannot be an attribute value")
        items.append(Attribute(name, Text(self._text(value), RecordType(value)), prefix))

  def _element(self, record: int) -> Tuple[Element, Dict[str, str]]:
    """Consume an element record with its attributes; return it and its namespace scope."""
    prefix, name = self._named(record, _R.SHORT_ELEMENT)
    element = Element(name, "", prefix, self._attributes())
    scope = self._scopes[-1] if self._scopes else {}
    if any(isinstance(item, Xmlns) for item in element.attributes):
      scope = declared(scope, element)
    element.namespace = resolve(scope, prefix, default=True)
    for item in element.attributes:
      if isinstance(item, Attribute):
        item.namespace = resolve(scope, item.prefix, default=False)
    return element, scope

  def _item(self, record: RecordType, data: bytes) -> Any:
    """Decode one array item of a type ``array.array`` cannot hold."""
    if record == _R.BOOL_TEXT:
      if data[0] > 1:
        raise NbfxError(f"boolean array item {data[0]} is neither 0 nor 1")
      return bool(data[0])
    if record == _R.DECIMAL_TEXT:
      return decimal_from_binary(data)
    if record == _R.DATETIME_TEXT:
      return datetime_from_binary(struct.unpack("<Q", data)[0])
    if record == _R.TIMESPAN_TEXT:
      return TimeSpan.from_ticks(struct.unpack("<q", data)[0])
    return uuid.UUID(bytes_le=data)

  def _array(self) -> Array:
    """Consume the rest of an Array record (2.3.3)."""
    record = self._byte()
    if not FIRST_ELEMENT <= record <= LAST_ELEMENT:
      raise NbfxError(f"an Array record holds an element record, not {record:#04x}")
    element, _ = self._element(record)
    if self._byte() != _R.END_ELEMENT:
      raise NbfxError("the element of an Array record must end at once")
    code = self._byte()
    if not code & 1 or code - 1 not in ARRAY_ITEMS:
      raise NbfxError(f"record {code:#04x} cannot be an array item")
    item = RecordType(code - 1)
    count = self._int31()
    if count == 0:
      raise NbfxError("an Array record must have at least one item")
    size, typecode = ARRAY_ITEMS[item]
    data = self._take(count * size)
    if typecode is not None:
      return Array(element, item, _swap(array.array(typecode, data)))
    chunks = [data[start : start + size] for start in range(0, len(data), size)]
    try:
      return Array(element, item, [self._item(item, chunk) for chunk in chunks])
    except NbfxError:
      raise
    except ValueError as error:
      raise NbfxError(str(error)) from error

  def _close(self) -> EndElement:
    """Close the innermost open element."""
    if not self._scopes:
      raise NbfxError("end of element without an open element")
    self._scopes.pop()
    return END_ELEMENT

  def _record(self) -> Event:
    """Consume one record and return its event."""
    record = self._byte()
    if record == _R.END_ELEMENT:
      return self._close()
    if record == _R.COMMENT:
      return Comment(self._string())
    if record == _R.ARRAY:
      return self._array()
    if FIRST_ELEMENT <= record <= LAST_ELEMENT:
      element, scope = self._element(record)
      self._scopes.append(scope)
      return element
    if record in TEXT_RECORDS and record != _R.END_LIST_TEXT:
      text = Text(self._text(record & ~1), RecordType(record))
      if record & 1 and not self._scopes:
        raise NbfxError("text record ends an element, but none is open")
      self._pending_end = bool(record & 1)
      return text
    raise NbfxError(f"unexpected record type {record:#04x}")

  def read(self) -> Event:
    """Read the next event.

    Returns:
      An ``Element`` without children when an element starts, :data:`END_ELEMENT` when
      one ends, or a ``Text``, ``Comment`` or ``Array`` node.

    Raises:
      NbfxIncompleteError: The bytes fed so far end before the next event is complete.
      NbfxError: The data is malformed.
    """
    if self._pending_end:
      self._pending_end = False
      return self._close()
    start = self._position
    try:
      return self._record()
    except NbfxIncompleteError:
      self._position = start
      raise

  def read_element(self, start: Optional[Element] = None) -> Element:
    """Read one whole element with everything inside it.

    Args:
      start: The element whose start :meth:`read` already returned; its children are read
        into it. Without it, the next event must be the start of an element.

    Raises:
      NbfxIncompleteError: The bytes fed so far end inside the element. Nothing is consumed.
      NbfxError: The data is malformed.
    """
    checkpoint = (self._position, list(self._scopes), self._pending_end)
    try:
      if start is None:
        event = self.read()
        if not isinstance(event, Element):
          raise NbfxError(f"expected an element, found {type(event).__name__}")
        start = event
      stack: List[Tuple[Element, List[Node]]] = [(start, [])]
      while stack:
        event = self.read()
        if isinstance(event, EndElement):
          element, children = stack.pop()
          element.children = children
        else:
          stack[-1][1].append(event)
          if isinstance(event, Element):
            stack.append((event, []))
      return start
    except NbfxIncompleteError:
      self._position, self._scopes, self._pending_end = checkpoint
      raise


def decode(data: bytes, lookup: Optional[DictionaryLookup] = None) -> Element:
  """Decode a document that consists of one element.

  Raises:
    NbfxError: The data is malformed, incomplete, or continues after the element.
  """
  reader = Reader(lookup)
  reader.feed(data)
  element = reader.read_element()
  if reader.buffered:
    raise NbfxError(f"{reader.buffered} bytes follow the document element")
  return element


@dataclass(frozen=True)
class _Forms:
  """The record types of the six ways to write an element or attribute name (2.2.1, 2.2.2)."""

  short: int
  prefixed: int
  short_dictionary: int
  dictionary: int
  letter_dictionary: int
  letter: int


_ELEMENT_FORMS = _Forms(
  _R.SHORT_ELEMENT,
  _R.ELEMENT,
  _R.SHORT_DICTIONARY_ELEMENT,
  _R.DICTIONARY_ELEMENT,
  _R.PREFIX_DICTIONARY_ELEMENT_A,
  _R.PREFIX_ELEMENT_A,
)
_ATTRIBUTE_FORMS = _Forms(
  _R.SHORT_ATTRIBUTE,
  _R.ATTRIBUTE,
  _R.SHORT_DICTIONARY_ATTRIBUTE,
  _R.DICTIONARY_ATTRIBUTE,
  _R.PREFIX_DICTIONARY_ATTRIBUTE_A,
  _R.PREFIX_ATTRIBUTE_A,
)

_MAX_SINGLE = float.fromhex("0x1.fffffep+127")

# A .NET writer collects its output in a buffer of 512 bytes and reserves room before each
# write, by the worst-case size of what it is about to convert: three bytes for each UTF-16
# unit of a string. When the room is missing it first passes the buffer on; a text record
# passed on can no longer absorb the end of its element. Blocks of 512 bytes or more skip
# the buffer. The constants below are the limits of that scheme.
_BUFFER_SIZE = 512
_SHORT_NAME_UNITS = 42
_SHORT_TEXT_UNITS = 85
_UTF8_PIECE_UNITS = 170
_UTF16_PIECE_UNITS = 256


class Writer:
  """[MC-NBFX] writer.

  Call :meth:`write` for a whole node, or :meth:`start_element`, :meth:`text`,
  :meth:`base64` and :meth:`end_element` to produce a document piece by piece, and collect
  the bytes with :meth:`take` or :meth:`take_pieces`.
  """

  def __init__(self, key_of: Optional[DictionaryKey] = None) -> None:
    """Create a writer.

    Args:
      key_of: Returns the dictionary key to write for a ``DictionaryString``, or None
        to write its characters. Without it every string is written as characters.
    """
    self._key_of = key_of
    self._out = bytearray()
    self._breaks: List[int] = []
    self._fill = 0
    self._scopes: List[Dict[str, str]] = []
    self._mergeable: Optional[int] = None
    self._forced: Optional[int] = None
    self._carry = b""

  def _flush(self) -> None:
    """Note that a .NET writer passes its buffer on at this point."""
    if self._fill:
      self._breaks.append(len(self._out))
      self._fill = 0
    self._mergeable = None

  def _put(self, data: bytes, reserve: Optional[int] = None) -> None:
    """Append bytes through the buffer, passing it on first if ``reserve`` bytes do not fit.

    Args:
      data: The bytes to append.
      reserve: The room a .NET writer asks for before this write; the size of ``data``
        when not given.
    """
    if self._fill + (len(data) if reserve is None else reserve) > _BUFFER_SIZE:
      self._flush()
    self._out += data
    self._fill += len(data)

  def _block(self, data: bytes) -> None:
    """Append a block of bytes: through the buffer when small, past it when large."""
    if len(data) < _BUFFER_SIZE:
      self._put(data)
    else:
      self._flush()
      self._out += data
      self._breaks.append(len(self._out))

  def _node(self, record: int) -> None:
    """Append the type byte of a record that is not a text record."""
    self._put(bytes((record,)))
    self._mergeable = None

  def _text_node(self, record: int, payload: bytes = b"", reserve: Optional[int] = None) -> None:
    """Append the type byte of a text record and the bytes buffered together with it."""
    data = bytes((record,)) + payload
    if self._fill + (len(data) if reserve is None else reserve) > _BUFFER_SIZE:
      self._flush()
    self._mergeable = len(self._out)
    self._out += data
    self._fill += len(data)

  def _int31(self, value: int) -> None:
    """Append a MultiByteInt31."""
    self._put(encode_int31(value), reserve=5)

  def _pieces(self, utf16: bytes, as_utf8: bool) -> None:
    """Append characters in the pieces a .NET writer converts them in.

    Args:
      utf16: The characters as UTF-16.
      as_utf8: Write them as UTF-8 instead of UTF-16.
    """
    limit = _UTF8_PIECE_UNITS if as_utf8 else _UTF16_PIECE_UNITS
    position, total = 0, len(utf16) // 2
    while position < total:
      count = total - position
      if count > limit:
        # A piece does not end between the two halves of a surrogate pair.
        count = limit - 1 if utf16[2 * (position + limit) - 1] & 0xFC == 0xD8 else limit
      piece = utf16[2 * position : 2 * (position + count)]
      data = piece.decode("utf-16-le").encode("utf-8") if as_utf8 else piece
      self._put(data, reserve=count * (3 if as_utf8 else 2))
      position += count

  def _string(self, value: str) -> None:
    """Append a String (2.1.3)."""
    data = value.encode("utf-8")
    utf16 = value.encode("utf-16-le")
    if len(utf16) // 2 < _SHORT_NAME_UNITS:
      self._put(encode_int31(len(data)) + data, reserve=1 + 3 * (len(utf16) // 2))
    else:
      self._int31(len(data))
      self._pieces(utf16, as_utf8=True)

  def _key(self, value: str) -> Optional[int]:
    """Return the dictionary key to write for a string, or None to write its characters."""
    if self._key_of is None or not isinstance(value, DictionaryString):
      return None
    return self._key_of(value)

  def _named(self, forms: _Forms, prefix: str, name: str) -> None:
    """Append the record type, prefix and name of an element or attribute."""
    if not name:
      raise NbfxError("empty element or attribute name")
    key = self._key(name)
    if not prefix:
      self._node(forms.short if key is None else forms.short_dictionary)
    elif _is_letter(prefix):
      first = forms.letter if key is None else forms.letter_dictionary
      self._node(first + ord(prefix) - ord("a"))
    else:
      self._node(forms.prefixed if key is None else forms.dictionary)
      self._string(prefix)
    if key is None:
      self._string(name)
    else:
      self._int31(key)

  def _counted(self, widths: Sequence[Tuple[RecordType, str]], length: int) -> None:
    """Append the type and length of the narrowest counted record for ``length`` bytes."""
    record, fmt = widths[0 if length < 2**8 else (1 if length < 2**16 else 2)]
    self._text_node(record, struct.pack(fmt, length), reserve=5)

  def _characters(self, value: str) -> None:
    """Append a string as text."""
    key = self._key(value)
    if key is not None:
      self._text_node(_R.DICTIONARY_TEXT)
      self._int31(key)
      return
    if value in ("", "0", "1"):
      self._text_node({"": _R.EMPTY_TEXT, "0": _R.ZERO_TEXT, "1": _R.ONE_TEXT}[value])
      return
    utf8 = value.encode("utf-8")
    utf16 = value.encode("utf-16-le")
    units = len(utf16) // 2
    # UTF-16 is used when it is the shorter of the two encodings.
    as_utf8 = len(utf8) // 2 <= units
    if units <= _SHORT_TEXT_UNITS:
      record = _R.CHARS8_TEXT if as_utf8 else _R.UNICODE_CHARS8_TEXT
      data = utf8 if as_utf8 else utf16
      self._text_node(record, bytes((len(data),)) + data, reserve=2 + 3 * units)
    else:
      self._counted(CHARS if as_utf8 else UNICODE, len(utf8) if as_utf8 else len(utf16))
      self._pieces(utf16, as_utf8)

  def _integer(self, value: int) -> None:
    """Append an integer in the narrowest record that holds it."""
    if value in (0, 1):
      self._text_node(_R.ONE_TEXT if value else _R.ZERO_TEXT)
      return
    for record in (_R.INT8_TEXT, _R.INT16_TEXT, _R.INT32_TEXT, _R.INT64_TEXT, _R.UINT64_TEXT):
      fmt = FIXED[record]
      bits = 8 * struct.calcsize(fmt)
      unsigned = record == _R.UINT64_TEXT
      low, high = (0, 2**bits) if unsigned else (-(2 ** (bits - 1)), 2 ** (bits - 1))
      if low <= value < high:
        self._text_node(record, struct.pack(fmt, value))
        return
    raise NbfxError(f"integer {value} does not fit 64 bits")

  def _float(self, value: float) -> None:
    """Append a float.

    A whole number that fits 64 bits becomes an integer record and any other
    single-precision value a FloatText record. A double that single precision cannot hold
    exactly, and a double that is infinite or not a number, becomes a DoubleText record.
    """
    single = isinstance(value, Single)
    if not single and not (abs(value) <= _MAX_SINGLE and to_single(value) == value):
      self._text_node(_R.DOUBLE_TEXT, struct.pack("<d", value))
    elif -(2.0**63) <= value < 2.0**63 and value == int(value):
      self._integer(int(value))
    else:
      self._text_node(_R.FLOAT_TEXT, struct.pack("<f", value))

  def _qname(self, value: QName) -> None:
    """Append a qualified name: as a QNameDictionaryText record when its parts allow it."""
    key = self._key(value.name)
    if key is None or not _is_letter(value.prefix):
      self._characters(str(value))
    else:
      self._text_node(_R.QNAME_DICTIONARY_TEXT, bytes((ord(value.prefix) - ord("a"),)))
      self._int31(key)

  def _binary(self, carried: bytes, data: bytes) -> None:
    """Append one Bytes record holding ``carried`` then ``data``."""
    self._counted(BYTES, len(carried) + len(data))
    if carried:
      self._put(carried)
    if data:
      self._block(data)

  def _structured(self, value: TextValue) -> Tuple[RecordType, bytes]:
    """Return the record and payload of a value whose type has a single record."""
    if isinstance(value, decimal.Decimal):
      return _R.DECIMAL_TEXT, decimal_to_binary(value)
    if isinstance(value, datetime.datetime):
      return _R.DATETIME_TEXT, struct.pack("<Q", datetime_to_binary(value))
    if isinstance(value, datetime.timedelta):
      return _R.TIMESPAN_TEXT, struct.pack("<q", TimeSpan.from_timedelta(value).ticks)
    if isinstance(value, UniqueId):
      return _R.UNIQUE_ID_TEXT, value.bytes_le
    if isinstance(value, uuid.UUID):
      return _R.UUID_TEXT, value.bytes_le
    raise NbfxError(f"{type(value).__name__} is not a text value")

  def _compact(self, value: TextValue) -> None:
    """Append a value as the record a .NET writer picks for its type."""
    if isinstance(value, str):
      self._characters(value)
    elif isinstance(value, bool):
      self._text_node(_R.TRUE_TEXT if value else _R.FALSE_TEXT)
    elif isinstance(value, int):
      self._integer(value)
    elif isinstance(value, float):
      self._float(value)
    elif isinstance(value, bytes):
      self._binary(b"", value)
    elif isinstance(value, QName):
      self._qname(value)
    elif isinstance(value, tuple):
      self._node(_R.START_LIST_TEXT)
      for item in value:
        self._compact(item)
      self._node(_R.END_LIST_TEXT)
    else:
      try:
        record, payload = self._structured(value)
      except NbfxError:
        raise
      except ValueError as error:
        raise NbfxError(str(error)) from error
      self._text_node(record, payload)

  def _typed(self, record: int, value: TextValue) -> None:
    """Append a value as one given record (without the end-element bit).

    Raises:
      NbfxError: The record cannot carry the value.
    """
    try:
      if record in (_R.ZERO_TEXT, _R.ONE_TEXT):
        accepted = (1, "1") if record == _R.ONE_TEXT else (0, "0")
        if isinstance(value, bool) or value not in accepted:
          raise ValueError
        self._text_node(record)
      elif record in (_R.FALSE_TEXT, _R.TRUE_TEXT):
        if value is not (record == _R.TRUE_TEXT):
          raise ValueError
        self._text_node(record)
      elif record == _R.EMPTY_TEXT:
        if value != "":
          raise ValueError
        self._text_node(record)
      elif record in FIXED:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
          raise ValueError
        self._text_node(record, struct.pack(FIXED[record], value))
      elif record in COUNTED and isinstance(value, (bytes, str)):
        binary = _R.BYTES8_TEXT <= record <= _R.BYTES32_TEXT
        if binary != isinstance(value, bytes):
          raise ValueError
        if isinstance(value, str):
          value = value.encode("utf-8" if record <= _R.CHARS32_TEXT else "utf-16-le")
        self._text_node(record, struct.pack(COUNTED[record], len(value)))
        self._block(value)
      elif record == _R.BOOL_TEXT and isinstance(value, bool):
        self._text_node(record, bytes((int(value),)))
      elif record == _R.DICTIONARY_TEXT and isinstance(value, str):
        key = self._key(value)
        if key is None:
          raise ValueError
        self._text_node(record)
        self._int31(key)
      elif record == _R.QNAME_DICTIONARY_TEXT and isinstance(value, QName):
        key = self._key(value.name)
        if key is None or not _is_letter(value.prefix):
          raise ValueError
        self._text_node(record, bytes((ord(value.prefix) - ord("a"),)))
        self._int31(key)
      elif record == _R.START_LIST_TEXT and isinstance(value, tuple):
        self._compact(value)
      else:
        expected, payload = self._structured(value)
        if expected != record:
          raise ValueError
        self._text_node(record, payload)
    except NbfxError:
      raise
    except (ValueError, struct.error, OverflowError) as error:
      raise NbfxError(f"record {record:#04x} cannot carry {value!r}") from error

  def _value(self, text: Text) -> None:
    """Append a text record, without the end-element bit."""
    if text.record is None:
      self._compact(text.value)
    elif text.record not in TEXT_RECORDS or text.record == _R.END_LIST_TEXT:
      raise NbfxError(f"record {text.record:#04x} is not a text record")
    else:
      self._typed(text.record & ~1, text.value)

  def _end_binary(self) -> None:
    """Append the bytes :meth:`base64` still holds back, as a record of their own."""
    if self._carry:
      carried, self._carry = self._carry, b""
      self._binary(carried, b"")

  def _before_node(self) -> None:
    """Check that another node may follow what was written last."""
    if self._forced is not None:
      raise NbfxError("a WithEndElement text record must be the last child of its element")
    self._end_binary()

  def start_element(self, element: Element) -> None:
    """Write the start of an element with its attributes, leaving it open for children."""
    self._before_node()
    scope = self._scopes[-1] if self._scopes else {}
    if any(isinstance(item, Xmlns) for item in element.attributes):
      scope = declared(scope, element)
    if resolve(scope, element.prefix, default=True) != element.namespace:
      raise NbfxError(
        f"prefix {element.prefix!r} of element {element.name!r} is not declared "
        f"for namespace {element.namespace!r}"
      )
    self._named(_ELEMENT_FORMS, element.prefix, element.name)
    for item in element.attributes:
      if isinstance(item, Xmlns):
        key = self._key(item.uri)
        dictionary = _R.SHORT_DICTIONARY_XMLNS_ATTRIBUTE - _R.SHORT_XMLNS_ATTRIBUTE
        record = _R.XMLNS_ATTRIBUTE if item.prefix else _R.SHORT_XMLNS_ATTRIBUTE
        self._node(record if key is None else record + dictionary)
        if item.prefix:
          self._string(item.prefix)
        if key is None:
          self._string(item.uri)
        else:
          self._int31(key)
      else:
        if resolve(scope, item.prefix, default=False) != item.namespace:
          raise NbfxError(
            f"prefix {item.prefix!r} of attribute {item.name!r} is not declared "
            f"for namespace {item.namespace!r}"
          )
        if item.value.record is not None and item.value.record & 1:
          raise NbfxError("an attribute value cannot be a WithEndElement record")
        self._named(_ATTRIBUTE_FORMS, item.prefix, item.name)
        self._value(item.value)
        self._mergeable = None
    self._scopes.append(scope)

  def end_element(self) -> None:
    """Write the end of the innermost open element."""
    if not self._scopes:
      raise NbfxError("end of element without an open element")
    self._end_binary()
    self._scopes.pop()
    merge = self._mergeable if self._forced is None else self._forced
    if merge is None:
      self._node(_R.END_ELEMENT)
    else:
      self._out[merge] += 1
    self._mergeable = self._forced = None

  def text(self, text: Text) -> None:
    """Write a text node.

    Bytes without a set record are written as :meth:`base64` writes them.
    """
    if text.record is None and isinstance(text.value, bytes):
      self.base64(text.value)
      return
    self._before_node()
    start = len(self._out)
    self._value(text)
    if text.record is not None:
      # The record says whether the text is merged with the end of its element.
      self._mergeable = None
      self._forced = start if text.record & 1 else None

  def base64(self, data: bytes) -> None:
    """Write binary content of the open element, as one of possibly many pieces.

    A record carries a multiple of three bytes, so that its characters are whole base64
    groups. Up to two bytes are held back for the next call; the end of the element, or
    the next node of another kind, writes them as a last record.
    """
    if self._forced is not None:
      raise NbfxError("a WithEndElement text record must be the last child of its element")
    carried = self._carry
    if carried:
      fill = 3 - len(carried)
      carried, data = carried + data[:fill], data[fill:]
    whole = len(carried) + len(data)
    whole -= whole % 3
    if whole < 3:
      self._carry = carried + data
      return
    split = whole - len(carried)
    self._binary(carried, data[:split])
    self._carry = data[split:]

  def comment(self, comment: Comment) -> None:
    """Write a comment."""
    self._before_node()
    self._node(_R.COMMENT)
    self._string(comment.text)

  def _items(self, record: RecordType, values: Sequence[Any]) -> bytes:
    """Encode the items of an array."""
    if record not in ARRAY_ITEMS:
      raise NbfxError(f"record {record:#04x} cannot be an array item")
    typecode = ARRAY_ITEMS[record][1]
    try:
      if typecode is not None:
        return _swap(array.array(typecode, values)).tobytes()
      if record == _R.BOOL_TEXT:
        return bytes(int(bool(value)) for value in values)
      if record == _R.DECIMAL_TEXT:
        return b"".join(decimal_to_binary(value) for value in values)
      if record == _R.DATETIME_TEXT:
        return b"".join(struct.pack("<Q", datetime_to_binary(value)) for value in values)
      if record == _R.TIMESPAN_TEXT:
        ticks = (TimeSpan.from_timedelta(value).ticks for value in values)
        return b"".join(struct.pack("<q", tick) for tick in ticks)
      return b"".join(value.bytes_le for value in values)
    except (TypeError, ValueError, OverflowError, struct.error) as error:
      raise NbfxError(f"array items do not fit record {record:#04x}: {error}") from error

  def array(self, node: Array) -> None:
    """Write an Array record (2.3.3)."""
    if node.element.children:
      raise NbfxError("the element of an array cannot have children")
    if len(node.values) == 0:
      raise NbfxError("an Array record must have at least one item")
    data = self._items(node.record, node.values)
    self._before_node()
    self._node(_R.ARRAY)
    self.start_element(node.element)
    self._scopes.pop()
    self._node(_R.END_ELEMENT)
    self._node(node.record + 1)
    self._int31(len(node.values))
    # Array data skips the buffer, in blocks of the buffer's size.
    self._flush()
    for start in range(0, len(data), _BUFFER_SIZE):
      self._out += data[start : start + _BUFFER_SIZE]
      self._breaks.append(len(self._out))

  def write(self, node: Node) -> None:
    """Write a node with everything inside it."""
    if isinstance(node, Text):
      self.text(node)
    elif isinstance(node, Comment):
      self.comment(node)
    elif isinstance(node, Array):
      self.array(node)
    else:
      self.start_element(node)
      for child in node.children:
        self.write(child)
      self.end_element()

  def take_flushed(self) -> List[bytes]:
    """Return the pieces a .NET writer has handed to its transport so far.

    A .NET writer hands its output on piece by piece: whenever its buffer fills, and for
    every large block. A streamed transport frames each piece on its own, so sending
    these pieces as they come reproduces its framing. What the writer would still hold in
    its buffer stays here, and a text record in it can still absorb the end of its
    element.
    """
    if not self._breaks:
      return []
    end = self._breaks[-1]
    if self._forced is not None and self._forced < end:
      raise NbfxError("a WithEndElement text record must be followed by the end of its element")
    pieces = [bytes(self._out[a:b]) for a, b in zip([0] + self._breaks, self._breaks)]
    del self._out[:end]
    self._breaks.clear()
    self._mergeable = None if self._mergeable is None else self._mergeable - end
    self._forced = None if self._forced is None else self._forced - end
    return pieces

  def take_pieces(self) -> List[bytes]:
    """Return everything written since the last call, in pieces (see :meth:`take_flushed`).

    A text record returned here can no longer absorb the end of its element, which is
    then written as a record of its own.
    """
    if self._forced is not None:
      raise NbfxError("a WithEndElement text record must be followed by the end of its element")
    self._flush()
    return self.take_flushed()

  def take(self) -> bytes:
    """Return everything written since the last call."""
    return b"".join(self.take_pieces())


def encode(node: Node, key_of: Optional[DictionaryKey] = None) -> bytes:
  """Encode one node with everything inside it."""
  writer = Writer(key_of)
  writer.write(node)
  return writer.take()
