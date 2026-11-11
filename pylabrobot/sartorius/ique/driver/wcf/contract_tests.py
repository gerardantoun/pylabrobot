"""Tests for service contracts: the messages of operations, checked against .NET's.

The recordings hold what a genuine .NET client and service sent each other for the
contract of ``recordings/sample.py``, with what each printed: the client every result and
callback, the service every call. The tests build each request from the arguments the
.NET client was called with and require the bytes it sent; they read every reply and
callback and require the values the client printed; they read every request back as the
service does and require what the service printed; and they write every callback again
and require the bytes the service sent.
"""

import datetime
import decimal
import math
import unittest
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pylabrobot.sartorius.ique.driver.wcf import nmf, recordings, soap
from pylabrobot.sartorius.ique.driver.wcf.contract import (
  STREAM,
  Operation,
  ServiceContract,
  read_fault_detail,
)
from pylabrobot.sartorius.ique.driver.wcf.datacontract import (
  BOOL,
  FLOAT,
  INT,
  STRING,
  VERSION,
  DataObject,
  DictionaryOf,
  EnumType,
  ListOf,
  Namespaces,
  Nullable,
  XmlType,
)
from pylabrobot.sartorius.ique.driver.wcf.dictionary import (
  ReaderSession,
  WriterSession,
  static_key,
)
from pylabrobot.sartorius.ique.driver.wcf.errors import ContractError, SoapFault
from pylabrobot.sartorius.ique.driver.wcf.nbfx import decode, encode
from pylabrobot.sartorius.ique.driver.wcf.recordings import sample as s
from pylabrobot.sartorius.ique.driver.wcf.tree import Element, Node, Text, declared, to_xml
from pylabrobot.sartorius.ique.driver.wcf.values import DateTime, TimeSpan, to_single

D = decimal.Decimal


def _single(value: float) -> str:
  """Format a single-precision value as .NET Framework's "R" format does."""
  if math.isnan(value):
    return "NaN"
  if math.isinf(value):
    return "Infinity" if value > 0 else "-Infinity"
  text = f"{value:.7G}"
  return text if to_single(float(text)) == value else f"{value:.9G}"


def _render(value: Any, xml_type: Optional[XmlType] = None) -> str:
  """Render a value as the recorded .NET programs print it (``Dump.Of``)."""
  if value is None:
    return "null"
  if isinstance(xml_type, Nullable):
    return _render(value, xml_type.inner)
  if isinstance(xml_type, EnumType):
    return f"{xml_type.name}.{value}"
  if isinstance(value, DataObject):
    members = [
      (name, member) for level in value.contract.lineage() for name, member in level.declared
    ]
    parts = [f"{name}={_render(value[name], member)}" for name, member in members]
    return f"{value.contract.name}{{{','.join(parts)}}}"
  if isinstance(xml_type, DictionaryOf):
    entries = [f"{_render(k, xml_type.key)}:{_render(v, xml_type.value)}" for k, v in value.items()]
    return "{" + ",".join(entries) + "}"
  if isinstance(xml_type, ListOf):
    return "[" + ",".join(_render(item, xml_type.item) for item in value) + "]"
  if xml_type is VERSION:
    return "Version(" + ".".join(map(str, value)) + ")"
  if isinstance(value, str):
    return f'"{value}"'
  if isinstance(value, bool):
    return "true" if value else "false"
  if isinstance(value, datetime.datetime):
    exact = DateTime.from_datetime(value)
    return f"DateTime({exact.ticks},{exact.kind.capitalize()})"
  if isinstance(value, datetime.timedelta):
    return f"TimeSpan({TimeSpan.from_timedelta(value).ticks})"
  if isinstance(value, uuid.UUID):
    return f"Guid({value})"
  if isinstance(value, decimal.Decimal):
    return f"{format(value, 'f')}m"
  if isinstance(value, float):
    return _single(value) + "f" if xml_type is FLOAT else repr(value) + "d"
  return str(value)


def _render_all(items: Sequence[Any], types: Sequence[XmlType]) -> str:
  """Render several values, each by its own type, separated by commas."""
  return ",".join(_render(item, xml_type) for item, xml_type in zip(items, types))


def _render_reply(operation: Operation, value: Any) -> str:
  """Render what a call returns; a return value with out parameters as a list."""
  if not operation.outputs:
    return _render(value, operation.result)
  types = ([] if operation.result is None else [operation.result]) + [
    t for _, t in operation.outputs
  ]
  return "[" + _render_all(value, types) + "]"


def _describe(detail: DataObject) -> str:
  """Render a ControllerServiceException with its inner exceptions, as the client logs it."""
  inner = detail["InnerException"]
  text = f"{detail['Type']}: {detail['Message']}"
  return text if inner is None else f"{text} <- {_describe(inner)}"


def _body_scope(envelope: Element) -> Dict[str, str]:
  """Return the namespace declarations in force inside the Body of an envelope."""
  body = envelope.child("Body", soap.SOAP_NAMESPACE)
  scope = declared({}, envelope)
  return scope if body is None else declared(scope, body)


def _entries(lines: Sequence[str]) -> List[str]:
  """Group logged lines into entries: a value with a line break continues on the next line."""
  entries: List[str] = []
  for line in lines:
    if line.startswith(("result:", "callback:", "call:", "done")) or not entries:
      entries.append(line)
    else:
      entries[-1] += "\n" + line
  return entries


_SCENARIOS: Dict[str, List[s.Call]] = {
  "net48_duplex": s.DUPLEX_CALLS,
  "net48_duplex_worklist": s.WORKLIST_CALLS,
  "net48_duplex_abort": s.ABORT_CALLS,
}
_CALLBACKS: Dict[str, Operation] = {operation.action: operation for operation in s.CALLBACKS}


def _envelopes(connection: recordings.Connection, side: str) -> List[bytes]:
  """Return the payloads of the sized envelopes one side of a connection sent."""
  records = nmf.split_records(connection.sent_by(side))
  return [record.payload for record in records if record.type == nmf.FramingRecord.SIZED_ENVELOPE]


def _request(operation: Operation, arguments: Sequence[Any], message_id: str, to: str) -> Element:
  """Build the envelope of a request as a client sends it."""
  headers, body, _ = operation.request(arguments)
  message = soap.Message(
    operation.action, [body], message_id, None, to, soap.ANONYMOUS_ADDRESS, headers
  )
  return soap.to_envelope(message)


class _Exchange:
  """The calls of a recorded duplex session, read and written as each side did."""

  def __init__(self, name: str) -> None:
    """Load a recording and pair its replies with the calls of its scenario."""
    self.connection = recordings.load(name).connections[0]
    self.notes = recordings.load(name).notes
    self.calls = _SCENARIOS[name]
    self.requests = recordings.envelopes(self.connection, "c")
    self.message_ids = [soap.from_envelope(envelope).message_id or "" for envelope in self.requests]
    self.operations = {
      message_id: operation for message_id, (operation, _) in zip(self.message_ids, self.calls)
    }

  def client_log(self) -> Tuple[List[str], List[Tuple[Operation, SoapFault]]]:
    """Read every message the service sent as the client does.

    Returns:
      The lines the client prints for results and callbacks, and the faults it received.
    """
    lines: List[str] = []
    faults: List[Tuple[Operation, SoapFault]] = []
    for envelope in recordings.envelopes(self.connection, "s"):
      message, scope = soap.from_envelope(envelope), _body_scope(envelope)
      if message.relates_to is None:
        callback = _CALLBACKS[message.action]
        arguments = callback.read_request(message.body, scope)
        types = [xml_type for _, xml_type in callback.parameters]
        lines.append(f"callback: {callback.name}({_render_all(arguments, types)})")
        continue
      operation = self.operations[message.relates_to]
      fault = soap.fault_of(envelope, message)
      if fault is not None:
        faults.append((operation, fault))
        continue
      if message.action != operation.reply_action:
        raise ContractError(f"reply {message.action} to {operation.name}")
      value = operation.read_reply(message.body, scope)
      if operation.result is not None or operation.outputs:
        lines.append(f"result: {operation.name} -> {_render_reply(operation, value)}")
    return lines, faults


class RequestTests(unittest.TestCase):
  """Requests built from arguments, against what the .NET client sent."""

  def test_requests_are_the_bytes_dotnet_sent(self) -> None:
    """Every request of every duplex recording, string table included."""
    for name in _SCENARIOS:
      exchange = _Exchange(name)
      writer = WriterSession()
      payloads = _envelopes(exchange.connection, "c")
      self.assertEqual(len(payloads), len(exchange.calls))
      for index, ((operation, arguments), message_id, payload) in enumerate(
        zip(exchange.calls, exchange.message_ids, payloads)
      ):
        with self.subTest(recording=name, call=index, operation=operation.name):
          envelope = _request(operation, arguments, message_id, exchange.connection.address)
          document = encode(envelope, writer.key_of)
          self.assertEqual(writer.string_table() + document, payload)

  def test_streamed_requests_are_the_bytes_dotnet_sent(self) -> None:
    """Requests without a stream to the streamed endpoint, with the static table only."""
    connections = recordings.load("net48_streamed").connections
    calls = {
      0: (s.GET_CURRENT_FCS_FILE, ()),
      1: (s.GET_COMPLETED_WORKLIST_FCS_FILE, (s.COMPLETED_WORKLIST_ID,)),
      2: (s.GET_COMPLETED_WORKLIST_FCS_FILE, (uuid.UUID(int=0),)),
      5: (s.CLEAR_CALIBRATION_SETTINGS, ("recalibrated",)),
      6: (s.CLEAR_CALIBRATION_SETTINGS, (None,)),
      7: (s.CLEAR_CALIBRATION_SETTINGS, ("",)),
      8: (s.DOWNLOAD_CALIBRATION_SETTINGS, ()),
    }
    for index, (operation, arguments) in calls.items():
      with self.subTest(call=index, operation=operation.name):
        connection = connections[index]
        (recorded,) = recordings.envelopes(connection, "c")
        message_id = soap.from_envelope(recorded).message_id or ""
        envelope = _request(operation, arguments, message_id, connection.address)
        (record,) = [
          record
          for record in nmf.split_records(connection.sent_by("c"))
          if record.type == nmf.FramingRecord.UNSIZED_ENVELOPE
        ]
        self.assertEqual(encode(envelope, static_key), record.payload)

  def test_message_contract_puts_its_header_members_in_the_header(self) -> None:
    """A message contract's header member travels as a SOAP header, its body in the wrapper."""
    headers, body, _ = s.UPLOAD_FIRMWARE.request(("firmware.bin",))
    connection = recordings.load("net48_streamed").connections[9]
    recorded = soap.from_envelope(recordings.envelopes(connection, "c")[0])
    self.assertEqual([to_xml(header) for header in headers], [to_xml(recorded.headers[0])])
    (wrapper,) = recorded.body
    assert isinstance(wrapper, Element)
    self.assertEqual(
      (body.name, body.namespace, body.children), (wrapper.name, wrapper.namespace, [])
    )
    self.assertEqual([child.name for child in wrapper.elements()], ["FileStream"])

  def test_service_reads_the_requests_as_it_logged_them(self) -> None:
    """Reading each request back as a receiver gives the values the service printed."""
    for name in _SCENARIOS:
      with self.subTest(recording=name):
        exchange = _Exchange(name)
        logged = _entries(exchange.notes["service_log"])
        names = {line[len("call: ") :].split("(")[0] for line in logged}
        calls: List[str] = []
        for (operation, _), envelope in zip(exchange.calls, exchange.requests):
          if operation.name in names:
            message = soap.from_envelope(envelope)
            values = operation.read_request(message.body, _body_scope(envelope))
            types = [xml_type for _, xml_type in operation.parameters]
            calls.append(f"call: {operation.name}({_render_all(values, types)})")
        self.assertEqual(calls, logged)

  def test_arguments_must_fit_the_operation(self) -> None:
    """The number of arguments and their types are checked before anything is sent."""
    with self.assertRaises(ValueError):
      s.PUMP_START.request((1,))
    with self.assertRaises(ValueError):
      s.PUMP_START.request((1, "Sideways"))
    with self.assertRaises(ValueError):
      s.SET_VERBOSE_LOGGING.request((None,))
    with self.assertRaises(ValueError):
      s.SAMPLER_MOVE_XY.request((1.5, D(0), True))


class ReplyTests(unittest.TestCase):
  """Replies and callbacks from the .NET service, against what the .NET client printed."""

  def test_replies_and_callbacks_give_the_values_dotnet_printed(self) -> None:
    """Every result and callback of every duplex recording, in the order they arrived."""
    for name in _SCENARIOS:
      with self.subTest(recording=name):
        exchange = _Exchange(name)
        lines, _ = exchange.client_log()
        # Faults and the state of the session are checked by the tests of their own.
        skipped = ("result: state ", "result: after fault ", "done")
        logged = [
          entry
          for entry in _entries(exchange.notes["client_log"])
          if " fault -> " not in entry and not entry.startswith(skipped)
        ]
        self.assertEqual(lines, logged)

  def test_declared_fault_detail(self) -> None:
    """A declared fault's detail reads as its fault contract, nested exceptions included."""
    _, faults = _Exchange("net48_duplex").client_log()
    ((operation, fault),) = faults
    self.assertIs(operation, s.SAMPLER_HOME)
    detail = read_fault_detail(fault, s.CONTROLLER_SERVICE_EXCEPTION)
    self.assertEqual(
      f"{fault.code[1]}|{fault.reason}|{_describe(detail)}|{fault.action}",
      "Sender|Sampler is not homed|System.InvalidOperationException: Sampler is not homed"
      " <- System.TimeoutException: axis X timed out"
      "|http://tempuri.org/IController/Sampler_HomeControllerServiceExceptionFault",
    )
    self.assertEqual(
      fault.action, ServiceContract("IController").fault_action(s.SAMPLER_HOME, detail.contract)
    )

  def test_undeclared_fault_has_no_detail(self) -> None:
    """An internal service fault carries no detail to read."""
    _, faults = _Exchange("net48_duplex_abort").client_log()
    ((operation, fault),) = faults
    self.assertIs(operation, s.SHAKER_HOME)
    with self.assertRaises(ContractError):
      read_fault_detail(fault, s.CONTROLLER_SERVICE_EXCEPTION)

  def test_callbacks_are_the_bytes_the_service_sent(self) -> None:
    """Writing each callback again from the values read gives the service's bytes."""
    for name in _SCENARIOS:
      connection = recordings.load(name).connections[0]
      reader, writer = ReaderSession(), WriterSession()
      for index, payload in enumerate(_envelopes(connection, "s")):
        envelope = decode(reader.read_string_table(payload), reader.lookup)
        message = soap.from_envelope(envelope)
        if message.relates_to is None:
          callback = _CALLBACKS[message.action]
          arguments = callback.read_request(message.body, _body_scope(envelope))
          _, body, _ = callback.request(arguments)
          envelope = soap.to_envelope(soap.Message(callback.action, [body], to=message.to))
        with self.subTest(recording=name, message=index, action=message.action):
          document = encode(envelope, writer.key_of)
          self.assertEqual(writer.string_table() + document, payload)

  def test_reply_of_the_wrong_shape_is_rejected(self) -> None:
    """A reply must hold the response element and every result and out part."""
    response = Element("Controller_GetSummaryResponse", "http://tempuri.org/")
    cases: Dict[str, List[Node]] = {
      "other element": [Element("Other", "http://tempuri.org/")],
      "other namespace": [Element("Controller_GetSummaryResponse", "urn:other")],
      "two elements": [response, response],
      "no result": [response],
      "empty body": [],
    }
    for case, body in cases.items():
      with self.subTest(case):
        with self.assertRaises(ContractError):
          s.GET_SUMMARY.read_reply(body, {})
    self.assertIsNone(s.SAMPLER_HOME.read_reply([], {}))


class OperationTests(unittest.TestCase):
  """Describing operations."""

  def test_actions_and_names(self) -> None:
    """Actions are the contract namespace, its name and the operation name."""
    contract = ServiceContract("IThing", "urn:things")
    operation = contract.operation("Do", [("x", INT)], result=STRING, outputs=[("y", BOOL)])
    self.assertEqual(operation.action, "urn:things/IThing/Do")
    self.assertEqual(operation.reply_action, "urn:things/IThing/DoResponse")
    self.assertEqual(s.CONNECT.action, "http://tempuri.org/IController/Controller_Connect")
    callback = contract.callback("Changed", [("x", INT)])
    self.assertEqual((callback.one_way, callback.reply_action), (True, ""))

  def test_primitive_operations(self) -> None:
    """WCF's formatter for all-primitive operations applies when no part needs more."""
    self.assertTrue(s.SET_VERBOSE_LOGGING.primitive)
    self.assertTrue(s.SAMPLER_MOVE_XY.primitive)
    self.assertTrue(s.GET_SYSTEM_ID.primitive)
    self.assertTrue(s.SAMPLER_HOME.primitive)
    self.assertFalse(s.PUMP_START.primitive)
    self.assertFalse(s.GET_OPERATION_DURATION.primitive)
    self.assertFalse(s.SET_TOP_PEAK_VALUES.primitive)
    self.assertFalse(s.UPLOAD_FIRMWARE.primitive)

  def test_message_contract_never_uses_the_primitive_formatter(self) -> None:
    """A message contract is a typed message, even without header members or other types."""
    contract = ServiceContract("ITest", "urn:test")
    typed = contract.operation("Send", [("Text", STRING)], wrapper="TextMessage")
    self.assertFalse(typed.primitive)
    _, body, _ = typed.request((None,))
    self.assertEqual(
      to_xml(body),
      '<TextMessage xmlns="urn:test"><Text i:nil="true" '
      'xmlns:i="http://www.w3.org/2001/XMLSchema-instance"></Text></TextMessage>',
    )

  def test_null_marks_differ_between_the_formatters(self) -> None:
    """The primitive formatter declares a fresh prefix for xsi; the other binds ``i``."""
    _, primitive, _ = s.CLEAR_CALIBRATION_SETTINGS.request((None,))
    self.assertEqual(
      to_xml(primitive),
      '<Controller_ClearCalibrationSettings xmlns="http://tempuri.org/">'
      '<clearReason b:nil="true" xmlns:b="http://www.w3.org/2001/XMLSchema-instance">'
      "</clearReason></Controller_ClearCalibrationSettings>",
    )
    _, composite, _ = s.CONFIGURE_DEVICES.request((None,))
    self.assertIn('<configuration i:nil="true" xmlns:b="', to_xml(composite))

  def test_streams_are_only_for_the_streamed_client(self) -> None:
    """A stream part takes no value in a request and cannot be read from a reply."""
    headers, body, _ = s.UPLOAD_FCS_DEMO_FILE.request(())
    self.assertEqual((headers, body.children), ([], []))
    with self.assertRaises(ValueError):
      STREAM.write(b"", Element("x"), Namespaces())
    result = Element("Controller_GetLogArchiveResult", "http://tempuri.org/", children=[Text(b"x")])
    reply = Element("Controller_GetLogArchiveResponse", "http://tempuri.org/", children=[result])
    with self.assertRaises(ContractError):
      s.GET_LOG_ARCHIVE.read_reply([reply], {})


if __name__ == "__main__":
  unittest.main()
