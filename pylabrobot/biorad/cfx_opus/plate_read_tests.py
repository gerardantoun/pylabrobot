import struct
import unittest
from typing import List, Tuple

from pylabrobot.biorad.cfx_opus.framing import CFXProtocolError
from pylabrobot.biorad.cfx_opus.plate_read import decode_plate_read, parse_icff

ROWS, COLUMNS, CHANNELS = 9, 12, 6


def _icff(fields: List[Tuple[str, bytes]]) -> bytes:
  """Pack values back to back, then the ICFF index and footer."""

  values = bytearray()
  entries = bytearray()
  for name, value in fields:
    entries += name.encode("ascii").ljust(255, b"\x00")
    entries += struct.pack("<II", len(values), len(value)) + b"\x01"
    values += value
  index_offset = len(values)
  return bytes(values) + b"ICFF" + bytes(entries) + struct.pack("<II", index_offset, 264)


def _be_int(value: int) -> bytes:
  return struct.pack(">i", value)


def _be_float(value: float) -> bytes:
  return struct.pack(">f", value)


def _array(values: List[float]) -> bytes:
  return struct.pack("<I", len(values)) + struct.pack(f"<{len(values)}f", *values)


def _plate_read(channel_mask: int = 0x3F, rows: int = ROWS) -> bytes:
  # The mean encodes where a reading sits: channel index * 10000 + row * 100 + column.
  wells: List[float] = []
  for channel in range(CHANNELS):
    for row in range(rows):
      for column in range(COLUMNS):
        wells += [channel * 10000 + row * 100 + column, 5.0, 1.0, 2.0]
  dark: List[float] = []
  for channel in range(CHANNELS):
    dark += [float(channel), 0.5, 0.0, 1.0]
  return _icff(
    [
      ("PRFILEVERSION", _be_int(1)),
      ("SCANINDEX", _be_int(45)),
      ("STEP", _be_int(4)),
      ("CYCLE", _be_int(45)),
      ("BLOCKTEMP", _be_float(59.99)),
      ("SAMPLETEMP", _be_float(60.0)),
      ("LIDTEMP", _be_float(105.1)),
      ("CHANNELMASK", _be_int(channel_mask)),
      ("NUMBERCOLUMNS", _be_int(COLUMNS)),
      ("NUMBERROWS", _be_int(rows)),
      ("DATETIME", b"Tue, 21 Jul 2026 06:22:23 GMT\x00"),
      ("WELLDATA", _array(wells)),
      ("DARKDATA", _array(dark)),
    ]
  )


class ParseIcffTests(unittest.TestCase):
  def test_reads_fields_by_name(self) -> None:
    fields = parse_icff(_icff([("A", b"\x01\x02"), ("B", b"xyz")]))

    self.assertEqual(fields, {"A": b"\x01\x02", "B": b"xyz"})

  def test_rejects_a_file_without_the_magic(self) -> None:
    data = bytearray(_icff([("A", b"\x01")]))
    data[1:5] = b"XXXX"

    with self.assertRaises(CFXProtocolError):
      parse_icff(bytes(data))


class DecodePlateReadTests(unittest.TestCase):
  def test_reads_the_header(self) -> None:
    read = decode_plate_read(_plate_read())

    self.assertEqual((read.cycle, read.step, read.scan_index), (45, 4, 45))
    self.assertAlmostEqual(read.block_temperature or 0, 59.99, places=4)
    self.assertEqual(read.sample_temperature, 60.0)
    self.assertEqual(read.timestamp, "Tue, 21 Jul 2026 06:22:23 GMT")
    self.assertIsNone(read.shuttle_temperature)

  def test_lays_wells_out_by_channel_row_and_column(self) -> None:
    read = decode_plate_read(_plate_read())

    self.assertEqual(read.channels, [1, 2, 3, 4, 5, 6])
    self.assertEqual(read.well(3, "C4").mean, 2 * 10000 + 2 * 100 + 3)
    self.assertEqual(read.well(1, "H12").mean, 7 * 100 + 11)
    self.assertEqual(read.well(6, "A1").std, 5.0)
    self.assertEqual(len(read.mean(2)), 8)
    self.assertEqual(len(read.mean(2)[0]), 12)

  def test_separates_the_reference_row(self) -> None:
    read = decode_plate_read(_plate_read())

    self.assertEqual(len(read.reference[1]), 1)
    self.assertEqual(read.reference[2][0][0].mean, 10000 + 8 * 100)
    self.assertEqual(read.dark[4].mean, 3.0)

  def test_keeps_only_the_channels_the_mask_selects(self) -> None:
    read = decode_plate_read(_plate_read(channel_mask=0x81))

    self.assertEqual(read.channels, [1])

  def test_rejects_an_unknown_grid(self) -> None:
    with self.assertRaises(CFXProtocolError):
      decode_plate_read(_plate_read(rows=7))


if __name__ == "__main__":
  unittest.main()
