"""Tests for the .NET Message Framing records ([MC-NMF])."""

import unittest
from typing import List

from pylabrobot.sartorius.ique.driver.wcf import nmf, recordings
from pylabrobot.sartorius.ique.driver.wcf.errors import FramingError, FramingFault
from pylabrobot.sartorius.ique.driver.wcf.nmf import EnvelopeEncoding, FramingMode, FramingRecord

_FAULT = nmf.FAULT_NAMESPACE + "EndpointNotFound"


class _Source:
  """Bytes read as a connection gives them: exactly as many as asked for, or a failure."""

  def __init__(self, data: bytes) -> None:
    """Create the source of some bytes."""
    self._data = data
    self._position = 0

  @property
  def rest(self) -> bytes:
    """The bytes not read yet."""
    return self._data[self._position :]

  async def read(self, count: int) -> bytes:
    """Return the next ``count`` bytes, or fail as a closed connection does."""
    if self._position + count > len(self._data):
      raise ConnectionError("Connection closed before num_bytes are read")
    data = self._data[self._position : self._position + count]
    self._position += count
    return data


def _types(data: bytes) -> List[FramingRecord]:
  """Return the types of the framing records in some bytes."""
  return [record.type for record in nmf.split_records(data)]


class BuildTests(unittest.TestCase):
  """The records an initiator sends."""

  def test_preambles_are_those_dotnet_sends(self) -> None:
    """The duplex and the streamed preamble match what a .NET client sent first."""
    duplex = recordings.load("net48_duplex").connections[0]
    expected = nmf.preamble(
      FramingMode.DUPLEX, duplex.address, EnvelopeEncoding.SOAP12_BINARY_SESSION
    )
    self.assertEqual(duplex.events[0], ("c", expected))
    streamed = recordings.load("net48_streamed").connections[0]
    expected = nmf.preamble(
      FramingMode.SINGLETON_UNSIZED, streamed.address, EnvelopeEncoding.SOAP12_BINARY
    )
    self.assertEqual(streamed.events[0], ("c", expected))

  def test_preamble_fields(self) -> None:
    """Version 1.0, the mode, the Via with its size, the encoding and Preamble End."""
    data = nmf.preamble(FramingMode.DUPLEX, "net.tcp://h/é", EnvelopeEncoding.SOAP12_BINARY)
    self.assertEqual(data.hex(), "000100010202" + "0e" + "net.tcp://h/é".encode().hex() + "03070c")
    with self.assertRaises(FramingError):
      nmf.preamble(FramingMode.DUPLEX, "", EnvelopeEncoding.SOAP12_BINARY)

  def test_sizes_at_each_length(self) -> None:
    """Sizes take one to five bytes; a .NET peer accepts no size beyond 2**31 - 1."""
    cases = {0x7F: "7f", 0x80: "8001", 0x3FFF: "ff7f", 0x4000: "808001", 2**31 - 1: "ffffffff07"}
    for size, encoded in cases.items():
      self.assertEqual(nmf.encode_size(size).hex(), encoded)
    for size in (-1, 2**31):
      with self.assertRaises(FramingError):
        nmf.encode_size(size)

  def test_envelope_chunk_and_fault_records(self) -> None:
    """A sized envelope and a chunk carry their size; neither can be empty."""
    payload = bytes(range(200))
    self.assertEqual(nmf.sized_envelope(payload), b"\x06\xc8\x01" + payload)
    self.assertEqual(nmf.chunk(payload), b"\xc8\x01" + payload)
    self.assertEqual(nmf.fault_record(_FAULT), b"\x08" + bytes((len(_FAULT),)) + _FAULT.encode())
    with self.assertRaises(FramingError):
      nmf.sized_envelope(b"")
    with self.assertRaises(FramingError):
      nmf.chunk(b"")
    self.assertEqual(nmf.END + nmf.CHUNKS_END + nmf.UNSIZED_ENVELOPE_START, b"\x07\x00\x05")


class ReadTests(unittest.IsolatedAsyncioTestCase):
  """The records an initiator reads."""

  async def test_size_at_each_length(self) -> None:
    """Sizes read back, consuming exactly their bytes."""
    for size in (0, 0x7F, 0x80, 0x3FFF, 0x4000, 0x1FFFFF, 0x200000, 2**31 - 1):
      source = _Source(nmf.encode_size(size) + b"\xaa")
      self.assertEqual(await nmf.read_size(source.read), size)
      self.assertEqual(source.rest, b"\xaa")

  async def test_size_beyond_limits_is_rejected(self) -> None:
    """A size above the limit, above 31 bits or longer than five bytes is malformed."""
    with self.assertRaises(FramingError):
      await nmf.read_size(_Source(b"\x81\x01").read, limit=128)
    self.assertEqual(await nmf.read_size(_Source(b"\x80\x01").read, limit=128), 128)
    for data in ("ffffffff08", "ffffffff0f", "ffffffff8f01"):
      with self.assertRaises(FramingError):
        await nmf.read_size(_Source(bytes.fromhex(data)).read)

  async def test_preamble_ack(self) -> None:
    """The receiver acknowledges the preamble, or refuses it with a fault."""
    source = _Source(b"\x0b\x06")
    await nmf.read_preamble_ack(source.read)
    self.assertEqual(source.rest, b"\x06")
    with self.assertRaises(FramingFault) as caught:
      await nmf.read_preamble_ack(_Source(nmf.fault_record(_FAULT)).read)
    self.assertEqual(caught.exception.fault, _FAULT)
    with self.assertRaises(FramingError) as other:
      await nmf.read_preamble_ack(_Source(b"\x07").read)
    self.assertNotIsInstance(other.exception, FramingFault)

  async def test_duplex_records(self) -> None:
    """Sized envelopes until the End record."""
    data = nmf.sized_envelope(b"one") + nmf.sized_envelope(bytes(300)) + nmf.END
    source = _Source(data)
    self.assertEqual(await nmf.read_duplex_record(source.read), b"one")
    self.assertEqual(await nmf.read_duplex_record(source.read), bytes(300))
    self.assertIsNone(await nmf.read_duplex_record(source.read))
    self.assertEqual(source.rest, b"")

  async def test_malformed_duplex_records(self) -> None:
    """An empty or oversized envelope, a fault or another record ends the reading."""
    with self.assertRaises(FramingError):
      await nmf.read_duplex_record(_Source(b"\x06\x00").read)
    with self.assertRaises(FramingError):
      await nmf.read_duplex_record(_Source(nmf.sized_envelope(bytes(11))).read, limit=10)
    with self.assertRaises(FramingFault):
      await nmf.read_duplex_record(_Source(nmf.fault_record(_FAULT)).read)
    with self.assertRaises(FramingError):
      await nmf.read_duplex_record(_Source(b"\x05\x01a\x00").read)
    with self.assertRaises(ConnectionError):
      await nmf.read_duplex_record(_Source(b"\x06\x05abc").read)

  async def test_unsized_envelope(self) -> None:
    """Chunks up to the zero terminator, then the End record."""
    data = nmf.UNSIZED_ENVELOPE_START + nmf.chunk(b"ab") + nmf.chunk(bytes(200))
    source = _Source(data + nmf.CHUNKS_END + nmf.END)
    await nmf.read_unsized_start(source.read)
    self.assertEqual(await nmf.read_chunk(source.read), b"ab")
    self.assertEqual(await nmf.read_chunk(source.read), bytes(200))
    self.assertEqual(await nmf.read_chunk(source.read), b"")
    await nmf.read_end(source.read)
    self.assertEqual(source.rest, b"")
    with self.assertRaises(FramingError):
      await nmf.read_chunk(_Source(b"\x03ab").read, limit=2)

  async def test_faults_in_place_of_unsized_records(self) -> None:
    """A receiver may answer with a fault instead of the envelope or instead of its End."""
    with self.assertRaises(FramingFault):
      await nmf.read_unsized_start(_Source(nmf.fault_record(_FAULT)).read)
    with self.assertRaises(FramingFault):
      await nmf.read_end(_Source(nmf.fault_record(_FAULT)).read)
    with self.assertRaises(FramingError):
      await nmf.read_unsized_start(_Source(b"\x06").read)
    with self.assertRaises(FramingError):
      await nmf.read_end(_Source(b"\x06").read)


class SplitRecordsTests(unittest.TestCase):
  """Splitting recorded byte streams into their records."""

  def test_duplex_session(self) -> None:
    """A duplex session: the preamble records, the envelopes and End on each side."""
    connection = recordings.load("net48_duplex_abort").connections[0]
    records = list(nmf.split_records(connection.sent_by("c")))
    self.assertEqual(
      [record.type for record in records],
      [
        FramingRecord.VERSION,
        FramingRecord.MODE,
        FramingRecord.VIA,
        FramingRecord.KNOWN_ENCODING,
        FramingRecord.PREAMBLE_END,
        FramingRecord.SIZED_ENVELOPE,
        FramingRecord.SIZED_ENVELOPE,
        FramingRecord.END,
      ],
    )
    version, mode, via, encoding = (record.payload for record in records[:4])
    self.assertEqual((version, mode, encoding), (b"\x01\x00", b"\x02", b"\x08"))
    self.assertEqual(via.decode(), connection.address)
    self.assertEqual(
      _types(connection.sent_by("s")),
      [
        FramingRecord.PREAMBLE_ACK,
        FramingRecord.SIZED_ENVELOPE,
        FramingRecord.SIZED_ENVELOPE,
        FramingRecord.END,
      ],
    )

  def test_streamed_exchange(self) -> None:
    """A streamed call: one unsized envelope each way, its chunks joined."""
    connection = recordings.load("net48_streamed").connections[1]
    reply = list(nmf.split_records(connection.sent_by("s")))
    self.assertEqual(
      [record.type for record in reply],
      [FramingRecord.PREAMBLE_ACK, FramingRecord.UNSIZED_ENVELOPE, FramingRecord.END],
    )
    self.assertEqual(reply[1].chunks, (499, 4092, 8, 65535, 3, 999, 6))
    self.assertEqual(len(reply[1].payload), sum(reply[1].chunks))
    request = list(nmf.split_records(connection.sent_by("c")))
    self.assertEqual(request[1].payload, bytes((FramingMode.SINGLETON_UNSIZED,)))
    self.assertEqual(request[3].payload, bytes((EnvelopeEncoding.SOAP12_BINARY,)))

  def test_truncated_records_are_not_handed_out(self) -> None:
    """Data that ends inside a record raises before that record is given."""
    for data in ("0b 0605616263", "0b 0001", "0b 0580", "0b 050361", "0b 0281"):
      with self.subTest(data):
        given: List[FramingRecord] = []
        with self.assertRaises(FramingError):
          for record in nmf.split_records(bytes.fromhex(data.replace(" ", ""))):
            given.append(record.type)
        self.assertEqual(given, [FramingRecord.PREAMBLE_ACK])

  def test_unknown_record_type_is_rejected(self) -> None:
    """Types 0x0D to 0xFF are reserved."""
    for value in (0x0D, 0xFF):
      with self.assertRaises(FramingError):
        list(nmf.split_records(bytes((value,))))


if __name__ == "__main__":
  unittest.main()
