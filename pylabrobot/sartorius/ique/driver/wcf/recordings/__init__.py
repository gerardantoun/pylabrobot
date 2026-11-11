"""Recorded exchanges with .NET's WCF stack, and the means to replay them in tests.

Each JSON file in this folder holds what a client and a WCF service sent each other over
one or more ``net.tcp`` connections, as bytes in the order they passed a relay between
the two. ``sample`` declares the service contract the exchanges follow.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import pathlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pylabrobot.io.socket import Socket
from pylabrobot.sartorius.ique.driver.wcf import nmf
from pylabrobot.sartorius.ique.driver.wcf.dictionary import ReaderSession, static_lookup
from pylabrobot.sartorius.ique.driver.wcf.nbfx import decode
from pylabrobot.sartorius.ique.driver.wcf.tree import Element

_FOLDER = pathlib.Path(__file__).parent


@dataclass(frozen=True)
class Connection:
  """What passed over one recorded connection.

  Attributes:
    address: The ``net.tcp`` address of the endpoint the client connected to.
    events: The bytes in the order they passed, each with the side that sent them:
      ``"c"`` for the client and ``"s"`` for the service.
  """

  address: str
  events: Tuple[Tuple[str, bytes], ...]

  def sent_by(self, side: str) -> bytes:
    """Return everything one side sent, joined."""
    return b"".join(data for sender, data in self.events if sender == side)


@dataclass(frozen=True)
class Recording:
  """A recorded scenario.

  Attributes:
    description: What produced the recording.
    connections: The recorded connections in the order they were opened.
    notes: Further facts about the scenario, such as what the client printed.
  """

  description: str
  connections: Tuple[Connection, ...]
  notes: Dict[str, Any]


def load(name: str) -> Recording:
  """Load the recording stored as ``<name>.json`` in this folder."""
  with open(_FOLDER / f"{name}.json", encoding="utf-8") as file:
    content = json.load(file)
  connections = tuple(
    Connection(
      item["address"],
      tuple((sender, base64.b64decode(data)) for sender, data in item["events"]),
    )
    for item in content["connections"]
  )
  return Recording(content["description"], connections, content.get("notes", {}))


def envelopes(connection: Connection, side: str) -> List[Element]:
  """Decode the SOAP envelopes one side sent over a recorded connection, in order."""
  session = ReaderSession()
  result: List[Element] = []
  for record in nmf.split_records(connection.sent_by(side)):
    if record.type == nmf.FramingRecord.SIZED_ENVELOPE:
      result.append(decode(session.read_string_table(record.payload), session.lookup))
    elif record.type == nmf.FramingRecord.UNSIZED_ENVELOPE:
      result.append(decode(record.payload, static_lookup))
  return result


class ReplayMismatch(AssertionError):
  """The code under test wrote something else than the recorded client did."""


class ReplaySocket(Socket):
  """A ``Socket`` that plays the service's side of a recorded connection.

  What the code under test writes must be, byte for byte, what the recorded client wrote.
  The service's bytes become readable in the recorded order: each piece once everything
  the client had written before it has been written again. When the recording is used up,
  reading fails as on a closed connection.
  """

  def __init__(self, connection: Connection, check_writes: bool = True) -> None:
    """Create the socket for one recorded connection.

    Args:
      connection: The recorded connection.
      check_writes: Whether to compare what is written with the recording. Without it,
        the service's bytes are released by the number of bytes written alone.
    """
    super().__init__(human_readable_device_name="recording", host="recording", port=0)
    self._expected = connection.sent_by("c")
    self._check_writes = check_writes
    self._releases: List[Tuple[int, bytes]] = []
    client_bytes = 0
    for sender, data in connection.events:
      if sender == "c":
        client_bytes += len(data)
      else:
        self._releases.append((client_bytes, data))
    self._readable = bytearray()
    self._changed = asyncio.Condition()
    self._reading = False
    self.written = bytearray()
    self.is_set_up = False
    self.is_stopped = False

  @property
  def finished(self) -> bool:
    """Whether everything recorded was written and read."""
    return not self._releases and not self._readable and len(self.written) == len(self._expected)

  def _release(self) -> None:
    """Make readable what the service sent up to this point of the client's writing."""
    while self._releases and self._releases[0][0] <= len(self.written):
      self._readable += self._releases.pop(0)[1]

  async def setup(self) -> None:
    """Start the replay."""
    self.is_set_up = True
    self._release()

  async def stop(self) -> None:
    """End the replay; like the real socket, this must not happen while a read waits."""
    if self._reading:
      raise ReplayMismatch("stop() while a read is waiting: a real Socket would block here")
    self.is_stopped = True

  async def write(self, data: bytes, timeout: Optional[float] = None) -> None:
    """Take bytes from the code under test and compare them with the recording."""
    if not self.is_set_up or self.is_stopped:
      raise RuntimeError("write on a socket that is not set up")
    start = len(self.written)
    expected = self._expected[start : start + len(data)]
    if self._check_writes and bytes(data) != expected:
      differs = next((i for i, (a, b) in enumerate(zip(data, expected)) if a != b), len(expected))
      raise ReplayMismatch(
        f"write differs from the recording at byte {start + differs}:\n"
        f"  written  {bytes(data[max(0, differs - 16) : differs + 32]).hex()}\n"
        f"  recorded {expected[max(0, differs - 16) : differs + 32].hex()}"
      )
    self.written += data
    async with self._changed:
      self._release()
      self._changed.notify_all()

  async def _take(self, count: int) -> bytes:
    """Wait for ``count`` readable bytes and take them."""
    async with self._changed:
      while len(self._readable) < count:
        if not self._releases and len(self.written) >= len(self._expected):
          raise ConnectionError("Connection closed before num_bytes are read")
        await self._changed.wait()
      data = bytes(self._readable[:count])
      del self._readable[:count]
      return data

  async def read_exact(self, num_bytes: int, timeout: Optional[float] = None) -> bytes:
    """Give the next bytes the recorded service sent."""
    if not self.is_set_up or self.is_stopped:
      raise RuntimeError("read on a socket that is not set up")
    limit = self._read_timeout if timeout is None else timeout
    self._reading = True
    try:
      if math.isinf(limit):
        return await self._take(num_bytes)
      try:
        return await asyncio.wait_for(self._take(num_bytes), timeout=limit)
      except asyncio.TimeoutError as error:
        raise TimeoutError(f"Timeout while reading from socket after {limit} seconds") from error
    finally:
      self._reading = False


def sockets(recording: Recording, check_writes: bool = True) -> Sequence[ReplaySocket]:
  """Return a replay socket for each connection of a recording."""
  return [ReplaySocket(connection, check_writes) for connection in recording.connections]
