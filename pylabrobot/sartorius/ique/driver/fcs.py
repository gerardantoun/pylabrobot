"""Events of the iQue Screener PLUS and their FCS 3.0 list-mode files.

The instrument streams every event as a fixed number of single-precision values, one per
parameter, in a fixed order that depends on its lasers (:data:`PARAMETER_TABLES`). The last
parameter is the event time in ticks of 5 ms since the acquisition started.
:class:`EventData` holds such a stream in a flat buffer without needing numpy.

An FCS 3.0 file is three byte ranges::

  offset 0     "FCS3.0" and four spaces
  offset 10    six right-justified fields of 8 ASCII characters: first and last byte of TEXT,
               of DATA and of ANALYSIS (0 and 0 when there is none)
  offset 58    TEXT: delimiter key delimiter value delimiter key ... delimiter
  after TEXT   DATA: events x parameters values

A delimiter inside a key or value is written twice. When the last byte of DATA lies beyond
99,999,999 the HEADER cannot hold it: both DATA fields are 0 there and readers use the
``$BEGINDATA`` and ``$ENDDATA`` keywords. The files written here, like the instrument's own,
hold big-endian 32-bit floats (``$DATATYPE`` ``F``, ``$BYTEORD`` ``4,3,2,1``), unscaled.
"""

from __future__ import annotations

import array
import datetime
import decimal
import io
import logging
import math
import pathlib
import re
import sys
import zlib
from dataclasses import dataclass
from typing import (
  Any,
  BinaryIO,
  Dict,
  Iterable,
  Iterator,
  List,
  Literal,
  Mapping,
  Optional,
  Sequence,
  Tuple,
  Union,
)

logger = logging.getLogger(__name__)

Laser = Literal["violet", "blue", "yellow", "red"]
Measurement = Literal["height", "area", "width", "time"]
OpticalConfiguration = Literal["VBR", "VYB", "BR"]

# Seconds per tick of the instrument's time parameter.
TIME_STEP = 0.005

FCS_VERSION = "FCS3.0"
HEADER_LENGTH = 58
# The largest offset one 8-character HEADER field can hold.
MAX_HEADER_OFFSET = 99_999_999
# The largest offset a 10-digit keyword value can hold.
MAX_KEYWORD_OFFSET = 9_999_999_999
DEFAULT_DELIMITER = "/"
PARAMETER_BITS = 32
PARAMETER_RANGE = 16_777_216
# Instrument metadata is written under keywords with this prefix.
METADATA_PREFIX = "IC$"
WELL_KEYWORD = METADATA_PREFIX + "WellID"

# Wavelength of each laser, in nm.
LASER_WAVELENGTHS: Dict[Laser, int] = {"violet": 405, "blue": 488, "yellow": 561, "red": 640}

_VALUE_SIZE = PARAMETER_BITS // 8
_VALUES_PER_CHUNK = 1 << 18
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


class FcsError(ValueError):
  """Data is not an FCS file this module can read, or content cannot be written as one."""


# ---------------------------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------------------------


class EventData:
  """Events of one acquisition: a flat single-precision buffer and its parameter names.

  The buffer is event-major: the value of parameter `p` of event `e` is
  `values[e * parameter_count + p]`. Values are kept as the instrument sent them.

  Attributes:
    parameter_names: Short name of each parameter (``$PnN``), in buffer order.
    long_names: Long name of each parameter (``$PnS``), in the same order.
    values: The buffer, an ``array('f')``.
  """

  def __init__(
    self,
    parameter_names: Sequence[str],
    values: Optional[Iterable[float]] = None,
    long_names: Optional[Sequence[str]] = None,
  ) -> None:
    """Create a container.

    Args:
      parameter_names: Short parameter names; they must be unique.
      values: The values, event after event. An ``array('f')`` becomes the buffer itself,
        without a copy; anything else is copied into a new one.
      long_names: Long parameter names; the short names when omitted.

    Raises:
      ValueError: If there is no parameter, names repeat, the two name lists differ in
        length, or the values do not make a whole number of events.
    """
    self.parameter_names: Tuple[str, ...] = tuple(parameter_names)
    self.long_names: Tuple[str, ...] = (
      self.parameter_names if long_names is None else tuple(long_names)
    )
    if not self.parameter_names:
      raise ValueError("events need at least one parameter")
    if len(set(self.parameter_names)) != len(self.parameter_names):
      raise ValueError("parameter names must be unique")
    if len(self.long_names) != len(self.parameter_names):
      raise ValueError("there must be one long name per parameter")
    if isinstance(values, array.array) and values.typecode == "f":
      self.values: array.array[float] = values
    else:
      self.values = array.array("f", () if values is None else values)
    self._check_whole_events(len(self.values))

  def _check_whole_events(self, value_count: int) -> None:
    """Check that a number of values is a whole number of events.

    Raises:
      ValueError: If it is not.
    """
    if value_count % self.parameter_count:
      raise ValueError(
        f"{value_count} values are not a whole number of events of "
        f"{self.parameter_count} parameters"
      )

  @property
  def parameter_count(self) -> int:
    """The number of values per event."""
    return len(self.parameter_names)

  @property
  def event_count(self) -> int:
    """The number of events."""
    return len(self.values) // self.parameter_count

  def __len__(self) -> int:
    """Return the number of events."""
    return self.event_count

  def __eq__(self, other: object) -> bool:
    """Return whether another container has the same names and values."""
    if not isinstance(other, EventData):
      return NotImplemented
    return (
      self.parameter_names == other.parameter_names
      and self.long_names == other.long_names
      and self.values == other.values
    )

  __hash__ = None  # type: ignore[assignment]

  def parameter_index(self, parameter: Union[str, int]) -> int:
    """Return the position of a parameter given by short name or by position.

    Raises:
      KeyError: If no parameter has that name.
      IndexError: If the position is out of range.
    """
    if isinstance(parameter, str):
      if parameter not in self.parameter_names:
        raise KeyError(f"no parameter named {parameter!r}")
      return self.parameter_names.index(parameter)
    if not 0 <= parameter < self.parameter_count:
      raise IndexError(f"parameter {parameter} is out of range")
    return parameter

  def event(self, index: int) -> Tuple[float, ...]:
    """Return the values of one event, in parameter order.

    Raises:
      IndexError: If there is no such event.
    """
    if not 0 <= index < self.event_count:
      raise IndexError(f"event {index} is out of range")
    start = index * self.parameter_count
    return tuple(self.values[start : start + self.parameter_count])

  def parameter(self, parameter: Union[str, int]) -> array.array[float]:
    """Return one parameter of every event as a new ``array('f')``.

    Raises:
      KeyError: If no parameter has that name.
      IndexError: If the position is out of range.
    """
    return self.values[self.parameter_index(parameter) :: self.parameter_count]

  def slice(self, start: int, stop: int) -> EventData:
    """Return events `start` up to, not including, `stop` as a new container with a copy.

    Raises:
      IndexError: If the range is not within the events.
    """
    if not 0 <= start <= stop <= self.event_count:
      raise IndexError(f"events {start} to {stop} are out of range")
    count = self.parameter_count
    return EventData(
      self.parameter_names, self.values[start * count : stop * count], self.long_names
    )

  def extend(self, values: Iterable[float]) -> None:
    """Append the values of more events, event after event.

    Raises:
      ValueError: If the values do not make a whole number of events; nothing is appended.
    """
    chunk = values
    if not (isinstance(chunk, array.array) and chunk.typecode == "f"):
      chunk = array.array("f", values)
    self._check_whole_events(len(chunk))
    self.values.extend(chunk)

  def to_numpy(self) -> Any:
    """Return the events as a new numpy ``float32`` array of shape (events, parameters).

    Raises:
      ImportError: If numpy, an optional dependency, is not installed.
    """
    try:
      import numpy  # type: ignore
    except ImportError as error:
      raise ImportError(
        "EventData.to_numpy() needs the optional dependency numpy (pip install numpy)"
      ) from error
    flat = numpy.array(self.values, dtype=numpy.float32)
    return flat.reshape(self.event_count, self.parameter_count)


# ---------------------------------------------------------------------------------------------
# Parameter tables
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Parameter:
  """One value of an event.

  Attributes:
    name: The short name, such as ``BL1-H``.
    long_name: The long name; the instrument uses the short name for both.
    measurement: Pulse height, pulse area, pulse width or event time.
    laser: The laser that excites a fluorescence channel; ``None`` for scatter, width and
      time. Channels of a laser that is switched off stay 0.
    emission_filter: Centre and width in nm of a fluorescence channel's filter, such as
      ``530/30``.
  """

  name: str
  long_name: str
  measurement: Measurement
  laser: Optional[Laser] = None
  emission_filter: Optional[str] = None


@dataclass(frozen=True)
class ParameterTable:
  """The parameters of one optical configuration, in the order the instrument sends them.

  Attributes:
    configuration: Which lasers the instrument has.
    cytometer: The cytometer name its files carry (``$CYT``).
    project: The project name its files carry (``$PROJ``).
    file_name: The file name its files state (``$FIL``); a constant, not the real name.
    lasers: The lasers.
    parameters: The parameters; width and time are the last two.
  """

  configuration: OpticalConfiguration
  cytometer: str
  project: str
  file_name: str
  lasers: Tuple[Laser, ...]
  parameters: Tuple[Parameter, ...]

  @property
  def names(self) -> Tuple[str, ...]:
    """The short names, in order."""
    return tuple(parameter.name for parameter in self.parameters)

  @property
  def long_names(self) -> Tuple[str, ...]:
    """The long names, in order."""
    return tuple(parameter.long_name for parameter in self.parameters)

  @property
  def width_index(self) -> int:
    """Position of the width parameter, the second to last."""
    return len(self.parameters) - 2

  @property
  def time_index(self) -> int:
    """Position of the time parameter, the last."""
    return len(self.parameters) - 1

  def new_events(self, values: Optional[Iterable[float]] = None) -> EventData:
    """Return a container for events of this configuration."""
    return EventData(self.names, values, self.long_names)


_Channel = Tuple[str, Optional[Laser], Optional[str]]
_SCATTER: Tuple[_Channel, ...] = (("FSC", None, None), ("SSC", None, None))


def _parameters(channels: Sequence[_Channel]) -> Tuple[Parameter, ...]:
  """Return the parameters of a configuration from its pulse channels.

  Every channel is reported twice: the heights of all channels come first, then the areas in
  the same order, then the width and the time.
  """
  heights = [Parameter(f"{c}-H", f"{c}-H", "height", laser, band) for c, laser, band in channels]
  areas = [Parameter(f"{c}-A", f"{c}-A", "area", laser, band) for c, laser, band in channels]
  tail = [Parameter("Width", "Width", "width"), Parameter("Time", "Time", "time")]
  return tuple(heights + areas + tail)


_VBR_TABLE = ParameterTable(
  configuration="VBR",
  cytometer="IntelliCyt iQue3",
  project="iQue3",
  file_name="IntelliCyt_iQue3.fcs",
  lasers=("violet", "blue", "red"),
  parameters=_parameters(
    _SCATTER
    + (
      ("BL1", "blue", "530/30"),
      ("BL2", "blue", "572/28"),
      ("BL3", "blue", "615/24"),
      ("BL4", "blue", "675/30"),
      ("BL5", "blue", "780/60"),
      ("RL1", "red", "675/30"),
      ("RL2", "red", "780/60"),
      ("VL1", "violet", "445/45"),
      ("VL2", "violet", "530/30"),
      ("VL3", "violet", "572/28"),
      ("VL4", "violet", "615/24"),
      ("VL5", "violet", "675/30"),
      ("VL6", "violet", "780/60"),
    )
  ),
)

_VYB_TABLE = ParameterTable(
  configuration="VYB",
  cytometer="IntelliCyt iQuePlus VYB",
  project="iQuePlusVYB",
  file_name="IntelliCyt_iQuePlusVYB.fcs",
  lasers=("violet", "yellow", "blue"),
  parameters=_parameters(
    _SCATTER
    + (
      ("BL1", "blue", "530/30"),
      ("BL3", "blue", "615/20"),
      ("BL4", "blue", "660/20"),
      ("VL1", "violet", "445/45"),
      ("VL2", "violet", "530/30"),
      ("VL3", "violet", "586/20"),
      ("VL4", "violet", "615/20"),
      ("VL5", "violet", "660/20"),
      ("VL6", "violet", "780/60"),
      ("YL1", "yellow", "586/20"),
      ("YL2", "yellow", "615/20"),
      ("YL3", "yellow", "660/20"),
      ("YL4", "yellow", "780/60"),
    )
  ),
)

_BR_TABLE = ParameterTable(
  configuration="BR",
  cytometer="IntelliCyt iQuePlus BR",
  project="iQuePlusBR",
  file_name="IntelliCyt_iQuePlusBR.fcs",
  lasers=("blue", "red"),
  parameters=_parameters(
    _SCATTER
    + (
      ("BL1", "blue", "530/30"),
      ("BL2", "blue", "572/28"),
      ("BL4", "blue", "675/30"),
      ("BL5", "blue", "780/60"),
      ("RL1", "red", "675/30"),
      ("RL2", "red", "780/60"),
    )
  ),
)

# The parameters of each optical configuration: violet-blue-red (13 fluorescence channels,
# 32 parameters), violet-yellow-blue (13 and 32) and blue-red (6 and 18).
PARAMETER_TABLES: Dict[OpticalConfiguration, ParameterTable] = {
  "VBR": _VBR_TABLE,
  "VYB": _VYB_TABLE,
  "BR": _BR_TABLE,
}

# The optical configuration of each instrument family member, by its system kind.
SYSTEM_KIND_CONFIGURATIONS: Dict[str, OpticalConfiguration] = {
  "iQue3": "VBR",
  "iQue3HD": "VBR",
  "iQuePlusVYB": "VYB",
  "iQuePlusBR": "BR",
}


def parameter_table_for_system_kind(system_kind: str) -> ParameterTable:
  """Return the parameter table of an instrument family member.

  Args:
    system_kind: The system kind the instrument reports, such as ``iQue3``.

  Raises:
    ValueError: If the system kind is not a member of the iQue Screener PLUS family.
  """
  if system_kind not in SYSTEM_KIND_CONFIGURATIONS:
    raise ValueError(f"no parameter table for system kind {system_kind!r}")
  return PARAMETER_TABLES[SYSTEM_KIND_CONFIGURATIONS[system_kind]]


# ---------------------------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------------------------


def time_parameter_index(parameter_names: Sequence[str]) -> int:
  """Return the position of the time parameter: the first whose name contains "time".

  The comparison ignores case.

  Raises:
    FcsError: If no parameter is a time.
  """
  for index, name in enumerate(parameter_names):
    if "time" in name.lower():
      return index
  raise FcsError("the events have no time parameter")


def time_step(keywords: Mapping[str, str]) -> float:
  """Return the seconds per tick of the time parameter that a file states (``$TIMESTEP``).

  Raises:
    FcsError: If the keyword is missing or not a positive number.
  """
  if "$TIMESTEP" not in keywords:
    raise FcsError("the file does not state its time step ($TIMESTEP)")
  text = keywords["$TIMESTEP"]
  try:
    step = float(text)
  except ValueError as error:
    raise FcsError(f"$TIMESTEP {text!r} is not a number") from error
  if not (math.isfinite(step) and step > 0):
    raise FcsError(f"$TIMESTEP {text!r} is not a positive number")
  return step


def event_times(events: EventData, step: float = TIME_STEP) -> array.array[float]:
  """Return the time of every event in seconds since the acquisition started.

  The time parameter counts ticks of `step` seconds, 5 ms on this instrument.

  Args:
    events: The events.
    step: Seconds per tick.

  Returns:
    A double-precision ``array('d')``, one time per event.

  Raises:
    FcsError: If the events have no time parameter.
  """
  ticks = events.parameter(time_parameter_index(events.parameter_names))
  return array.array("d", (tick * step for tick in ticks))


# ---------------------------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------------------------

# Keywords the writer fills in itself; a caller may not supply them.
_STRUCTURAL_KEYWORDS = frozenset(
  {
    "$DATATYPE",
    "$MODE",
    "$BYTEORD",
    "$NEXTDATA",
    "$PAR",
    "$TOT",
    "$BEGINSTEXT",
    "$ENDSTEXT",
    "$BEGINDATA",
    "$ENDDATA",
    "$BEGINANALYSIS",
    "$ENDANALYSIS",
  }
)
_PARAMETER_KEYWORD = re.compile(r"\$P[0-9]+[BRNES]")
# Where caller keywords go so that TEXT lists keywords in the order of the instrument's files.
_KEYWORDS_BEFORE_PARAMETERS = ("$FIL", "$OP", "$SMNO")
_KEYWORDS_AFTER_PARAMETERS = (
  "$DATE",
  "$CYT",
  "$SPILLOVER",
  "$TIMESTEP",
  "$PROJ",
  "$BTIM",
  "$ETIM",
)


def file_crc32(data: bytes) -> int:
  """Return the checksum the instrument reports for an FCS file.

  It is the standard CRC-32 of the whole file. The instrument carries it beside the file; an
  FCS file itself has no checksum field here.
  """
  return zlib.crc32(data) & 0xFFFFFFFF


def _check_delimiter(delimiter: str) -> None:
  """Check that a TEXT delimiter is one ASCII character other than NUL.

  Raises:
    FcsError: If it is not.
  """
  if len(delimiter) != 1 or not 0 < ord(delimiter) < 127:
    raise FcsError(f"{delimiter!r} cannot delimit TEXT; it must be one ASCII character")


def _check_token(token: str, delimiter: str, what: str) -> None:
  """Check that a key or value can be written unambiguously.

  An empty one would read back as an escaped delimiter and merge its neighbours; so would one
  that starts or ends with the delimiter.

  Raises:
    FcsError: If the token is empty or starts or ends with the delimiter.
  """
  if not token:
    raise FcsError(f"{what} is empty; FCS cannot hold an empty key or value")
  if token.startswith(delimiter) or token.endswith(delimiter):
    raise FcsError(f"{what} {token!r} starts or ends with the delimiter {delimiter!r}")


def _normalised_keywords(keywords: Mapping[str, str], delimiter: str) -> Dict[str, str]:
  """Return caller keywords checked and with standard (``$``) keys in upper case.

  Raises:
    FcsError: If a key is not ASCII, is one the writer fills in, occurs twice once case is
      ignored, or a key or value cannot be written.
  """
  normalised: Dict[str, str] = {}
  for key, value in keywords.items():
    _check_token(key, delimiter, "a keyword")
    _check_token(value, delimiter, f"the value of {key}")
    if not key.isascii():
      raise FcsError(f"keyword {key!r} is not ASCII")
    standard = key.upper()
    if standard in _STRUCTURAL_KEYWORDS or _PARAMETER_KEYWORD.fullmatch(standard):
      raise FcsError(f"keyword {key} is written from the events and cannot be supplied")
    name = standard if key.startswith("$") else key
    if name.upper() in (existing.upper() for existing in normalised):
      raise FcsError(f"keyword {key} is supplied twice")
    normalised[name] = value
  return normalised


def _parameter_keywords(events: EventData) -> List[Tuple[str, str]]:
  """Return the per-parameter keywords: bits, range, name, amplification and long name.

  All bit widths come first, then all ranges, and so on. Values are linear (``0,0``) 32-bit
  floats with the instrument's range of 2^24.
  """
  numbers = range(1, events.parameter_count + 1)
  pairs = [(f"$P{n}B", str(PARAMETER_BITS)) for n in numbers]
  pairs += [(f"$P{n}R", str(PARAMETER_RANGE)) for n in numbers]
  pairs += [(f"$P{n}N", name) for n, name in zip(numbers, events.parameter_names)]
  pairs += [(f"$P{n}E", "0,0") for n in numbers]
  pairs += [(f"$P{n}S", name) for n, name in zip(numbers, events.long_names)]
  return pairs


def _offset_text(offset: int) -> str:
  """Write an offset as 10 digits, so TEXT has the same length whatever the offsets are.

  Raises:
    FcsError: If the offset needs more than 10 digits.
  """
  if offset > MAX_KEYWORD_OFFSET:
    raise FcsError(f"the file is too large for FCS 3.0: offset {offset}")
  return f"{offset:010d}"


def _ordered_pairs(
  events: EventData, keywords: Dict[str, str], data_begin: int, data_end: int
) -> List[Tuple[str, str]]:
  """Return every keyword of a file with its value, in the order they are written.

  The order is that of the instrument's own files: file name, operator and sample; data type
  and layout; the per-parameter keywords; the event count; date, cytometer and time
  keywords; segment offsets; then any other keyword the caller supplied, in the caller's
  order. There is no supplemental TEXT and no ANALYSIS segment, so their offsets are 0.
  """
  remaining = dict(keywords)
  pairs = [(key, remaining.pop(key)) for key in _KEYWORDS_BEFORE_PARAMETERS if key in remaining]
  pairs += [
    ("$DATATYPE", "F"),
    ("$MODE", "L"),
    ("$BYTEORD", "4,3,2,1"),
    ("$NEXTDATA", "0"),
    ("$PAR", str(events.parameter_count)),
  ]
  pairs += _parameter_keywords(events)
  pairs.append(("$TOT", f"{events.event_count:010d}"))
  pairs += [(key, remaining.pop(key)) for key in _KEYWORDS_AFTER_PARAMETERS if key in remaining]
  pairs += [
    ("$BEGINSTEXT", "0"),
    ("$ENDSTEXT", "0"),
    ("$ENDDATA", _offset_text(data_end)),
    ("$BEGINDATA", _offset_text(data_begin)),
    ("$BEGINANALYSIS", "0"),
    ("$ENDANALYSIS", "0"),
  ]
  return pairs + list(remaining.items())


def _text_segment(pairs: Sequence[Tuple[str, str]], delimiter: str) -> bytes:
  """Return the TEXT segment: every key and value between delimiters, delimiters doubled.

  The text is encoded as UTF-8, which is plain ASCII unless a value has other characters.
  """
  doubled = delimiter * 2
  tokens = [token.replace(delimiter, doubled) for pair in pairs for token in pair]
  return (delimiter + delimiter.join(tokens) + delimiter).encode("utf-8")


def _data_offsets(text_length: int, data_length: int) -> Tuple[int, int]:
  """Return the first and last byte of DATA, which follows TEXT directly.

  An empty DATA segment is stated as one byte long, first and last offset equal.
  """
  begin = HEADER_LENGTH + text_length
  return begin, begin + max(data_length, 1) - 1


def _header_segment(text_length: int, data_length: int) -> bytes:
  """Return the 58 HEADER bytes for a TEXT and a DATA segment of the given lengths.

  Both DATA fields are 0 when the last byte of DATA lies beyond what a field can hold.

  Raises:
    FcsError: If TEXT itself ends beyond what a field can hold.
  """
  text_end = HEADER_LENGTH + text_length - 1
  if text_end > MAX_HEADER_OFFSET:
    raise FcsError(f"TEXT is too long for an FCS HEADER: it ends at byte {text_end}")
  data_begin, data_end = _data_offsets(text_length, data_length)
  if data_end > MAX_HEADER_OFFSET:
    data_begin, data_end = 0, 0
  fields = (HEADER_LENGTH, text_end, data_begin, data_end, 0, 0)
  return (FCS_VERSION.ljust(10) + "".join(f"{field:>8d}" for field in fields)).encode("ascii")


def _data_chunks(values: array.array[float]) -> Iterator[bytes]:
  """Yield the DATA segment piece by piece: every value as a big-endian 32-bit float.

  Raises:
    FcsError: If this platform's ``array('f')`` is not made of 32-bit floats.
  """
  if values.itemsize != _VALUE_SIZE:
    raise FcsError(f"array('f') holds {values.itemsize}-byte values here; 4 are needed")
  for start in range(0, len(values), _VALUES_PER_CHUNK):
    chunk = values[start : start + _VALUES_PER_CHUNK]
    if sys.byteorder == "little":
      chunk.byteswap()
    yield chunk.tobytes()


def write_fcs_to(
  stream: BinaryIO,
  events: EventData,
  keywords: Optional[Mapping[str, str]] = None,
  *,
  delimiter: str = DEFAULT_DELIMITER,
) -> None:
  """Write events to a binary stream as an FCS 3.0 list-mode file.

  The writer fills in what follows from the events: data type, byte order, mode, the
  parameter keywords (``$PnB`` 32, ``$PnR`` 16777216, ``$PnN``, ``$PnE`` ``0,0``, ``$PnS``),
  ``$TOT`` and the segment offsets. Everything else comes from `keywords`; see
  :func:`instrument_keywords` for the set this instrument's files carry.

  Args:
    stream: Where to write; it is written front to back, without seeking.
    events: The events; values are written unscaled.
    keywords: Further keywords and their values. Standard keywords start with ``$``.
    delimiter: The TEXT delimiter. One inside a key or value is written twice.

  Raises:
    FcsError: If a keyword or value cannot be written (empty, starting or ending with the
      delimiter, a keyword the writer fills in) or the file would be too large for FCS 3.0.
  """
  _check_delimiter(delimiter)
  supplied = _normalised_keywords(keywords or {}, delimiter)
  data_length = len(events.values) * _VALUE_SIZE
  text_length = len(_text_segment(_ordered_pairs(events, supplied, 0, 0), delimiter))
  data_begin, data_end = _data_offsets(text_length, data_length)
  text = _text_segment(_ordered_pairs(events, supplied, data_begin, data_end), delimiter)
  stream.write(_header_segment(len(text), data_length))
  stream.write(text)
  for chunk in _data_chunks(events.values):
    stream.write(chunk)
  logger.debug("wrote FCS: %d events of %d parameters", events.event_count, events.parameter_count)


def write_fcs(
  events: EventData,
  keywords: Optional[Mapping[str, str]] = None,
  *,
  delimiter: str = DEFAULT_DELIMITER,
) -> bytes:
  """Return events as the bytes of an FCS 3.0 list-mode file.

  See :func:`write_fcs_to`, which this calls, for the arguments and errors. For a large
  acquisition, writing to a file with :func:`write_fcs_to` avoids holding the file in memory.
  """
  buffer = io.BytesIO()
  write_fcs_to(buffer, events, keywords, delimiter=delimiter)
  return buffer.getvalue()


def _clock_text(seconds: float) -> str:
  """Write a duration as ``HH:MM:SS``, the hours wrapping at 24."""
  whole = int(seconds)
  return f"{whole // 3600 % 24:02d}:{whole // 60 % 60:02d}:{whole % 60:02d}"


def _date_text(date: datetime.date) -> str:
  """Write a date as ``dd-MMM-yyyy`` with an upper-case English month, as FCS asks."""
  return f"{date.day:02d}-{_MONTHS[date.month - 1]}-{date.year:04d}"


def _number_text(value: float) -> str:
  """Write a number with a point and no exponent, whatever the locale."""
  return format(decimal.Decimal(repr(float(value))), "f")


def instrument_keywords(
  table: ParameterTable,
  *,
  operator: str = "UNK",
  sample: str = "A01",
  acquired_on: Optional[datetime.date] = None,
  events: Optional[EventData] = None,
  metadata: Optional[Mapping[str, str]] = None,
  step: float = TIME_STEP,
) -> Dict[str, str]:
  """Return the keywords that this instrument's files carry beside the parameter keywords.

  They are ``$FIL``, ``$OP``, ``$SMNO``, ``$DATE``, ``$CYT``, ``$TIMESTEP``, ``$PROJ``,
  ``$BTIM``, ``$ETIM`` and one ``IC$<key>`` per metadata entry. Pass the result to
  :func:`write_fcs`.

  Args:
    table: The parameter table of the instrument; it names the cytometer.
    operator: Who ran the acquisition; ``UNK`` when unknown.
    sample: The sample name; the instrument writes ``A01`` for a whole plate.
    acquired_on: The date of the acquisition; ``$DATE`` is left out without it.
    events: The events of the file. With them ``$BTIM`` and ``$ETIM`` are written as the
      instrument does: the time of the first and of the last event since the acquisition
      started, not clock times.
    metadata: Instrument metadata such as ``SerialNumber`` or ``Protocol``, by key without
      prefix.
    step: Seconds per tick of the time parameter.

  Raises:
    FcsError: If `events` is given but has no time parameter.
  """
  keywords = {"$FIL": table.file_name, "$OP": operator, "$SMNO": sample}
  if acquired_on is not None:
    keywords["$DATE"] = _date_text(acquired_on)
  keywords["$CYT"] = table.cytometer
  keywords["$TIMESTEP"] = _number_text(step)
  keywords["$PROJ"] = table.project
  if events is not None:
    times = event_times(events, step)
    keywords["$BTIM"] = _clock_text(times[0] if times else 0.0)
    keywords["$ETIM"] = _clock_text(times[-1] if times else 0.0)
  for key, value in (metadata or {}).items():
    keywords[METADATA_PREFIX + key] = value
  return keywords


# ---------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------


def _header_fields(data: bytes) -> Tuple[int, ...]:
  """Return the six offsets of the HEADER; a blank field is 0.

  Raises:
    FcsError: If the data is not an FCS 3.0 or 3.1 file or a field is not a number.
  """
  version = data[:6]
  if version not in (b"FCS3.0", b"FCS3.1"):
    raise FcsError(f"not an FCS 3 file: it starts with {version!r}")
  if len(data) < HEADER_LENGTH:
    raise FcsError("the file ends inside its HEADER")
  fields = []
  for start in range(10, HEADER_LENGTH, 8):
    text = data[start : start + 8].strip()
    if text and not text.isdigit():
      raise FcsError(f"HEADER offset {text!r} is not a number")
    fields.append(int(text) if text else 0)
  return tuple(fields)


def _split_text(text: str) -> List[str]:
  """Split a TEXT segment into its keys and values.

  The first character is the delimiter. Reading left to right, two delimiters in a row are
  one delimiter character inside a key or value; a single one ends it. Spaces or NULs after
  the closing delimiter are padding.

  Raises:
    FcsError: If the segment is empty, a key or value is empty, or text follows the closing
      delimiter.
  """
  if not text:
    raise FcsError("the TEXT segment is empty")
  delimiter = text[0]
  tokens: List[str] = []
  current: List[str] = []
  index = 1
  while index < len(text):
    if text[index] != delimiter:
      current.append(text[index])
      index += 1
    elif text[index + 1 : index + 2] == delimiter:
      current.append(delimiter)
      index += 2
    else:
      tokens.append("".join(current))
      current = []
      index += 1
  if "".join(current).strip(" \x00"):
    raise FcsError("the TEXT segment does not end with its delimiter")
  if "" in tokens:
    raise FcsError("the TEXT segment has an empty key or value")
  return tokens


def _text_keywords(data: bytes, begin: int, end: int) -> Dict[str, str]:
  """Read the keywords of one TEXT segment, standard (``$``) keys in upper case.

  Raises:
    FcsError: If the segment lies outside the file, is not UTF-8 text, is malformed, has a key
      without a value or has a key twice.
  """
  if not HEADER_LENGTH <= begin <= end < len(data):
    raise FcsError(f"TEXT segment {begin} to {end} is not inside the file")
  try:
    text = data[begin : end + 1].decode("utf-8")
  except UnicodeDecodeError as error:
    raise FcsError(f"the TEXT segment is not UTF-8 text: {error}") from error
  tokens = _split_text(text)
  if len(tokens) % 2:
    raise FcsError(f"keyword {tokens[-1]!r} has no value")
  keywords: Dict[str, str] = {}
  for key, value in zip(tokens[0::2], tokens[1::2]):
    name = key.upper() if key.startswith("$") else key
    if name in keywords:
      raise FcsError(f"keyword {name} occurs twice")
    keywords[name] = value
  return keywords


def _required(keywords: Mapping[str, str], key: str) -> str:
  """Return the value of a keyword that a file must have.

  Raises:
    FcsError: If the keyword is missing.
  """
  if key not in keywords:
    raise FcsError(f"required keyword {key} is missing")
  return keywords[key]


def _integer(keywords: Mapping[str, str], key: str) -> int:
  """Return the value of a required keyword that is a whole number, zero padded or not.

  Raises:
    FcsError: If the keyword is missing or its value is not a whole number.
  """
  text = _required(keywords, key).strip()
  if not text.isdigit():
    raise FcsError(f"{key} {text!r} is not a whole number")
  return int(text)


def _with_supplemental_text(
  data: bytes, keywords: Dict[str, str], primary: Tuple[int, int]
) -> Dict[str, str]:
  """Return the keywords with those of a supplemental TEXT segment added, if there is one.

  The instrument's files point ``$BEGINSTEXT`` and ``$ENDSTEXT`` at the primary TEXT segment
  itself; that is not a second segment.

  Raises:
    FcsError: If the supplemental segment is malformed or repeats a keyword.
  """
  if "$BEGINSTEXT" not in keywords or "$ENDSTEXT" not in keywords:
    return keywords
  supplemental = (_integer(keywords, "$BEGINSTEXT"), _integer(keywords, "$ENDSTEXT"))
  if supplemental == (0, 0) or supplemental == primary:
    return keywords
  merged = dict(keywords)
  for key, value in _text_keywords(data, supplemental[0], supplemental[1]).items():
    if key in merged:
      raise FcsError(f"keyword {key} occurs in both TEXT segments")
    merged[key] = value
  return merged


def _is_linear(amplification: str) -> bool:
  """Return whether a ``$PnE`` value, "decades,offset", says the values are linear.

  Linear is zero decades; floats are always stored that way.
  """
  decades = amplification.split(",")[0].strip()
  return re.fullmatch(r"0*\.?0*", decades) is not None and any(c == "0" for c in decades)


def _check_data_layout(keywords: Mapping[str, str], parameter_count: int) -> bool:
  """Check that DATA is list-mode 32-bit floats and return whether it is big-endian.

  Raises:
    FcsError: If the mode, data type, byte order, a parameter's width or its amplification
      is not what this reader supports, or the file continues with another data set.
  """
  if _required(keywords, "$MODE").strip() != "L":
    raise FcsError(f"$MODE {keywords['$MODE']!r} is not supported; only list mode (L) is")
  data_type = _required(keywords, "$DATATYPE").strip()
  if data_type != "F":
    raise FcsError(f"$DATATYPE {data_type!r} is not supported; only 32-bit floats (F) are")
  if "$NEXTDATA" in keywords and _integer(keywords, "$NEXTDATA") != 0:
    raise FcsError("the file holds more than one data set, which is not supported")
  for number in range(1, parameter_count + 1):
    if _integer(keywords, f"$P{number}B") != PARAMETER_BITS:
      raise FcsError(f"$P{number}B is {keywords[f'$P{number}B']}; floats need 32 bits")
    key = f"$P{number}E"
    if key in keywords and not _is_linear(keywords[key]):
      raise FcsError(f"{key} {keywords[key]!r}: only linear values (0,0) are supported")
  byte_order = _required(keywords, "$BYTEORD").replace(" ", "")
  if byte_order not in ("4,3,2,1", "1,2,3,4"):
    raise FcsError(f"$BYTEORD {byte_order!r} is not supported")
  return byte_order == "4,3,2,1"


def _data_range(keywords: Mapping[str, str], header: Tuple[int, ...]) -> Tuple[int, int]:
  """Return the first and last byte of DATA.

  ``$BEGINDATA`` and ``$ENDDATA``, when the file has them and they are not 0, take the place
  of the HEADER fields, which cannot hold offsets beyond 99,999,999.
  """
  if "$BEGINDATA" in keywords and "$ENDDATA" in keywords:
    begin, end = _integer(keywords, "$BEGINDATA"), _integer(keywords, "$ENDDATA")
    if begin and end:
      return begin, end
  return header[2], header[3]


def _data_values(
  data: bytes, begin: int, end: int, count: int, big_endian: bool
) -> array.array[float]:
  """Return `count` 32-bit floats read from the DATA segment.

  Raises:
    FcsError: If the segment is shorter than the values need or not inside the file.
  """
  values = array.array("f")
  if values.itemsize != _VALUE_SIZE:
    raise FcsError(f"array('f') holds {values.itemsize}-byte values here; 4 are needed")
  if count == 0:
    return values
  size = count * _VALUE_SIZE
  if begin < HEADER_LENGTH or end - begin + 1 < size or begin + size > len(data):
    raise FcsError(
      f"DATA segment {begin} to {end} of a {len(data)}-byte file cannot hold {size} bytes"
    )
  if end - begin + 1 > size:
    logger.warning("FCS DATA segment is %d bytes longer than its events", end - begin + 1 - size)
  values.frombytes(data[begin : begin + size])
  if big_endian != (sys.byteorder == "big"):
    values.byteswap()
  return values


def read_fcs(data: bytes) -> Tuple[EventData, Dict[str, str]]:
  """Read an FCS 3.0 or 3.1 list-mode file of 32-bit floats.

  That covers the files written by :func:`write_fcs` and the instrument's own (big-endian);
  little-endian files are read as well. Values are returned as stored, without scaling.

  Args:
    data: The bytes of the file.

  Returns:
    The events, named by ``$PnN`` and ``$PnS``, and every keyword of the file with its value.
    Standard keywords (those starting with ``$``) are in upper case.

  Raises:
    FcsError: If the data is not such a file: another version, mode, data type, bit width or
      byte order, logarithmic values, several data sets, missing or malformed keywords, or
      segments that do not fit the file.
  """
  header = _header_fields(data)
  primary = (header[0], header[1])
  keywords = _text_keywords(data, primary[0], primary[1])
  keywords = _with_supplemental_text(data, keywords, primary)
  parameter_count = _integer(keywords, "$PAR")
  if parameter_count == 0:
    raise FcsError("the file has no parameters")
  event_count = _integer(keywords, "$TOT")
  big_endian = _check_data_layout(keywords, parameter_count)
  names = [_required(keywords, f"$P{number}N") for number in range(1, parameter_count + 1)]
  long_names = [keywords.get(f"$P{number}S", name) for number, name in enumerate(names, start=1)]
  begin, end = _data_range(keywords, header)
  values = _data_values(data, begin, end, event_count * parameter_count, big_endian)
  try:
    events = EventData(names, values, long_names)
  except ValueError as error:
    raise FcsError(f"the parameters of the file are unusable: {error}") from error
  return events, keywords


# ---------------------------------------------------------------------------------------------
# One file per well
# ---------------------------------------------------------------------------------------------

EventRange = Optional[Tuple[int, int]]


def well_file_name(well: str) -> str:
  """Return the name of the FCS file of one well, such as ``Well_A01.fcs``.

  Raises:
    ValueError: If the well name has characters other than letters and digits.
  """
  if re.fullmatch(r"[A-Za-z0-9]+", well) is None:
    raise ValueError(f"{well!r} is not a well name")
  return f"Well_{well}.fcs"


def _well_events(events: EventData, event_range: EventRange, well: str) -> EventData:
  """Return the events of one well: an inclusive range of event indices, or none.

  Raises:
    FcsError: If the range is not within the events.
  """
  if event_range is None:
    return events.slice(0, 0)
  first, last = event_range
  if not 0 <= first <= last < events.event_count:
    raise FcsError(f"well {well}: events {first} to {last} are not within the acquisition")
  return events.slice(first, last + 1)


def well_fcs_files(
  events: EventData,
  well_ranges: Mapping[str, EventRange],
  keywords: Optional[Mapping[str, str]] = None,
  *,
  delimiter: str = DEFAULT_DELIMITER,
) -> Iterator[Tuple[str, bytes]]:
  """Yield one FCS file per well of a plate acquisition, as (file name, bytes).

  A well's file holds every event from its first to its last event index, with the times
  the events have in the plate acquisition. A well without events gives a file with
  ``$TOT`` 0. Each file carries the given keywords and the well name as ``IC$WellID``.

  Args:
    events: The events of the whole plate.
    well_ranges: For each well name, the first and last event index of the well, both
      included, or ``None`` for a well without events.
    keywords: Keywords for every file, as for :func:`write_fcs`.
    delimiter: The TEXT delimiter.

  Raises:
    FcsError: If a range is not within the events, the keywords already name a well, or a
      file cannot be written.
    ValueError: If a well name cannot be part of a file name.
  """
  shared = dict(keywords or {})
  if WELL_KEYWORD.upper() in (key.upper() for key in shared):
    raise FcsError(f"the keywords already hold {WELL_KEYWORD}")
  for well, event_range in well_ranges.items():
    name = well_file_name(well)
    well_keywords = {**shared, WELL_KEYWORD: well}
    selected = _well_events(events, event_range, well)
    yield name, write_fcs(selected, well_keywords, delimiter=delimiter)


def write_well_fcs_files(
  directory: Union[str, pathlib.Path],
  events: EventData,
  well_ranges: Mapping[str, EventRange],
  keywords: Optional[Mapping[str, str]] = None,
  *,
  delimiter: str = DEFAULT_DELIMITER,
  overwrite: bool = False,
) -> List[pathlib.Path]:
  """Write one FCS file per well into a directory; see :func:`well_fcs_files`.

  Args:
    directory: Where the files go; it is created if it does not exist.
    events: The events of the whole plate.
    well_ranges: First and last event index per well name, or ``None`` for no events.
    keywords: Keywords for every file.
    delimiter: The TEXT delimiter.
    overwrite: Whether a file that already exists may be replaced.

  Returns:
    The paths written, in the order of `well_ranges`.

  Raises:
    FileExistsError: If a file exists and `overwrite` is false; earlier wells are written.
    FcsError: As for :func:`well_fcs_files`.
  """
  folder = pathlib.Path(directory)
  folder.mkdir(parents=True, exist_ok=True)
  paths = []
  for name, content in well_fcs_files(events, well_ranges, keywords, delimiter=delimiter):
    path = folder / name
    with open(path, "wb" if overwrite else "xb") as stream:
      stream.write(content)
    paths.append(path)
  logger.info("wrote %d well FCS files to %s", len(paths), folder)
  return paths
