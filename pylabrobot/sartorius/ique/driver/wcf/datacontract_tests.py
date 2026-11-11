"""Tests for the DataContractSerializer shapes of .NET values.

The recorded exchanges (see ``contract_tests``) check the shapes the sample contract uses
against .NET byte for byte. These tests cover the rest: every primitive type, arrays of
each type .NET writes as one Array record, values a peer sends as plain characters, the
rules for member order and namespace prefixes, and the errors.
"""

import array
import datetime
import decimal
import math
import unittest
import uuid
from typing import Any, List, Sequence, Tuple

from pylabrobot.sartorius.ique.driver.wcf import nbfx
from pylabrobot.sartorius.ique.driver.wcf.contract import ServiceContract
from pylabrobot.sartorius.ique.driver.wcf.datacontract import (
  ARRAYS_NAMESPACE,
  BOOL,
  BYTE,
  BYTES,
  DATETIME,
  DECIMAL,
  DOUBLE,
  FLOAT,
  GUID,
  INT,
  LONG,
  SBYTE,
  SHORT,
  STRING,
  TIMESPAN,
  UINT,
  ULONG,
  USHORT,
  VERSION,
  XSI_NAMESPACE,
  ArrayOf,
  DataContract,
  DataObject,
  DictionaryOf,
  EnumType,
  ListOf,
  Namespaces,
  Nullable,
  XmlType,
  clr_namespace,
  read_value,
)
from pylabrobot.sartorius.ique.driver.wcf.errors import ContractError
from pylabrobot.sartorius.ique.driver.wcf.recordings import sample as s
from pylabrobot.sartorius.ique.driver.wcf.tree import Array, Attribute, Element, Text, Xmlns, to_xml
from pylabrobot.sartorius.ique.driver.wcf.values import DateTime, Single, TimeSpan

D = decimal.Decimal
UTC = datetime.timezone.utc
_NS = clr_namespace("Tests")
_OTHER = clr_namespace("Other")
_TEST = ServiceContract("ITest", "urn:test")
_I = f'xmlns:i="{XSI_NAMESPACE}"'

SHAPE = DataContract("Shape", _NS, [("Name", STRING)])
CIRCLE = DataContract("Circle", _NS, [("Radius", DOUBLE)], SHAPE)
SQUARE = DataContract("Square", _OTHER, [("Side", INT)], SHAPE)
COLOR = EnumType("Color", _NS, ["Red", "Green"])


def _parameter(xml_type: XmlType, value: Any) -> Element:
  """Write a value as the only parameter of a request, as a WCF client lays it out."""
  _, body, _ = _TEST.operation("Op", [("value", xml_type)]).request((value,))
  (element,) = body.elements()
  return element


def _round_trip(xml_type: XmlType, value: Any) -> Any:
  """Write a value as a parameter, through binary XML, and read it back as a service does."""
  operation = _TEST.operation("Op", [("value", xml_type)])
  _, body, _ = operation.request((value,))
  received = nbfx.decode(nbfx.encode(body))
  (result,) = operation.read_request([received], {})
  return result


def _read(xml_type: XmlType, *text: str) -> Any:
  """Read a value from an element that holds it as characters, as some peers send it."""
  return read_value(xml_type, Element("value", children=[Text(item) for item in text]), {})


class PrimitiveTests(unittest.TestCase):
  """Values whose element holds one text record."""

  def test_values_survive_the_wire(self) -> None:
    """Every primitive type, at the edges of its range."""
    cases: List[Tuple[XmlType, Sequence[Any]]] = [
      (BOOL, [True, False]),
      (SBYTE, [-128, 127]),
      (BYTE, [0, 255]),
      (SHORT, [-(2**15), 2**15 - 1]),
      (USHORT, [0, 2**16 - 1]),
      (INT, [-(2**31), 0, 1, 2**31 - 1]),
      (UINT, [0, 2**32 - 1]),
      (LONG, [-(2**63), 2**63 - 1]),
      (ULONG, [0, 2**63, 2**64 - 1]),
      (FLOAT, [Single(0.1), -1.5, Single(3e38), math.inf]),
      (DOUBLE, [0.1, 1e300, 5e-324, -math.inf]),
      (
        DECIMAL,
        [D("-12.750"), D("79228162514264337593543950335"), D("0.0000000000000000000000000001")],
      ),
      (STRING, ["", "x", "日本 & <tags>", "x" * 1000]),
      (BYTES, [b"", b"\x00\xff", bytes(range(256)) * 3]),
      (TIMESPAN, [datetime.timedelta(0), TimeSpan.from_ticks(-36000000001)]),
      (GUID, [uuid.UUID(int=0), uuid.UUID("33221100-5544-7766-8899-aabbccddeeff")]),
      (VERSION, [(1, 2), (1, 2, 3), (7, 2, 6010, 25563)]),
    ]
    for xml_type, examples in cases:
      for value in examples:
        with self.subTest(type=xml_type.name, value=value):
          self.assertEqual(_round_trip(xml_type, value), value)
    self.assertTrue(math.isnan(_round_trip(DOUBLE, math.nan)))
    # Like .NET, a whole number travels as an integer record, so zero loses its sign.
    self.assertEqual(math.copysign(1, _round_trip(DOUBLE, -0.0)), 1)
    # The range of a decimal is checked when the message is encoded.
    with self.assertRaises(ValueError):
      _round_trip(DECIMAL, D("1E-29"))

  def test_datetime_kind_and_tick_survive(self) -> None:
    """Unspecified, UTC and local times keep their kind and their 100 ns digit."""
    for value in (
      DateTime(2026, 11, 11, 10, 20, 30, 123456, tick=7),
      DateTime(2026, 11, 11, 10, 20, 30, 123456, UTC, tick=7),
      DateTime(2026, 11, 11, 8, 20, 30, 123456, UTC, tick=7, kind="local"),
      DateTime(1, 1, 1),
    ):
      with self.subTest(value=value):
        received = _round_trip(DATETIME, value)
        self.assertEqual((received, received.tick, received.kind), (value, value.tick, value.kind))

  def test_values_sent_as_characters_are_read(self) -> None:
    """A value may arrive as plain characters instead of a typed record."""
    cases: List[Tuple[XmlType, str, Any]] = [
      (BOOL, "1", True),
      (BOOL, " false ", False),
      (INT, "-123", -123),
      (ULONG, "18446744073709551615", 2**64 - 1),
      (FLOAT, "INF", math.inf),
      (DOUBLE, "-1.5E+20", -1.5e20),
      (DECIMAL, "-12.750", D("-12.750")),
      (TIMESPAN, "PT1.5S", datetime.timedelta(seconds=1.5)),
      (
        GUID,
        "33221100-5544-7766-8899-AABBCCDDEEFF",
        uuid.UUID("33221100-5544-7766-8899-aabbccddeeff"),
      ),
      (BYTES, "AAEC", b"\x00\x01\x02"),
      (STRING, "text", "text"),
      (COLOR, " Green", "Green"),
    ]
    for xml_type, text, value in cases:
      with self.subTest(type=xml_type.name, text=text):
        self.assertEqual(_read(xml_type, text), value)
    received = _read(DATETIME, "2026-11-11T10:20:30.1234567Z")
    self.assertEqual((received.tick, received.kind), (7, "utc"))
    self.assertEqual(_read(STRING, "ab", "cd"), "abcd")

  def test_malformed_characters_are_rejected(self) -> None:
    """Characters that are not a value of the type raise a contract error."""
    cases: List[Tuple[XmlType, str]] = [
      (INT, "1.5"),
      (INT, "4294967296"),
      (BYTE, "-1"),
      (BOOL, "yes"),
      (DOUBLE, "nan"),
      (DECIMAL, "1E+2"),
      (DATETIME, "never"),
      (TIMESPAN, "1S"),
      (GUID, "not a guid"),
      (BYTES, "not base64!"),
      (COLOR, "Blue"),
    ]
    for xml_type, text in cases:
      with self.subTest(type=xml_type.name, text=text):
        with self.assertRaises(ContractError):
          _read(xml_type, text)
    with self.assertRaises(ContractError):
      read_value(INT, Element("value", children=[Element("inner")]), {})

  def test_values_of_the_wrong_type_or_range_are_rejected(self) -> None:
    """Writing checks the Python type and the range of the .NET type."""
    cases: List[Tuple[XmlType, Any]] = [
      (INT, 2**31),
      (INT, True),
      (INT, 1.0),
      (UINT, -1),
      (SBYTE, 128),
      (BOOL, 1),
      (FLOAT, "1"),
      (DECIMAL, 1.5),
      (STRING, b"x"),
      (BYTES, "x"),
      (DATETIME, datetime.date(2026, 1, 1)),
      (TIMESPAN, 5),
      (GUID, "33221100-5544-7766-8899-aabbccddeeff"),
      (VERSION, (1,)),
      (VERSION, (1, 2, 3, 4, 5)),
      (COLOR, "Blue"),
      (INT, None),
      (COLOR, None),
    ]
    for xml_type, value in cases:
      with self.subTest(type=xml_type.name, value=value):
        with self.assertRaises(ValueError):
          _parameter(xml_type, value)

  def test_nil(self) -> None:
    """None is written as ``i:nil``, and only a type that can be null reads it back."""
    self.assertEqual(
      to_xml(_parameter(Nullable(INT), None)),
      f'<value i:nil="true" xmlns:i="{XSI_NAMESPACE}"></value>',
    )
    self.assertIsNone(_round_trip(Nullable(GUID), None))
    self.assertEqual(_round_trip(Nullable(INT), 5), 5)
    nil = Element("value", attributes=[Attribute("nil", Text("true"), "i", XSI_NAMESPACE)])
    with self.assertRaises(ContractError):
      read_value(INT, nil, {})
    not_nil = Element("value", attributes=[Attribute("nil", Text("false"), "i", XSI_NAMESPACE)])
    not_nil.children.append(Text("7"))
    self.assertEqual(read_value(Nullable(INT), not_nil, {}), 7)
    malformed = Element("value", attributes=[Attribute("nil", Text("maybe"), "i", XSI_NAMESPACE)])
    with self.assertRaises(ContractError):
      read_value(Nullable(INT), malformed, {})


class DataContractTests(unittest.TestCase):
  """Classes with data members."""

  def test_members_come_base_first_then_in_ordinal_order(self) -> None:
    """Within each class the members are sorted by name, upper case before lower case."""
    base = DataContract("Base", _NS, [("b", INT), ("A", INT)])
    derived = DataContract("Derived", _NS, [("a", INT), ("C", INT)], base)
    self.assertEqual([name for name, _, _ in derived.members()], ["A", "b", "C", "a"])
    self.assertEqual([name for name, _ in derived.declared], ["a", "C"])
    self.assertEqual([level.name for level in derived.lineage()], ["Base", "Derived"])

  def test_derived_class_is_marked_with_its_type(self) -> None:
    """An instance of a derived class names its contract with ``i:type``."""
    circle = CIRCLE.new(Name="c", Radius=1.5)
    self.assertEqual(
      to_xml(_parameter(SHAPE, circle)),
      f'<value i:type="b:Circle" xmlns:b="{_NS}" {_I}>'
      "<b:Name>c</b:Name><b:Radius>1.5</b:Radius></value>",
    )
    received = _round_trip(SHAPE, circle)
    self.assertIs(received.contract, CIRCLE)
    self.assertEqual(received.values, {"Name": "c", "Radius": 1.5})

  def test_derived_class_from_another_namespace(self) -> None:
    """Members keep the namespace of the class that declares them."""
    square = SQUARE.new(Name="q", Side=2)
    self.assertEqual(
      to_xml(_parameter(SHAPE, square)),
      f'<value i:type="c:Square" xmlns:b="{_NS}" {_I} xmlns:c="{_OTHER}">'
      "<b:Name>q</b:Name><c:Side>2</c:Side></value>",
    )
    received = _round_trip(SHAPE, square)
    self.assertEqual((received.contract, received.values), (SQUARE, {"Name": "q", "Side": 2}))
    self.assertIs(_round_trip(SHAPE, SHAPE.new(Name="s")).contract, SHAPE)

  def test_instances_must_fit_their_contract(self) -> None:
    """Every member needs a value, and only a derived class may stand in for its base."""
    with self.assertRaises(ValueError):
      SHAPE.new()
    with self.assertRaises(ValueError):
      SHAPE.new(Name="x", Extra=1)
    with self.assertRaises(ValueError):
      _parameter(CIRCLE, SHAPE.new(Name="x"))
    with self.assertRaises(ValueError):
      _parameter(SHAPE, {"Name": "x"})
    with self.assertRaises(ValueError):
      DataContract("Sub", _NS, [("Name", STRING)], SHAPE)
    unrelated = DataContract("Unrelated", _NS, [("Name", STRING)])
    with self.assertRaises(ValueError):
      _parameter(SHAPE, unrelated.new(Name="x"))

  def test_unknown_type_mark_is_rejected(self) -> None:
    """``i:type`` must name the declared class or one derived from it."""
    element = _parameter(SHAPE, CIRCLE.new(Name="c", Radius=1.0))
    element.attributes[0] = Attribute("type", Text("b:Triangle"), "i", XSI_NAMESPACE)
    with self.assertRaises(ContractError):
      read_value(SHAPE, element, {})
    element.attributes[0] = Attribute("type", Text("x:Circle"), "i", XSI_NAMESPACE)
    with self.assertRaises(ContractError):
      read_value(SHAPE, element, {})

  def test_unknown_members_are_kept_and_reported(self) -> None:
    """A member the contract lacks, sent by a newer peer, is kept aside with a warning."""
    element = _parameter(SHAPE, SHAPE.new(Name="s"))
    extra = Element("Color", _NS, "b", children=[Text("Red")])
    element.children.append(extra)
    with self.assertLogs("pylabrobot.sartorius.ique.driver.wcf.datacontract", "WARNING"):
      received = read_value(SHAPE, element, {})
    self.assertEqual((received.values, received.unknown), ({"Name": "s"}, [extra]))

  def test_malformed_instances_are_rejected(self) -> None:
    """A member twice, or text among the members, is not an instance."""
    twice = _parameter(SHAPE, SHAPE.new(Name="s"))
    twice.children.append(twice.children[0])
    text = _parameter(SHAPE, SHAPE.new(Name="s"))
    text.children.append(Text("loose"))
    for element in (twice, text):
      with self.assertRaises(ContractError):
        read_value(SHAPE, element, {})

  def test_data_object_gives_member_values(self) -> None:
    """Members are read by name; a missing one is an error, not a default."""
    instance = DataObject(SHAPE, {"Name": "s"})
    self.assertEqual(instance["Name"], "s")
    with self.assertRaises(KeyError):
      instance["Radius"]

  def test_version_absent_parts(self) -> None:
    """System.Version stores absent parts as -1; reading drops them again."""
    element = _parameter(VERSION, (1, 2))
    values = {child.name: child.content() for child in element.elements()}
    self.assertEqual(values, {"_Build": -1, "_Major": 1, "_Minor": 2, "_Revision": -1})
    del element.children[1]
    with self.assertRaises(ContractError):
      read_value(VERSION, element, {})


class CollectionTests(unittest.TestCase):
  """Lists, arrays and dictionaries."""

  def test_list_items_are_named_after_their_type(self) -> None:
    """Items of a built-in type are in the Arrays namespace, others in their type's."""
    self.assertEqual(
      to_xml(_parameter(ListOf(INT), [1, 2])),
      f'<value xmlns:b="{ARRAYS_NAMESPACE}" {_I}><b:int>1</b:int><b:int>2</b:int></value>',
    )
    self.assertEqual(
      to_xml(_parameter(ListOf(COLOR), ["Red"])),
      f'<value xmlns:b="{_NS}" {_I}><b:Color>Red</b:Color></value>',
    )
    self.assertEqual(
      to_xml(_parameter(ListOf(STRING), [None, ""])),
      f'<value xmlns:b="{ARRAYS_NAMESPACE}" {_I}>'
      '<b:string i:nil="true"></b:string><b:string></b:string></value>',
    )
    self.assertEqual(
      _round_trip(ListOf(SHAPE), [CIRCLE.new(Name="c", Radius=2.0), None]),
      [
        CIRCLE.new(Name="c", Radius=2.0),
        None,
      ],
    )
    self.assertEqual(_round_trip(ListOf(INT), []), [])

  def test_arrays_of_some_types_are_one_array_record(self) -> None:
    """bool, int, long, float, double, decimal and DateTime arrays use an Array record."""
    cases: List[Tuple[XmlType, List[Any]]] = [
      (BOOL, [True, False]),
      (INT, [1, -2, 2**31 - 1]),
      (LONG, [2**40, -1]),
      (FLOAT, [Single(0.1), math.inf]),
      (DOUBLE, [0.1, 1e300]),
      (DECIMAL, [D("1.50"), D("-0.001")]),
      (DATETIME, [DateTime(2026, 11, 11, tzinfo=UTC), DateTime(1, 1, 1)]),
    ]
    for item, values in cases:
      with self.subTest(item=item.name):
        (child,) = _parameter(ArrayOf(item), values).children
        self.assertIsInstance(child, Array)
        received = _round_trip(ArrayOf(item), values)
        self.assertEqual(list(received), values)
    self.assertIsInstance(_round_trip(ArrayOf(INT), [1, 2]), array.array)
    self.assertEqual(_round_trip(ArrayOf(INT), []), array.array("i"))

  def test_arrays_of_other_types_have_an_element_per_item(self) -> None:
    """uint, string and class arrays are written like lists; numbers still read as arrays."""
    element = _parameter(ArrayOf(UINT), [0, 2**32 - 1])
    self.assertTrue(all(isinstance(child, Element) for child in element.children))
    received = _round_trip(ArrayOf(UINT), [0, 2**32 - 1])
    self.assertEqual(
      (received.typecode, list(received)), (array.array("I").typecode, [0, 2**32 - 1])
    )
    self.assertEqual(_round_trip(ArrayOf(STRING), ["a", None]), ["a", None])

  def test_items_of_either_form_are_read(self) -> None:
    """A list reads items from an Array record, and an array from one element per item."""
    item = Element("int", ARRAYS_NAMESPACE, "b")
    compact = Element("value", children=[Array(item, nbfx.RecordType.INT32_TEXT, [3, 4])])
    self.assertEqual(read_value(ListOf(INT), compact, {}), [3, 4])
    spread = Element("value", children=[Element("int", ARRAYS_NAMESPACE, "b", children=[Text(5)])])
    self.assertEqual(read_value(ArrayOf(INT), spread, {}), array.array("i", [5]))

  def test_malformed_collections_are_rejected(self) -> None:
    """Items must have the item type's name and namespace; classes never come as arrays."""
    wrong = Element("value", children=[Element("long", ARRAYS_NAMESPACE, "b", children=[Text(5)])])
    with self.assertRaises(ContractError):
      read_value(ListOf(INT), wrong, {})
    item = Element("Shape", _NS, "b")
    compact = Element("value", children=[Array(item, nbfx.RecordType.INT32_TEXT, [1])])
    with self.assertRaises(ContractError):
      read_value(ListOf(SHAPE), compact, {})
    with self.assertRaises(ContractError):
      read_value(ListOf(INT), Element("value", children=[Text("1")]), {})

  def test_dictionary_entry_names(self) -> None:
    """Entries are named after both types, with a digest when one is outside XML Schema."""
    self.assertEqual(DictionaryOf(STRING, INT).entry, "KeyValueOfstringint")
    # The name a .NET client gave the entries of the recorded Dictionary<DeviceKind, ...>.
    configuration = DictionaryOf(s.DEVICE_KIND, s.DEVICE_CONFIGURATION)
    self.assertEqual(configuration.entry, "KeyValueOfDeviceKindDeviceConfigurationB_SIY0oB6")
    self.assertEqual(configuration.name, "ArrayOf" + configuration.entry)
    self.assertEqual(configuration.namespace, ARRAYS_NAMESPACE)

  def test_dictionaries_survive_the_wire(self) -> None:
    """Entries keep their order, keys and values; null values stay null."""
    mapping = {"Red": SHAPE.new(Name="r"), "Green": None}
    received = _round_trip(DictionaryOf(COLOR, SHAPE), mapping)
    self.assertEqual(list(received.items()), list(mapping.items()))
    self.assertEqual(_round_trip(DictionaryOf(STRING, INT), {}), {})
    self.assertIsNone(_round_trip(DictionaryOf(STRING, INT), None))

  def test_malformed_dictionaries_are_rejected(self) -> None:
    """Each entry must be an entry element with a Key and a Value."""
    element = _parameter(DictionaryOf(STRING, INT), {"a": 1})
    entry = element.children[0]
    assert isinstance(entry, Element)
    del entry.children[1]
    with self.assertRaises(ContractError):
      read_value(DictionaryOf(STRING, INT), element, {})
    element = _parameter(DictionaryOf(STRING, INT), {"a": 1})
    element.children.append(Element("Other", ARRAYS_NAMESPACE, "b"))
    with self.assertRaises(ContractError):
      read_value(DictionaryOf(STRING, INT), element, {})


class NamespacesTests(unittest.TestCase):
  """How prefixes are chosen, as a .NET writer chooses them."""

  def test_default_namespace_then_letters(self) -> None:
    """An element without a prefix takes the default namespace; others get a free letter."""
    namespaces = Namespaces([("s", "urn:s"), ("a", "urn:a")])
    outer = namespaces.open("outer", "urn:x")
    self.assertEqual(outer.attributes, [Xmlns("", "urn:x")])
    self.assertEqual(namespaces.declare(outer, "urn:y"), "b")
    self.assertEqual(namespaces.declare(outer, "urn:y"), "b")
    inner = namespaces.open("inner", "urn:y")
    self.assertEqual((inner.prefix, inner.attributes), ("b", []))
    self.assertEqual(namespaces.declare(inner, "urn:z"), "c")
    namespaces.close()
    self.assertIsNone(namespaces.lookup("urn:z"))
    self.assertEqual(namespaces.lookup("urn:x"), "")
    self.assertIsNone(namespaces.lookup("urn:x", attribute=True))
    self.assertEqual(namespaces.declare(outer, "urn:x", attribute=True), "c")

  def test_hidden_prefix_is_not_used(self) -> None:
    """A prefix bound again further in no longer stands for its outer namespace."""
    namespaces = Namespaces([("b", "urn:outer")])
    element = namespaces.open("e", "")
    namespaces.bind(element, "b", "urn:inner")
    self.assertIsNone(namespaces.lookup("urn:outer"))
    self.assertEqual(namespaces.lookup("urn:inner"), "b")
    namespaces.bind(element, "b", "urn:inner")
    self.assertEqual(element.attributes, [Xmlns("b", "urn:inner")])


if __name__ == "__main__":
  unittest.main()
