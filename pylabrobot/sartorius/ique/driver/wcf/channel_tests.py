"""Tests for the duplex and streamed clients, over in-memory connections.

``RecordedSessionTests`` and ``StreamedClientTests`` replay what .NET services sent in the
recordings; the replayed connection requires every byte the client writes to be what the
genuine .NET client wrote. ``DuplexBehaviourTests`` covers what the recordings do not show
with a service in memory that answers each request by a function of it.
"""

import asyncio
import hashlib
import math
import unittest
import uuid
from typing import AsyncIterator, Callable, List, Optional, Tuple

from pylabrobot.io.socket import Socket
from pylabrobot.sartorius.ique.driver.wcf import nmf, recordings, soap
from pylabrobot.sartorius.ique.driver.wcf.channel import DuplexChannel, StreamedClient
from pylabrobot.sartorius.ique.driver.wcf.contract import Operation, read_fault_detail
from pylabrobot.sartorius.ique.driver.wcf.dictionary import ReaderSession, WriterSession
from pylabrobot.sartorius.ique.driver.wcf.errors import (
  ChannelClosedError,
  ContractError,
  FramingError,
  FramingFault,
  SoapFault,
)
from pylabrobot.sartorius.ique.driver.wcf.nbfx import decode, encode
from pylabrobot.sartorius.ique.driver.wcf.recordings import sample as s
from pylabrobot.sartorius.ique.driver.wcf.tree import Element, Node, Text, Xmlns

_ADDRESS = "net.tcp://instrument:9002/Controller"
_DISPATCHER = "http://schemas.microsoft.com/net/2005/12/windowscommunicationfoundation/dispatcher"
_PREAMBLE_ACK = bytes((nmf.FramingRecord.PREAMBLE_ACK,))

Answer = Callable[["_FakeService", soap.Message], List[bytes]]


def _message_ids(connection: recordings.Connection) -> Callable[[], str]:
  """Return the MessageIDs a recorded client used, one per call, in order."""
  ids = [
    soap.from_envelope(envelope).message_id or ""
    for envelope in recordings.envelopes(connection, "c")
  ]
  return iter(ids).__next__


def _pattern(length: int, seed: int) -> bytes:
  """Return the bytes the recorded client uploaded."""
  return bytes((i * 31 + (i >> 8) * 7 + (i >> 16) * 3 + seed) & 0xFF for i in range(length))


async def _hashed(stream: AsyncIterator[bytes]) -> Tuple[str, int]:
  """Read a downloaded stream; return ``length:sha256`` as the client logged it, and the pieces."""
  digest, length, pieces = hashlib.sha256(), 0, 0
  async for piece in stream:
    digest.update(piece)
    length += len(piece)
    pieces += 1
  return f"{length}:{digest.hexdigest()}", pieces


def _reply(
  request: soap.Message, operation: Operation, result: Optional[str] = None
) -> soap.Message:
  """Build the reply to a request, with a string result if given."""
  namespace = operation.namespace
  children: List[Node] = []
  if result is not None:
    children.append(Element(operation.name + "Result", namespace, children=[Text(result)]))
  body = Element(operation.name + "Response", namespace, "", [Xmlns("", namespace)], children)
  return soap.Message(
    operation.reply_action, [body], relates_to=request.message_id, to=soap.ANONYMOUS_ADDRESS
  )


def _callback(operation: Operation, *arguments: object) -> soap.Message:
  """Build a one-way message of the callback contract."""
  _, body, _ = operation.request(arguments)
  return soap.Message(operation.action, [body], to=soap.ANONYMOUS_ADDRESS)


class _FakeService(Socket):
  """A service in memory: it acknowledges the preamble and answers each envelope it reads.

  Attributes:
    written: Everything the client wrote.
    requests: The messages the client sent, in order.
    is_stopped: Whether the client disconnected.
  """

  def __init__(
    self, answer: Answer, on_preamble: bytes = _PREAMBLE_ACK, on_end: bytes = nmf.END
  ) -> None:
    """Create the service.

    Args:
      answer: Returns the records to send back for a message the client sent.
      on_preamble: What to send back for the client's preamble.
      on_end: What to send back for the client's End record.
    """
    super().__init__(human_readable_device_name="fake service", host="fake", port=0)
    self._answer = answer
    self._on_preamble = on_preamble
    self._on_end = on_end
    self._received = ReaderSession()
    self._sent = WriterSession()
    self._readable = bytearray()
    self._changed = asyncio.Condition()
    self._reading = False
    self.written = bytearray()
    self.requests: List[soap.Message] = []
    self.is_stopped = False

  def envelope(self, message: soap.Message) -> bytes:
    """Return the Sized Envelope record of a message this service sends."""
    document = encode(soap.to_envelope(message), self._sent.key_of)
    return nmf.sized_envelope(self._sent.string_table() + document)

  async def push(self, data: bytes) -> None:
    """Make bytes readable for the client."""
    async with self._changed:
      self._readable += data
      self._changed.notify_all()

  async def setup(self) -> None:
    """Accept the connection."""

  async def stop(self) -> None:
    """Disconnect; like the real socket, this must not happen while a read waits."""
    if self._reading:
      raise AssertionError("stop() while a read is waiting: a real Socket would block here")
    self.is_stopped = True

  async def write(self, data: bytes, timeout: Optional[float] = None) -> None:
    """Take a record from the client and send back what the service answers."""
    if self.is_stopped:
      raise ConnectionError("the connection is closed")
    self.written += data
    if data.startswith(b"\x00\x01\x00"):
      await self.push(self._on_preamble)
    elif data == nmf.END:
      await self.push(self._on_end)
    else:
      (record,) = nmf.split_records(data)
      envelope = decode(self._received.read_string_table(record.payload), self._received.lookup)
      message = soap.from_envelope(envelope)
      self.requests.append(message)
      await self.push(b"".join(self._answer(self, message)))

  async def _take(self, count: int) -> bytes:
    """Wait for ``count`` readable bytes and take them."""
    async with self._changed:
      while len(self._readable) < count:
        await self._changed.wait()
      data = bytes(self._readable[:count])
      del self._readable[:count]
      return data

  async def read_exact(self, num_bytes: int, timeout: Optional[float] = None) -> bytes:
    """Give the next bytes the service sent, waiting at most ``timeout`` seconds."""
    self._reading = True
    try:
      if timeout is None or math.isinf(timeout):
        return await self._take(num_bytes)
      try:
        return await asyncio.wait_for(self._take(num_bytes), timeout)
      except asyncio.TimeoutError as error:
        raise TimeoutError(f"Timeout while reading from socket after {timeout} seconds") from error
    finally:
      self._reading = False


def _summary(service: _FakeService, request: soap.Message) -> List[bytes]:
  """Answer every request as Controller_GetSummary does."""
  return [service.envelope(_reply(request, s.GET_SUMMARY, "summary"))]


class RecordedSessionTests(unittest.IsolatedAsyncioTestCase):
  """Duplex sessions replayed from the recordings."""

  async def _replay(self, name: str) -> Tuple[DuplexChannel, recordings.ReplaySocket, List[str]]:
    """Open a channel on a replayed connection, with a handler for every callback."""
    connection = recordings.load(name).connections[0]
    socket = recordings.ReplaySocket(connection)
    channel = DuplexChannel(connection.address, socket=socket, message_ids=_message_ids(connection))
    received: List[str] = []
    for operation in s.CALLBACKS:
      channel.on(operation, lambda *values, name=operation.name: received.append(name))
    await channel.open()
    return channel, socket, received

  async def test_sessions_replay_byte_for_byte(self) -> None:
    """Every call of each recorded session, then an orderly close: End both ways."""
    for name, calls in (
      ("net48_duplex", s.DUPLEX_CALLS),
      ("net48_duplex_worklist", s.WORKLIST_CALLS),
    ):
      with self.subTest(recording=name):
        channel, socket, received = await self._replay(name)
        results = []
        for operation, arguments in calls:
          try:
            results.append(await channel.call(operation, *arguments))
          except SoapFault as fault:
            results.append(fault)
        await channel.close()
        self.assertTrue(socket.finished)
        self.assertTrue(socket.is_stopped)
        self.assertFalse(channel.is_open)
        log = recordings.load(name).notes["client_log"]
        callbacks = [
          line[len("callback: ") :].split("(")[0] for line in log if line.startswith("callback: ")
        ]
        self.assertEqual(received, callbacks)
        self.assertEqual(channel.unhandled, {})
        faults = [result for result in results if isinstance(result, SoapFault)]
        self.assertEqual(len(faults), sum(" fault -> " in line for line in log))

  async def test_callbacks_that_arrive_before_their_reply_are_handled_first(self) -> None:
    """Controller_Connect makes the service call back twice before it replies."""
    channel, _, received = await self._replay("net48_duplex")
    info = await channel.call(s.CONNECT, s.CLIENT_TIME)
    self.assertEqual(received, ["TestConnection", "OnSystemStateChanged"])
    self.assertEqual(info["Version"], (7, 2, 6010, 25563))
    self.assertEqual(
      await channel.call(s.GET_SUMMARY), "Controller_Connect(DateTime(639299820301234567,Local))"
    )

  async def test_declared_fault_leaves_the_session_open(self) -> None:
    """A fault the contract declares raises SoapFault with its detail; calls go on."""
    channel, socket, _ = await self._replay("net48_duplex")
    calls = s.DUPLEX_CALLS
    home = calls.index((s.SAMPLER_HOME, ()))
    for operation, arguments in calls[:home]:
      await channel.call(operation, *arguments)
    with self.assertRaises(SoapFault) as caught:
      await channel.call(s.SAMPLER_HOME)
    fault = caught.exception
    self.assertEqual(
      (fault.code[1], fault.subcodes, fault.reason), ("Sender", (), "Sampler is not homed")
    )
    detail = read_fault_detail(fault, s.CONTROLLER_SERVICE_EXCEPTION)
    self.assertEqual(detail["InnerException"]["Message"], "axis X timed out")
    self.assertTrue(channel.is_open)
    for operation, arguments in calls[home + 1 :]:
      await channel.call(operation, *arguments)
    await channel.close()
    self.assertTrue(socket.finished)

  async def test_service_ends_the_session_after_an_internal_error(self) -> None:
    """An internal service fault ends the session; later calls fail; close answers with End."""
    channel, socket, _ = await self._replay("net48_duplex_abort")
    self.assertIs(await channel.call(s.IS_1536_CAPABLE), True)
    with self.assertLogs("pylabrobot.sartorius.ique.driver.wcf.channel", "ERROR"):
      with self.assertRaises(SoapFault) as caught:
        await channel.call(s.SHAKER_HOME)
    self.assertEqual(caught.exception.subcodes, ((_DISPATCHER, "InternalServiceFault"),))
    await asyncio.sleep(0)
    self.assertFalse(channel.is_open)
    with self.assertRaises(ChannelClosedError):
      await channel.call(s.IS_1536_CAPABLE)
    await channel.close()
    self.assertEqual(
      bytes(socket.written), recordings.load("net48_duplex_abort").connections[0].sent_by("c")
    )
    self.assertTrue(socket.is_stopped)


class StreamedClientTests(unittest.IsolatedAsyncioTestCase):
  """Calls to a streamed endpoint replayed from the recording, one connection each."""

  def setUp(self) -> None:
    """Load the recording; each call takes the next recorded connection."""
    self.recording = recordings.load("net48_streamed")
    self.sockets = list(recordings.sockets(self.recording))
    connections = self.recording.connections
    ids = [
      soap.from_envelope(recordings.envelopes(c, "c")[0]).message_id or "" for c in connections
    ]
    self.client = StreamedClient(
      connections[0].address,
      socket_factory=iter(self.sockets).__next__,
      message_ids=iter(ids).__next__,
    )

  async def test_calls_replay_byte_for_byte(self) -> None:
    """Downloads, uploads, plain calls and faults, as the .NET client made them."""
    client = self.client
    empty, _ = await _hashed(client.download(s.GET_CURRENT_FCS_FILE))
    full, pieces = await _hashed(
      client.download(s.GET_COMPLETED_WORKLIST_FCS_FILE, s.COMPLETED_WORKLIST_ID)
    )
    with self.assertRaises(SoapFault) as missing:
      await _hashed(client.download(s.GET_COMPLETED_WORKLIST_FCS_FILE, uuid.UUID(int=0)))
    self.assertIsNone(await client.upload(s.UPLOAD_FCS_DEMO_FILE, source=_pattern(1000, 2)))
    await client.upload(s.UPLOAD_CALIBRATION_SETTINGS, source=_pattern(70000, 3))
    for reason in ("recalibrated", None, ""):
      self.assertIsNone(await client.call(s.CLEAR_CALIBRATION_SETTINGS, reason))
    with self.assertRaises(SoapFault) as nothing:
      await _hashed(client.download(s.DOWNLOAD_CALIBRATION_SETTINGS))
    await client.upload(s.UPLOAD_FIRMWARE, "firmware.bin", source=_pattern(5000, 4))
    await client.upload(s.UPLOAD_FCS_DEMO_FILE, source=b"")

    log = self.recording.notes["client_log"]
    self.assertEqual(f'result: Worklist_GetCurrentFCSFile -> "{empty}"', log[0])
    self.assertEqual(f'result: Worklist_GetCompletedWorklistFCSFile -> "{full}"', log[1])
    self.assertGreater(pieces, 1)
    detail = read_fault_detail(missing.exception, s.CONTROLLER_SERVICE_EXCEPTION)
    self.assertEqual(detail["Type"], "System.IO.FileNotFoundException")
    self.assertEqual(nothing.exception.reason, "No calibration settings stored")
    uploaded = hashlib.sha256(_pattern(70000, 3)).hexdigest()
    self.assertIn(
      f'call: Controller_UploadCalibrationSettings(70000,"{uploaded}")',
      self.recording.notes["service_log"],
    )
    for socket in self.sockets:
      self.assertTrue(socket.is_stopped)
    for index in (0, 1, 3, 4, 5, 6, 7, 9, 10):
      self.assertTrue(self.sockets[index].finished, index)

  async def test_upload_source_may_come_in_pieces(self) -> None:
    """Bytes in pieces of any size, or from an async iterator, are sent in .NET's blocks."""
    data = _pattern(70000, 3)

    async def pieces() -> AsyncIterator[bytes]:
      """Give the data in uneven pieces."""
      for start in range(0, len(data), 999):
        yield data[start : start + 999]

    connection = self.recording.connections[4]
    message_id = soap.from_envelope(recordings.envelopes(connection, "c")[0]).message_id or ""
    sources = ([data[:10], data[10:70000]], pieces())
    for source in sources:
      socket = recordings.ReplaySocket(connection)
      client = StreamedClient(
        connection.address, socket_factory=lambda: socket, message_ids=lambda: message_id
      )
      await client.upload(s.UPLOAD_CALIBRATION_SETTINGS, source=source)
      self.assertTrue(socket.finished)

  async def test_download_stopped_early_disconnects(self) -> None:
    """Closing the iteration before the end of the stream closes the connection."""
    connection = self.recording.connections[1]
    socket = recordings.ReplaySocket(connection)
    client = StreamedClient(
      connection.address, socket_factory=lambda: socket, message_ids=_message_ids(connection)
    )
    stream = client.download(s.GET_COMPLETED_WORKLIST_FCS_FILE, s.COMPLETED_WORKLIST_ID)
    self.assertTrue(await stream.__anext__())
    self.assertFalse(socket.is_stopped)
    await stream.aclose()  # type: ignore[attr-defined]
    self.assertTrue(socket.is_stopped)
    self.assertFalse(socket.finished)

  async def test_streams_need_the_streamed_calls(self) -> None:
    """call() is for operations without a stream; upload() and download() need one."""
    with self.assertRaises(ValueError):
      await self.client.call(s.GET_LOG_ARCHIVE)
    with self.assertRaises(ValueError):
      await self.client.upload(s.CLEAR_CALIBRATION_SETTINGS, "x", source=b"")
    with self.assertRaises(ValueError):
      await self.client.download(s.CLEAR_CALIBRATION_SETTINGS, "x").__anext__()


class DuplexBehaviourTests(unittest.IsolatedAsyncioTestCase):
  """Duplex sessions with a service in memory."""

  async def _open(
    self, answer: Answer, request_timeout: float = 60.0
  ) -> Tuple[DuplexChannel, _FakeService]:
    """Open a channel to a service that answers by a given function."""
    service = _FakeService(answer)
    channel = DuplexChannel(_ADDRESS, socket=service, request_timeout=request_timeout)
    await channel.open()
    return channel, service

  async def test_overlapping_calls_get_their_own_replies(self) -> None:
    """Replies are matched to requests by RelatesTo, whatever their order."""
    waiting: List[soap.Message] = []

    def answer(service: _FakeService, request: soap.Message) -> List[bytes]:
      """Hold the first request back and answer both, last first, with the second."""
      if not waiting:
        waiting.append(request)
        return []
      return [
        service.envelope(_reply(request, s.GET_SUMMARY, "second")),
        service.envelope(_reply(waiting[0], s.GET_SUMMARY, "first")),
      ]

    channel, _ = await self._open(answer)
    first = asyncio.create_task(channel.call(s.GET_SUMMARY))
    await asyncio.sleep(0)
    second = await channel.call(s.GET_SUMMARY)
    self.assertEqual((await first, second), ("first", "second"))
    await channel.close()

  async def test_timeout_leaves_the_session_open(self) -> None:
    """A reply that does not come in time raises; one that comes later is dropped."""
    late: List[soap.Message] = []

    def answer(service: _FakeService, request: soap.Message) -> List[bytes]:
      """Answer the first request only together with the second."""
      if not late:
        late.append(request)
        return []
      return [
        service.envelope(_reply(late[0], s.GET_SUMMARY, "late")),
        service.envelope(_reply(request, s.GET_SUMMARY, "now")),
      ]

    channel, _ = await self._open(answer)
    with self.assertRaises(TimeoutError):
      await channel.call(s.GET_SUMMARY, timeout=0.05)
    self.assertTrue(channel.is_open)
    with self.assertLogs("pylabrobot.sartorius.ique.driver.wcf.channel", "WARNING") as logs:
      self.assertEqual(await channel.call(s.GET_SUMMARY), "now")
    self.assertIn("answers no waiting request", "".join(logs.output))
    await channel.close()

  async def test_callbacks_reach_their_handlers(self) -> None:
    """A handler gets the parameters; its errors are logged; messages without one are counted."""
    calls: List[Tuple[object, ...]] = []

    def answer(service: _FakeService, request: soap.Message) -> List[bytes]:
      """Call back three times, then reply."""
      return [
        service.envelope(_callback(s.ON_SYSTEM_STATE_CHANGED, "Yellow")),
        service.envelope(_callback(s.ON_CONTACT_SENSOR_STATE_CHANGED, True)),
        service.envelope(_callback(s.TEST_CONNECTION)),
        service.envelope(_reply(request, s.GET_SUMMARY, "done")),
      ]

    def broken(*values: object) -> None:
      """Fail as a faulty handler would."""
      raise RuntimeError("handler failed")

    channel, _ = await self._open(answer)
    channel.on(s.ON_SYSTEM_STATE_CHANGED, lambda *values: calls.append(values))
    channel.on(s.ON_CONTACT_SENSOR_STATE_CHANGED, broken)
    with self.assertLogs("pylabrobot.sartorius.ique.driver.wcf.channel", "WARNING") as logs:
      self.assertEqual(await channel.call(s.GET_SUMMARY), "done")
    self.assertEqual(calls, [("Yellow",)])
    self.assertIn("RuntimeError: handler failed", "\n".join(logs.output))
    self.assertEqual(channel.unhandled, {s.TEST_CONNECTION.action: 1})
    self.assertTrue(channel.is_open)
    await channel.close()

  async def test_malformed_fault_fails_the_call_at_once(self) -> None:
    """A fault that cannot be read ends the session and raises in the call that waits for it."""

    def answer(service: _FakeService, request: soap.Message) -> List[bytes]:
      """Answer with a fault that lacks its code."""
      fault = Element(
        "Fault", soap.SOAP_NAMESPACE, "s", children=[Element("Reason", soap.SOAP_NAMESPACE, "s")]
      )
      message = soap.Message("urn:fault", [fault], relates_to=request.message_id)
      return [service.envelope(message)]

    channel, _ = await self._open(answer, request_timeout=5.0)
    with self.assertLogs("pylabrobot.sartorius.ique.driver.wcf.channel", "ERROR"):
      with self.assertRaises(ContractError):
        await asyncio.wait_for(channel.call(s.GET_SUMMARY), timeout=1.0)
    self.assertFalse(channel.is_open)
    await channel.close()

  async def test_service_ends_the_session(self) -> None:
    """An End record from the service fails the waiting calls; close answers with End."""
    channel, service = await self._open(lambda service, request: [nmf.END])
    with self.assertLogs("pylabrobot.sartorius.ique.driver.wcf.channel", "ERROR"):
      with self.assertRaises(ChannelClosedError):
        await channel.call(s.GET_SUMMARY)
    self.assertFalse(channel.is_open)
    written = len(service.written)
    await channel.close()
    self.assertEqual(bytes(service.written[written:]), nmf.END)
    self.assertTrue(service.is_stopped)

  async def test_close_without_the_service_end_raises(self) -> None:
    """An orderly close waits for the service's End record, and disconnects either way."""
    service = _FakeService(_summary, on_end=b"")
    channel = DuplexChannel(_ADDRESS, socket=service, io_timeout=0.05)
    await channel.open()
    self.assertEqual(await channel.call(s.GET_SUMMARY), "summary")
    with self.assertRaises(FramingError):
      await channel.close()
    self.assertTrue(service.is_stopped)
    with self.assertRaises(ChannelClosedError):
      await channel.call(s.GET_SUMMARY)

  async def test_refused_session(self) -> None:
    """A framing fault in answer to the preamble refuses the session and disconnects."""
    service = _FakeService(
      _summary, on_preamble=nmf.fault_record(nmf.FAULT_NAMESPACE + "EndpointNotFound")
    )
    channel = DuplexChannel(_ADDRESS, socket=service)
    with self.assertRaises(FramingFault) as caught:
      await channel.open()
    self.assertTrue(caught.exception.fault.endswith("/EndpointNotFound"))
    self.assertTrue(service.is_stopped)
    with self.assertRaises(RuntimeError):
      await channel.open()

  async def test_request_carries_addressing_headers(self) -> None:
    """A call sends its MessageID, the anonymous ReplyTo and the endpoint address as To."""
    channel, service = await self._open(_summary)
    await channel.call(s.GET_SUMMARY)
    (request,) = service.requests
    self.assertEqual((request.action, request.to), (s.GET_SUMMARY.action, _ADDRESS))
    self.assertEqual(request.reply_to, soap.ANONYMOUS_ADDRESS)
    self.assertTrue((request.message_id or "").startswith("urn:uuid:"))
    await channel.close()
    with self.assertRaises(ChannelClosedError):
      await channel.call(s.GET_SUMMARY)


if __name__ == "__main__":
  unittest.main()
