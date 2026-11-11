"""Tests for SOAP 1.2 envelopes with WS-Addressing headers."""

import unittest
from typing import List, Optional

from pylabrobot.sartorius.ique.driver.wcf import nmf, recordings, soap
from pylabrobot.sartorius.ique.driver.wcf.dictionary import (
  ReaderSession,
  WriterSession,
  static_key,
  static_lookup,
)
from pylabrobot.sartorius.ique.driver.wcf.errors import ContractError, SoapFault
from pylabrobot.sartorius.ique.driver.wcf.nbfx import decode, encode
from pylabrobot.sartorius.ique.driver.wcf.recordings import sample
from pylabrobot.sartorius.ique.driver.wcf.soap import (
  ADDRESSING_NAMESPACE,
  ANONYMOUS_ADDRESS,
  SOAP_NAMESPACE,
  Message,
)
from pylabrobot.sartorius.ique.driver.wcf.tree import Attribute, Element, Node, Text, Xmlns, to_xml
from pylabrobot.sartorius.ique.driver.wcf.values import DictionaryString

_DISPATCHER = "http://schemas.microsoft.com/net/2005/12/windowscommunicationfoundation/dispatcher"
_ID = "urn:uuid:cc50a80f-665f-4036-ba7a-b931e8fdb6e6"


def _soap(name: str, children: List[Node], attributes: Optional[List[Xmlns]] = None) -> Element:
  """Build an element of the SOAP envelope namespace with prefix ``s``."""
  return Element(name, SOAP_NAMESPACE, "s", list(attributes or []), children)


def _addressing(name: str, value: str, must_understand: bool = False) -> Element:
  """Build an addressing header with prefix ``a``."""
  flags = [Attribute("mustUnderstand", Text("1"), "s", SOAP_NAMESPACE)] if must_understand else []
  return Element(name, ADDRESSING_NAMESPACE, "a", list(flags), [Text(value)])


def _envelope(headers: List[Element], body: List[Node]) -> Element:
  """Build an envelope that declares ``s`` and ``a`` as WCF does."""
  declarations = [Xmlns("s", SOAP_NAMESPACE), Xmlns("a", ADDRESSING_NAMESPACE)]
  return _soap("Envelope", [_soap("Header", list(headers)), _soap("Body", body)], declarations)


def _fault(*parts: Node) -> Element:
  """Build an envelope whose body is a SOAP fault with the given parts."""
  return _envelope([_addressing("Action", "urn:fault")], [_soap("Fault", list(parts))])


def _value(text: str, declarations: Optional[List[Xmlns]] = None) -> Element:
  """Build the Value of a fault code."""
  return _soap("Value", [Text(text)], declarations)


def _flag(value: str) -> List[Attribute]:
  """Return a mustUnderstand attribute with a given value."""
  return [Attribute("mustUnderstand", Text(value), "s", SOAP_NAMESPACE)]


def _fault_of(envelope: Element) -> Optional[SoapFault]:
  """Read the fault an envelope carries, as a receiver does."""
  return soap.fault_of(envelope, soap.from_envelope(envelope))


def _required_fault(envelope: Element) -> SoapFault:
  """Read the fault an envelope must carry."""
  fault = _fault_of(envelope)
  if fault is None:
    raise AssertionError("the envelope carries no fault")
  return fault


def _recorded_faults(name: str, side: str = "s") -> List[SoapFault]:
  """Return the faults one side of a recording sent, from all its connections."""
  faults: List[SoapFault] = []
  for connection in recordings.load(name).connections:
    for envelope in recordings.envelopes(connection, side):
      fault = _fault_of(envelope)
      if fault is not None:
        faults.append(fault)
  return faults


class RecordedEnvelopeTests(unittest.TestCase):
  """Every envelope .NET sent in the recordings, read and written back."""

  def test_sized_envelopes_write_back_to_the_same_bytes(self) -> None:
    """The headers WCF writes, in its order, with its dictionary references."""
    for name in ("net48_duplex", "net48_duplex_worklist", "net48_duplex_abort"):
      connection = recordings.load(name).connections[0]
      for side in "cs":
        reader, writer = ReaderSession(), WriterSession()
        for index, record in enumerate(nmf.split_records(connection.sent_by(side))):
          if record.type != nmf.FramingRecord.SIZED_ENVELOPE:
            continue
          with self.subTest(recording=name, side=side, record=index):
            document = reader.read_string_table(record.payload)
            envelope = decode(document, reader.lookup)
            message = soap.from_envelope(envelope)
            rebuilt = soap.to_envelope(message)
            if message.action == _DISPATCHER + "/fault":
              # The dispatcher writes the Action of a fault it makes itself as characters,
              # never as a string of the session table.
              self.assertNotIsInstance(message.action, DictionaryString)
              self.assertEqual(to_xml(rebuilt), to_xml(envelope))
              rebuilt = envelope
            written = encode(rebuilt, writer.key_of)
            self.assertEqual(writer.string_table() + written, record.payload)

  def test_unsized_envelopes_write_back_to_the_same_bytes(self) -> None:
    """The streamed endpoint's messages use the static table only."""
    for connection in recordings.load("net48_streamed").connections:
      for side in "cs":
        for record in nmf.split_records(connection.sent_by(side)):
          if record.type == nmf.FramingRecord.UNSIZED_ENVELOPE:
            message = soap.from_envelope(decode(record.payload, static_lookup))
            self.assertEqual(encode(soap.to_envelope(message), static_key), record.payload)

  def test_request_and_reply_headers(self) -> None:
    """A request carries its MessageID, ReplyTo and To; its reply relates to the MessageID."""
    connection = recordings.load("net48_duplex_abort").connections[0]
    request = soap.from_envelope(recordings.envelopes(connection, "c")[0])
    reply = soap.from_envelope(recordings.envelopes(connection, "s")[0])
    self.assertEqual(request.action, "http://tempuri.org/IController/Controller_Is1536Capable")
    self.assertEqual(request.message_id, _ID)
    self.assertEqual(request.reply_to, ANONYMOUS_ADDRESS)
    self.assertEqual(request.to, connection.address)
    self.assertIsNone(request.relates_to)
    self.assertEqual(reply.action, request.action + "Response")
    self.assertEqual(reply.relates_to, _ID)
    self.assertEqual(reply.to, ANONYMOUS_ADDRESS)
    self.assertEqual((reply.message_id, reply.reply_to, reply.headers), (None, None, []))

  def test_message_contract_header_is_kept(self) -> None:
    """A header of the operation's own travels between the Action and the MessageID."""
    connection = recordings.load("net48_streamed").connections[9]
    message = soap.from_envelope(recordings.envelopes(connection, "c")[0])
    self.assertEqual(len(message.headers), 1)
    self.assertEqual(
      to_xml(message.headers[0]),
      '<h:FileName xmlns:h="http://tempuri.org/">firmware.bin</h:FileName>',
    )


class RecordedFaultTests(unittest.TestCase):
  """The faults .NET services sent in the recordings."""

  def test_internal_service_fault(self) -> None:
    """An exception the contract does not declare: a Receiver fault with a dispatcher subcode."""
    (fault,) = _recorded_faults("net48_duplex_abort")
    self.assertEqual(fault.code, (SOAP_NAMESPACE, "Receiver"))
    self.assertEqual(fault.subcodes, ((_DISPATCHER, "InternalServiceFault"),))
    self.assertTrue(fault.reason.startswith("The server was unable to process the request"))
    self.assertIsNone(fault.detail)
    self.assertEqual(fault.action, _DISPATCHER + "/fault")
    self.assertTrue(str(fault).startswith("Receiver/InternalServiceFault: The server was"))

  def test_declared_faults(self) -> None:
    """A fault contract: a Sender fault whose detail is the serialized contract."""
    faults = _recorded_faults("net48_duplex") + _recorded_faults("net48_streamed")
    self.assertEqual(
      [(fault.code, fault.subcodes, fault.reason) for fault in faults],
      [
        ((SOAP_NAMESPACE, "Sender"), (), "Sampler is not homed"),
        (
          (SOAP_NAMESPACE, "Sender"),
          (),
          "No FCS file for worklist 00000000-0000-0000-0000-000000000000",
        ),
        ((SOAP_NAMESPACE, "Sender"), (), "No calibration settings stored"),
      ],
    )
    self.assertEqual(
      faults[0].action, "http://tempuri.org/IController/Sampler_HomeControllerServiceExceptionFault"
    )
    exception = sample.CONTROLLER_SERVICE_EXCEPTION
    for fault in faults:
      self.assertEqual(
        (fault.detail.name, fault.detail.namespace) if fault.detail is not None else None,
        (exception.name, exception.namespace),
      )


class EnvelopeTests(unittest.TestCase):
  """Envelopes built and read in code."""

  def test_specification_example(self) -> None:
    """The example of [MC-NBFS] section 3: an Action and a body."""
    data = bytes.fromhex(
      "56020b0161060b0173045608440a1e00829906616374696f6e01560e4009496e76656e746f7279810101"
    )
    message = soap.from_envelope(decode(data, static_lookup))
    self.assertEqual(message.action, "action")
    self.assertEqual((message.message_id, message.to, message.headers), (None, None, []))
    self.assertEqual([to_xml(node) for node in message.body], ["<Inventory>0</Inventory>"])

  def test_one_way_message_has_no_message_id_or_reply_to(self) -> None:
    """Without a MessageID there is nothing to reply to."""
    envelope = soap.to_envelope(Message("urn:a", [Element("Op")], to="net.tcp://h/e"))
    self.assertEqual(
      to_xml(envelope),
      '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
      'xmlns:a="http://www.w3.org/2005/08/addressing"><s:Header>'
      '<a:Action s:mustUnderstand="1">urn:a</a:Action>'
      '<a:To s:mustUnderstand="1">net.tcp://h/e</a:To></s:Header>'
      "<s:Body><Op></Op></s:Body></s:Envelope>",
    )

  def test_message_round_trip(self) -> None:
    """Every field survives writing and reading, including identifiers that are no UUIDs."""
    custom = Element("Custom", "urn:c", "", [Xmlns("", "urn:c")], [Text("x")])
    for message_id in (_ID, "urn:custom:42"):
      message = Message(
        action="urn:op",
        body=[Element("Op")],
        message_id=message_id,
        relates_to="urn:custom:7",
        to="net.tcp://h/e",
        reply_to="net.tcp://client/reply",
        headers=[custom],
      )
      envelope = decode(encode(soap.to_envelope(message), static_key), static_lookup)
      self.assertEqual(soap.from_envelope(envelope), message)

  def test_malformed_envelopes_are_rejected(self) -> None:
    """Only a SOAP 1.2 envelope with a Body and an Action is a message."""
    action = _addressing("Action", "urn:a")
    soap11 = Element("Envelope", "http://schemas.xmlsoap.org/soap/envelope/", "s")
    cases = {
      "SOAP 1.1": soap11,
      "no Body": _soap("Envelope", [_soap("Header", [action])]),
      "no Action": _envelope([], []),
      "mandatory unknown header": _envelope(
        [action, Element("Lock", "urn:x", "x", [Xmlns("x", "urn:x"), *_flag("true")])], []
      ),
    }
    for name, envelope in cases.items():
      with self.subTest(name):
        with self.assertRaises(ContractError):
          soap.from_envelope(envelope)

  def test_optional_unknown_header_is_kept(self) -> None:
    """A header that need not be understood is passed on."""
    extra = Element("Hint", "urn:x", "x", [Xmlns("x", "urn:x"), *_flag("0")], [Text("h")])
    message = soap.from_envelope(_envelope([_addressing("Action", "urn:a"), extra], []))
    self.assertEqual(message.headers, [extra])


class FaultTests(unittest.TestCase):
  """Faults built in code."""

  def test_codes_are_qualified_names_in_scope(self) -> None:
    """A subcode's prefix may be declared on its own Value element."""
    custom = [Xmlns("c", "urn:codes")]
    code = _soap("Code", [_value("s:Sender"), _soap("Subcode", [_value("c:Busy", custom)])])
    reason = _soap("Reason", [_soap("Text", [Text("try later")])])
    fault = _required_fault(_fault(code, reason))
    self.assertEqual(fault.code, (SOAP_NAMESPACE, "Sender"))
    self.assertEqual(fault.subcodes, (("urn:codes", "Busy"),))
    self.assertEqual((fault.reason, fault.detail, fault.action), ("try later", None, "urn:fault"))

  def test_detail_with_one_element_is_kept(self) -> None:
    """The detail of a declared fault is its single child; several children are not one."""
    code = _soap("Code", [_value("s:Receiver")])
    one = _soap("Detail", [Element("Info")])
    two = _soap("Detail", [Element("Info"), Element("More")])
    first = _required_fault(_fault(code, one))
    self.assertEqual(first.detail, Element("Info"))
    self.assertIsNone(_required_fault(_fault(code, two)).detail)
    self.assertEqual(first.reason, "")

  def test_body_without_fault_is_no_fault(self) -> None:
    """An ordinary reply, or an empty body, carries no fault."""
    self.assertIsNone(_fault_of(_envelope([_addressing("Action", "urn:a")], [Element("Reply")])))
    self.assertIsNone(_fault_of(_envelope([_addressing("Action", "urn:a")], [])))

  def test_malformed_faults_are_rejected(self) -> None:
    """A fault needs a Code with a Value whose prefix is declared."""
    cases = {
      "no Code": _fault(_soap("Reason", [])),
      "Code without Value": _fault(_soap("Code", [])),
      "undeclared prefix": _fault(_soap("Code", [_value("x:Sender")])),
      "Subcode without Value": _fault(_soap("Code", [_value("s:Sender"), _soap("Subcode", [])])),
    }
    for name, envelope in cases.items():
      with self.subTest(name):
        with self.assertRaises(ContractError):
          _fault_of(envelope)


if __name__ == "__main__":
  unittest.main()
