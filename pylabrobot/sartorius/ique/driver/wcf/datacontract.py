"""Descriptions of .NET types as the DataContractSerializer puts them into XML.

A description is an :class:`XmlType`. It writes a Python value into an element and reads
one back, and it carries the type's data contract name and namespace, which decide the
names of collection items and the ``i:type`` of a derived class.

==========================  ==============================================================
.NET type                   Description and Python value
==========================  ==============================================================
bool, numbers               ``BOOL``, ``INT``, ``UINT``, ``LONG``, ... ``FLOAT``,
                            ``DOUBLE``, ``DECIMAL``: bool, int, float, ``decimal.Decimal``
string                      ``STRING``: str, or None
DateTime, TimeSpan, Guid    ``DATETIME``, ``TIMESPAN``, ``GUID``: ``datetime`` (see
                            ``values.DateTime`` for the kind), ``timedelta``, ``uuid.UUID``
byte[]                      ``BYTES``: bytes, or None
System.Version              ``VERSION``: a tuple of two to four ints, or None
enum                        :class:`EnumType`: the name of the value, as a str
T? (Nullable<T>)            :class:`Nullable`: the value, or None
class with [DataContract]   :class:`DataContract`: a :class:`DataObject`, or None
List<T>, T[]                :class:`ListOf`, :class:`ArrayOf`: a list (an ``array.array``
                            for an array of numbers), or None
Dictionary<K, V>            :class:`DictionaryOf`: a dict, or None
==========================  ==============================================================

A reference type holds None as ``i:nil="true"``. The namespace of a class is
``http://schemas.datacontract.org/2004/07/`` followed by its CLR namespace
(:func:`clr_namespace`).
"""

from __future__ import annotations

import abc
import array
import base64
import datetime
import decimal
import hashlib
import logging
import string
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from pylabrobot.sartorius.ique.driver.wcf.errors import ContractError, NbfxError
from pylabrobot.sartorius.ique.driver.wcf.records import RecordType
from pylabrobot.sartorius.ique.driver.wcf.tree import (
  Array,
  Attribute,
  Element,
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
  format_duration,
  parse_bool,
  parse_bytes,
  parse_datetime,
  parse_decimal,
  parse_duration,
  parse_float,
  parse_int,
  text_of,
)

logger = logging.getLogger(__name__)

XSD_NAMESPACE = "http://www.w3.org/2001/XMLSchema"
XSI_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"
SERIALIZATION_NAMESPACE = "http://schemas.microsoft.com/2003/10/Serialization/"
ARRAYS_NAMESPACE = SERIALIZATION_NAMESPACE + "Arrays"
DATACONTRACT_NAMESPACE = "http://schemas.datacontract.org/2004/07/"

_BUILT_IN_NAMESPACES = (XSD_NAMESPACE, SERIALIZATION_NAMESPACE)
_XSI_PREFIX = "i"


def clr_namespace(name: str) -> str:
  """Return the data contract namespace of the classes in a CLR namespace."""
  return DATACONTRACT_NAMESPACE + name


class Namespaces:
  """The namespace prefixes in force at the element being written.

  Prefixes are assigned as a .NET writer assigns them: an element in a namespace without
  a prefix makes that namespace the default one; a namespace that needs a prefix gets
  the first letter from ``a`` on that no enclosing element uses.
  """

  def __init__(self, in_force: Sequence[Tuple[str, str]] = ()) -> None:
    """Create the table.

    Args:
      in_force: The (prefix, namespace) pairs declared around what will be written.
    """
    self._bindings: List[Tuple[str, str]] = list(in_force)
    self._marks: List[int] = []

  def _bind(self, element: Element, prefix: str, uri: str) -> None:
    """Declare a prefix on an element."""
    element.attributes.append(Xmlns(prefix, DictionaryString(uri)))
    self._bindings.append((prefix, uri))

  def _default(self) -> str:
    """Return the default namespace in force, or an empty string."""
    return next((uri for prefix, uri in reversed(self._bindings) if not prefix), "")

  def lookup(self, uri: str, attribute: bool = False) -> Optional[str]:
    """Return the prefix in force for a namespace, or None.

    Args:
      uri: The namespace.
      attribute: Whether the prefix is for an attribute, which cannot use the default
        namespace.
    """
    for index in range(len(self._bindings) - 1, -1, -1):
      prefix, bound = self._bindings[index]
      hidden = any(later == prefix for later, _ in self._bindings[index + 1 :])
      if bound == uri and not hidden and not (attribute and not prefix):
        return prefix
    return None

  def open(self, name: str, uri: str) -> Element:
    """Start an element; pair with :meth:`close` once its content is written."""
    self._marks.append(len(self._bindings))
    prefix = self.lookup(uri)
    element = Element(DictionaryString(name), uri, prefix or "")
    if prefix is None and (uri or self._default()):
      self._bind(element, "", uri)
    return element

  def close(self) -> None:
    """End the element started last."""
    del self._bindings[self._marks.pop() :]

  def declare(self, element: Element, uri: str, attribute: bool = False) -> str:
    """Return the prefix for a namespace, declaring a new one on ``element`` if needed."""
    prefix = self.lookup(uri, attribute)
    if prefix is None:
      used = {bound for bound, _ in self._bindings}
      free = [letter for letter in string.ascii_lowercase if letter not in used]
      if not free:
        raise ContractError("more than 26 namespaces need a prefix at once")
      prefix = free[0]
      self._bind(element, prefix, uri)
    return prefix

  def bind(self, element: Element, prefix: str, uri: str) -> None:
    """Make a given prefix stand for a namespace, declaring it on ``element`` if needed."""
    current = next((bound for used, bound in reversed(self._bindings) if used == prefix), None)
    if current != uri:
      self._bind(element, prefix, uri)


def _add_attribute(element: Element, attribute: Attribute) -> None:
  """Add an attribute in front of the element's namespace declarations."""
  count = sum(isinstance(item, Attribute) for item in element.attributes)
  element.attributes.insert(count, attribute)


class XmlType(abc.ABC):
  """How the values of one .NET type appear inside an XML element.

  Attributes:
    name: The data contract name of the type.
    namespace: The data contract namespace of the type.
    nullable: Whether None is a value of the type.
    composite: Whether the type is a class or a collection, which have namespace
      declarations of their own, rather than a primitive or an enum.
    primitive: Whether WCF's simpler serializer for all-primitive operations handles it.
  """

  def __init__(
    self, name: str, namespace: str, nullable: bool, composite: bool, primitive: bool = False
  ) -> None:
    """Set the attributes common to all types."""
    self.name = name
    self.namespace = namespace
    self.nullable = nullable
    self.composite = composite
    self.primitive = primitive

  @abc.abstractmethod
  def write(self, value: Any, element: Element, namespaces: Namespaces) -> None:
    """Write a value that is not None into an element.

    Raises:
      ValueError: The value is not one of this type.
    """

  @abc.abstractmethod
  def read(self, element: Element, scope: Mapping[str, str]) -> Any:
    """Read a value from an element that is not nil.

    Args:
      element: The element that holds the value.
      scope: The namespace declarations in force inside the element, by prefix.

    Raises:
      ContractError: The element does not hold a value of this type.
    """


def is_nil(element: Element) -> bool:
  """Tell whether an element is marked ``xsi:nil``."""
  flag = element.attribute("nil", XSI_NAMESPACE)
  try:
    return flag is not None and parse_bool(flag.characters)
  except ValueError as error:
    raise ContractError(f"malformed nil attribute on {element.name!r}: {error}") from error


def write_value(xml_type: XmlType, value: Any, element: Element, namespaces: Namespaces) -> None:
  """Write a value, or the nil mark for None, into an element."""
  if value is not None:
    xml_type.write(value, element, namespaces)
    return
  if not xml_type.nullable:
    raise ValueError(f"{element.name}: a {xml_type.name} cannot be None")
  namespaces.bind(element, _XSI_PREFIX, XSI_NAMESPACE)
  nil = Attribute(DictionaryString("nil"), Text(True), _XSI_PREFIX, XSI_NAMESPACE)
  _add_attribute(element, nil)


def read_value(xml_type: XmlType, element: Element, scope: Mapping[str, str]) -> Any:
  """Read a value, or None for a nil element.

  Args:
    xml_type: The type the element is declared to hold.
    element: The element.
    scope: The namespace declarations in force around the element, by prefix.
  """
  if any(isinstance(item, Xmlns) for item in element.attributes):
    scope = declared(scope, element)
  if is_nil(element):
    if not xml_type.nullable:
      raise ContractError(f"{element.name!r} is nil, but a {xml_type.name} cannot be")
    return None
  return xml_type.read(element, scope)


def child_element(
  xml_type: XmlType, name: str, uri: str, value: Any, parent: XmlType, namespaces: Namespaces
) -> Element:
  """Build the element of a class member or collection item.

  Args:
    xml_type: The declared type of the member or item.
    name: The element name.
    uri: The element namespace.
    value: The value to write.
    parent: The class or collection the element belongs to. A member of a class or
      collection type from another namespace declares that namespace on its element.
    namespaces: The prefixes in force.
  """
  element = namespaces.open(name, uri)
  if xml_type.composite and xml_type.namespace not in ("", parent.namespace):
    namespaces.declare(element, xml_type.namespace)
  write_value(xml_type, value, element, namespaces)
  namespaces.close()
  return element


class _Primitive(XmlType):
  """A type whose value is the text of its element."""

  def __init__(
    self,
    name: str,
    to_text: Callable[[Any], Optional[TextValue]],
    from_content: Callable[[Optional[TextValue]], Any],
    namespace: str = XSD_NAMESPACE,
    nullable: bool = False,
    primitive: bool = False,
    typecode: Optional[str] = None,
    array_record: Optional[RecordType] = None,
  ) -> None:
    """Create the type.

    Args:
      name: The XML Schema name of the type.
      to_text: Converts a value to its typed text, or to None for no text at all.
      from_content: Converts the typed content of an element, None when it has none.
      namespace: The namespace of the name.
      nullable: Whether None is a value of the type.
      primitive: Whether WCF's serializer for all-primitive operations handles it.
      typecode: The ``array.array`` typecode an array of the type is read into.
      array_record: The Array item record a .NET array of the type is written with.
    """
    super().__init__(name, namespace, nullable, composite=False, primitive=primitive)
    self._to_text = to_text
    self._from_content = from_content
    self.typecode = typecode
    self.array_record = array_record

  def write(self, value: Any, element: Element, namespaces: Namespaces) -> None:
    """Write the value as the element's text."""
    text = self.to_text(value)
    if text is not None:
      element.children.append(Text(text))

  def to_text(self, value: Any) -> Optional[TextValue]:
    """Convert a value to its typed text; None stands for no text at all."""
    try:
      return self._to_text(value)
    except (TypeError, ValueError, ArithmeticError) as error:
      raise ValueError(f"{value!r} is not a valid {self.name}: {error}") from error

  def from_content(self, content: Optional[TextValue]) -> Any:
    """Convert the typed content of an element; None stands for no content."""
    try:
      return self._from_content(content)
    except (TypeError, ValueError, ArithmeticError) as error:
      raise ContractError(f"{content!r} is not a valid {self.name}: {error}") from error

  def read(self, element: Element, scope: Mapping[str, str]) -> Any:
    """Read the value from the element's text."""
    try:
      content = element.content()
    except NbfxError as error:
      raise ContractError(str(error)) from error
    return self.from_content(content)


def _expect(value: Any, *types: type) -> Any:
  """Return a value after checking its type; a bool never counts as a number."""
  if isinstance(value, bool) and bool not in types or not isinstance(value, types):
    names = " or ".join(kind.__name__ for kind in types)
    raise TypeError(f"expected {names}, got {type(value).__name__}")
  return value


def _text_content(content: Optional[TextValue]) -> str:
  """Return the characters of some content; a typed value counts as its characters."""
  return "" if content is None else text_of(content)


def _read_bool(content: Optional[TextValue]) -> bool:
  """Read a boolean from typed content or from its characters."""
  return content if isinstance(content, bool) else parse_bool(_text_content(content))


def _integer(name: str, bits: int, signed: bool, typecode: Optional[str] = None) -> _Primitive:
  """Describe an integer type of a given width."""
  low, high = (-(2 ** (bits - 1)), 2 ** (bits - 1)) if signed else (0, 2**bits)

  def check(value: int) -> int:
    """Check that an integer fits the type."""
    if not low <= value < high:
      raise ValueError(f"out of range for a {'signed' if signed else 'unsigned'} {bits}-bit number")
    return value

  def to_text(value: Any) -> TextValue:
    """Convert an int to text."""
    return check(_expect(value, int))

  def from_content(content: Optional[TextValue]) -> int:
    """Read an int from typed content or from its characters."""
    if isinstance(content, int) and not isinstance(content, bool):
      return check(content)
    return check(parse_int(_text_content(content)))

  record = {32: RecordType.INT32_TEXT, 64: RecordType.INT64_TEXT}.get(bits) if signed else None
  return _Primitive(
    name,
    to_text,
    from_content,
    primitive=record is not None,
    typecode=typecode,
    array_record=record,
  )


def _read_float(content: Optional[TextValue]) -> float:
  """Read a float from typed content or from its characters."""
  if isinstance(content, (int, float)) and not isinstance(content, bool):
    return float(content)
  return parse_float(_text_content(content))


def _read_decimal(content: Optional[TextValue]) -> decimal.Decimal:
  """Read a decimal from typed content or from its characters."""
  if isinstance(content, decimal.Decimal):
    return content
  if isinstance(content, int) and not isinstance(content, bool):
    return decimal.Decimal(content)
  return parse_decimal(_text_content(content))


def _write_decimal(value: Any) -> TextValue:
  """Convert a Decimal or an int to decimal text."""
  return decimal.Decimal(_expect(value, decimal.Decimal, int))


def _read_datetime(content: Optional[TextValue]) -> datetime.datetime:
  """Read a date and time from typed content or from its characters."""
  if isinstance(content, datetime.datetime):
    return content
  return parse_datetime(_text_content(content))


def _read_duration(content: Optional[TextValue]) -> datetime.timedelta:
  """Read a duration from typed content or from its characters."""
  if isinstance(content, datetime.timedelta):
    return TimeSpan.from_timedelta(content)
  return parse_duration(_text_content(content))


def _read_guid(content: Optional[TextValue]) -> uuid.UUID:
  """Read a GUID from typed content or from its characters."""
  if isinstance(content, uuid.UUID):
    return uuid.UUID(int=content.int)
  return uuid.UUID(_text_content(content).strip())


def _read_bytes(content: Optional[TextValue]) -> bytes:
  """Read binary data from typed content or from base64 characters."""
  return content if isinstance(content, bytes) else parse_bytes(_text_content(content))


def _write_string(value: Any) -> Optional[TextValue]:
  """Convert a str to text; an empty string is an element without text."""
  return _expect(value, str) or None


def _write_bytes(value: Any) -> Optional[TextValue]:
  """Convert bytes to text; no bytes are an element without text."""
  return bytes(_expect(value, bytes, bytearray)) or None


_UINT32_TYPECODE = "I" if array.array("I").itemsize == 4 else "L"
_INT32_TYPECODE = "i" if array.array("i").itemsize == 4 else "l"

BOOL = _Primitive(
  "boolean",
  lambda value: _expect(value, bool),
  _read_bool,
  primitive=True,
  array_record=RecordType.BOOL_TEXT,
)
SBYTE = _integer("byte", 8, signed=True, typecode="b")
BYTE = _integer("unsignedByte", 8, signed=False, typecode="B")
SHORT = _integer("short", 16, signed=True, typecode="h")
USHORT = _integer("unsignedShort", 16, signed=False, typecode="H")
INT = _integer("int", 32, signed=True, typecode=_INT32_TYPECODE)
UINT = _integer("unsignedInt", 32, signed=False, typecode=_UINT32_TYPECODE)
LONG = _integer("long", 64, signed=True, typecode="q")
ULONG = _integer("unsignedLong", 64, signed=False, typecode="Q")
FLOAT = _Primitive(
  "float",
  lambda value: Single(_expect(value, float, int)),
  _read_float,
  primitive=True,
  typecode="f",
  array_record=RecordType.FLOAT_TEXT,
)
DOUBLE = _Primitive(
  "double",
  lambda value: float(_expect(value, float, int)),
  _read_float,
  primitive=True,
  typecode="d",
  array_record=RecordType.DOUBLE_TEXT,
)
DECIMAL = _Primitive(
  "decimal", _write_decimal, _read_decimal, primitive=True, array_record=RecordType.DECIMAL_TEXT
)
STRING = _Primitive("string", _write_string, _text_content, nullable=True, primitive=True)
DATETIME = _Primitive(
  "dateTime",
  lambda value: _expect(value, datetime.datetime),
  _read_datetime,
  primitive=True,
  array_record=RecordType.DATETIME_TEXT,
)
TIMESPAN = _Primitive(
  "duration",
  lambda value: format_duration(_expect(value, datetime.timedelta)),
  _read_duration,
  namespace=SERIALIZATION_NAMESPACE,
)
GUID = _Primitive(
  "guid",
  lambda value: str(uuid.UUID(int=_expect(value, uuid.UUID).int)),
  _read_guid,
  namespace=SERIALIZATION_NAMESPACE,
)
BYTES = _Primitive("base64Binary", _write_bytes, _read_bytes, nullable=True, primitive=True)


class EnumType(XmlType):
  """A .NET enum, written as the name of its value."""

  def __init__(self, name: str, namespace: str, values: Sequence[str]) -> None:
    """Create the type.

    Args:
      name: The name of the enum.
      namespace: Its data contract namespace.
      values: The names of its values.
    """
    super().__init__(name, namespace, nullable=False, composite=False)
    self.values = tuple(values)

  def write(self, value: Any, element: Element, namespaces: Namespaces) -> None:
    """Write the name of the value."""
    if value not in self.values:
      raise ValueError(f"{value!r} is not a value of the enum {self.name}")
    element.children.append(Text(str(value)))

  def read(self, element: Element, scope: Mapping[str, str]) -> str:
    """Read the name of the value."""
    try:
      name = _text_content(element.content()).strip()
    except NbfxError as error:
      raise ContractError(str(error)) from error
    if name not in self.values:
      raise ContractError(f"{name!r} is not a value of the enum {self.name}")
    return name


class Nullable(XmlType):
  """A ``Nullable<T>`` of a value type: the value, or None."""

  def __init__(self, inner: XmlType) -> None:
    """Create the nullable form of a type."""
    super().__init__(inner.name, inner.namespace, nullable=True, composite=inner.composite)
    self.inner = inner

  def write(self, value: Any, element: Element, namespaces: Namespaces) -> None:
    """Write the value as the inner type does."""
    self.inner.write(value, element, namespaces)

  def read(self, element: Element, scope: Mapping[str, str]) -> Any:
    """Read the value as the inner type does."""
    return self.inner.read(element, scope)


@dataclass
class DataObject:
  """An instance of a class with a data contract.

  Attributes:
    contract: The contract of the instance's own class, which may derive from the type a
      member or parameter declares.
    values: The values of the data members, by name.
    unknown: Elements read from the wire that the contract does not describe.
  """

  contract: "DataContract"
  values: Dict[str, Any] = field(default_factory=dict)
  unknown: List[Element] = field(default_factory=list)

  def __getitem__(self, name: str) -> Any:
    """Return the value of a data member."""
    return self.values[name]


class DataContract(XmlType):
  """A class with ``[DataContract]``: an element per data member.

  The members of a base class come first; within a class they are in ordinal order of
  their names, whatever the order they are given in. An instance of a derived class in
  place of its base is marked with ``i:type``.
  """

  def __init__(
    self,
    name: str,
    namespace: str,
    members: Sequence[Tuple[str, XmlType]] = (),
    base: Optional["DataContract"] = None,
  ) -> None:
    """Create the contract.

    Args:
      name: The name of the class.
      namespace: Its data contract namespace (see :func:`clr_namespace`).
      members: The data members the class itself declares, as (name, type) pairs.
      base: The contract of the base class. This contract becomes one of its known types.
    """
    super().__init__(name, namespace, nullable=True, composite=True)
    self.base = base
    self._members: Dict[str, XmlType] = {}
    self._known: Dict[Tuple[str, str], DataContract] = {}
    for member_name, member_type in members:
      self.member(member_name, member_type)
    ancestor = base
    while ancestor is not None:
      ancestor._known[(namespace, name)] = self
      ancestor = ancestor.base

  def member(self, name: str, xml_type: XmlType) -> None:
    """Add a data member; needed after creation for a member of the class's own type."""
    if any(name in level._members for level in self.lineage()):
      raise ValueError(f"{self.name} already has a member {name!r}")
    self._members[name] = xml_type

  @property
  def declared(self) -> List[Tuple[str, XmlType]]:
    """The members the class itself declares, in the order they were given."""
    return list(self._members.items())

  def lineage(self) -> List["DataContract"]:
    """Return this contract and its bases, the root class first."""
    chain: List[DataContract] = []
    level: Optional[DataContract] = self
    while level is not None:
      chain.insert(0, level)
      level = level.base
    return chain

  def members(self) -> List[Tuple[str, XmlType, "DataContract"]]:
    """Return all members in wire order, each with its type and the class declaring it."""
    return [
      (name, level._members[name], level)
      for level in self.lineage()
      for name in sorted(level._members)
    ]

  def new(self, **values: Any) -> DataObject:
    """Create an instance from values for all of its members.

    Raises:
      ValueError: A member has no value, or a value is for no member.
    """
    names = {name for name, _, _ in self.members()}
    if names != set(values):
      missing, extra = sorted(names - set(values)), sorted(set(values) - names)
      raise ValueError(f"{self.name}: missing members {missing}, unknown members {extra}")
    return DataObject(self, dict(values))

  def write(self, value: Any, element: Element, namespaces: Namespaces) -> None:
    """Write the members of an instance, marking a derived class with ``i:type``."""
    if not isinstance(value, DataObject):
      raise ValueError(f"expected a DataObject of {self.name}, got {type(value).__name__}")
    actual = value.contract
    if actual is not self:
      if self not in actual.lineage():
        raise ValueError(f"{actual.name} does not derive from {self.name}")
      namespaces.bind(element, _XSI_PREFIX, XSI_NAMESPACE)
      prefix = namespaces.declare(element, actual.namespace)
      type_name = QName(prefix, DictionaryString(actual.name))
      mark = Attribute(DictionaryString("type"), Text(type_name), _XSI_PREFIX, XSI_NAMESPACE)
      _add_attribute(element, mark)
    for name, xml_type, level in actual.members():
      if name not in value.values:
        raise ValueError(f"{actual.name} instance has no value for member {name!r}")
      member = child_element(xml_type, name, level.namespace, value.values[name], level, namespaces)
      element.children.append(member)

  def _actual(self, element: Element, scope: Mapping[str, str]) -> "DataContract":
    """Return the contract an element's ``i:type`` names, or this one without the mark."""
    mark = element.attribute("type", XSI_NAMESPACE)
    if mark is None:
      return self
    prefix, _, name = mark.characters.strip().rpartition(":")
    key = (resolve(scope, prefix, default=True), name)
    if key == (self.namespace, self.name):
      return self
    if key not in self._known:
      raise ContractError(f"{key[1]!r} in {key[0]!r} is not a known type of {self.name}")
    return self._known[key]

  def read(self, element: Element, scope: Mapping[str, str]) -> DataObject:
    """Read an instance; members the contract lacks are kept in ``unknown``."""
    try:
      actual = self._actual(element, scope)
    except NbfxError as error:
      raise ContractError(f"malformed type mark on {element.name!r}: {error}") from error
    known = {(name, level.namespace): xml_type for name, xml_type, level in actual.members()}
    result = DataObject(actual)
    for child in element.children:
      if not isinstance(child, Element):
        raise ContractError(f"{actual.name} holds a {type(child).__name__}, not only elements")
      key = (str(child.name), child.namespace)
      if key not in known:
        logger.warning("%s: member %s is not part of the contract", actual.name, child.name)
        result.unknown.append(child)
      elif key[0] in result.values:
        raise ContractError(f"{actual.name} holds member {child.name!r} twice")
      else:
        result.values[key[0]] = read_value(known[key], child, scope)
    return result


def _collection_namespace(item: XmlType) -> str:
  """Return the namespace of a collection of an item type."""
  return ARRAYS_NAMESPACE if item.namespace in _BUILT_IN_NAMESPACES else item.namespace


class ListOf(XmlType):
  """A ``List<T>`` or other collection: one element per item, named after the item type."""

  def __init__(self, item: XmlType) -> None:
    """Create the collection type of an item type."""
    namespace = _collection_namespace(item)
    super().__init__("ArrayOf" + item.name, namespace, nullable=True, composite=True)
    self.item = item

  def write(self, value: Any, element: Element, namespaces: Namespaces) -> None:
    """Write an element for each item."""
    for item in value:
      child = child_element(self.item, self.item.name, self.namespace, item, self, namespaces)
      element.children.append(child)

  def _item_values(self, element: Element, scope: Mapping[str, str]) -> List[Any]:
    """Read the items, whether they come as elements or as array records."""
    if not isinstance(self.item, _Primitive) and any(
      isinstance(child, Array) for child in element.children
    ):
      raise ContractError(f"array record of values in a collection of {self.item.name}")
    values: List[Any] = []
    for child in element.children:
      if isinstance(child, Element):
        self._check_item(child)
        values.append(read_value(self.item, child, scope))
      elif isinstance(child, Array) and isinstance(self.item, _Primitive):
        self._check_item(child.element)
        values.extend(self.item.from_content(item) for item in child.values)
      else:
        raise ContractError(f"collection of {self.item.name} holds a {type(child).__name__}")
    return values

  def _check_item(self, element: Element) -> None:
    """Check the name of an item element."""
    if (element.name, element.namespace) != (self.item.name, self.namespace):
      raise ContractError(
        f"item {element.name!r} in {element.namespace!r} is not a {self.item.name} "
        f"in {self.namespace!r}"
      )

  def read(self, element: Element, scope: Mapping[str, str]) -> Any:
    """Read the items into a list."""
    return self._item_values(element, scope)


class ArrayOf(ListOf):
  """A .NET array ``T[]``.

  It looks like a :class:`ListOf`, except that an array of bool, int, long, float, double,
  decimal or DateTime is written as one compact Array record. An array of numbers is read
  into an ``array.array``.
  """

  def write(self, value: Any, element: Element, namespaces: Namespaces) -> None:
    """Write the items: as an Array record where a .NET writer does, else one by one."""
    item = self.item
    if not isinstance(item, _Primitive) or item.array_record is None:
      super().write(value, element, namespaces)
    elif len(value):
      texts = [item.to_text(entry) for entry in value]
      repeated = namespaces.open(item.name, self.namespace)
      namespaces.close()
      element.children.append(Array(repeated, item.array_record, texts))

  def read(self, element: Element, scope: Mapping[str, str]) -> Any:
    """Read the items: numbers into an ``array.array``, anything else into a list."""
    item = self.item
    if not isinstance(item, _Primitive) or item.typecode is None:
      return super().read(element, scope)
    children = element.children
    if len(children) == 1 and isinstance(children[0], Array):
      self._check_item(children[0].element)
      values = children[0].values
      if isinstance(values, array.array) and values.typecode == item.typecode:
        return values
    return array.array(item.typecode, self._item_values(element, scope))


def _namespaces_digest(*types: XmlType) -> str:
  """Return the suffix .NET adds to a generic type's name for parameters outside XML Schema.

  It is the base64 of the first six bytes of the MD5 of the parameters' namespaces, each
  preceded by a space and all preceded by the number of parameters.
  """
  if all(xml_type.namespace in _BUILT_IN_NAMESPACES for xml_type in types):
    return ""
  text = f" {len(types)}" + "".join(f" {xml_type.namespace}" for xml_type in types)
  digest = hashlib.md5(text.encode("utf-8"), usedforsecurity=False).digest()[:6]
  return base64.b64encode(digest).decode("ascii").replace("/", "_S").replace("+", "_P")


class DictionaryOf(XmlType):
  """A ``Dictionary<K, V>``: an element per entry with a ``Key`` and a ``Value`` element."""

  def __init__(self, key: XmlType, value: XmlType) -> None:
    """Create the dictionary type of a key type and a value type."""
    entry = f"KeyValueOf{key.name}{value.name}{_namespaces_digest(key, value)}"
    super().__init__("ArrayOf" + entry, ARRAYS_NAMESPACE, nullable=True, composite=True)
    self.key = key
    self.value = value
    self.entry = entry

  def write(self, value: Any, element: Element, namespaces: Namespaces) -> None:
    """Write an entry element for each item of a mapping."""
    for key, item in value.items():
      entry = namespaces.open(self.entry, self.namespace)
      for name, xml_type, part in (("Key", self.key, key), ("Value", self.value, item)):
        entry.children.append(child_element(xml_type, name, self.namespace, part, self, namespaces))
      namespaces.close()
      element.children.append(entry)

  def read(self, element: Element, scope: Mapping[str, str]) -> Dict[Any, Any]:
    """Read the entries into a dict."""
    result: Dict[Any, Any] = {}
    for entry in element.children:
      if not isinstance(entry, Element) or (entry.name, entry.namespace) != (
        self.entry,
        self.namespace,
      ):
        raise ContractError(f"dictionary holds something else than {self.entry} elements")
      inner = declared(scope, entry)
      key = entry.child("Key", self.namespace)
      value = entry.child("Value", self.namespace)
      if key is None or value is None:
        raise ContractError(f"dictionary entry {self.entry} lacks its Key or Value")
      result[read_value(self.key, key, inner)] = read_value(self.value, value, inner)
    return result


class _VersionType(XmlType):
  """``System.Version``, serialized through its private fields."""

  def __init__(self) -> None:
    """Create the type."""
    self._contract = DataContract(
      "Version",
      clr_namespace("System"),
      [("_Build", INT), ("_Major", INT), ("_Minor", INT), ("_Revision", INT)],
    )
    super().__init__(self._contract.name, self._contract.namespace, nullable=True, composite=True)

  def write(self, value: Any, element: Element, namespaces: Namespaces) -> None:
    """Write a tuple (major, minor[, build[, revision]]); an absent part is written as -1."""
    parts = tuple(value)
    if not 2 <= len(parts) <= 4:
      raise ValueError(f"a version has two to four parts, got {parts!r}")
    major, minor, build, revision = parts + (-1,) * (4 - len(parts))
    instance = self._contract.new(_Major=major, _Minor=minor, _Build=build, _Revision=revision)
    self._contract.write(instance, element, namespaces)

  def read(self, element: Element, scope: Mapping[str, str]) -> Tuple[int, ...]:
    """Read a tuple of the parts that are present."""
    instance = self._contract.read(element, scope)
    try:
      parts = [instance[name] for name in ("_Major", "_Minor", "_Build", "_Revision")]
    except KeyError as error:
      raise ContractError(f"version without its {error.args[0]} field") from error
    while parts and parts[-1] == -1:
      parts.pop()
    return tuple(parts)


VERSION = _VersionType()
