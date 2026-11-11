"""Service contracts: the operations of a WCF service and the shape of their messages.

An operation's request is an element named after the operation, in the contract's
namespace, with one child per parameter; its reply is an element named
``<operation>Response`` with a ``<operation>Result`` child for the return value followed by
one child per ``out`` parameter. The Action of a message is the contract's namespace, its
name and the operation name; a reply's Action has ``Response`` appended.

Example::

  CONTROLLER = ServiceContract("IController")
  GET_PEAKS = CONTROLLER.operation("HTFC_GetTopPeakValues", outputs=[("fsc", UINT), ("ssc", UINT)])
  STATE_CHANGED = CONTROLLER.callback("OnSystemStateChanged", [("state", SYSTEM_STATE)])
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from pylabrobot.sartorius.ique.driver.wcf.datacontract import (
  XSI_NAMESPACE,
  Namespaces,
  XmlType,
  read_value,
  write_value,
)
from pylabrobot.sartorius.ique.driver.wcf.errors import ContractError, SoapFault
from pylabrobot.sartorius.ique.driver.wcf.soap import ADDRESSING_NAMESPACE, SOAP_NAMESPACE
from pylabrobot.sartorius.ique.driver.wcf.tree import Attribute, Element, Node, Text, declared
from pylabrobot.sartorius.ique.driver.wcf.values import DictionaryString

DEFAULT_NAMESPACE = "http://tempuri.org/"

# The prefixes an envelope declares around its body.
ENVELOPE_PREFIXES = (("s", SOAP_NAMESPACE), ("a", ADDRESSING_NAMESPACE))

Part = Tuple[str, XmlType]


class _StreamType(XmlType):
  """A ``System.IO.Stream`` parameter or return value of a streamed operation."""

  def __init__(self) -> None:
    """Create the type."""
    super().__init__("base64Binary", "http://www.w3.org/2001/XMLSchema", False, False)

  def write(self, value: Any, element: Element, namespaces: Namespaces) -> None:
    """Refuse: a stream is sent by the streamed client, piece by piece."""
    raise ValueError("a stream can only be sent with StreamedClient.upload")

  def read(self, element: Element, scope: Mapping[str, str]) -> Any:
    """Refuse: a stream is received by the streamed client, piece by piece."""
    raise ContractError("a stream can only be received with StreamedClient.download")


STREAM = _StreamType()


@dataclass(frozen=True)
class Operation:
  """One operation of a service contract.

  Attributes:
    name: The operation name.
    namespace: The namespace of the message elements.
    action: The Action of the request.
    reply_action: The Action of the reply.
    parameters: The input parameters in order, as (name, type) pairs.
    result: The type of the return value, or None for ``void``.
    outputs: The ``out`` parameters in order, as (name, type) pairs.
    one_way: Whether the operation has no reply.
    wrapper: The name of the request element: the operation name, or the name of the
      message contract class the operation takes.
    headers: The parts of a message contract that travel as SOAP headers.
  """

  name: str
  namespace: str
  action: str
  reply_action: str
  parameters: Tuple[Part, ...] = ()
  result: Optional[XmlType] = None
  outputs: Tuple[Part, ...] = ()
  one_way: bool = False
  wrapper: str = ""
  headers: Tuple[Part, ...] = ()

  @property
  def primitive(self) -> bool:
    """Whether WCF serializes the operation with its formatter for all-primitive ones.

    That formatter is used when the operation takes no message contract and no part is
    anything but bool, int, long, float, double, decimal, string, DateTime or byte[]. It
    differs in how it marks a null value.
    """
    parts = [xml_type for _, xml_type in self.parameters + self.outputs]
    parts += [] if self.result is None else [self.result]
    typed = bool(self.headers or self.wrapper)
    return not typed and all(xml_type.primitive for xml_type in parts)

  def _part(self, part: Part, value: Any, namespaces: Namespaces) -> Element:
    """Build the element of one parameter, declaring namespaces as WCF does at this level."""
    name, xml_type = part
    element = namespaces.open(name, self.namespace)
    if self.primitive and value is None:
      if not xml_type.nullable:
        raise ValueError(f"{self.name}: parameter {name} cannot be None")
      prefix = namespaces.declare(element, XSI_NAMESPACE, attribute=True)
      nil = Attribute(DictionaryString("nil"), Text(True), prefix, XSI_NAMESPACE)
      element.attributes.insert(0, nil)
    else:
      if xml_type.composite and xml_type.namespace not in ("", self.namespace):
        namespaces.declare(element, xml_type.namespace)
      if xml_type.composite and value is not None:
        namespaces.bind(element, "i", XSI_NAMESPACE)
      try:
        write_value(xml_type, value, element, namespaces)
      except ValueError as error:
        raise ValueError(f"{self.name}: parameter {name}: {error}") from error
    namespaces.close()
    return element

  def request(self, arguments: Sequence[Any]) -> Tuple[List[Element], Element, Namespaces]:
    """Build the parts of a request message.

    Args:
      arguments: A value per header, then one per parameter. A trailing ``STREAM``
        parameter takes no value here.

    Returns:
      The SOAP headers of the operation's own, the request element still without the
      element of a trailing stream parameter, and the namespace prefixes in force inside
      the request element.
    """
    parts = list(self.headers + self.parameters)
    streamed = bool(parts) and parts[-1][1] is STREAM
    if len(arguments) != len(parts) - streamed:
      raise ValueError(f"{self.name} takes {len(parts) - streamed} arguments, got {len(arguments)}")
    headers: List[Element] = []
    for part, value in zip(self.headers, arguments):
      header = Element(DictionaryString(part[0]), self.namespace, "h")
      header_namespaces = Namespaces(ENVELOPE_PREFIXES)
      header_namespaces.bind(header, "h", self.namespace)
      write_value(part[1], value, header, header_namespaces)
      headers.append(header)
    namespaces = Namespaces(ENVELOPE_PREFIXES)
    body = namespaces.open(self.wrapper or self.name, self.namespace)
    for part, value in zip(parts[len(self.headers) :], arguments[len(self.headers) :]):
      body.children.append(self._part(part, value, namespaces))
    return headers, body, namespaces

  def _wrapper(self, body: Sequence[Node], name: str) -> Optional[Element]:
    """Return the single element of a message body, checking its name."""
    elements = [node for node in body if isinstance(node, Element)]
    if not elements:
      return None
    if len(elements) > 1 or (elements[0].name, elements[0].namespace) != (name, self.namespace):
      raise ContractError(
        f"{self.name}: expected a {name!r} element in {self.namespace!r}, "
        f"found {elements[0].name!r} in {elements[0].namespace!r}"
      )
    return elements[0]

  def _read_parts(
    self, wrapper: Optional[Element], parts: Sequence[Part], scope: Mapping[str, str]
  ) -> List[Any]:
    """Read the values of some parts from the children of a message element."""
    if wrapper is None:
      raise ContractError(f"{self.name}: the message body is empty")
    scope = declared(scope, wrapper)
    values: List[Any] = []
    for name, xml_type in parts:
      element = wrapper.child(name, self.namespace)
      if element is None:
        raise ContractError(f"{self.name}: the message lacks its {name!r} element")
      try:
        values.append(read_value(xml_type, element, scope))
      except ContractError as error:
        raise ContractError(f"{self.name}: {name}: {error}") from error
    return values

  def read_request(self, body: Sequence[Node], scope: Mapping[str, str]) -> List[Any]:
    """Read the parameter values from the body of a request, as a receiver of the call.

    Args:
      body: The children of the SOAP Body.
      scope: The namespace declarations in force inside the Body, by prefix.
    """
    if not self.parameters and not any(isinstance(node, Element) for node in body):
      return []
    wrapper = self._wrapper(body, self.wrapper or self.name)
    return self._read_parts(wrapper, self.parameters, scope)

  def read_reply(self, body: Sequence[Node], scope: Mapping[str, str]) -> Any:
    """Read what a call returns from the body of its reply.

    Args:
      body: The children of the SOAP Body.
      scope: The namespace declarations in force inside the Body, by prefix.

    Returns:
      None for an operation without return value or ``out`` parameters; the return value
      for one without ``out`` parameters; else a tuple of the return value, if the
      operation has one, and the ``out`` parameters in order.
    """
    parts = list(self.outputs)
    if self.result is not None:
      parts.insert(0, (self.name + "Result", self.result))
    wrapper = self._wrapper(body, self.name + "Response")
    if not parts:
      return None
    values = self._read_parts(wrapper, parts, scope)
    return tuple(values) if self.outputs else values[0]


class ServiceContract:
  """A WCF service contract: a name and namespace that its operations share."""

  def __init__(self, name: str, namespace: str = DEFAULT_NAMESPACE) -> None:
    """Create the contract.

    Args:
      name: The contract name, by default the name of the .NET interface.
      namespace: The contract namespace.
    """
    self.name = name
    self.namespace = namespace
    self._base = namespace + ("" if namespace.endswith("/") else "/") + name + "/"

  def operation(
    self,
    name: str,
    parameters: Sequence[Part] = (),
    result: Optional[XmlType] = None,
    outputs: Sequence[Part] = (),
    wrapper: str = "",
    headers: Sequence[Part] = (),
  ) -> Operation:
    """Describe a request-reply operation.

    Args:
      name: The operation name.
      parameters: The input parameters in order.
      result: The type of the return value, or None for ``void``.
      outputs: The ``out`` parameters in order.
      wrapper: For an operation that takes a message contract, the name of that class.
      headers: For an operation that takes a message contract, its header members.
    """
    return Operation(
      name=name,
      namespace=self.namespace,
      action=self._base + name,
      reply_action=self._base + name + "Response",
      parameters=tuple(parameters),
      result=result,
      outputs=tuple(outputs),
      wrapper=wrapper,
      headers=tuple(headers),
    )

  def callback(self, name: str, parameters: Sequence[Part] = ()) -> Operation:
    """Describe a one-way operation of the callback contract, which the service calls."""
    return Operation(
      name=name,
      namespace=self.namespace,
      action=self._base + name,
      reply_action="",
      parameters=tuple(parameters),
      one_way=True,
    )

  def fault_action(self, operation: Operation, detail: XmlType) -> str:
    """Return the Action of the fault an operation declares for a detail type."""
    return f"{operation.action}{detail.name}Fault"


def read_fault_detail(fault: SoapFault, xml_type: XmlType) -> Any:
  """Read the detail of a fault as the type its fault contract declares.

  Raises:
    ContractError: The fault has no detail element, or it is not of that type.
  """
  if fault.detail is None:
    raise ContractError(f"the fault {fault} has no detail element")
  return read_value(xml_type, fault.detail, {})
