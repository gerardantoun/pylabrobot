"""SOAP 1.2 envelopes with WS-Addressing 1.0 headers, laid out as WCF lays them out.

A request carries, in this order, the Action, any headers of the operation's own, a
MessageID, a ReplyTo with the anonymous address, and the To address; its reply carries the
Action and a RelatesTo that repeats the request's MessageID. A one-way message has neither
MessageID nor ReplyTo. A failed request is answered with a ``Fault`` element in the body,
which :func:`fault_of` turns into a :class:`SoapFault`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pylabrobot.sartorius.ique.driver.wcf.dictionary import StaticString
from pylabrobot.sartorius.ique.driver.wcf.errors import ContractError, NbfxError, SoapFault
from pylabrobot.sartorius.ique.driver.wcf.tree import (
  Attribute,
  Element,
  Node,
  Text,
  Xmlns,
  declared,
  resolve,
)
from pylabrobot.sartorius.ique.driver.wcf.values import DictionaryString, UniqueId, text_of

SOAP_NAMESPACE = "http://www.w3.org/2003/05/soap-envelope"
ADDRESSING_NAMESPACE = "http://www.w3.org/2005/08/addressing"
ANONYMOUS_ADDRESS = "http://www.w3.org/2005/08/addressing/anonymous"

_SOAP = StaticString(SOAP_NAMESPACE)
_ADDRESSING = StaticString(ADDRESSING_NAMESPACE)
_MUST_UNDERSTAND = StaticString("mustUnderstand")
_URN_UUID = "urn:uuid:"

# The addressing headers a receiver has to understand to process a message at all.
_UNDERSTOOD = frozenset(("Action", "To", "MessageID", "RelatesTo", "ReplyTo", "FaultTo", "From"))


@dataclass
class Message:
  """A SOAP message.

  Attributes:
    action: The Action URI, which names the operation and the direction.
    body: The children of the Body element; for an operation, its single wrapper element.
    message_id: The MessageID of a request that expects a reply, as ``urn:uuid:...``.
    relates_to: The MessageID of the request a reply answers.
    to: The address the message is for.
    reply_to: The address a reply should go to; on a connection, the anonymous address.
    headers: The other headers, such as those a message contract defines.
  """

  action: str
  body: List[Node] = field(default_factory=list)
  message_id: Optional[str] = None
  relates_to: Optional[str] = None
  to: Optional[str] = None
  reply_to: Optional[str] = None
  headers: List[Element] = field(default_factory=list)


def new_message_id() -> str:
  """Return a fresh MessageID."""
  return _URN_UUID + str(uuid.uuid4())


def _soap(name: str, children: List[Node], attributes: Optional[List[Xmlns]] = None) -> Element:
  """Build an element of the SOAP envelope namespace."""
  return Element(StaticString(name), SOAP_NAMESPACE, "s", list(attributes or []), children)


def _header(name: str, text: Text, must_understand: bool = False) -> Element:
  """Build an addressing header that holds text."""
  flag = Attribute(_MUST_UNDERSTAND, Text("1"), "s", SOAP_NAMESPACE)
  attributes = [flag] if must_understand else []
  return Element(StaticString(name), ADDRESSING_NAMESPACE, "a", list(attributes), [text])


def _identifier(value: str) -> Text:
  """Build the text of a MessageID or RelatesTo: binary for the usual UUID form."""
  if value.startswith(_URN_UUID):
    try:
      return Text(UniqueId(value[len(_URN_UUID) :]))
    except ValueError:
      return Text(value)
  return Text(value)


def to_envelope(message: Message) -> Element:
  """Build the Envelope element of a message, with the headers in WCF's order."""
  headers: List[Node] = [_header("Action", Text(DictionaryString(message.action)), True)]
  headers += message.headers
  if message.relates_to is not None:
    headers.append(_header("RelatesTo", _identifier(message.relates_to)))
  if message.message_id is not None:
    headers.append(_header("MessageID", _identifier(message.message_id)))
  if message.reply_to is not None:
    is_anonymous = message.reply_to == ANONYMOUS_ADDRESS
    address = StaticString(message.reply_to) if is_anonymous else message.reply_to
    reply_to = Element(StaticString("Address"), ADDRESSING_NAMESPACE, "a", [], [Text(address)])
    headers.append(Element(StaticString("ReplyTo"), ADDRESSING_NAMESPACE, "a", [], [reply_to]))
  if message.to is not None:
    is_anonymous = message.to == ANONYMOUS_ADDRESS
    to = StaticString(message.to) if is_anonymous else DictionaryString(message.to)
    headers.append(_header("To", Text(to), True))
  namespaces = [Xmlns("s", _SOAP), Xmlns("a", _ADDRESSING)]
  return _soap("Envelope", [_soap("Header", headers), _soap("Body", message.body)], namespaces)


def _characters(element: Element) -> str:
  """Return the character content of an element."""
  content = element.content()
  return "" if content is None else text_of(content)


def _must_understand(header: Element) -> bool:
  """Tell whether a header is marked as mandatory to understand."""
  flag = header.attribute("mustUnderstand", SOAP_NAMESPACE)
  return flag is not None and flag.characters in ("1", "true")


def from_envelope(envelope: Element) -> Message:
  """Read a message from its Envelope element.

  Raises:
    ContractError: The element is not a SOAP 1.2 envelope with an Action, or carries a
      header that must be understood and is not an addressing header.
  """
  if (envelope.name, envelope.namespace) != ("Envelope", SOAP_NAMESPACE):
    raise ContractError(f"not a SOAP 1.2 envelope: {envelope.name!r} in {envelope.namespace!r}")
  body = envelope.child("Body", SOAP_NAMESPACE)
  if body is None:
    raise ContractError("SOAP envelope without a Body")
  message = Message(action="", body=list(body.children))
  action: Optional[str] = None
  header = envelope.child("Header", SOAP_NAMESPACE)
  for item in header.elements() if header is not None else ():
    try:
      if item.namespace != ADDRESSING_NAMESPACE or item.name not in _UNDERSTOOD:
        if _must_understand(item):
          raise ContractError(f"header {item.name!r} in {item.namespace!r} must be understood")
        message.headers.append(item)
      elif item.name == "Action":
        action = _characters(item)
      elif item.name == "To":
        message.to = _characters(item)
      elif item.name == "MessageID":
        message.message_id = _characters(item)
      elif item.name == "RelatesTo":
        message.relates_to = _characters(item)
      elif item.name == "ReplyTo":
        address = item.child("Address", ADDRESSING_NAMESPACE)
        message.reply_to = None if address is None else _characters(address)
    except NbfxError as error:
      raise ContractError(f"malformed {item.name} header: {error}") from error
  if action is None:
    raise ContractError("SOAP envelope without an Action header")
  message.action = action
  return message


def _qualified(element: Element, scope: Dict[str, str]) -> Tuple[str, str]:
  """Read an element whose content is a qualified name, as (namespace, name)."""
  prefix, _, name = _characters(element).strip().rpartition(":")
  return resolve(declared(scope, element), prefix, default=True), name


def fault_of(envelope: Element, message: Message) -> Optional[SoapFault]:
  """Return the fault a message carries, or None when its body is not a fault.

  Args:
    envelope: The Envelope element; its namespace declarations qualify the fault codes.
    message: The message read from it.

  Raises:
    ContractError: The Fault element lacks its code.
  """
  fault = next((item for item in message.body if isinstance(item, Element)), None)
  if fault is None or (fault.name, fault.namespace) != ("Fault", SOAP_NAMESPACE):
    return None
  scope = declared({}, envelope)
  for ancestor in (envelope.child("Body", SOAP_NAMESPACE), fault):
    scope = declared(scope, ancestor) if ancestor is not None else scope
  codes: List[Tuple[str, str]] = []
  level = fault.child("Code", SOAP_NAMESPACE)
  try:
    while level is not None:
      scope = declared(scope, level)
      value = level.child("Value", SOAP_NAMESPACE)
      if value is None:
        raise ContractError("SOAP fault code without a Value")
      codes.append(_qualified(value, scope))
      level = level.child("Subcode", SOAP_NAMESPACE)
    if not codes:
      raise ContractError("SOAP fault without a Code")
    reason = fault.child("Reason", SOAP_NAMESPACE)
    text = None if reason is None else reason.child("Text", SOAP_NAMESPACE)
    detail = fault.child("Detail", SOAP_NAMESPACE)
    details = [] if detail is None else list(detail.elements())
    return SoapFault(
      code=codes[0],
      subcodes=tuple(codes[1:]),
      reason="" if text is None else _characters(text),
      detail=details[0] if len(details) == 1 else None,
      action=message.action,
    )
  except NbfxError as error:
    raise ContractError(f"malformed SOAP fault: {error}") from error
