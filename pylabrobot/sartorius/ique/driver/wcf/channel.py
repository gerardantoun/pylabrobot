"""Clients for a WCF service over ``net.tcp``.

:class:`DuplexChannel` talks to an endpoint with buffered transfer and a session, the
default of ``NetTcpBinding``: one connection carries requests, their replies and the
one-way messages the service sends on its own for the callback contract.

:class:`StreamedClient` talks to an endpoint with ``TransferMode.Streamed``: each call is
one request and one reply on a connection of its own, either of which may carry a stream
of bytes of any length.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import (
  Any,
  AsyncIterable,
  AsyncIterator,
  Callable,
  Dict,
  Iterable,
  List,
  Optional,
  Tuple,
  Union,
)
from urllib.parse import urlsplit

from pylabrobot.io.socket import Socket
from pylabrobot.sartorius.ique.driver.wcf import nmf, soap
from pylabrobot.sartorius.ique.driver.wcf.contract import STREAM, Operation
from pylabrobot.sartorius.ique.driver.wcf.dictionary import (
  DEFAULT_MAX_SESSION_SIZE,
  ReaderSession,
  WriterSession,
  static_key,
  static_lookup,
)
from pylabrobot.sartorius.ique.driver.wcf.errors import (
  ChannelClosedError,
  ContractError,
  FramingError,
  NbfxIncompleteError,
)
from pylabrobot.sartorius.ique.driver.wcf.nbfx import (
  EndElement,
  Event,
  Reader,
  Writer,
  decode,
  encode,
)
from pylabrobot.sartorius.ique.driver.wcf.tree import Element, Text, declared
from pylabrobot.sartorius.ique.driver.wcf.values import parse_bytes

logger = logging.getLogger(__name__)

# The subcode of the fault a service sends for an exception it did not expect. A service
# with sessions ends the session along with it.
_INTERNAL_SERVICE_FAULT = (
  "http://schemas.microsoft.com/net/2005/12/windowscommunicationfoundation/dispatcher",
  "InternalServiceFault",
)

# The sizes in which a .NET writer reads a stream it sends: each full block is followed by
# one sixteen times as large, up to the last size.
_STREAM_BLOCK_SIZES = (256, 4096, 65536)

DEFAULT_NET_TCP_PORT = 808

ByteSource = Union[bytes, Iterable[bytes], AsyncIterable[bytes]]
Handler = Callable[..., None]
Reply = Tuple[Element, soap.Message]


def _endpoint(address: str, host: Optional[str], port: Optional[int]) -> Tuple[str, int]:
  """Return the host and port to connect to for a ``net.tcp`` address."""
  parts = urlsplit(address)
  if parts.scheme != "net.tcp" or not parts.hostname:
    raise ValueError(f"not a net.tcp address: {address!r}")
  return host or parts.hostname, port or parts.port or DEFAULT_NET_TCP_PORT


def _body_scope(envelope: Element) -> Dict[str, str]:
  """Return the namespace declarations in force inside the Body of an envelope."""
  scope = declared({}, envelope)
  body = envelope.child("Body", soap.SOAP_NAMESPACE)
  return scope if body is None else declared(scope, body)


class DuplexChannel:
  """A session with a WCF service over one ``net.tcp`` connection.

  After :meth:`open`, :meth:`call` sends a request and returns what the service answers,
  and the handlers registered with :meth:`on` receive the one-way messages the service
  sends. Calls may overlap; the service answers them in turn.

  Anything that breaks the protocol ends the session: the calls still waiting raise, and
  later calls raise :class:`ChannelClosedError`. A session does not reconnect;
  :meth:`close` it and open a new one.
  """

  def __init__(
    self,
    address: str,
    host: Optional[str] = None,
    port: Optional[int] = None,
    request_timeout: float = 60.0,
    io_timeout: float = 30.0,
    max_session_size: int = DEFAULT_MAX_SESSION_SIZE,
    socket: Optional[Socket] = None,
    message_ids: Callable[[], str] = soap.new_message_id,
  ) -> None:
    """Create a channel; :meth:`open` connects it.

    Args:
      address: The endpoint address, such as ``net.tcp://instrument:9002/Controller``. The
        service matches its port and path, not its host name.
      host: The host to connect to, when it differs from the one in the address.
      port: The port to connect to, when it differs from the one in the address.
      request_timeout: Seconds to wait for the reply to a call, unless the call says else.
      io_timeout: Seconds to wait for a write to complete, for the rest of a message that
        started to arrive, and for the service to acknowledge the opening and the closing.
      max_session_size: The size of the string table the service accepts from a client;
        2048 bytes unless its binding sets ``MaxSessionSize``.
      socket: The transport to use instead of a new ``Socket``.
      message_ids: Returns the MessageID of the next request.
    """
    self.address = address
    target_host, target_port = _endpoint(address, host, port)
    self._socket = socket or Socket(
      human_readable_device_name=f"WCF service {address}",
      host=target_host,
      port=target_port,
      read_timeout=io_timeout,
      write_timeout=io_timeout,
    )
    self._request_timeout = request_timeout
    self._io_timeout = io_timeout
    self._message_ids = message_ids
    self._written = WriterSession(max_session_size)
    self._read = ReaderSession()
    self._send_lock = asyncio.Lock()
    self._pending: Dict[str, "asyncio.Future[Reply]"] = {}
    self._handlers: Dict[str, Tuple[Operation, Handler]] = {}
    self._unhandled: Dict[str, int] = {}
    self._reader_task: Optional["asyncio.Task[None]"] = None
    self._failure: Optional[BaseException] = None
    self._is_open = False
    self._closing = False
    self._peer_ended = False
    self._idle = False

  @property
  def is_open(self) -> bool:
    """Whether the session is open for calls."""
    return self._is_open

  @property
  def unhandled(self) -> Dict[str, int]:
    """How many one-way messages arrived without a handler, by Action."""
    return dict(self._unhandled)

  async def _receive(self, count: int) -> bytes:
    """Read bytes of a message; only the wait for a new message has no time limit."""
    timeout = math.inf if self._idle else self._io_timeout
    self._idle = False
    return await self._socket.read_exact(count, timeout=timeout)

  def _fail(self, error: BaseException) -> None:
    """End the session for an error and pass it to every call still waiting."""
    if self._failure is None:
      self._failure = error
    self._is_open = False
    for future in self._pending.values():
      if not future.done():
        future.set_exception(error)
    self._pending.clear()

  def _require_open(self) -> None:
    """Raise unless the session is open."""
    if not self._is_open:
      raise ChannelClosedError(f"the session with {self.address} is not open") from self._failure

  def on(self, operation: Operation, handler: Handler) -> None:
    """Register the handler of a one-way message from the service.

    The handler is called with the operation's parameters as soon as the message arrives,
    inside the task that receives messages: it should return quickly and hand longer work
    to a task or queue. An exception it raises is logged and does not end the session.

    Args:
      operation: A callback operation (``ServiceContract.callback``).
      handler: The function to call; a second registration replaces the first.
    """
    self._handlers[operation.action] = (operation, handler)

  def _dispatch(self, envelope: Element, message: soap.Message) -> None:
    """Pass a one-way message to its handler."""
    registered = self._handlers.get(message.action)
    if registered is None:
      if message.action not in self._unhandled:
        logger.warning("no handler for messages with action %s", message.action)
      self._unhandled[message.action] = self._unhandled.get(message.action, 0) + 1
      return
    operation, handler = registered
    arguments = operation.read_request(message.body, _body_scope(envelope))
    try:
      handler(*arguments)
    except Exception:  # the handler is the caller's code; its errors must not end the session
      logger.exception("the handler for %s raised", message.action)

  def _complete(self, envelope: Element, message: soap.Message) -> None:
    """Pass a reply to the call that waits for it."""
    # A fault that cannot be read ends the session; the call stays pending until then, so
    # that it receives the error.
    fault = soap.fault_of(envelope, message)
    future = self._pending.pop(message.relates_to or "", None)
    if future is None or future.done():
      logger.warning("reply %s answers no waiting request", message.action)
    elif fault is not None:
      future.set_exception(fault)
    else:
      future.set_result((envelope, message))
    if fault is not None and _INTERNAL_SERVICE_FAULT in fault.subcodes:
      self._peer_ended = True
      raise ChannelClosedError("the service ended the session after an internal error")

  async def _read_loop(self) -> None:
    """Receive messages until the session ends, routing each to its call or handler."""
    try:
      while True:
        self._idle = True
        payload = await nmf.read_duplex_record(self._receive)
        if payload is None:
          if not self._closing:
            self._peer_ended = True
            raise ChannelClosedError("the service ended the session")
          self._fail(ChannelClosedError("the session was closed"))
          return
        document = self._read.read_string_table(payload)
        envelope = decode(document, self._read.lookup)
        message = soap.from_envelope(envelope)
        if message.relates_to is None:
          self._dispatch(envelope, message)
        else:
          self._complete(envelope, message)
    except asyncio.CancelledError:
      self._fail(ChannelClosedError("the session was closed"))
      raise
    except Exception as error:  # whatever stops the receiving ends the session for all calls
      if not self._closing:
        logger.error("the session with %s ended: %s", self.address, error)
      self._fail(error)

  async def open(self) -> None:
    """Connect, exchange the framing preamble and start receiving.

    Raises:
      FramingFault: The service refused the session, for example because the address is
        not one of its endpoints.
    """
    if self._reader_task is not None or self._failure is not None:
      raise RuntimeError("the channel was opened before; create a new one")
    await self._socket.setup()
    try:
      preamble = nmf.preamble(
        nmf.FramingMode.DUPLEX, self.address, nmf.EnvelopeEncoding.SOAP12_BINARY_SESSION
      )
      await self._socket.write(preamble, timeout=self._io_timeout)
      await nmf.read_preamble_ack(self._receive)
    except BaseException as error:
      self._failure = error
      await self._socket.stop()
      raise
    self._is_open = True
    self._reader_task = asyncio.create_task(self._read_loop())
    logger.info("opened a session with %s", self.address)

  async def _send(self, message: soap.Message) -> None:
    """Encode and send one message; the string table follows the order of sending."""
    async with self._send_lock:
      self._require_open()
      document = encode(soap.to_envelope(message), self._written.key_of)
      envelope = nmf.sized_envelope(self._written.string_table() + document)
      try:
        await self._socket.write(envelope, timeout=self._io_timeout)
      except Exception as error:
        self._fail(error)
        raise

  async def request(
    self,
    action: str,
    body: Element,
    headers: Optional[List[Element]] = None,
    timeout: Optional[float] = None,
  ) -> Reply:
    """Send a request and wait for its reply.

    Args:
      action: The Action of the request.
      body: The element to send in the SOAP Body.
      headers: SOAP headers to send besides the addressing ones.
      timeout: Seconds to wait for the reply; the channel's request timeout if not given.

    Returns:
      The reply's Envelope element and the message read from it.

    Raises:
      SoapFault: The service answered with a fault.
      TimeoutError: No reply arrived in time. The session stays open.
      ChannelClosedError: The session is closed or ended before the reply arrived.
    """
    self._require_open()
    message_id = self._message_ids()
    message = soap.Message(
      action=action,
      body=[body],
      message_id=message_id,
      reply_to=soap.ANONYMOUS_ADDRESS,
      to=self.address,
      headers=list(headers or []),
    )
    future: "asyncio.Future[Reply]" = asyncio.get_running_loop().create_future()
    self._pending[message_id] = future
    limit = self._request_timeout if timeout is None else timeout
    try:
      await self._send(message)
      done, _ = await asyncio.wait({future}, timeout=limit)
      if not done:
        raise TimeoutError(f"no reply to {action} within {limit} seconds")
      return future.result()
    finally:
      self._pending.pop(message_id, None)
      if not future.cancel() and not future.cancelled():
        # Mark an error nobody waited for as seen, so that it is not reported again later.
        future.exception()

  async def call(
    self, operation: Operation, *arguments: Any, timeout: Optional[float] = None
  ) -> Any:
    """Call an operation of the service.

    Args:
      operation: The operation (``ServiceContract.operation``).
      *arguments: A value for each of its parameters.
      timeout: Seconds to wait for the reply; the channel's request timeout if not given.

    Returns:
      What ``Operation.read_reply`` gives for the reply.

    Raises:
      SoapFault: The service answered with a fault.
      ContractError: The reply does not have the shape the operation declares.
      TimeoutError: No reply arrived in time.
      ChannelClosedError: The session is closed or ended before the reply arrived.
    """
    headers, body, _ = operation.request(arguments)
    envelope, reply = await self.request(operation.action, body, headers, timeout)
    if reply.action != operation.reply_action:
      raise ContractError(f"{operation.name}: unexpected reply action {reply.action!r}")
    return operation.read_reply(reply.body, _body_scope(envelope))

  async def close(self) -> None:
    """End the session in an orderly way and disconnect.

    A session the service has ended is answered with this side's End record; a session
    that failed in another way is only disconnected.

    Raises:
      FramingError: The service did not end its side of the session in time, or the
        session failed while closing. The connection is closed all the same.
    """
    task, self._reader_task = self._reader_task, None
    if task is None:
      return
    orderly = self._is_open
    try:
      if orderly:
        self._closing = True
        async with self._send_lock:
          self._is_open = False
          await self._socket.write(nmf.END, timeout=self._io_timeout)
        await asyncio.wait({task}, timeout=self._io_timeout)
      elif self._peer_ended:
        try:
          await self._socket.write(nmf.END, timeout=self._io_timeout)
        except OSError as error:
          logger.debug("could not answer the end of the session with %s: %s", self.address, error)
    finally:
      ended = task.done()
      if not ended:
        task.cancel()
        await asyncio.wait({task})
      failure = self._failure
      self._fail(ChannelClosedError("the session was closed"))
      await self._socket.stop()
      logger.info("closed the session with %s", self.address)
    if orderly and not ended:
      raise FramingError("the service did not end the session in time")
    if orderly and not isinstance(failure, ChannelClosedError):
      raise FramingError("the session failed while closing") from failure


async def _pieces(source: ByteSource) -> AsyncIterator[bytes]:
  """Iterate over the pieces of a byte source of any of the accepted kinds."""
  if isinstance(source, (bytes, bytearray)):
    yield bytes(source)
  elif isinstance(source, AsyncIterable):
    async for piece in source:
      yield piece
  else:
    for piece in source:
      yield piece


async def _stream_blocks(source: ByteSource) -> AsyncIterator[bytes]:
  """Regroup a byte source into the blocks a .NET writer reads a stream in."""
  sizes = list(_STREAM_BLOCK_SIZES)
  buffer = bytearray()
  async for piece in _pieces(source):
    buffer += piece
    while len(buffer) >= sizes[0]:
      yield bytes(buffer[: sizes[0]])
      del buffer[: sizes[0]]
      sizes = sizes[1:] or sizes
  if buffer:
    yield bytes(buffer)


class _StreamedReply:
  """The reply of a streamed call, read as its data chunks arrive."""

  def __init__(self, socket: Socket, reply_timeout: float, io_timeout: float, name: str) -> None:
    """Create the reader of a reply.

    Args:
      socket: The connection the reply arrives on.
      reply_timeout: Seconds to wait for the reply to begin.
      io_timeout: Seconds to wait for each further part of it.
      name: The operation name, for error messages.
    """
    self._socket = socket
    self._timeout = reply_timeout
    self._io_timeout = io_timeout
    self._name = name
    self._reader = Reader(static_lookup)

  async def _receive(self, count: int) -> bytes:
    """Read bytes of the reply; only its first byte may take long to come."""
    timeout, self._timeout = self._timeout, self._io_timeout
    return await self._socket.read_exact(count, timeout=timeout)

  async def _feed(self) -> None:
    """Pass the next data chunk to the binary XML reader."""
    data = await nmf.read_chunk(self._receive)
    if not data:
      raise FramingError(f"{self._name}: the reply ends inside its envelope")
    self._reader.feed(data)

  async def begin(self) -> None:
    """Read the start of the reply's envelope record."""
    await nmf.read_unsized_start(self._receive)

  async def event(self) -> Event:
    """Read the next binary XML event."""
    while True:
      try:
        return self._reader.read()
      except NbfxIncompleteError:
        await self._feed()

  async def start(self, where: str) -> Element:
    """Read the start of an element, which must come next."""
    found = await self.event()
    if not isinstance(found, Element):
      raise ContractError(f"{self._name}: expected an element inside {where}")
    return found

  async def rest(self, start: Element) -> Element:
    """Read everything inside an element that has started, through its end."""
    while True:
      try:
        return self._reader.read_element(start)
      except NbfxIncompleteError:
        await self._feed()

  async def finish(self) -> None:
    """Read what follows the envelope: the end of the chunks and the End record."""
    if self._reader.buffered or await nmf.read_chunk(self._receive):
      raise FramingError(f"{self._name}: data follows the reply's envelope")
    await nmf.read_end(self._receive)


class StreamedClient:
  """A client for a WCF endpoint with streamed transfer.

  Every call opens a connection, sends one message, receives one and disconnects.
  :meth:`call` is for operations with ordinary parameters and results, :meth:`download`
  for an operation that returns a stream and :meth:`upload` for one that takes a stream.
  """

  def __init__(
    self,
    address: str,
    host: Optional[str] = None,
    port: Optional[int] = None,
    reply_timeout: float = 300.0,
    io_timeout: float = 30.0,
    socket_factory: Optional[Callable[[], Socket]] = None,
    message_ids: Callable[[], str] = soap.new_message_id,
  ) -> None:
    """Create a client; it connects when called.

    Args:
      address: The endpoint address, such as
        ``net.tcp://instrument:9003/ControllerStreaming``.
      host: The host to connect to, when it differs from the one in the address.
      port: The port to connect to, when it differs from the one in the address.
      reply_timeout: Seconds to wait for a reply to begin.
      io_timeout: Seconds to wait for a write to complete and for the next part of a
        reply that began to arrive.
      socket_factory: Returns the transport for one call, instead of a new ``Socket``.
      message_ids: Returns the MessageID of the next request.
    """
    self.address = address
    self._host, self._port = _endpoint(address, host, port)
    self._reply_timeout = reply_timeout
    self._io_timeout = io_timeout
    self._socket_factory = socket_factory
    self._message_ids = message_ids

  async def _connect(self) -> Socket:
    """Connect and exchange the framing preamble."""
    socket = (
      self._socket_factory()
      if self._socket_factory is not None
      else Socket(
        human_readable_device_name=f"WCF service {self.address}",
        host=self._host,
        port=self._port,
        read_timeout=self._io_timeout,
        write_timeout=self._io_timeout,
      )
    )
    await socket.setup()
    try:
      preamble = nmf.preamble(
        nmf.FramingMode.SINGLETON_UNSIZED, self.address, nmf.EnvelopeEncoding.SOAP12_BINARY
      )
      await socket.write(preamble, timeout=self._io_timeout)
      await nmf.read_preamble_ack(lambda count: socket.read_exact(count, timeout=self._io_timeout))
    except BaseException:
      await socket.stop()
      raise
    return socket

  async def _send(self, socket: Socket, pieces: List[bytes], head: bytes, last: bool) -> bytes:
    """Send pieces of a message as data chunks.

    Args:
      socket: The connection.
      pieces: The pieces to send, each as a chunk of its own.
      head: Bytes to send in front of the chunks, when there are any.
      last: Whether to close the message after the chunks.

    Returns:
      The head, when there was nothing to send it with yet.
    """
    data = b"".join(nmf.chunk(piece) for piece in pieces)
    if last:
      data += nmf.CHUNKS_END + nmf.END
    if not data:
      return head
    await socket.write(head + data, timeout=self._io_timeout)
    return b""

  async def _request(
    self, operation: Operation, arguments: Tuple[Any, ...], source: Optional[ByteSource]
  ) -> Tuple[Socket, soap.Message]:
    """Connect and send the request of a call, with the stream it takes if any."""
    headers, wrapper, namespaces = operation.request(arguments)
    message = soap.Message(
      action=operation.action,
      body=[wrapper],
      message_id=self._message_ids(),
      reply_to=soap.ANONYMOUS_ADDRESS,
      to=self.address,
      headers=headers,
    )
    envelope = soap.to_envelope(message)
    writer = Writer(static_key)
    socket = await self._connect()
    try:
      head = nmf.UNSIZED_ENVELOPE_START
      if source is None:
        writer.write(envelope)
      else:
        body = envelope.children[-1]
        if not isinstance(body, Element):
          raise ContractError(f"{operation.name}: the envelope has no Body element")
        writer.start_element(envelope)
        for header in envelope.children[:-1]:
          writer.write(header)
        writer.start_element(body)
        writer.start_element(wrapper)
        for child in wrapper.children:
          writer.write(child)
        writer.start_element(namespaces.open(operation.parameters[-1][0], operation.namespace))
        async for block in _stream_blocks(source):
          writer.base64(block)
          head = await self._send(socket, writer.take_flushed(), head, last=False)
        for _ in range(4):
          writer.end_element()
      await self._send(socket, writer.take_pieces(), head, last=True)
    except BaseException:
      await socket.stop()
      raise
    return socket, message

  def _check(self, operation: Operation, request: soap.Message, envelope: Element) -> soap.Message:
    """Read a reply's message from its envelope, raising the fault it may carry."""
    reply = soap.from_envelope(envelope)
    if reply.relates_to != request.message_id:
      raise ContractError(f"{operation.name}: the reply relates to {reply.relates_to!r}")
    fault = soap.fault_of(envelope, reply)
    if fault is not None:
      raise fault
    if reply.action != operation.reply_action:
      raise ContractError(f"{operation.name}: unexpected reply action {reply.action!r}")
    return reply

  async def _invoke(
    self, operation: Operation, arguments: Tuple[Any, ...], source: Optional[ByteSource]
  ) -> Any:
    """Make a call whose reply carries no stream."""
    socket, request = await self._request(operation, arguments, source)
    try:
      reply = _StreamedReply(socket, self._reply_timeout, self._io_timeout, operation.name)
      await reply.begin()
      envelope = await reply.rest(await reply.start("the reply"))
      await reply.finish()
    finally:
      await socket.stop()
    message = self._check(operation, request, envelope)
    return operation.read_reply(message.body, _body_scope(envelope))

  async def call(self, operation: Operation, *arguments: Any) -> Any:
    """Call an operation that neither takes nor returns a stream.

    Returns:
      What ``Operation.read_reply`` gives for the reply.

    Raises:
      SoapFault: The service answered with a fault.
      ContractError: The reply does not have the shape the operation declares.
    """
    if STREAM in [xml_type for _, xml_type in operation.parameters] or operation.result is STREAM:
      raise ValueError(f"{operation.name} has a stream: use upload or download")
    return await self._invoke(operation, arguments, None)

  async def upload(self, operation: Operation, *arguments: Any, source: ByteSource) -> Any:
    """Call an operation whose last parameter is a stream.

    Args:
      operation: The operation; its last parameter is declared as ``STREAM``.
      *arguments: A value for each parameter before the stream.
      source: The bytes to send as the stream: all at once or in pieces of any size.

    Returns:
      What ``Operation.read_reply`` gives for the reply.

    Raises:
      SoapFault: The service answered with a fault.
      ContractError: The reply does not have the shape the operation declares.
    """
    if not operation.parameters or operation.parameters[-1][1] is not STREAM:
      raise ValueError(f"the last parameter of {operation.name} is not a stream")
    return await self._invoke(operation, arguments, source)

  async def download(self, operation: Operation, *arguments: Any) -> AsyncIterator[bytes]:
    """Call an operation that returns a stream, and iterate over the bytes as they arrive.

    The connection stays open until the iteration ends; ending it early, by closing the
    iterator, disconnects.

    Args:
      operation: The operation; its result is declared as ``STREAM``.
      *arguments: A value for each of its parameters.

    Yields:
      The bytes of the stream, in pieces of the sizes the service writes them in.

    Raises:
      SoapFault: The service answered with a fault.
      ContractError: The reply does not have the shape the operation declares.
    """
    if operation.result is not STREAM:
      raise ValueError(f"{operation.name} does not return a stream")
    socket, request = await self._request(operation, arguments, None)
    try:
      reply = _StreamedReply(socket, self._reply_timeout, self._io_timeout, operation.name)
      await reply.begin()
      envelope = await reply.start("the reply")
      part = await reply.start("the envelope")
      if part.name == "Header":
        envelope.children.append(await reply.rest(part))
        part = await reply.start("the envelope")
      envelope.children.append(part)
      content = await reply.start("the body")
      part.children.append(content)
      if content.name == "Fault":
        await reply.rest(content)
      self._check(operation, request, envelope)
      if (content.name, content.namespace) != (operation.name + "Response", operation.namespace):
        raise ContractError(f"{operation.name}: the reply body is a {content.name!r} element")
      result = await reply.start(str(content.name))
      if (result.name, result.namespace) != (operation.name + "Result", operation.namespace):
        raise ContractError(f"{operation.name}: the reply holds {result.name!r}, not the result")
      while True:
        found = await reply.event()
        if isinstance(found, EndElement):
          break
        if not isinstance(found, Text):
          raise ContractError(f"{operation.name}: the stream holds a {type(found).__name__}")
        data = found.value if isinstance(found.value, bytes) else parse_bytes(found.characters)
        if data:
          yield data
      for closing in ("response", "body", "envelope"):
        if not isinstance(await reply.event(), EndElement):
          raise ContractError(
            f"{operation.name}: unexpected content before the end of the {closing}"
          )
      await reply.finish()
    finally:
      await socket.stop()
