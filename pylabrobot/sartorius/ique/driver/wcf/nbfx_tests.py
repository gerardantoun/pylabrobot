"""Tests for the .NET Binary XML reader and writer ([MC-NBFX]).

Most cases come from ``recordings/net48_nbfx_vectors.json``: documents written by the binary
writer of .NET Framework 4.8 (``XmlDictionaryWriter.CreateBinaryWriter``), each with the XML
text .NET's own reader gives for it. Documents too large or too many to store there are kept
as hashes; the tests build them again from the same content and compare the hashes, which
checks that the writer chooses the records and flush points a .NET writer chooses.
"""

import datetime
import decimal
import hashlib
import json
import math
import pathlib
import unittest
import uuid
import xml.etree.ElementTree as ElementTree
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from pylabrobot.sartorius.ique.driver.wcf import nbfx, values
from pylabrobot.sartorius.ique.driver.wcf.errors import NbfxError, NbfxIncompleteError
from pylabrobot.sartorius.ique.driver.wcf.records import RecordType
from pylabrobot.sartorius.ique.driver.wcf.tree import (
  Array,
  Attribute,
  Comment,
  Element,
  Node,
  Text,
  Xmlns,
  to_xml,
)
from pylabrobot.sartorius.ique.driver.wcf.values import (
  DateTime,
  DictionaryString,
  QName,
  Single,
  UniqueId,
)

_FIXTURE = pathlib.Path(__file__).parent / "recordings" / "net48_nbfx_vectors.json"
with open(_FIXTURE, encoding="utf-8") as _file:
  _VECTORS: Dict[str, Any] = json.load(_file)
_STATIC: List[str] = _VECTORS["static"]
_SESSION: List[str] = _VECTORS["session"]
_GUID = uuid.UUID("33221100-5544-7766-8899-aabbccddeeff")
_R = RecordType

# The prefix of most examples of [MC-NBFX] section 3: a ShortElement record named "doc".
_DOC = "4003646f63"


class _Static(DictionaryString):
  """A string of the vectors' static dictionary, which the even keys index."""

  __slots__ = ()


def _lookup(key: int) -> DictionaryString:
  """Resolve a dictionary key of the vectors: even keys are static, odd keys session strings."""
  if key % 2:
    return DictionaryString(_SESSION[key // 2])
  return _Static(_STATIC[key // 2])


def _key_of(value: DictionaryString) -> Optional[int]:
  """Return the key the vectors' writer used for a dictionary string, if any."""
  if isinstance(value, _Static):
    return 2 * _STATIC.index(value)
  return 2 * _SESSION.index(value) + 1 if value in _SESSION else None


def _spec_lookup(key: int) -> DictionaryString:
  """Name a dictionary string as the examples of [MC-NBFX] section 3 do: ``strXXX``."""
  return DictionaryString(f"str{key}")


def _spec_key(value: DictionaryString) -> Optional[int]:
  """Return the key of a string named by :func:`_spec_lookup`."""
  return int(value[3:])


def _vector(name: str) -> bytes:
  """Return the bytes of the stored vector of a given name."""
  return bytes.fromhex(next(item["hex"] for item in _VECTORS["vectors"] if item["name"] == name))


def _strip(node: Node) -> Node:
  """Return a copy of a tree without the records its text was read from."""
  if isinstance(node, Text):
    return Text(node.value)
  if isinstance(node, Element):
    attributes: List[Union[Attribute, Xmlns]] = [
      item
      if isinstance(item, Xmlns)
      else Attribute(item.name, Text(item.value.value), item.prefix, item.namespace)
      for item in node.attributes
    ]
    children = [_strip(child) for child in node.children]
    return Element(node.name, node.namespace, node.prefix, attributes, children)
  return node


def _same_text(ours: str, theirs: str) -> bool:
  """Compare two renderings of a value, allowing the digits and offsets they may differ in.

  .NET Framework prints a single-precision float with up to nine digits where the shortest
  form has fewer, and a local time in the reader's time zone.
  """
  if ours == theirs:
    return True
  try:
    a, b = values.parse_float(ours), values.parse_float(theirs)
  except ValueError:
    pass
  else:
    return a == b or values.to_single(a) == values.to_single(b) or math.isnan(a) and math.isnan(b)
  try:
    return values.parse_datetime(ours) == values.parse_datetime(theirs)
  except ValueError:
    return False


def _same_element(ours: ElementTree.Element, theirs: ElementTree.Element) -> bool:
  """Compare two parsed XML elements: names, attributes, text and children."""
  if ours.tag != theirs.tag or set(ours.attrib) != set(theirs.attrib):
    return False
  if not all(_same_text(ours.attrib[key], theirs.attrib[key]) for key in ours.attrib):
    return False
  if not _same_text(ours.text or "", theirs.text or ""):
    return False
  if not _same_text(ours.tail or "", theirs.tail or "") or len(ours) != len(theirs):
    return False
  return all(_same_element(a, b) for a, b in zip(ours, theirs))


def _pattern(length: int) -> bytes:
  """Return the test bytes NbfxVectors.cs writes: byte ``i`` is ``i * 7 + 3``."""
  return bytes((i * 7 + 3) & 0xFF for i in range(length))


def _document(element: Element) -> bytes:
  """Encode a tree built in code with the vectors' dictionaries."""
  return nbfx.encode(element, _key_of)


def _in_pieces(data: bytes, sizes: Sequence[int]) -> bytes:
  """Encode an element ``v`` whose binary content is written by one base64 call per size."""
  writer = nbfx.Writer(_key_of)
  writer.start_element(Element("v"))
  start = 0
  for size in sizes:
    writer.base64(data[start : start + size])
    start += size
  writer.end_element()
  return writer.take()


def _letter_prefixes() -> bytes:
  """Build the "all letter prefixes" vector: each letter as a prefix of each name form."""
  session = DictionaryString(_SESSION[1])
  children: List[Node] = []
  for letter in "abcdefghijklmnopqrstuvwxyz":
    uri = "urn:" + letter
    flag = Attribute("at", Text("1"), letter, uri)
    children.append(Element("e", uri, letter, [flag, Xmlns(letter, uri)]))
    mark = Attribute(DictionaryString(_SESSION[0]), Text(False), letter, session)
    item = DictionaryString(_SESSION[2])
    children.append(Element(item, session, letter, [mark, Xmlns(letter, session)]))
  children.append(Element("e", "urn:upper", "A", [Xmlns("A", "urn:upper")]))
  return _document(Element("v", children=children))


def _long_names() -> bytes:
  """Build the "long names" vector: names around the writer's buffer limits."""
  uri = "urn:" + "u" * 20000
  innermost = Element("日" * 100, uri, "", [Xmlns("", uri)])
  inner = Element("n" * 128, children=[innermost])
  middle = Element("n" * 127, children=[inner])
  attribute = Attribute("a" * 200, Text("vvv"))
  outer = Element("n" * 42, attributes=[attribute], children=[middle])
  return _document(Element("n" * 41, children=[outer]))


def _float_array() -> bytes:
  """Build the "array single 300 with namespace" vector."""
  uri = "http://example.org/arrays"
  item = Element("float", uri, "b", [Xmlns("b", uri)])
  ramp = [i * 0.25 - 10 for i in range(300)]
  return _document(Element("v", children=[Array(item, _R.FLOAT_TEXT, ramp)]))


_SPECIAL_LARGE: Dict[str, Callable[[], bytes]] = {
  "array single 300 with namespace": _float_array,
  "all letter prefixes": _letter_prefixes,
  "long names": _long_names,
  "bytes in pieces 256+4096+70000": lambda: _in_pieces(_pattern(74352), (256, 4096, 70000)),
  # A stream is read in blocks of 256 bytes, then 4096, then 65536.
  "stream 70888": lambda: _in_pieces(_pattern(70888), (256, 4096, 65536, 1000)),
  "stream 768": lambda: _in_pieces(_pattern(768), (256, 512)),
}


def _large(name: str) -> bytes:
  """Build the large vector of a given name as NbfxVectors.cs writes it."""
  if name in _SPECIAL_LARGE:
    return _SPECIAL_LARGE[name]()
  kind, _, count = name.rpartition(" ")
  if kind == "bytes":
    return _document(Element("v", children=[Text(_pattern(int(count)))]))
  unit, _, repeat = name.partition("*")
  characters = {"string x": "x", "string cjk": "日"}[unit]
  return _document(Element("v", children=[Text(characters * int(repeat))]))


def _sweep_value(kind: str) -> List[Node]:
  """Return the content of the "t" element of a sweep vector of one kind."""
  if kind == "empty":
    # An empty string written into an element produces no record at all.
    return []
  qualified = DictionaryString(_SESSION[3])
  contents: Dict[str, Any] = {
    "int8": 5,
    "int64": 2**63 - 1,
    "short string": "0123456789",
    "string 100": "y" * 100,
    "string 171": "y" * 171,
    "cjk 50": "日" * 50,
    "cjk 100": "日" * 100,
    "dictionary": qualified,
    "qname": QName("b", qualified),
    "bytes 30": _pattern(30),
    "bytes 300": _pattern(300),
    "bytes 31": _pattern(31),
    "guid": _GUID,
    "decimal": decimal.Decimal("1.5"),
    "datetime": DateTime(2026, 11, 11, 10, 20, 30, 123000, datetime.timezone.utc),
    "uniqueid": UniqueId(int=_GUID.int),
    "double": 0.1,
    "bool": True,
    "zero": 0,
  }
  return [Text(contents[kind])]


def _sweep(kind: str, pad: int) -> bytes:
  """Build a sweep vector: a value behind an attribute of ``pad`` characters."""
  attributes: List[Union[Attribute, Xmlns]] = [
    Attribute("a", Text("x" * pad)),
    Xmlns("b", DictionaryString(_SESSION[1])),
  ]
  children: List[Node] = [
    Element("t", children=_sweep_value(kind)),
    Element("u", children=[Text("tail")]),
  ]
  return _document(Element("v", attributes=attributes, children=children))


def _sweep_array(count: int) -> bytes:
  """Build a sweep vector with an int array of ``count`` items before two strings."""
  items = Array(Element("i"), _R.INT32_TEXT, [i * 1000 for i in range(count)])
  first = Element("t", children=[Text("y" * 100)])
  second = Element("u", children=[Text("z" * 120)])
  return _document(Element("v", children=[items, first, second]))


def _sweep_names(count: int) -> bytes:
  """Build a sweep vector with prefixes, names and attributes of about ``count`` characters."""
  prefix, uri = "p" + "q" * count, "urn:" + "u" * count
  children: List[Node] = []
  for i in range(6):
    attributes: List[Union[Attribute, Xmlns]] = [
      Attribute("a" * (count + i), Text("é" * count)),
      Xmlns(prefix, uri),
    ]
    content: List[Node] = [Text("s" * (80 + i))]
    children.append(Element("e" * (count + i), uri, prefix, attributes, content))
  return _document(Element("n" * count, children=children))


def _short_hash(data: bytes) -> str:
  """Return the hash form the fixture stores for sweep vectors."""
  return hashlib.sha256(data).hexdigest()[:10]


def _events(reader: nbfx.Reader) -> List[str]:
  """Read events until the data fed so far runs out, and describe each."""
  events: List[str] = []
  while True:
    try:
      event = reader.read()
    except NbfxIncompleteError:
      return events
    events.append("end" if isinstance(event, nbfx.EndElement) else repr(event))


class MultiByteInt31Tests(unittest.TestCase):
  """The variable-length integers of [MC-NBFX] 2.1.2."""

  def test_encoding_at_each_length(self) -> None:
    """Values at the bounds of each length, and the examples of the specification."""
    cases = {
      0: "00",
      17: "11",
      0x7F: "7f",
      0x80: "8001",
      145: "9101",
      5521: "912b",
      0x3FFF: "ff7f",
      16384: "808001",
      0x1FFFFF: "ffff7f",
      0x200000: "80808001",
      0x0FFFFFFF: "ffffff7f",
      268435456: "8080808001",
      2**31 - 1: "ffffffff07",
    }
    for value, encoded in cases.items():
      self.assertEqual(nbfx.encode_int31(value).hex(), encoded)
      data = b"\xaa" + bytes.fromhex(encoded) + b"\xbb"
      self.assertEqual(nbfx.decode_int31(data, 1), (value, 1 + len(encoded) // 2))

  def test_values_outside_31_bits_are_rejected(self) -> None:
    """Negative numbers and 2**31 cannot be written; a fifth byte above 7 cannot be read."""
    for value in (-1, 2**31):
      with self.assertRaises(NbfxError):
        nbfx.encode_int31(value)
    for data in ("ffffffff08", "ffffffff87", "ffffffffff01"):
      with self.assertRaises(NbfxError) as caught:
        nbfx.decode_int31(bytes.fromhex(data), 0)
      self.assertNotIsInstance(caught.exception, NbfxIncompleteError)

  def test_truncated_integer_is_incomplete(self) -> None:
    """Data that ends inside the integer asks for more."""
    for data in ("", "80", "ffffffff"):
      with self.assertRaises(NbfxIncompleteError):
        nbfx.decode_int31(bytes.fromhex(data), 0)


class DotNetVectorTests(unittest.TestCase):
  """Documents written by the .NET Framework 4.8 binary writer."""

  def test_every_vector_reads_and_writes_back_to_the_same_bytes(self) -> None:
    """Decoding and encoding again gives the bytes .NET wrote, records remembered or not."""
    for vector in _VECTORS["vectors"]:
      with self.subTest(vector["name"]):
        data = bytes.fromhex(vector["hex"])
        document = nbfx.decode(data, _lookup)
        self.assertEqual(nbfx.encode(document, _key_of), data)
        # Without the remembered records the writer must choose what .NET chose.
        self.assertEqual(nbfx.encode(_strip(document), _key_of), data)

  def test_every_vector_means_what_dotnet_reads_from_it(self) -> None:
    """The characters of each document are those .NET's own reader gives."""
    for vector in _VECTORS["vectors"]:
      with self.subTest(vector["name"]):
        self.assertFalse(vector["xml"].startswith("ERROR"))
        document = nbfx.decode(bytes.fromhex(vector["hex"]), _lookup)
        ours = ElementTree.fromstring(to_xml(document))
        theirs = ElementTree.fromstring(vector["xml"])
        self.assertTrue(_same_element(ours, theirs), f"{to_xml(document)} != {vector['xml']}")

  def test_large_documents_built_in_code_match_dotnet(self) -> None:
    """Long strings, large binary data, streams and long names: the writer's buffer limits."""
    for vector in _VECTORS["large"]:
      with self.subTest(vector["name"]):
        data = _large(vector["name"])
        self.assertEqual(len(data), vector["size"])
        self.assertEqual(hashlib.sha256(data).hexdigest(), vector["sha256"])
        self.assertEqual(nbfx.encode(nbfx.decode(data, _lookup), _key_of), data)

  def test_sweeps_match_dotnet(self) -> None:
    """Values at every offset of the writer's buffer merge with their end exactly as in .NET."""
    sweep = _VECTORS["sweep"]
    for kind, pads in sweep["kinds"].items():
      for pad, digest in pads.items():
        with self.subTest(kind=kind, pad=pad):
          self.assertEqual(_short_hash(_sweep(kind, int(pad))), digest)
    for count, digest in sweep["arrays"].items():
      with self.subTest(array=count):
        self.assertEqual(_short_hash(_sweep_array(int(count))), digest)
    for count, digest in sweep["names"].items():
      with self.subTest(names=count):
        self.assertEqual(_short_hash(_sweep_names(int(count))), digest)


class SpecificationExampleTests(unittest.TestCase):
  """The examples of [MC-NBFX] section 3."""

  def test_examples_read_as_the_characters_the_specification_gives(self) -> None:
    """Each example reads as the characters the specification shows, and writes back."""
    cases = {
      _DOC + "0207636f6d6d656e74 01": "<doc><!--comment--></doc>",
      "5e0568656c6c6f 0901610a687474703a2f2f616263 01": (
        '<a:hello xmlns:a="http://abc"></a:hello>'
      ),
      "70094d794d657373616765 0901730a687474703a2f2f616263 01": (
        '<s:MyMessage xmlns:s="http://abc"></s:MyMessage>'
      ),
      _DOC + "06a003 80 01": '<doc str416="0"></doc>',
      "4003616263 81": "<abc>0</abc>",
      _DOC + "06ec01 88de 01": '<doc str236="-34"></doc>',
      "429a01 897f": "<str154>127</str154>",
      _DOC + "06ec01 8a0080 01": '<doc str236="-32768"></doc>',
      "429a01 8bff7f": "<str154>32767</str154>",
      _DOC + "06ec01 8c15cd5b07 01": '<doc str236="123456789"></doc>',
      "429a01 8dffffff7f": "<str154>2147483647</str154>",
      _DOC + "06ec01 8e0000008000000000 01": '<doc str236="2147483648"></doc>',
      "429a01 8f0000000000010000": "<str154>1099511627776</str154>",
      _DOC + "040161 90cdcc8c3f 01": '<doc a="1.1"></doc>',
      "40055072696365 91cdcc0142": "<Price>32.45</Price>",
      _DOC + "0403696e74 94 0000060000000000802d4e0000000000 01": '<doc int="5.123456"></doc>',
      _DOC + "066e 96ff3f37f47528ca2b 01": '<doc str110="9999-12-31T23:59:59.9999999"></doc>',
      "426c 9700408ef95b47c808": "<str108>2006-05-17T00:00:00</str108>",
      _DOC + "980568656c6c6f 01": "<doc>hello</doc>",
      _DOC + "9a050068656c6c6f 01": "<doc>hello</doc>",
      _DOC + "9c0500000068656c6c6f 01": "<doc>hello</doc>",
      "400161 9d0500000068656c6c6f": "<a>hello</a>",
      _DOC + "9e080001020304050607 01": "<doc>AAECAwQFBgc=</doc>",
      "4006426173653634 a108000001020304050607": "<Base64>AAECAwQFBgc=</Base64>",
      _DOC + "a2080000000001020304050607 01": "<doc>AAECAwQFBgc=</doc>",
      _DOC + "040161 a4887b980568656c6c6f86a6 01": '<doc a="123 hello true"></doc>',
      _DOC + "040161 a8 01": '<doc a=""></doc>',
      _DOC + "a9": "<doc></doc>",
      _DOC + "04026e73 aa38 01": '<doc ns="str56"></doc>',
      "400454797065 abc401": "<Type>str196</Type>",
      _DOC + "ac00112233445566778899aabbccddeeff 01": (
        "<doc>urn:uuid:33221100-5544-7766-8899-aabbccddeeff</doc>"
      ),
      _DOC + "ae00c4f532ffffffff 01": "<doc>-PT5M44S</doc>",
      "429407 af00b08ef01b000000": "<str916>PT3H20M</str916>",
      _DOC + "b0000102030405060708090a0b0c0d0e0f 01": (
        "<doc>03020100-0504-0706-0809-0a0b0c0d0e0f</doc>"
      ),
      _DOC + "b2ffffffffffffffff 01": "<doc>18446744073709551615</doc>",
      "429a01 b3feffffffffffffff": "<str154>18446744073709551614</str154>",
      _DOC + "b401 01": "<doc>true</doc>",
      "03 4003617272 01 b505 0100010001": (
        "<arr>true</arr><arr>false</arr><arr>true</arr><arr>false</arr><arr>true</arr>"
      ),
      "03 4003617272 01 8b03 33338888dddd": "<arr>13107</arr><arr>-30584</arr><arr>-8739</arr>",
      _DOC + "040175 b60675006e006900 01": '<doc u="uni"></doc>',
      "400155 b70675006e006900": "<U>uni</U>",
      _DOC + "0403753136 b8080075006e0069003200 01": '<doc u16="uni2"></doc>',
      "4003553136 b9080075006e0069003200": "<U16>uni2</U16>",
      # The length of UnicodeChars32Text is a 32-bit integer in these examples, as in .NET.
      _DOC + "0403753332 ba0400000033003200 01": '<doc u32="32"></doc>',
      "4003553332 bb0400000033003200": "<U32>32</U32>",
      _DOC + "06f006 bc088e07 01": '<doc str880="i:str910"></doc>',
      "400454797065 bd129007": "<Type>s:str912</Type>",
    }
    for data, characters in cases.items():
      raw = bytes.fromhex(data.replace(" ", ""))
      with self.subTest(characters):
        reader = nbfx.Reader(_spec_lookup)
        reader.feed(raw)
        event = reader.read()
        if not isinstance(event, (Element, Array)):
          self.fail(f"the example starts with {event!r}")
        node = reader.read_element(event) if isinstance(event, Element) else event
        self.assertEqual(reader.buffered, 0)
        self.assertEqual(to_xml(node), characters)
        self.assertEqual(nbfx.encode(node, _spec_key), raw)


class ReaderTests(unittest.TestCase):
  """Reading documents piece by piece and rejecting malformed ones."""

  def test_bytes_fed_one_at_a_time_give_the_same_events(self) -> None:
    """A record split across feeds is read once it is complete."""
    for name in ("dictionary strings", "array with dictionary name", "list of values", "comment"):
      with self.subTest(name):
        data = _vector(name)
        whole = nbfx.Reader(_lookup)
        whole.feed(data)
        piecewise = nbfx.Reader(_lookup)
        events: List[str] = []
        for byte in data:
          piecewise.feed(bytes((byte,)))
          events += _events(piecewise)
        self.assertEqual(events, _events(whole))

  def test_read_element_consumes_nothing_until_the_element_is_complete(self) -> None:
    """An incomplete element leaves the reader where it was, depth included."""
    data = _vector("prefixes")
    reader = nbfx.Reader(_lookup)
    reader.feed(data[:-3])
    with self.assertRaises(NbfxIncompleteError):
      reader.read_element()
    self.assertEqual((reader.depth, reader.buffered), (0, len(data) - 3))
    reader.feed(data[-3:])
    self.assertEqual(reader.read_element(), nbfx.decode(data, _lookup))
    self.assertEqual(reader.buffered, 0)

  def test_text_with_end_element_closes_its_element(self) -> None:
    """A *TextWithEndElement record reads as the text and then the end of the element."""
    reader = nbfx.Reader()
    reader.feed(bytes.fromhex("4001768d05000000"))
    self.assertEqual(reader.read(), Element("v"))
    self.assertEqual(reader.read(), Text(5))
    self.assertEqual(reader.depth, 1)
    self.assertIs(reader.read(), nbfx.END_ELEMENT)
    self.assertEqual(reader.depth, 0)

  def test_malformed_documents_are_rejected(self) -> None:
    """Every violation of the format raises, none of them as merely incomplete data."""
    too_late = (values.MAX_DATETIME_TICKS + 1).to_bytes(8, "little").hex()
    cases = {
      "reserved record type": "00",
      "reserved record type after an element": "400176 78",
      "reserved text record": "400176 a5",
      "end without element": "01",
      "text that ends no element": "990161",
      "empty element name": "4000 01",
      "undeclared prefix": "4101700176 01",
      "attribute value with end element": "400176 040161 990178 01",
      "BoolText other than 0 or 1": "400176 b502",
      "QName prefix beyond z": "400176 bd1a00",
      "negative Chars32Text length": "400176 9dffffffff",
      "malformed UTF-8": "400176 9902c328",
      "odd UTF-16 length": "400176 b703610062",
      "list inside a list": "400176 a4a4a6a6 01",
      "end element inside a list": "400176 a4990161a6 01",
      "EndListText without StartListText": "400176 a6 01",
      "reserved DateTime kind": "400176 97" + (3 << 62).to_bytes(8, "little").hex(),
      "DateTime after year 9999": "400176 97" + too_late,
      "decimal scale above 28": "400176 95 00001d00" + "00" * 12,
      "array of zero items": "400176 03 400169 01 8d00 01",
      "array of a type without arrays": "400176 03 400169 01 9901 61 01",
      "array item without end element": "400176 03 400169 01 8c01 01000000 01",
      "array whose element does not end": "400176 03 400169 8d01 01000000 01",
      "array of text": "400176 03 9801 61",
      "data after the document": "400176 01 01",
    }
    for name, data in cases.items():
      with self.subTest(name):
        with self.assertRaises(NbfxError) as caught:
          nbfx.decode(bytes.fromhex(data.replace(" ", "")), _lookup)
        self.assertNotIsInstance(caught.exception, NbfxIncompleteError)

  def test_dictionary_string_without_a_dictionary_is_rejected(self) -> None:
    """A document that refers to a dictionary string needs a lookup."""
    with self.assertRaises(NbfxError):
      nbfx.decode(bytes.fromhex("420201"))
    self.assertEqual(nbfx.decode(bytes.fromhex("420401"), _lookup), Element("Envelope"))

  def test_truncated_document_is_incomplete(self) -> None:
    """Decoding a document cut short reports the missing data."""
    with self.assertRaises(NbfxIncompleteError):
      nbfx.decode(bytes.fromhex("4001768d0500"))


class WriterTests(unittest.TestCase):
  """Writing trees built in code."""

  def test_only_dictionary_strings_use_the_dictionary(self) -> None:
    """A plain string is written inline even when the dictionary holds it."""
    self.assertEqual(_document(Element("item")).hex(), "4004" + b"item".hex() + "01")
    self.assertEqual(_document(Element(DictionaryString("item"))).hex(), "420501")
    other = _document(Element(DictionaryString("other")))
    self.assertEqual(other.hex(), "4005" + b"other".hex() + "01")

  def test_given_record_is_kept(self) -> None:
    """A value with a record is written as that record, even where another is shorter."""
    element = Element("v", children=[Text(5, _R.INT64_TEXT_WITH_END_ELEMENT)])
    self.assertEqual(nbfx.encode(element).hex(), "4001768f0500000000000000")
    element = Element("v", children=[Text("ab", _R.UNICODE_CHARS8_TEXT)])
    self.assertEqual(nbfx.encode(element).hex(), "400176b6046100620001")

  def test_numbers_take_the_records_a_dotnet_writer_takes(self) -> None:
    """Integers and whole floats use the narrowest integer record; other floats keep precision."""
    cases = [
      (0, "81"),
      (1, "83"),
      (-1, "89ff"),
      (200, "8bc800"),
      (2**31, "8f0000008000000000"),
      (2**64 - 1, "b3ffffffffffffffff"),
      (2.0, "8902"),
      (Single(0.1), "91cdcccc3d"),
      (0.1, "939a9999999999b93f"),
      (Single(math.inf), "910000807f"),
      (math.inf, "93000000000000f07f"),
    ]
    for value, encoded in cases:
      with self.subTest(value):
        self.assertEqual(
          nbfx.encode(Element("v", children=[Text(value)])).hex(), "400176" + encoded
        )

  def test_comment_and_list(self) -> None:
    """Comments and lists of values are written as records of their own."""
    element = Element("v", children=[Comment("c"), Text((1, "a", True))])
    self.assertEqual(nbfx.encode(element).hex(), "400176" + "020163" + "a482980161" + "86a601")

  def test_values_a_record_cannot_carry_are_rejected(self) -> None:
    """An explicit record must fit its value."""
    cases = [
      Text(300, _R.INT8_TEXT),
      Text("x", _R.INT32_TEXT),
      Text(True, _R.ZERO_TEXT),
      Text(1, _R.BOOL_TEXT),
      Text(b"x", _R.CHARS8_TEXT),
      Text("x", _R.DICTIONARY_TEXT),
      Text(_GUID, _R.UNIQUE_ID_TEXT),
      Text("x", _R.END_LIST_TEXT),
    ]
    for text in cases:
      with self.subTest(repr(text)):
        with self.assertRaises(NbfxError):
          nbfx.encode(Element("v", children=[text]))

  def test_malformed_trees_are_rejected(self) -> None:
    """Trees that no document can represent raise instead of producing bytes."""
    end = Text("x", _R.CHARS8_TEXT_WITH_END_ELEMENT)
    undeclared = Attribute("x", Text("1"), "a", "urn:a")
    cases: Dict[str, Node] = {
      "undeclared element prefix": Element("v", "urn:a", "a"),
      "namespace without declaration": Element("v", "urn:a"),
      "undeclared attribute prefix": Element("v", attributes=[undeclared]),
      "attribute with end element": Element("v", attributes=[Attribute("x", end)]),
      "node after an end element record": Element("v", children=[end, Element("w")]),
      "empty name": Element(""),
      "array without items": Element("v", children=[Array(Element("i"), _R.INT32_TEXT, [])]),
      "array element with children": Element(
        "v", children=[Array(Element("i", children=[Text(1)]), _R.INT32_TEXT, [1])]
      ),
      "array of a type without arrays": Element(
        "v", children=[Array(Element("i"), _R.CHARS8_TEXT, ["a"])]
      ),
      "integer beyond 64 bits": Element("v", children=[Text(2**64)]),
      "value of no text type": Element("v", children=[Text(object())]),  # type: ignore[arg-type]
    }
    for name, node in cases.items():
      with self.subTest(name):
        with self.assertRaises(NbfxError):
          nbfx.encode(node)


if __name__ == "__main__":
  unittest.main()
