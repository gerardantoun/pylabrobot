import struct
import unittest
from typing import List, Optional

from pylabrobot.biorad.cfx_opus.cfx_opus import CFXOpus
from pylabrobot.biorad.cfx_opus.framing import MessageReader, encode_command, parse_reply
from pylabrobot.biorad.cfx_opus.protocol import Cycle, Hold, PlateRead, Protocol


def _plate_read_bytes(cycle: int) -> bytes:
  """A minimal one-channel, 1x1 .Plateread the decoder accepts."""

  rows, columns = 8, 12
  wells = [0.0] * (rows * columns * 4)
  dark = [0.0, 0.0, 0.0, 0.0]
  fields = [
    ("CYCLE", struct.pack(">i", cycle)),
    ("STEP", struct.pack(">i", 3)),
    ("SCANINDEX", struct.pack(">i", cycle)),
    ("CHANNELMASK", struct.pack(">i", 0x01)),
    ("NUMBERCOLUMNS", struct.pack(">i", columns)),
    ("NUMBERROWS", struct.pack(">i", rows)),
    ("WELLDATA", struct.pack("<I", len(wells)) + struct.pack(f"<{len(wells)}f", *wells)),
    ("DARKDATA", struct.pack("<I", len(dark)) + struct.pack(f"<{len(dark)}f", *dark)),
  ]
  values = bytearray()
  entries = bytearray()
  for name, value in fields:
    entries += (
      name.encode("ascii").ljust(255, b"\x00")
      + struct.pack("<II", len(values), len(value))
      + b"\x01"
    )
    values += value
  return bytes(values) + b"ICFF" + bytes(entries) + struct.pack("<II", len(values), 264)


class FakeOpus:
  """A scripted CFX Opus: answers the command language from an in-memory run folder and a clock.

  It advances one step each time ``STATUS?`` is polled, so a :meth:`CFXOpus.run_protocol` loop
  walks a whole short run deterministically.
  """

  def __init__(self, protocol_steps: int) -> None:
    self.sent: List[str] = []
    self._reader = MessageReader()
    self._out = bytearray()
    self._run_files = {"Read00001.Plateread": _plate_read_bytes(1)}
    self._report_files = {"20260101_000000_OPUS_OLD.alf": b"old"}
    self._poll = 0
    # STATUS? step text for each poll: a plate read, then a hold (its file lands), then finished.
    self._script = [
      ('60.2;104;1;1;PLATEREAD #h3F;3;"RUN",CALC,ON;97;5;5;5;0;5;0;0;6;60;CLOSED;0', None),
      (
        '95.0;104;1;2;TEMP 95.0,10;2;"RUN",CALC,ON;96;7;2;3;2;0;0;0;6;95;CLOSED;0',
        "Read00002.Plateread",
      ),
      ('20.0;104;0;0;IDLE;0;"RUN",CALC,OFF;32;9;0;0;0;0;0;0;6;20;CLOSED;0', None),
      ('20.0;30;0;0;IDLE;0;"",BLOCK,OFF;0;0;0;0;0;0;0;0;0;20;CLOSED;0', None),
    ]

  async def setup(self) -> None: ...

  async def stop(self) -> None: ...

  async def write(self, data: bytes, timeout: Optional[float] = None) -> None:
    for message in self._reader.feed(data):
      self._handle(message.payload.decode("ascii").rstrip("\r\n"))

  async def read(self, timeout: Optional[float] = None) -> bytes:
    chunk, self._out = bytes(self._out), bytearray()
    return chunk

  def _reply(self, payload: bytes) -> None:
    self._out += bytes([1, 0, 0]) + len(payload).to_bytes(2, "big") + payload

  def _handle(self, command: str) -> None:
    self.sent.append(command)
    verb = command.split(" ", 1)[0]
    if command == "*IDN?":
      self._reply(b"BIO-RAD LABORATORIES,CFX Opus 96,OP123456,1.5.144;0000")
    elif command == "STATUS?":
      value, lands = self._script[min(self._poll, len(self._script) - 1)]
      if lands and self._poll >= 1:
        self._run_files[lands] = _plate_read_bytes(2)
      self._poll += 1
      self._reply(value.encode("ascii") + b";0000")
    elif command == "SUPERLOCKDOWNMODE?":
      self._reply(b"OFF;0000")
    elif command == "ERRORS?":
      self._reply(b"0;0000")
    elif verb == "GETFILESLEN":
      directory = command.split(" ", 1)[1]
      files = self._files_for(directory)
      if not files:
        self._out += bytes([1, 0, 2]) + (4).to_bytes(2, "big") + bytes.fromhex("07 00 09 00")
      else:
        listing = ",".join(files)
        self._reply(f"{len(listing)};0000".encode("ascii"))
        self._buffered = listing
    elif verb == "LISTALLFILES":
      self._reply(f"{self._buffered};0000".encode("ascii"))
    elif verb == "GETFILESIZE":
      path = command.split(" ", 1)[1]
      self._reply(f"{len(self._file(path))};0000".encode("ascii"))
    elif verb == "GETFILE":
      self._reply(self._file(command.split(" ", 1)[1]))
    elif verb == "DELFILE":
      path = command.split(" ", 1)[1]
      self._report_files.pop(path.rsplit("\\", 1)[-1], None)
      self._reply(b"0000")
    else:
      self._reply(b"0000")

  def _files_for(self, directory: str) -> List[str]:
    if directory.endswith("CurrentRun"):
      return sorted(self._run_files)
    if directory.endswith("PCRunReport"):
      return sorted(self._report_files)
    return []

  def _file(self, path: str) -> bytes:
    name = path.rsplit("\\", 1)[-1]
    return self._run_files.get(name) or self._report_files.get(name) or b""


class RunProtocolTests(unittest.IsolatedAsyncioTestCase):
  async def _opus(self, steps: int) -> CFXOpus:
    opus = CFXOpus()
    opus.io = FakeOpus(steps)  # type: ignore[assignment]
    await opus.setup()
    return opus

  async def test_identifies_the_instrument(self) -> None:
    opus = await self._opus(3)
    identity = await opus.identify()

    self.assertEqual(identity.model, "CFX Opus 96")
    self.assertEqual(identity.serial_number, "OP123456")

  async def test_runs_a_protocol_and_collects_both_reads(self) -> None:
    opus = await self._opus(3)
    protocol = Protocol(
      steps=[Cycle([Hold(95, 10), Hold(60, 30), PlateRead()], count=2)], volume=20
    )

    reads = await opus.run_protocol(protocol, run_name="RUN", poll_interval=0)

    self.assertEqual([r.name for r in reads], ["Read00001.Plateread", "Read00002.Plateread"])
    self.assertEqual(reads[0].read.cycle, 1)
    fake = opus.io
    assert isinstance(fake, FakeOpus)
    self.assertIn("PROTOCOL 'PCRUN'", fake.sent)
    self.assertIn("GOTO 1,1", fake.sent)
    self.assertTrue(any(c.startswith("RemoteRun ") for c in fake.sent))
    self.assertEqual(fake.sent.count("CANCEL"), 1)  # acknowledged once
    # The stale report was cleared before the run.
    self.assertIn("DELFILE \\Storage Card\\PCRunReport\\20260101_000000_OPUS_OLD.alf", fake.sent)

  async def test_opens_the_lid_only_when_closed(self) -> None:
    opus = await self._opus(1)
    fake = opus.io
    assert isinstance(fake, FakeOpus)
    fake._script = [('20;30;0;0;IDLE;0;"",BLOCK,OFF;0;0;0;0;0;0;0;0;0;20;OPEN;0', None)]

    await opus.open_lid()

    self.assertNotIn("LID OPEN", fake.sent)

  async def test_rejects_a_run_name_with_a_separator(self) -> None:
    opus = await self._opus(1)
    with self.assertRaises(ValueError):
      await opus.start_run(run_name="a,b")


class EncodeRoundTripTests(unittest.TestCase):
  def test_a_framed_command_reads_back(self) -> None:
    (message,) = MessageReader().feed(encode_command("STATUS?"))
    self.assertEqual(message.payload, b"STATUS?\r\n")

  def test_reply_parsing_on_the_wire_value(self) -> None:
    self.assertEqual(parse_reply(b"OFF;0000").value, "OFF")


if __name__ == "__main__":
  unittest.main()
