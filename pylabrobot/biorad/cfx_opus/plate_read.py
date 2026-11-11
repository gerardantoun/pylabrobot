"""Decoding of ``.Plateread`` files, the fluorescence the instrument writes for each plate read.

A ``.Plateread`` is an "ICFF" container: every field's bytes packed from offset 0, then the magic
``ICFF`` and one 264-byte index entry per field, then an 8-byte footer ``[index offset][entry
stride]``. Each entry is a NUL-padded 255-byte name, then the field's offset and length as
little-endian ``uint32``, then a flag byte. Scalars are big-endian; the ``WELLDATA`` and
``DARKDATA`` arrays are a little-endian ``uint32`` float count followed by little-endian
``float32`` values.

``WELLDATA`` holds four floats per reading (mean, standard deviation, minimum, maximum), ordered
channel by channel, then row by row, then column by column. Its rows include the instrument's
reference rows after the plate's own rows.
"""

import string
import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from pylabrobot.biorad.cfx_opus.framing import CFXProtocolError

_MAGIC = b"ICFF"
_NAME_LENGTH = 255
_ENTRY_LENGTH = 264
_FOOTER_LENGTH = 8

#: Optical channels in a scan mask (bits 0-5).
CHANNEL_COUNT = 6
#: Plate rows per plate column count, for the blocks the format is known for.
_PLATE_ROWS = {12: 8, 24: 16}


@dataclass(frozen=True)
class IcffField:
  name: str
  offset: int
  length: int


def parse_icff(data: bytes) -> Dict[str, bytes]:
  """Return every field of an ICFF container by name.

  Raises:
    CFXProtocolError: The bytes are not an ICFF container.
  """

  if len(data) < _FOOTER_LENGTH + len(_MAGIC):
    raise CFXProtocolError("File is too short to be an ICFF container")
  index_offset, stride = struct.unpack_from("<II", data, len(data) - _FOOTER_LENGTH)
  if stride != _ENTRY_LENGTH:
    raise CFXProtocolError(f"ICFF entry stride is {stride}, expected {_ENTRY_LENGTH}")
  if data[index_offset : index_offset + len(_MAGIC)] != _MAGIC:
    raise CFXProtocolError("ICFF footer does not point at the ICFF magic")
  entries_start = index_offset + len(_MAGIC)
  entries_end = len(data) - _FOOTER_LENGTH
  if (entries_end - entries_start) % stride != 0:
    raise CFXProtocolError("ICFF index does not divide into whole entries")

  fields: Dict[str, bytes] = {}
  for start in range(entries_start, entries_end, stride):
    name = data[start : start + _NAME_LENGTH].split(b"\x00", 1)[0].decode("ascii")
    offset, length = struct.unpack_from("<II", data, start + _NAME_LENGTH)
    if offset + length > index_offset:
      raise CFXProtocolError(f"ICFF field {name} runs past the value area")
    fields[name] = data[offset : offset + length]
  return fields


def _int(fields: Dict[str, bytes], name: str) -> int:
  value = fields.get(name)
  if value is None or len(value) != 4:
    raise CFXProtocolError(f"Plate read has no 4-byte field {name}")
  return int(struct.unpack(">i", value)[0])


def _float(fields: Dict[str, bytes], name: str) -> Optional[float]:
  value = fields.get(name)
  if value is None:
    return None
  if len(value) != 4:
    raise CFXProtocolError(f"Plate read field {name} is {len(value)} bytes, not 4")
  return float(struct.unpack(">f", value)[0])


def _text(fields: Dict[str, bytes], name: str) -> Optional[str]:
  value = fields.get(name)
  if value is None:
    return None
  return value.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def _float_array(fields: Dict[str, bytes], name: str) -> Tuple[float, ...]:
  value = fields.get(name)
  if value is None or len(value) < 4:
    raise CFXProtocolError(f"Plate read has no array field {name}")
  (count,) = struct.unpack_from("<I", value, 0)
  if len(value) != 4 + 4 * count:
    raise CFXProtocolError(
      f"Plate read field {name} announces {count} floats in {len(value)} bytes"
    )
  return struct.unpack_from(f"<{count}f", value, 4)


@dataclass(frozen=True)
class WellReading:
  """One well's fluorescence in one channel, in relative fluorescence units (RFU)."""

  mean: float
  std: float
  minimum: float
  maximum: float


@dataclass(frozen=True)
class PlateRead:
  """The fluorescence of one plate read.

  ``wells[channel]`` is a row-major grid (``wells[channel][0][0]`` is well A1) for each optical
  channel the read covered, channels numbered from 1.
  """

  cycle: int
  #: The step number the instrument records for the read.
  step: int
  scan_index: int
  channel_mask: int
  block_temperature: Optional[float]
  sample_temperature: Optional[float]
  lid_temperature: Optional[float]
  shuttle_temperature: Optional[float]
  ambient_temperature: Optional[float]
  #: When the read was taken, as the instrument writes it (e.g. ``"Tue, 21 Jul 2026 06:22:23 GMT"``).
  timestamp: Optional[str]
  wells: Dict[int, List[List[WellReading]]]
  #: The instrument's reference row(s) per channel, after the plate's own rows.
  reference: Dict[int, List[List[WellReading]]]
  #: The LED-off background reading per channel.
  dark: Dict[int, WellReading]

  @property
  def channels(self) -> List[int]:
    """The optical channels this read covered, numbered from 1."""

    return sorted(self.wells)

  def mean(self, channel: int) -> List[List[float]]:
    """The mean fluorescence of every well in one channel, as a row-major grid."""

    return [[well.mean for well in row] for row in self.wells[channel]]

  def well(self, channel: int, name: str) -> WellReading:
    """One well's reading, by well name such as ``"B7"``."""

    row = string.ascii_uppercase.index(name[0].upper())
    column = int(name[1:]) - 1
    return self.wells[channel][row][column]


def _readings(values: Tuple[float, ...]) -> List[WellReading]:
  return [WellReading(*values[i : i + 4]) for i in range(0, len(values), 4)]


def decode_plate_read(data: bytes) -> PlateRead:
  """Decode the bytes of a ``Read<NNNNN>.Plateread`` file."""

  fields = parse_icff(data)
  columns = _int(fields, "NUMBERCOLUMNS")
  rows = _int(fields, "NUMBERROWS")
  plate_rows = _PLATE_ROWS.get(columns)
  if plate_rows is None or rows < plate_rows:
    raise CFXProtocolError(f"Plate read has an unknown well grid of {rows} x {columns}")
  channel_mask = _int(fields, "CHANNELMASK")

  readings = _readings(_float_array(fields, "WELLDATA"))
  per_channel = rows * columns
  if len(readings) % per_channel != 0:
    raise CFXProtocolError(f"{len(readings)} readings do not fill {rows} x {columns} wells")
  dark_readings = _readings(_float_array(fields, "DARKDATA"))

  wells: Dict[int, List[List[WellReading]]] = {}
  reference: Dict[int, List[List[WellReading]]] = {}
  dark: Dict[int, WellReading] = {}
  for index in range(len(readings) // per_channel):
    if not channel_mask & (1 << index):
      continue
    channel = index + 1
    grid = [
      readings[index * per_channel + row * columns : index * per_channel + (row + 1) * columns]
      for row in range(rows)
    ]
    wells[channel] = grid[:plate_rows]
    reference[channel] = grid[plate_rows:]
    if index < len(dark_readings):
      dark[channel] = dark_readings[index]

  return PlateRead(
    cycle=_int(fields, "CYCLE"),
    step=_int(fields, "STEP"),
    scan_index=_int(fields, "SCANINDEX"),
    channel_mask=channel_mask,
    block_temperature=_float(fields, "BLOCKTEMP"),
    sample_temperature=_float(fields, "SAMPLETEMP"),
    lid_temperature=_float(fields, "LIDTEMP"),
    shuttle_temperature=_float(fields, "SHUTTLETEMP"),
    ambient_temperature=_float(fields, "AMBIENTTEMP"),
    timestamp=_text(fields, "DATETIME"),
    wells=wells,
    reference=reference,
    dark=dark,
  )
