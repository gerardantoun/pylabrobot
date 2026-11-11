"""A small XML tree: what a binary XML document decodes to and is encoded from.

The tree keeps what the binary format distinguishes and textual XML does not: the typed
value of each piece of text, the record it was read from, and runs of same-named elements
stored as one array.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Union
from xml.sax.saxutils import escape, quoteattr

from pylabrobot.sartorius.ique.driver.wcf.errors import NbfxError
from pylabrobot.sartorius.ique.driver.wcf.records import RecordType
from pylabrobot.sartorius.ique.driver.wcf.values import Single, TextValue, text_of

XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"


@dataclass
class Text:
  """Character content of an element, or the value of an attribute.

  Attributes:
    value: The typed value; :attr:`characters` gives the characters it stands for.
    record: The record to write the value as. Unset, the writer picks what a .NET writer
      picks. A reader sets it to the record it read, WithEndElement form included; it does
      not take part in comparisons.
  """

  value: TextValue
  record: Optional[RecordType] = field(default=None, compare=False)

  @property
  def characters(self) -> str:
    """The XML characters this text stands for."""
    return text_of(self.value)


@dataclass
class Attribute:
  """An attribute. ``namespace`` is empty for an attribute without a prefix."""

  name: str
  value: Text
  prefix: str = ""
  namespace: str = ""


@dataclass
class Xmlns:
  """A namespace declaration: ``xmlns:prefix="uri"``, or ``xmlns="uri"`` for no prefix."""

  prefix: str
  uri: str


@dataclass
class Comment:
  """An XML comment."""

  text: str


@dataclass
class Element:
  """An element with its attributes and children.

  ``attributes`` holds attributes and namespace declarations in document order. The prefix
  of an element or attribute must be declared, on it or on an ancestor, for its namespace.
  """

  name: str
  namespace: str = ""
  prefix: str = ""
  attributes: List[Union[Attribute, Xmlns]] = field(default_factory=list)
  children: List["Node"] = field(default_factory=list)

  def elements(self) -> Iterator["Element"]:
    """Iterate over the child elements."""
    return (child for child in self.children if isinstance(child, Element))

  def child(self, name: str, namespace: str) -> Optional["Element"]:
    """Return the first child element with this name and namespace, if any."""
    for element in self.elements():
      if element.name == name and element.namespace == namespace:
        return element
    return None

  def attribute(self, name: str, namespace: str = "") -> Optional[Text]:
    """Return the value of the attribute with this name and namespace, if present."""
    for item in self.attributes:
      if isinstance(item, Attribute) and item.name == name and item.namespace == namespace:
        return item.value
    return None

  def content(self) -> Optional[TextValue]:
    """Return the typed character content, or None when the element has none.

    Several text nodes are joined: as bytes when all are bytes, else as characters.

    Raises:
      NbfxError: The element has child elements or arrays.
    """
    if any(not isinstance(child, (Text, Comment)) for child in self.children):
      raise NbfxError(f"element {self.name!r} has element content, not text")
    texts = [child for child in self.children if isinstance(child, Text)]
    if not texts:
      return None
    if len(texts) == 1:
      return texts[0].value
    if all(isinstance(text.value, bytes) for text in texts):
      return b"".join(text.value for text in texts if isinstance(text.value, bytes))
    return "".join(text.characters for text in texts)


@dataclass
class Array:
  """A run of elements of one name, each holding one fixed-size value ([MC-NBFX] 2.3.3).

  Attributes:
    element: The repeated element, without children.
    record: The text record type of the items, such as ``RecordType.FLOAT_TEXT``.
    values: The item values. A reader gives an ``array.array`` for 16, 32 and 64-bit
      integers, floats and doubles, and a list for the other types.
  """

  element: Element
  record: RecordType
  values: Sequence[Any]


Node = Union[Element, Text, Comment, Array]


def declared(scope: Mapping[str, str], element: Element) -> Dict[str, str]:
  """Return the namespace declarations in force inside an element.

  Args:
    scope: The declarations in force around the element, by prefix.
    element: The element, whose own declarations are added.
  """
  own = {item.prefix: item.uri for item in element.attributes if isinstance(item, Xmlns)}
  return {**scope, **own}


def resolve(scope: Mapping[str, str], prefix: str, default: bool) -> str:
  """Return the namespace a prefix stands for.

  Args:
    scope: The namespace declarations in force, by prefix.
    prefix: The prefix to resolve.
    default: Whether an empty prefix takes the default namespace (as for elements and
      for qualified names in text) or none (as for attributes).

  Raises:
    NbfxError: The prefix is not declared.
  """
  if not prefix:
    return scope.get("", "") if default else ""
  if prefix == "xml":
    return XML_NAMESPACE
  if prefix not in scope:
    raise NbfxError(f"namespace prefix {prefix!r} is not declared")
  return scope[prefix]


def to_xml(node: Node) -> str:
  """Return the XML characters a node stands for, as [MC-NBFX] defines them per record.

  Meant for logs and for comparing documents: two trees with the same characters carry
  the same information, whatever records they were read from.
  """
  if isinstance(node, Text):
    return escape(node.characters)
  if isinstance(node, Comment):
    return f"<!--{node.text}-->"
  if isinstance(node, Array):
    values = map(Single, node.values) if node.record == RecordType.FLOAT_TEXT else node.values
    one = node.element
    return "".join(
      to_xml(Element(one.name, one.namespace, one.prefix, one.attributes, [Text(value)]))
      for value in values
    )
  name = f"{node.prefix}:{node.name}" if node.prefix else node.name
  parts = [name]
  for item in node.attributes:
    if isinstance(item, Xmlns):
      parts.append(f"xmlns:{item.prefix}" if item.prefix else "xmlns")
      parts[-1] += f"={quoteattr(item.uri)}"
    else:
      qualified = f"{item.prefix}:{item.name}" if item.prefix else item.name
      parts.append(f"{qualified}={quoteattr(item.value.characters)}")
  body = "".join(to_xml(child) for child in node.children)
  return f"<{' '.join(parts)}>{body}</{name}>"
