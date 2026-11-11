"""Calibration of the iQue Screener PLUS sampler: data model, file format and coordinates.

The instrument keeps one calibration file. It is a ZIP archive with a single entry,
``Calibration.xml``, an XML document whose root element name is also its content version;
``CalibrationContainer4`` is the current one. :func:`read_calibration` accepts the four
versions that exist and brings older ones to the current model; :func:`write_calibration`
writes the current one.

All positions are in millimetres in the sampler's own frame: X 0 to 131, Y 0 to 195,
Z 190 to 250. A larger Z is higher. Across a plate, X grows from column 1 to the last column
and Y shrinks from row A to the last row.

The file stores two absolute heights, the travel height and the deck height. Every other
depth (stations, plate models, depth maps) is stored relative to the deck height, so
measuring the deck height again moves all of them together.

Rows and columns are zero-based integers (row A is 0, column 1 is 0);
:func:`parse_well_name` converts names such as ``A1`` or ``A01``.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import io
import logging
import math
import re
import xml.etree.ElementTree as ElementTree
import zipfile
import zlib
from dataclasses import dataclass
from typing import (
  Callable,
  Dict,
  List,
  Literal,
  Mapping,
  NamedTuple,
  Optional,
  Sequence,
  Tuple,
  TypeVar,
)

logger = logging.getLogger(__name__)

PlateType = Literal["Well96", "Well384", "TestTube", "Vial", "Well1536"]
PlateFormat = Literal["Well96", "Well384", "Well1536"]
PumpLocation = Literal["Right", "Left"]
SystemKind = Literal[
  "Unknown",
  "HyperCyt",
  "HTFC",
  "Matryoshka",
  "iQue",
  "iQueHD",
  "iQue3",
  "iQue3HD",
  "iQuePlusVYB",
  "iQuePlusBR",
]
LaserConfiguration = Literal["Normal", "HalfBlue", "AllBlue"]
DetectorFilter = Literal[
  "Bandpass_533_30",
  "Bandpass_510_15",
  "Bandpass_585_40",
  "Bandpass_540_20",
  "Longpass_670",
  "Bandpass_610_20",
  "Bandpass_780_60",
  "Bandpass_675_25",
  "Bandpass_630_30",
]

PLATE_TYPES: Tuple[PlateType, ...] = ("Well96", "Well384", "TestTube", "Vial", "Well1536")
PLATE_FORMATS: Tuple[PlateFormat, ...] = ("Well96", "Well384", "Well1536")
PUMP_LOCATIONS: Tuple[PumpLocation, ...] = ("Right", "Left")
SYSTEM_KINDS: Tuple[SystemKind, ...] = (
  "Unknown",
  "HyperCyt",
  "HTFC",
  "Matryoshka",
  "iQue",
  "iQueHD",
  "iQue3",
  "iQue3HD",
  "iQuePlusVYB",
  "iQuePlusBR",
)
LASER_CONFIGURATIONS: Tuple[LaserConfiguration, ...] = ("Normal", "HalfBlue", "AllBlue")
DETECTOR_FILTERS: Tuple[DetectorFilter, ...] = (
  "Bandpass_533_30",
  "Bandpass_510_15",
  "Bandpass_585_40",
  "Bandpass_540_20",
  "Longpass_670",
  "Bandpass_610_20",
  "Bandpass_780_60",
  "Bandpass_675_25",
  "Bandpass_630_30",
)

# Rows and columns of each plate format.
PLATE_DIMENSIONS: Dict[PlateFormat, Tuple[int, int]] = {
  "Well96": (8, 12),
  "Well384": (16, 24),
  "Well1536": (32, 48),
}

# How far above the mapped well bottom the probe samples, per plate format. These are protocol
# values chosen for a plate read, not calibration: the calibration file does not hold them.
DEFAULT_SAMPLING_HEIGHTS: Dict[PlateFormat, float] = {
  "Well96": 0.5,
  "Well384": 0.2,
  "Well1536": 0.5,
}

# The sampler's travel in each axis.
SAMPLER_X_RANGE: Tuple[float, float] = (0.0, 131.0)
SAMPLER_Y_RANGE: Tuple[float, float] = (0.0, 195.0)
SAMPLER_Z_RANGE: Tuple[float, float] = (190.0, 250.0)

# Station names as the file spells them.
STATION_NAMES: Tuple[str, ...] = ("Buffer", "Decon", "Clean", "Water", "Markers", "Tube")
TUBE_STATION = "Tube"
# Short station names that some files use in place of the names above.
STATION_ALIASES: Dict[str, str] = {
  "S1": "Buffer",
  "S2": "Decon",
  "S3": "Clean",
  "S4": "Water",
  "Rinse": "Water",
  "MB": "Markers",
}

# The probe samples a tube, and draws continuously from a station, this far above the
# station's taught depth.
TUBE_SAMPLING_HEIGHT = 2.0

# Well depth offsets that mean "this plate model was never taught".
UNCALIBRATED_WELL_DEPTH_OFFSETS: Tuple[float, ...] = (0.0, 250.0)

# The depth a station carries before it is taught. It is a marker, not a usable depth.
UNCALIBRATED_STATION_DEPTH = 190.0

# The 96- and 1536-well corner wells lie this far inside and outside the 384-well corners.
CORNER_INSET_96 = 2.2
CORNER_OUTSET_1536 = 1.1

# A calibration file larger than this is refused by the instrument.
MAX_FILE_SIZE = 5000 * 1024

ENTRY_NAME = "Calibration.xml"
CURRENT_ROOT = "CalibrationContainer4"

_XSI = "http://www.w3.org/2001/XMLSchema-instance"
_XSD = "http://www.w3.org/2001/XMLSchema"
_NIL = f"{{{_XSI}}}nil"
_NAMESPACES = f'xmlns:xsi="{_XSI}" xmlns:xsd="{_XSD}"'
_MAX_DOCUMENT_SIZE = 64 * 1024 * 1024
_MINUTE = decimal.Decimal(60)
_THOUSANDTH = decimal.Decimal("0.001")
_CONTEXT = decimal.Context(prec=28, rounding=decimal.ROUND_HALF_EVEN)

_Member = TypeVar("_Member", bound=str)
_Item = TypeVar("_Item")


class CalibrationError(Exception):
  """The calibration cannot answer a question, for example because a value was never taught."""


class CalibrationFormatError(CalibrationError, ValueError):
  """Data does not follow the calibration file format."""


class XY(NamedTuple):
  """A position in the sampler's horizontal plane, in mm."""

  x: float
  y: float


@dataclass(frozen=True)
class Instrument:
  """The instrument record of a calibration file.

  Attributes:
    travel_height: The Z every move between positions retracts to.
    deck_height: The Z at which the probe touches the plate holder at the test point. Every
      other depth in the file is relative to it.
    pump_location: Which side the pump sits on; ``"Right"`` means it turns clockwise.
    system_kind: The instrument family member the file was written for.
    has_shaker: Whether the instrument has a shaker.
    selectable_detectors_enabled: The "selectable detector sets" switch of older detectors.
    travel_height_calibrated_on: When the travel height was taught, if ever.
    deck_height_calibrated_on: When the deck height was measured. ``None`` means the deck
      height is not calibrated and no depth may be derived from it.
    deck_height_test_x: X of the spot on the plate holder where the deck height is measured.
    deck_height_test_y: Y of that spot.
    name: The instrument's display name.
    device_id: The controller's system identifier.
    serial_number: The serial number recorded for the instrument.
    probe_change_reminder_hours: Run time after which a probe change is suggested. The file
      holds whole hours.
  """

  travel_height: float
  deck_height: float
  pump_location: PumpLocation
  system_kind: SystemKind
  has_shaker: bool = True
  selectable_detectors_enabled: bool = False
  travel_height_calibrated_on: Optional[datetime.datetime] = None
  deck_height_calibrated_on: Optional[datetime.datetime] = None
  deck_height_test_x: Optional[float] = None
  deck_height_test_y: Optional[float] = None
  name: Optional[str] = None
  device_id: Optional[str] = None
  serial_number: Optional[str] = None
  probe_change_reminder_hours: int = 0

  @property
  def deck_height_is_calibrated(self) -> bool:
    """Whether the deck height has been measured."""
    return self.deck_height_calibrated_on is not None


@dataclass(frozen=True)
class Corners:
  """The centres of the four corner wells of a plate format.

  Attributes:
    top_left: Row A, column 1.
    top_right: Row A, last column.
    bottom_left: Last row, column 1.
    bottom_right: Last row, last column.
  """

  top_left: XY
  top_right: XY
  bottom_left: XY
  bottom_right: XY


@dataclass(frozen=True)
class PlateTypeCalibration:
  """Where the corner wells of one plate type are.

  The file also carries entries for the ``TestTube`` and ``Vial`` types; their corners are
  not used for anything.

  Attributes:
    plate_type: The plate type.
    corners: The four corner wells.
    calibrated_on: When the corners were taught; ``None`` means not calibrated.
  """

  plate_type: PlateType
  corners: Corners
  calibrated_on: Optional[datetime.datetime] = None

  @property
  def is_calibrated(self) -> bool:
    """Whether the corners have been taught."""
    return self.calibrated_on is not None


@dataclass(frozen=True)
class Station:
  """A fluid station, or the tube holder (the station named ``Tube``).

  Attributes:
    name: The station name, such as ``Buffer``.
    x: Probe X over the station.
    y: Probe Y over the station.
    depth: Dip depth relative to the deck height: the probe dips to deck height + depth. For
      the tube holder it is the contact height in the empty holder.
    calibrated_on: When the station was taught; ``None`` means not calibrated.
  """

  name: str
  x: float
  y: float
  depth: float
  calibrated_on: Optional[datetime.datetime] = None

  @property
  def is_calibrated(self) -> bool:
    """Whether the station has been taught: it has a date and is not at X 0, Y 0."""
    return self.calibrated_on is not None and not (self.x == 0 and self.y == 0)


# Depth maps exist with these numbers of measured wells: count -> (columns, rows) of the grid.
DEPTH_MAP_GRIDS: Dict[int, Tuple[int, int]] = {
  4: (2, 2),
  6: (3, 2),
  9: (3, 3),
  12: (4, 3),
  25: (5, 5),
  35: (7, 5),
  96: (12, 8),
  384: (24, 16),
  1536: (48, 32),
}


def _dimensions(plate_format: str) -> Tuple[int, int]:
  """Return (rows, columns) of a plate format.

  Raises:
    CalibrationError: If the name is not a plate format with wells.
  """
  for known, dimensions in PLATE_DIMENSIONS.items():
    if known == plate_format:
      return dimensions
  raise CalibrationError(f"{plate_format!r} is not a plate format with wells")


def _node_positions(count: int, extent: int) -> Tuple[int, ...]:
  """Return where `count` grid nodes sit along an axis of `extent` wells.

  Nodes are a whole number of wells apart, `extent // (count - 1)`, and the last one is pulled
  back onto the last well, so the last interval can be shorter than the others.
  """
  step = extent // (count - 1)
  return tuple(min(index * step, extent - 1) for index in range(count))


def _spans(nodes: Sequence[int], extent: int) -> bool:
  """Return whether nodes rise strictly and end on the last well of the axis."""
  rising = all(later > earlier for earlier, later in zip(nodes, nodes[1:]))
  return rising and nodes[-1] == extent - 1


def depth_map_nodes(
  plate_format: PlateFormat, count: int
) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
  """Return the columns and the rows at which a depth map of `count` points was measured.

  Args:
    plate_format: The plate format of the mapped plate.
    count: The number of measured wells.

  Returns:
    The node columns and the node rows, both zero-based and rising.

  Raises:
    CalibrationError: If no map with this number of points exists, or its grid does not span
      the plate, so that wells would lie outside the measured area.
  """
  if count not in DEPTH_MAP_GRIDS:
    raise CalibrationError(f"a depth map cannot have {count} points")
  rows, columns = _dimensions(plate_format)
  node_columns, node_rows = DEPTH_MAP_GRIDS[count]
  xs = _node_positions(node_columns, columns)
  ys = _node_positions(node_rows, rows)
  if not (_spans(xs, columns) and _spans(ys, rows)):
    raise CalibrationError(f"a depth map of {count} points does not span a {plate_format} plate")
  return xs, ys


def depth_map_points(plate_format: PlateFormat, count: int) -> Tuple[int, ...]:
  """Return the measured wells of a depth map as row-major indices, row by row.

  The index of a well is `row * columns + column`.

  Raises:
    CalibrationError: As for :func:`depth_map_nodes`.
  """
  xs, ys = depth_map_nodes(plate_format, count)
  columns = PLATE_DIMENSIONS[plate_format][1]
  return tuple(x + y * columns for y in ys for x in xs)


@dataclass(frozen=True)
class DepthMap:
  """Measured well-bottom heights of one plate model on this instrument.

  The probe was lowered into a grid of wells until it touched; each value is that contact
  height minus the deck height. The grid is fixed by the plate format and the number of
  values (:func:`depth_map_nodes`); values are listed row by row.

  Attributes:
    plate_format: The format of the mapped plate.
    values: Contact height above the deck at each measured well, in mm.
    calibrated_on: When the map was measured.
  """

  plate_format: PlateFormat
  values: Tuple[float, ...]
  calibrated_on: datetime.datetime

  def __post_init__(self) -> None:
    """Check that the number of values is one a map of this plate format can have."""
    depth_map_nodes(self.plate_format, len(self.values))


@dataclass(frozen=True)
class PlateModel:
  """A kind of plate, with what was taught about it on this instrument.

  Attributes:
    name: The model's unique name.
    well_depth_offset: Sampling depth above the deck height, used only when the model has no
      depth map. 0 and 250 mean it was never taught.
    x_offset: Added to the X of every well of plates of this model.
    y_offset: Added to the Y of every well of plates of this model.
    depth_map: The measured well-bottom heights, if the model was mapped on this instrument.
  """

  name: str
  well_depth_offset: float = 0.0
  x_offset: float = 0.0
  y_offset: float = 0.0
  depth_map: Optional[DepthMap] = None

  @property
  def is_calibrated(self) -> bool:
    """Whether a sampling depth can be derived: a depth map or a taught depth offset."""
    return self.depth_map is not None or (
      self.well_depth_offset not in UNCALIBRATED_WELL_DEPTH_OFFSETS
    )


@dataclass(frozen=True)
class DetectorConfiguration:
  """The filter set of the selectable detectors of older instruments.

  Attributes:
    laser_configuration: How the lasers are assigned.
    fl1_filter: Filter in front of detector FL1.
    fl2_filter: Filter in front of detector FL2.
    fl3_filter: Filter in front of detector FL3.
    fl4_filter: Filter in front of detector FL4.
    calibrated_on: When the set was configured, if ever.
  """

  laser_configuration: LaserConfiguration = "Normal"
  fl1_filter: DetectorFilter = "Bandpass_533_30"
  fl2_filter: DetectorFilter = "Bandpass_585_40"
  fl3_filter: DetectorFilter = "Longpass_670"
  fl4_filter: DetectorFilter = "Bandpass_675_25"
  calibrated_on: Optional[datetime.datetime] = None


@dataclass(frozen=True)
class ConsumablesOdometer:
  """Run-time counters of consumables as of the moment the file was written, in seconds.

  The file holds the counters in minutes; they are converted here.

  Attributes:
    last_run_on: When the instrument last ran.
    run_time_since_new_probe: Run time since the probe was changed.
    run_time_since_new_filter: Run time since the filter was changed.
    run_time_since_bottle_clean: Run time since the bottles were cleaned.
    run_time_since_new_pig_tail: Run time since the pig tail tubing was changed.
    run_time_since_new_cytometer_tubing: Run time since the cytometer tubing was changed.
    cumulative_run_time_pump: Total run time of the pump.
    cumulative_run_time_cytometer: Total run time of the cytometer.
  """

  last_run_on: Optional[datetime.datetime] = None
  run_time_since_new_probe: float = 0.0
  run_time_since_new_filter: float = 0.0
  run_time_since_bottle_clean: float = 0.0
  run_time_since_new_pig_tail: float = 0.0
  run_time_since_new_cytometer_tubing: float = 0.0
  cumulative_run_time_pump: float = 0.0
  cumulative_run_time_cytometer: float = 0.0


def _only(matches: Sequence[_Item], description: str) -> _Item:
  """Return the single match of a lookup.

  Raises:
    CalibrationError: If there is no match or more than one.
  """
  if not matches:
    raise CalibrationError(f"the calibration has no {description}")
  if len(matches) > 1:
    raise CalibrationError(f"the calibration has {len(matches)} entries for {description}")
  return matches[0]


@dataclass(frozen=True)
class Calibration:
  """Everything a calibration file holds.

  Attributes:
    instrument: Heights, the deck-height test point and identity of the instrument.
    plate_types: Corner wells per plate type.
    stations: Fluid stations and the tube holder.
    plate_models: Plate models with their offsets and depth maps.
    detector_configuration: Filter set of older detectors, when the file has one.
    consumables_odometer: Consumable counters, when the file has them.
  """

  instrument: Instrument
  plate_types: Tuple[PlateTypeCalibration, ...] = ()
  stations: Tuple[Station, ...] = ()
  plate_models: Tuple[PlateModel, ...] = ()
  detector_configuration: Optional[DetectorConfiguration] = None
  consumables_odometer: Optional[ConsumablesOdometer] = None

  def plate_type(self, plate_type: PlateType) -> PlateTypeCalibration:
    """Return the entry of one plate type.

    Raises:
      CalibrationError: If the calibration has no entry for it, or several.
    """
    matches = [entry for entry in self.plate_types if entry.plate_type == plate_type]
    return _only(matches, f"plate type {plate_type}")

  def station(self, name: str) -> Station:
    """Return the station with this name, such as ``Buffer``.

    Names are matched exactly. A station that the file names by a short name
    (`STATION_ALIASES`) is found under the name that stands for.

    Raises:
      CalibrationError: If the calibration has no such station, or several.
    """
    matches = [
      station
      for station in self.stations
      if STATION_ALIASES.get(station.name, station.name) == name
    ]
    return _only(matches, f"station {name!r}")

  def plate_model(self, name: str) -> PlateModel:
    """Return the plate model with this exact name.

    Raises:
      CalibrationError: If the calibration has no such plate model, or several.
    """
    matches = [model for model in self.plate_models if model.name == name]
    return _only(matches, f"plate model {name!r}")


# ---------------------------------------------------------------------------------------------
# Well names
# ---------------------------------------------------------------------------------------------

_WELL_NAME = re.compile(r"([A-Z]{1,2})([0-9]{1,2})")


def parse_well_name(name: str, plate_format: Optional[PlateFormat] = None) -> Tuple[int, int]:
  """Convert a well name such as ``A1``, ``A01`` or ``AF48`` to zero-based (row, column).

  Rows after ``Z`` have two letters: ``AA`` is row 26, as on 1536-well plates.

  Args:
    name: Row letters followed by a one-based column number.
    plate_format: When given, the well must exist on a plate of this format.

  Returns:
    The zero-based row and column.

  Raises:
    ValueError: If the text is not a well name, or the well is not on the plate.
  """
  match = _WELL_NAME.fullmatch(name)
  if match is None:
    raise ValueError(f"{name!r} is not a well name")
  letters, digits = match.group(1), match.group(2)
  row = ord(letters[-1]) - ord("A")
  if len(letters) == 2:
    row += 26 * (ord(letters[0]) - ord("A") + 1)
  column = int(digits) - 1
  if column < 0:
    raise ValueError(f"{name!r} is not a well name: columns start at 1")
  if plate_format is not None:
    rows, columns = _dimensions(plate_format)
    if row >= rows or column >= columns:
      raise ValueError(f"well {name} is not on a {plate_format} plate")
  return row, column


def format_well_name(row: int, column: int) -> str:
  """Return the name of a well from its zero-based row and column, such as ``A01``.

  Raises:
    ValueError: If the row or column cannot be named (row beyond ``ZZ``, column beyond 99).
  """
  if not (0 <= row < 27 * 26 and 0 <= column < 99):
    raise ValueError(f"row {row}, column {column} has no well name")
  letters = chr(ord("A") + row % 26)
  if row >= 26:
    letters = chr(ord("A") + row // 26 - 1) + letters
  return f"{letters}{column + 1:02d}"


# ---------------------------------------------------------------------------------------------
# Reading: text values
# ---------------------------------------------------------------------------------------------

_DECIMAL_TEXT = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)")
_INTEGER_TEXT = re.compile(r"[+-]?[0-9]+")
_TIMESTAMP_TEXT = re.compile(
  r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
  r"(?:\.([0-9]+))?(Z|[+-][0-9]{2}:[0-9]{2})?"
)
_BOOLEANS: Dict[str, bool] = {"true": True, "1": True, "false": False, "0": False}


def _parse_decimal(text: str, where: str) -> decimal.Decimal:
  """Parse a decimal number written with a point and no exponent.

  Raises:
    CalibrationFormatError: If the text is not such a number.
  """
  stripped = text.strip()
  if _DECIMAL_TEXT.fullmatch(stripped) is None:
    raise CalibrationFormatError(f"{where}: {text!r} is not a decimal number")
  return decimal.Decimal(stripped)


def _parse_integer(text: str, where: str) -> int:
  """Parse a whole number.

  Raises:
    CalibrationFormatError: If the text is not a whole number.
  """
  stripped = text.strip()
  if _INTEGER_TEXT.fullmatch(stripped) is None:
    raise CalibrationFormatError(f"{where}: {text!r} is not a whole number")
  return int(stripped)


def _parse_boolean(text: str, where: str) -> bool:
  """Parse ``true``, ``false``, ``1`` or ``0``.

  Raises:
    CalibrationFormatError: If the text is none of them.
  """
  stripped = text.strip()
  if stripped not in _BOOLEANS:
    raise CalibrationFormatError(f"{where}: {text!r} is not true or false")
  return _BOOLEANS[stripped]


def _parse_zone(text: Optional[str]) -> Optional[datetime.timezone]:
  """Convert the zone part of a timestamp: nothing, ``Z`` or ``+hh:mm``."""
  if text is None:
    return None
  if text == "Z":
    return datetime.timezone.utc
  sign = -1 if text[0] == "-" else 1
  offset = datetime.timedelta(hours=int(text[1:3]), minutes=int(text[4:6]))
  return datetime.timezone(sign * offset)


def _parse_timestamp(text: str, where: str) -> datetime.datetime:
  """Parse an ``xsd:dateTime`` such as ``2021-06-01T14:20:05.573`` or one with a zone.

  A fraction of a second finer than a microsecond is cut to microseconds.

  Raises:
    CalibrationFormatError: If the text is not such a timestamp.
  """
  match = _TIMESTAMP_TEXT.fullmatch(text.strip())
  if match is None:
    raise CalibrationFormatError(f"{where}: {text!r} is not a date and time")
  year, month, day, hour, minute, second = (int(match.group(index)) for index in range(1, 7))
  microsecond = int((match.group(7) or "").ljust(6, "0")[:6])
  try:
    return datetime.datetime(
      year, month, day, hour, minute, second, microsecond, _parse_zone(match.group(8))
    )
  except ValueError as error:
    raise CalibrationFormatError(f"{where}: {text!r} is not a date and time") from error


def _member(text: str, members: Sequence[_Member], where: str) -> _Member:
  """Return the member of a fixed set of names that equals the text exactly.

  Raises:
    CalibrationFormatError: If the text is not one of the names.
  """
  for member in members:
    if member == text:
      return member
  raise CalibrationFormatError(f"{where}: {text!r} is not one of {', '.join(members)}")


# ---------------------------------------------------------------------------------------------
# Reading: elements
# ---------------------------------------------------------------------------------------------


def _value_text(element: ElementTree.Element, where: str) -> str:
  """Return the text of an element that holds one value.

  Raises:
    CalibrationFormatError: If the element has elements inside it.
  """
  if len(element):
    raise CalibrationFormatError(f"{where} holds elements, not a value")
  return element.text or ""


class _Children:
  """The child elements of one element, each read once by name.

  Elements may come in any order. Reading the same name twice, a name that occurs twice and a
  child left unread at :meth:`finish` are all errors, so nothing in a document goes unnoticed.
  """

  def __init__(self, element: ElementTree.Element, path: str) -> None:
    """Index the children of an element.

    Args:
      element: The parent element.
      path: Where the element is in the document, for error messages.

    Raises:
      CalibrationFormatError: If two children share a name.
    """
    self._path = path
    self._unread: Dict[str, ElementTree.Element] = {}
    for child in element:
      if child.tag in self._unread:
        raise CalibrationFormatError(f"{path}: <{child.tag}> appears more than once")
      self._unread[child.tag] = child

  def where(self, name: str) -> str:
    """Return the location of a child, for error messages."""
    return f"{self._path}/{name}"

  def optional(self, name: str) -> Optional[ElementTree.Element]:
    """Take a child if it is there and not marked nil."""
    element = self._unread.pop(name, None)
    if element is None or element.get(_NIL) in ("true", "1"):
      return None
    return element

  def required(self, name: str) -> ElementTree.Element:
    """Take a child that must be there with a value.

    Raises:
      CalibrationFormatError: If it is missing or marked nil.
    """
    element = self.optional(name)
    if element is None:
      raise CalibrationFormatError(f"{self.where(name)} is missing")
    return element

  def discard(self, name: str) -> None:
    """Take a child, if it is there, without looking inside it."""
    self._unread.pop(name, None)

  def optional_string(self, name: str) -> Optional[str]:
    """Read a string; an absent element is how the format writes "no string"."""
    element = self.optional(name)
    return None if element is None else _value_text(element, self.where(name))

  def string(self, name: str) -> str:
    """Read a string that must be there."""
    return _value_text(self.required(name), self.where(name))

  def decimal(self, name: str) -> decimal.Decimal:
    """Read a decimal number that must be there."""
    return _parse_decimal(self.string(name), self.where(name))

  def millimetres(self, name: str) -> float:
    """Read a length in mm that must be there."""
    return float(self.decimal(name))

  def optional_millimetres(self, name: str) -> Optional[float]:
    """Read a length in mm that may be absent or nil."""
    text = self.optional_string(name)
    return None if text is None else float(_parse_decimal(text, self.where(name)))

  def tenths(self, name: str) -> float:
    """Read a length written as a whole number of tenths of a millimetre, in mm."""
    tenths = _parse_integer(self.string(name), self.where(name))
    return float(decimal.Decimal(tenths) / 10)

  def minutes(self, name: str) -> float:
    """Read a duration written in minutes, in seconds."""
    return float(self.decimal(name) * _MINUTE)

  def integer(self, name: str) -> int:
    """Read a whole number that must be there."""
    return _parse_integer(self.string(name), self.where(name))

  def boolean(self, name: str) -> bool:
    """Read a boolean that must be there."""
    return _parse_boolean(self.string(name), self.where(name))

  def timestamp(self, name: str) -> datetime.datetime:
    """Read a date and time that must be there."""
    return _parse_timestamp(self.string(name), self.where(name))

  def optional_timestamp(self, name: str) -> Optional[datetime.datetime]:
    """Read a date and time that may be absent or nil."""
    text = self.optional_string(name)
    return None if text is None else _parse_timestamp(text, self.where(name))

  def member(self, name: str, members: Sequence[_Member]) -> _Member:
    """Read one name out of a fixed set; it must be there."""
    return _member(self.string(name).strip(), members, self.where(name))

  def items(self, name: str, item_name: str, versioned: bool = False) -> List[ElementTree.Element]:
    """Read a list: a wrapper element with one child per item. No wrapper means no items.

    Args:
      name: The wrapper's name.
      item_name: The element name of every item.
      versioned: Whether item names may end in a version number, as the names of record
        items do (``...Container``, ``...Container2``, ``...Container3``).

    Raises:
      CalibrationFormatError: If the wrapper holds anything but such items.
    """
    wrapper = self.optional(name)
    if wrapper is None:
      return []
    pattern = re.escape(item_name) + ("[0-9]*" if versioned else "")
    items = list(wrapper)
    for item in items:
      if re.fullmatch(pattern, item.tag) is None:
        raise CalibrationFormatError(f"{self.where(name)}: unexpected <{item.tag}>")
    return items

  def finish(self) -> None:
    """Check that every child was read.

    Raises:
      CalibrationFormatError: If the element has children this reader does not know.
    """
    if self._unread:
      names = ", ".join(f"<{name}>" for name in self._unread)
      raise CalibrationFormatError(f"{self._path}: unexpected {names}")


def _parse_document(text: str, what: str) -> ElementTree.Element:
  """Parse an XML document held in a string and return its root element.

  The declaration at the top is dropped first: the text is already decoded, so the encoding
  it names (``utf-16`` for a document embedded in another) no longer applies.

  Raises:
    CalibrationFormatError: If the text is not well-formed XML or declares a document type.
  """
  body = re.sub(r"\A\ufeff?<\?xml[^>]*\?>", "", text, count=1)
  if "<!DOCTYPE" in body:
    raise CalibrationFormatError(f"{what} declares a document type, which the format never has")
  try:
    return ElementTree.fromstring(body)
  except ElementTree.ParseError as error:
    raise CalibrationFormatError(f"{what} is not well-formed XML: {error}") from error


def _read_depth_map(text: str, where: str) -> DepthMap:
  """Read the depth map document that a plate model carries as text.

  The document lists the measured wells (``Points``, row-major indices), one value per well
  (``Values``), the plate type, the number of points again and the date of the map.

  Raises:
    CalibrationFormatError: If the document is malformed, the lists differ in length, the
      number of points is not one a map can have, or the points are not the wells of that grid.
  """
  root = _parse_document(text, where)
  if root.tag != "PlateMappingState":
    raise CalibrationFormatError(f"{where}: unexpected root <{root.tag}>")
  children = _Children(root, where)
  points = [
    _parse_integer(_value_text(item, f"{where}/Points"), f"{where}/Points")
    for item in children.items("Points", "int")
  ]
  values = [
    float(_parse_decimal(_value_text(item, f"{where}/Values"), f"{where}/Values"))
    for item in children.items("Values", "decimal")
  ]
  plate_format = children.member("PlateType", PLATE_FORMATS)
  stated_count = children.integer("InterpolationPoints")
  calibrated_on = children.timestamp("LastCalibratedOn")
  children.finish()
  if len(values) != len(points) or stated_count != len(points):
    raise CalibrationFormatError(
      f"{where}: {len(points)} points, {len(values)} values and a stated count of {stated_count}"
    )
  try:
    expected_points = depth_map_points(plate_format, len(points))
  except CalibrationError as error:
    raise CalibrationFormatError(f"{where}: {error}") from error
  if tuple(points) != expected_points:
    raise CalibrationFormatError(
      f"{where}: the points are not the wells a {len(points)}-point map of {plate_format} uses"
    )
  return DepthMap(plate_format=plate_format, values=tuple(values), calibrated_on=calibrated_on)


def _read_corners(children: _Children) -> Corners:
  """Read the eight corner coordinates of a plate type entry."""
  return Corners(
    top_left=XY(children.millimetres("TopLeftX"), children.millimetres("TopLeftY")),
    top_right=XY(children.millimetres("TopRightX"), children.millimetres("TopRightY")),
    bottom_left=XY(children.millimetres("BottomLeftX"), children.millimetres("BottomLeftY")),
    bottom_right=XY(children.millimetres("BottomRightX"), children.millimetres("BottomRightY")),
  )


def _read_plate_type(element: ElementTree.Element, where: str) -> PlateTypeCalibration:
  """Read one plate type entry of the current version."""
  children = _Children(element, where)
  entry = PlateTypeCalibration(
    plate_type=children.member("PlateType", PLATE_TYPES),
    corners=_read_corners(children),
    calibrated_on=children.optional_timestamp("LastCalibratedOn"),
  )
  children.finish()
  return entry


def _read_named_plate_type(
  element: ElementTree.Element, where: str
) -> Optional[PlateTypeCalibration]:
  """Read a plate type entry of version 3, where the type is free text.

  Returns:
    The entry, or ``None`` when its type is not one that exists today; such entries are
    dropped.
  """
  children = _Children(element, where)
  name = children.string("PlateType").strip()
  corners = _read_corners(children)
  calibrated_on = children.optional_timestamp("LastCalibratedOn")
  children.finish()
  for plate_type in PLATE_TYPES:
    if plate_type == name:
      return PlateTypeCalibration(plate_type, corners, calibrated_on)
  return None


def _read_station(element: ElementTree.Element, where: str) -> Station:
  """Read one station entry."""
  children = _Children(element, where)
  station = Station(
    name=children.string("RinseStationName"),
    x=children.millimetres("XPos"),
    y=children.millimetres("YPos"),
    depth=children.millimetres("ZLow"),
    calibrated_on=children.optional_timestamp("LastCalibratedOn"),
  )
  children.finish()
  return station


def _read_plate_model(element: ElementTree.Element, where: str, version: int) -> PlateModel:
  """Read one plate model entry.

  Version 1 holds only the name and the depth offset, in tenths of a millimetre. Version 2
  adds the XY offset. From version 3 on a model can carry its depth map.
  """
  children = _Children(element, where)
  name = children.string("ModelName")
  if version == 1:
    model = PlateModel(name=name, well_depth_offset=children.tenths("WellDepthOffset"))
  else:
    map_text = children.optional_string("MapState") if version >= 3 else None
    model = PlateModel(
      name=name,
      well_depth_offset=children.millimetres("WellDepthOffset"),
      x_offset=children.millimetres("XOffset"),
      y_offset=children.millimetres("YOffset"),
      depth_map=None if map_text is None else _read_depth_map(map_text, f"{where}/MapState"),
    )
  children.finish()
  return model


def _read_plate_models(children: _Children, version: int) -> Tuple[PlateModel, ...]:
  """Read the list of plate models."""
  where = children.where("PlateModels")
  items = children.items("PlateModels", "PlateModelCalibrationContainer", versioned=True)
  return tuple(
    _read_plate_model(item, f"{where}[{index}]", version) for index, item in enumerate(items)
  )


def _read_stations(children: _Children) -> Tuple[Station, ...]:
  """Read the list of stations."""
  where = children.where("RinseStations")
  items = children.items("RinseStations", "RinseStationCalibrationContainer", versioned=True)
  return tuple(_read_station(item, f"{where}[{index}]") for index, item in enumerate(items))


def _read_detector_configuration(children: _Children) -> Optional[DetectorConfiguration]:
  """Read the detector filter set, if the document has one."""
  element = children.optional("SDSConfiguration")
  if element is None:
    return None
  fields = _Children(element, children.where("SDSConfiguration"))
  configuration = DetectorConfiguration(
    laser_configuration=fields.member("LaserConfiguration", LASER_CONFIGURATIONS),
    fl1_filter=fields.member("FL1Filter", DETECTOR_FILTERS),
    fl2_filter=fields.member("FL2Filter", DETECTOR_FILTERS),
    fl3_filter=fields.member("FL3Filter", DETECTOR_FILTERS),
    fl4_filter=fields.member("FL4Filter", DETECTOR_FILTERS),
    calibrated_on=fields.optional_timestamp("LastCalibratedOn"),
  )
  fields.finish()
  return configuration


def _read_odometer(children: _Children) -> Optional[ConsumablesOdometer]:
  """Read the consumable counters, if the document has them."""
  element = children.optional("ConsumablesOdometer")
  if element is None:
    return None
  fields = _Children(element, children.where("ConsumablesOdometer"))
  odometer = ConsumablesOdometer(
    last_run_on=fields.optional_timestamp("LastRunOn"),
    run_time_since_new_probe=fields.minutes("RunTimeSinceNewProbe"),
    run_time_since_new_filter=fields.minutes("RunTimeSinceNewFilter"),
    run_time_since_bottle_clean=fields.minutes("RunTimeSinceBottleClean"),
    run_time_since_new_pig_tail=fields.minutes("RunTimeSinceNewPigTail"),
    run_time_since_new_cytometer_tubing=fields.minutes("RunTimeSinceNewCytTubing"),
    cumulative_run_time_pump=fields.minutes("CumulativeRunTimePump"),
    cumulative_run_time_cytometer=fields.minutes("CumulativeRunTimeCyt"),
  )
  fields.finish()
  return odometer


def _read_instrument(element: ElementTree.Element, version: int) -> Instrument:
  """Read the instrument record of any version, as that version states it.

  Version 1 writes heights in tenths of a millimetre and names the instrument family only
  through a flag. Versions 1 to 3 carry two flags about accessories that no longer exist;
  they are read and dropped. The deck-height test point exists from version 3 on and the
  probe-change reminder from version 4 on.
  """
  fields = _Children(element, "HyperCyt")
  has_shaker = fields.boolean("HasShaker")
  pump_location = fields.member("PumpLocation", PUMP_LOCATIONS)
  travel_height = fields.tenths("ZHigh") if version == 1 else fields.millimetres("ZHigh")
  travel_height_calibrated_on = fields.optional_timestamp("ZHighLastCalibratedOn")
  deck_height = fields.tenths("ZLow") if version == 1 else fields.millimetres("ZLow")
  deck_height_calibrated_on = fields.optional_timestamp("ZLowLastCalibratedOn")
  system_kind: SystemKind
  if version == 1:
    system_kind = "HTFC" if fields.boolean("HasHTFC") else "Unknown"
  else:
    system_kind = fields.member("SystemKind", SYSTEM_KINDS)
  if version <= 3:
    fields.boolean("HasTestTubeRack")
    fields.boolean("HasCirculatingRinseStation")
  instrument = Instrument(
    travel_height=travel_height,
    deck_height=deck_height,
    pump_location=pump_location,
    system_kind=system_kind,
    has_shaker=has_shaker,
    selectable_detectors_enabled=False if version == 1 else fields.boolean("SDSEnabled"),
    travel_height_calibrated_on=travel_height_calibrated_on,
    deck_height_calibrated_on=deck_height_calibrated_on,
    deck_height_test_x=fields.optional_millimetres("DeckHeightX") if version >= 3 else None,
    deck_height_test_y=fields.optional_millimetres("DeckHeightY") if version >= 3 else None,
    name="" if version == 1 else fields.optional_string("HyperCytName"),
    device_id="" if version == 1 else fields.optional_string("DeviceID"),
    serial_number="" if version == 1 else fields.optional_string("SerialNumber"),
    probe_change_reminder_hours=fields.integer("NotifyHoursProbeChange") if version >= 4 else 0,
  )
  fields.finish()
  return instrument


# ---------------------------------------------------------------------------------------------
# Reading: versions
# ---------------------------------------------------------------------------------------------

_NO_CORNERS = Corners(XY(0.0, 0.0), XY(0.0, 0.0), XY(0.0, 0.0), XY(0.0, 0.0))

# What a document older than version 3 becomes: its taught positions are replaced by these
# uncalibrated placeholders. The 96-well top-right Y equals the bottom-right Y in this rule,
# and the station list names "Buffer" twice and has no "Water"; neither is usable as it is.
_REPLACEMENT_TRAVEL_HEIGHT = 250.0
_REPLACEMENT_DECK_HEIGHT = 150.0
_REPLACEMENT_PLATE_TYPES: Tuple[PlateTypeCalibration, ...] = (
  PlateTypeCalibration(
    "Well96",
    Corners(XY(20.0, 109.7), XY(119.8, 45.8), XY(19.8, 46.1), XY(119.65, 45.8)),
  ),
  PlateTypeCalibration(
    "Well384",
    Corners(XY(18.0, 111.7), XY(121.8, 111.35), XY(17.8, 43.8), XY(121.65, 43.8)),
  ),
  PlateTypeCalibration(
    "Well1536",
    Corners(XY(17.2, 112.5), XY(122.6, 112.15), XY(17.0, 43.3), XY(122.45, 43.0)),
  ),
  PlateTypeCalibration("TestTube", _NO_CORNERS),
)
_REPLACEMENT_STATION_NAMES: Tuple[str, ...] = ("Buffer", "Markers", "Decon", "Clean", "Buffer")


def _complete_version_3(calibration: Calibration) -> Calibration:
  """Bring version 3 content to the current version.

  The deck height loses its date, so it counts as not calibrated; an untaught ``Tube``
  station and empty consumable counters are added.
  """
  instrument = dataclasses.replace(calibration.instrument, deck_height_calibrated_on=None)
  tube = Station(name=TUBE_STATION, x=0.0, y=0.0, depth=0.0)
  return dataclasses.replace(
    calibration,
    instrument=instrument,
    stations=calibration.stations + (tube,),
    consumables_odometer=ConsumablesOdometer(),
  )


def _discard_taught_positions(calibration: Calibration) -> Calibration:
  """Bring content older than version 3 to version 3, which keeps none of its positions.

  The travel height survives only for the ``HTFC`` family; the deck height, the corners of
  every plate type and the stations become placeholders without dates. Identity, flags, plate
  models and the detector filter set are kept.
  """
  old = calibration.instrument
  keeps_travel_height = old.system_kind == "HTFC"
  instrument = dataclasses.replace(
    old,
    travel_height=old.travel_height if keeps_travel_height else _REPLACEMENT_TRAVEL_HEIGHT,
    deck_height=_REPLACEMENT_DECK_HEIGHT,
    travel_height_calibrated_on=None,
    deck_height_calibrated_on=None,
  )
  stations = tuple(
    Station(name=name, x=0.0, y=0.0, depth=0.0) for name in _REPLACEMENT_STATION_NAMES
  )
  return dataclasses.replace(
    calibration,
    instrument=instrument,
    plate_types=_REPLACEMENT_PLATE_TYPES,
    stations=stations,
  )


def _read_version_4(root: ElementTree.Element) -> Calibration:
  """Read a document of the current version."""
  children = _Children(root, root.tag)
  instrument = _read_instrument(children.required("HyperCyt"), 4)
  where = children.where("PlateTypes")
  items = children.items("PlateTypes", "PlateTypeCalibrationContainer", versioned=True)
  plate_types = tuple(
    _read_plate_type(item, f"{where}[{index}]") for index, item in enumerate(items)
  )
  calibration = Calibration(
    instrument=instrument,
    plate_types=plate_types,
    stations=_read_stations(children),
    plate_models=_read_plate_models(children, 4),
    detector_configuration=_read_detector_configuration(children),
    consumables_odometer=_read_odometer(children),
  )
  children.finish()
  return calibration


def _read_version_3(root: ElementTree.Element) -> Calibration:
  """Read a version 3 document: plate types are free text and there are no counters."""
  logger.warning("calibration content version 3: the deck height counts as not calibrated")
  children = _Children(root, root.tag)
  instrument = _read_instrument(children.required("HyperCyt"), 3)
  where = children.where("PlateTypes")
  items = children.items("PlateTypes", "PlateTypeCalibrationContainer", versioned=True)
  entries = [_read_named_plate_type(item, f"{where}[{index}]") for index, item in enumerate(items)]
  calibration = Calibration(
    instrument=instrument,
    plate_types=tuple(entry for entry in entries if entry is not None),
    stations=_read_stations(children),
    plate_models=_read_plate_models(children, 3),
    detector_configuration=_read_detector_configuration(children),
  )
  children.finish()
  return _complete_version_3(calibration)


def _read_discarding_version(root: ElementTree.Element, version: int) -> Calibration:
  """Read a version 1 or 2 document.

  Its plate types and stations, and the bead and test-tube station lists these versions
  have, are replaced on the way to the current version, so they are not interpreted.
  """
  logger.warning(
    "calibration content version %d: its taught positions are not kept; "
    "the instrument must be calibrated again",
    version,
  )
  children = _Children(root, root.tag)
  instrument = _read_instrument(children.required("HyperCyt"), version)
  for replaced in ("PlateTypes", "RinseStations", "BeadStations", "TestTubeStations"):
    children.discard(replaced)
  calibration = Calibration(
    instrument=instrument,
    plate_models=_read_plate_models(children, version),
    detector_configuration=None if version == 1 else _read_detector_configuration(children),
  )
  children.finish()
  return _complete_version_3(_discard_taught_positions(calibration))


def _read_version_2(root: ElementTree.Element) -> Calibration:
  """Read a version 2 document: millimetres, but still the station model of version 1."""
  return _read_discarding_version(root, 2)


def _read_version_1(root: ElementTree.Element) -> Calibration:
  """Read a version 1 document: positions are whole tenths of a millimetre."""
  return _read_discarding_version(root, 1)


def _decode(document: bytes) -> str:
  """Decode the bytes of the XML entry: by its byte-order mark, else as UTF-8.

  Raises:
    CalibrationFormatError: If the bytes are not text in that encoding.
  """
  encoding = "utf-8-sig"
  if document.startswith((b"\xff\xfe", b"\xfe\xff")):
    encoding = "utf-16"
  try:
    return document.decode(encoding)
  except UnicodeDecodeError as error:
    raise CalibrationFormatError(f"{ENTRY_NAME} is not {encoding} text: {error}") from error


def _extract_document(data: bytes) -> bytes:
  """Return the bytes of the ``Calibration.xml`` entry of a calibration file.

  The entry is found whatever the case of its name; other entries are ignored.

  Raises:
    CalibrationFormatError: If the data is not a ZIP archive, or the entry is missing,
      ambiguous, encrypted or implausibly large.
  """
  try:
    archive = zipfile.ZipFile(io.BytesIO(data))
  except zipfile.BadZipFile as error:
    raise CalibrationFormatError(f"not a calibration file: {error}") from error
  with archive:
    entries = [info for info in archive.infolist() if info.filename.lower() == ENTRY_NAME.lower()]
    if len(entries) != 1:
      raise CalibrationFormatError(
        f"a calibration file has one {ENTRY_NAME} entry; this archive has {len(entries)}"
      )
    entry = entries[0]
    if entry.flag_bits & 0x1:
      raise CalibrationFormatError(f"{ENTRY_NAME} is encrypted, which the format never is")
    if entry.file_size > _MAX_DOCUMENT_SIZE:
      raise CalibrationFormatError(f"{ENTRY_NAME} is {entry.file_size} bytes, too large to be real")
    try:
      return archive.read(entry)
    except (zipfile.BadZipFile, zlib.error, NotImplementedError) as error:
      raise CalibrationFormatError(f"{ENTRY_NAME} cannot be extracted: {error}") from error


def read_calibration(data: bytes) -> Calibration:
  """Read a calibration file.

  All four content versions are accepted. Older ones are brought to the current model as the
  format defines, which loses calibration:

  - versions 1 and 2 keep none of their taught positions: heights, plate corners and
    stations come back as uncalibrated placeholders;
  - version 3 loses the date of its deck height (so it counts as not calibrated) and plate
    types that no longer exist, and gains an untaught ``Tube`` station.

  A warning is logged in both cases.

  Args:
    data: The bytes of the file (a ZIP archive).

  Returns:
    The calibration, in the current model.

  Raises:
    CalibrationFormatError: If the data is not a calibration file, its version is unknown, or
      any element is missing, unknown or malformed. Nothing is filled in with a default.
  """
  root = _parse_document(_decode(_extract_document(data)), ENTRY_NAME)
  if root.tag == CURRENT_ROOT:
    return _read_version_4(root)
  if root.tag == "CalibrationContainer3":
    return _read_version_3(root)
  if root.tag == "CalibrationContainer2":
    return _read_version_2(root)
  if root.tag == "CalibrationContainer":
    return _read_version_1(root)
  raise CalibrationFormatError(f"unknown calibration content version: root element <{root.tag}>")


# ---------------------------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------------------------


def _exact(value: float) -> decimal.Decimal:
  """Return the decimal number a float prints as, so 20.35 is exactly 20.35.

  Raises:
    CalibrationError: If the value is not a finite number.
  """
  number = float(value)
  if not math.isfinite(number):
    raise CalibrationError(f"{value!r} is not a finite number")
  return decimal.Decimal(repr(number))


def _decimal_text(number: decimal.Decimal, places: int) -> str:
  """Write a decimal with at least `places` decimals and never an exponent.

  Nothing is rounded: a number with more decimals keeps them.
  """
  if number == 0:
    number = abs(number)
  exponent = number.as_tuple().exponent
  if isinstance(exponent, int) and exponent > -places:
    number = number.quantize(decimal.Decimal(1).scaleb(-places), context=_CONTEXT)
  return format(number, "f")


def _millimetres_text(value: float) -> str:
  """Write a length in mm with three decimals or more, such as ``250.000``."""
  return _decimal_text(_exact(value), 3)


def _minutes_text(seconds: float) -> str:
  """Write a duration given in seconds as minutes with two decimals or more."""
  minutes = _CONTEXT.divide(_exact(seconds), _MINUTE)
  return _decimal_text(minutes, 2)


def _zone_text(value: datetime.datetime) -> str:
  """Write the zone of a timestamp: nothing for a naive one, ``Z`` for UTC, else ``+hh:mm``.

  Raises:
    CalibrationError: If the offset is not a whole number of minutes.
  """
  offset = value.utcoffset()
  if offset is None:
    return ""
  if not offset:
    return "Z"
  minutes, remainder = divmod(offset, datetime.timedelta(minutes=1))
  if remainder:
    raise CalibrationError(f"{value!r} has a zone offset finer than a minute")
  sign = "-" if minutes < 0 else "+"
  return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _timestamp_text(value: datetime.datetime) -> str:
  """Write an ``xsd:dateTime``; the fraction of a second appears only when it is not zero."""
  text = (
    f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
    f"T{value.hour:02d}:{value.minute:02d}:{value.second:02d}"
  )
  if value.microsecond:
    text += f".{value.microsecond:06d}".rstrip("0")
  return text + _zone_text(value)


def _escape(text: str) -> str:
  """Escape text for use as element content. Line ends become CR LF, as in the whole file.

  Raises:
    CalibrationError: If the text has a control character other than a tab or a line feed.
  """
  for character in text:
    if ord(character) < 0x20 and character not in "\t\n":
      raise CalibrationError(f"{text!r} has a character a calibration file cannot hold")
  escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
  return escaped.replace("\n", "\r\n")


class _Document:
  """An XML document built line by line, indented by two spaces per level."""

  def __init__(self, declared_encoding: str) -> None:
    """Start a document with its XML declaration."""
    self._lines: List[str] = [f'<?xml version="1.0" encoding="{declared_encoding}"?>']
    self._depth = 0

  def _line(self, text: str) -> None:
    """Add one line at the current depth."""
    self._lines.append("  " * self._depth + text)

  def open(self, name: str, attributes: str = "") -> None:
    """Start an element that holds other elements."""
    self._line(f"<{name} {attributes}>" if attributes else f"<{name}>")
    self._depth += 1

  def close(self, name: str) -> None:
    """End an element started with :meth:`open`."""
    self._depth -= 1
    self._line(f"</{name}>")

  def value(self, name: str, text: str) -> None:
    """Add an element holding text; an empty text gives an empty element."""
    self._line(f"<{name}>{_escape(text)}</{name}>" if text else f"<{name} />")

  def nil(self, name: str) -> None:
    """Add an element that says "no value"."""
    self._line(f'<{name} xsi:nil="true" />')

  def string(self, name: str, text: Optional[str]) -> None:
    """Add a string; "no string" is written by leaving the element out."""
    if text is not None:
      self.value(name, text)

  def timestamp(self, name: str, value: Optional[datetime.datetime]) -> None:
    """Add a date and time, or the "no value" form."""
    if value is None:
      self.nil(name)
    else:
      self.value(name, _timestamp_text(value))

  def millimetres(self, name: str, value: Optional[float]) -> None:
    """Add a length in mm, or the "no value" form."""
    if value is None:
      self.nil(name)
    else:
      self.value(name, _millimetres_text(value))

  def empty(self, name: str) -> None:
    """Add a list without items."""
    self._line(f"<{name} />")

  def text(self, line_end: str) -> str:
    """Return the document; nothing follows the end of the root element."""
    return line_end.join(self._lines)


def _boolean_text(value: bool) -> str:
  """Write a boolean as ``true`` or ``false``."""
  return "true" if value else "false"


def _write_instrument(document: _Document, instrument: Instrument) -> None:
  """Write the instrument record; every value element is present."""
  document.open("HyperCyt")
  document.value("HasShaker", _boolean_text(instrument.has_shaker))
  document.value("PumpLocation", instrument.pump_location)
  document.millimetres("ZHigh", instrument.travel_height)
  document.timestamp("ZHighLastCalibratedOn", instrument.travel_height_calibrated_on)
  document.millimetres("ZLow", instrument.deck_height)
  document.timestamp("ZLowLastCalibratedOn", instrument.deck_height_calibrated_on)
  document.value("SystemKind", instrument.system_kind)
  document.string("HyperCytName", instrument.name)
  document.string("DeviceID", instrument.device_id)
  document.string("SerialNumber", instrument.serial_number)
  document.value("SDSEnabled", _boolean_text(instrument.selectable_detectors_enabled))
  document.millimetres("DeckHeightX", instrument.deck_height_test_x)
  document.millimetres("DeckHeightY", instrument.deck_height_test_y)
  document.value("NotifyHoursProbeChange", str(int(instrument.probe_change_reminder_hours)))
  document.close("HyperCyt")


def _write_plate_type(document: _Document, entry: PlateTypeCalibration) -> None:
  """Write one plate type entry."""
  document.open("PlateTypeCalibrationContainer3")
  document.value("PlateType", entry.plate_type)
  document.millimetres("TopLeftX", entry.corners.top_left.x)
  document.millimetres("TopLeftY", entry.corners.top_left.y)
  document.millimetres("TopRightX", entry.corners.top_right.x)
  document.millimetres("TopRightY", entry.corners.top_right.y)
  document.millimetres("BottomLeftX", entry.corners.bottom_left.x)
  document.millimetres("BottomLeftY", entry.corners.bottom_left.y)
  document.millimetres("BottomRightX", entry.corners.bottom_right.x)
  document.millimetres("BottomRightY", entry.corners.bottom_right.y)
  document.timestamp("LastCalibratedOn", entry.calibrated_on)
  document.close("PlateTypeCalibrationContainer3")


def _write_station(document: _Document, station: Station) -> None:
  """Write one station entry."""
  document.open("RinseStationCalibrationContainer2")
  document.value("RinseStationName", station.name)
  document.millimetres("XPos", station.x)
  document.millimetres("YPos", station.y)
  document.millimetres("ZLow", station.depth)
  document.timestamp("LastCalibratedOn", station.calibrated_on)
  document.close("RinseStationCalibrationContainer2")


def _depth_map_text(depth_map: DepthMap) -> str:
  """Write the depth map document a plate model carries as text.

  It is a document of its own, declared as ``utf-16`` because it lives in a string.
  """
  document = _Document("utf-16")
  document.open("PlateMappingState", _NAMESPACES)
  document.open("Points")
  for point in depth_map_points(depth_map.plate_format, len(depth_map.values)):
    document.value("int", str(point))
  document.close("Points")
  document.open("Values")
  for value in depth_map.values:
    document.value("decimal", _decimal_text(_exact(value), 0))
  document.close("Values")
  document.value("PlateType", depth_map.plate_format)
  document.value("InterpolationPoints", str(len(depth_map.values)))
  document.value("LastCalibratedOn", _timestamp_text(depth_map.calibrated_on))
  document.close("PlateMappingState")
  return document.text("\n")


def _write_plate_model(document: _Document, model: PlateModel) -> None:
  """Write one plate model entry, with its depth map as escaped text when it has one."""
  document.open("PlateModelCalibrationContainer3")
  document.value("ModelName", model.name)
  document.millimetres("WellDepthOffset", model.well_depth_offset)
  document.millimetres("XOffset", model.x_offset)
  document.millimetres("YOffset", model.y_offset)
  if model.depth_map is not None:
    document.value("MapState", _depth_map_text(model.depth_map))
  document.close("PlateModelCalibrationContainer3")


def _write_detector_configuration(
  document: _Document, configuration: DetectorConfiguration
) -> None:
  """Write the detector filter set."""
  document.open("SDSConfiguration")
  document.value("LaserConfiguration", configuration.laser_configuration)
  document.value("FL1Filter", configuration.fl1_filter)
  document.value("FL2Filter", configuration.fl2_filter)
  document.value("FL3Filter", configuration.fl3_filter)
  document.value("FL4Filter", configuration.fl4_filter)
  document.timestamp("LastCalibratedOn", configuration.calibrated_on)
  document.close("SDSConfiguration")


def _write_odometer(document: _Document, odometer: ConsumablesOdometer) -> None:
  """Write the consumable counters, in minutes."""
  document.open("ConsumablesOdometer")
  document.timestamp("LastRunOn", odometer.last_run_on)
  document.value("RunTimeSinceNewProbe", _minutes_text(odometer.run_time_since_new_probe))
  document.value("RunTimeSinceNewFilter", _minutes_text(odometer.run_time_since_new_filter))
  document.value("RunTimeSinceBottleClean", _minutes_text(odometer.run_time_since_bottle_clean))
  document.value("RunTimeSinceNewPigTail", _minutes_text(odometer.run_time_since_new_pig_tail))
  document.value(
    "RunTimeSinceNewCytTubing", _minutes_text(odometer.run_time_since_new_cytometer_tubing)
  )
  document.value("CumulativeRunTimePump", _minutes_text(odometer.cumulative_run_time_pump))
  document.value("CumulativeRunTimeCyt", _minutes_text(odometer.cumulative_run_time_cytometer))
  document.close("ConsumablesOdometer")


def _write_list(
  document: _Document,
  name: str,
  items: Sequence[_Item],
  write_item: Callable[[_Document, _Item], None],
) -> None:
  """Write a list: a wrapper with one element per item, or an empty wrapper."""
  if not items:
    document.empty(name)
    return
  document.open(name)
  for item in items:
    write_item(document, item)
  document.close(name)


def calibration_document(calibration: Calibration) -> str:
  """Return the text of ``Calibration.xml`` for a calibration, in the current version.

  Elements come in the format's fixed order, indented by two spaces, with CR LF line ends and
  no namespace on any element. Lengths have at least three decimals. A list without items is
  an empty element; "no value" is ``xsi:nil`` for numbers and dates, and a missing element for
  strings and for the two optional records.

  Raises:
    CalibrationError: If a value cannot be written (a number that is not finite, text with
      control characters).
  """
  document = _Document("utf-8")
  document.open(CURRENT_ROOT, _NAMESPACES)
  _write_instrument(document, calibration.instrument)
  _write_list(document, "PlateTypes", calibration.plate_types, _write_plate_type)
  _write_list(document, "RinseStations", calibration.stations, _write_station)
  _write_list(document, "PlateModels", calibration.plate_models, _write_plate_model)
  if calibration.detector_configuration is not None:
    _write_detector_configuration(document, calibration.detector_configuration)
  if calibration.consumables_odometer is not None:
    _write_odometer(document, calibration.consumables_odometer)
  document.close(CURRENT_ROOT)
  return document.text("\r\n")


def write_calibration(calibration: Calibration) -> bytes:
  """Write a calibration file in the current content version.

  The result is a ZIP archive with the single entry ``Calibration.xml``, deflated, without
  ZIP64, encryption or comment. The document is UTF-8 with a byte-order mark; see
  :func:`calibration_document` for its layout. The entry carries a fixed date (the ZIP epoch),
  so equal calibrations give equal bytes.

  The calibration is written as it is. Whether another program accepts the file can depend on
  content this function does not judge, such as an entry for every plate type and station.

  Args:
    calibration: The calibration to write.

  Returns:
    The bytes of the file.

  Raises:
    CalibrationError: If a value cannot be written, or the file would exceed the size the
      instrument accepts.
  """
  document = calibration_document(calibration).encode("utf-8-sig")
  entry = zipfile.ZipInfo(ENTRY_NAME)
  entry.compress_type = zipfile.ZIP_DEFLATED
  entry.create_system = 0
  buffer = io.BytesIO()
  with zipfile.ZipFile(buffer, "w", allowZip64=False) as archive:
    archive.writestr(entry, document)
  data = buffer.getvalue()
  if len(data) > MAX_FILE_SIZE:
    raise CalibrationError(
      f"the calibration file is {len(data)} bytes; the instrument accepts {MAX_FILE_SIZE}"
    )
  return data


# ---------------------------------------------------------------------------------------------
# Coordinates
# ---------------------------------------------------------------------------------------------


def _from_double(value: float) -> decimal.Decimal:
  """Return a float as a decimal of 15 significant digits, the precision a double carries."""
  if not math.isfinite(value):
    raise CalibrationError(f"{value!r} is not a finite number")
  return decimal.Decimal(format(value, ".15g"))


def _total(*terms: decimal.Decimal) -> float:
  """Add decimal terms exactly and return the sum as a float."""
  total = decimal.Decimal(0)
  for term in terms:
    total = _CONTEXT.add(total, term)
  return float(total)


def _check_well(plate_format: str, row: int, column: int) -> Tuple[int, int]:
  """Return (rows, columns) of a plate format after checking that a well is on it.

  Raises:
    CalibrationError: If the plate format has no wells or the well is not on the plate.
  """
  rows, columns = _dimensions(plate_format)
  if not (0 <= row < rows and 0 <= column < columns):
    raise CalibrationError(f"row {row}, column {column} is not on a {plate_format} plate")
  return rows, columns


def _step_towards(
  start: decimal.Decimal, end: decimal.Decimal, step: int, steps: int
) -> decimal.Decimal:
  """Return the point `step` of `steps` equal parts from start to end, rounded to 0.001.

  The rounding is to the nearest thousandth with halves going to the even digit.
  """
  travelled = _CONTEXT.divide(_CONTEXT.multiply(step, _CONTEXT.subtract(end, start)), steps)
  return _CONTEXT.add(start, travelled).quantize(
    _THOUSANDTH, rounding=decimal.ROUND_HALF_EVEN, context=_CONTEXT
  )


def interpolate_well_xy(corners: Corners, rows: int, columns: int, row: int, column: int) -> XY:
  """Return the centre of a well from the four corner wells of its plate.

  The plate's edges are divided first, each result rounded to 0.001 mm (halves to the even
  digit): the X of the left and right edge at the well's row, and the Y of the top and bottom
  edge at the well's column. The well's X then divides its row from left to right and its Y
  divides its column from top to bottom, rounded again. Row A takes the X of the two top
  corners as taught, and column 1 the Y of the two left corners as taught, so the top-left
  well is exactly its taught corner.

  Args:
    corners: The corner wells.
    rows: Number of rows of the plate.
    columns: Number of columns of the plate.
    row: Zero-based row of the well.
    column: Zero-based column of the well.

  Returns:
    The well centre in mm.
  """
  top_left = (_exact(corners.top_left.x), _exact(corners.top_left.y))
  top_right = (_exact(corners.top_right.x), _exact(corners.top_right.y))
  bottom_left = (_exact(corners.bottom_left.x), _exact(corners.bottom_left.y))
  bottom_right = (_exact(corners.bottom_right.x), _exact(corners.bottom_right.y))
  left, right = top_left[0], top_right[0]
  if row > 0:
    left = _step_towards(top_left[0], bottom_left[0], row, rows - 1)
    right = _step_towards(top_right[0], bottom_right[0], row, rows - 1)
  top, bottom = top_left[1], bottom_left[1]
  if column > 0:
    top = _step_towards(top_left[1], top_right[1], column, columns - 1)
    bottom = _step_towards(bottom_left[1], bottom_right[1], column, columns - 1)
  x = left if column == 0 else _step_towards(left, right, column, columns - 1)
  y = top if row == 0 else _step_towards(top, bottom, row, rows - 1)
  return XY(float(x), float(y))


def _moved(point: XY, dx: decimal.Decimal, dy: decimal.Decimal) -> XY:
  """Return a point moved by exact decimal amounts."""
  return XY(_total(_exact(point.x), dx), _total(_exact(point.y), dy))


def derive_corners(
  corners_384: Corners, plate_format: PlateFormat, y_axis_inverted: bool = True
) -> Corners:
  """Return the corner wells of a plate format from the taught 384-well corners.

  Only the 384-well corners are taught. The corner wells of a 96-well plate lie 2.2 mm inside
  them in X and Y, and those of a 1536-well plate 1.1 mm outside them.

  Args:
    corners_384: The taught corners of the 384-well format.
    plate_format: The format to derive; ``Well384`` returns the corners unchanged.
    y_axis_inverted: Whether the sampler's Y shrinks from row A to the last row, as on this
      instrument. It decides which way "inside" is in Y.

  Returns:
    The corners of the requested format.
  """
  if plate_format == "Well384":
    return corners_384
  inwards = _exact(CORNER_INSET_96)
  if plate_format == "Well1536":
    inwards = -_exact(CORNER_OUTSET_1536)
  down = -inwards if y_axis_inverted else inwards
  return Corners(
    top_left=_moved(corners_384.top_left, inwards, down),
    top_right=_moved(corners_384.top_right, -inwards, down),
    bottom_left=_moved(corners_384.bottom_left, inwards, -down),
    bottom_right=_moved(corners_384.bottom_right, -inwards, -down),
  )


def well_xy(calibration: Calibration, plate_format: PlateFormat, row: int, column: int) -> XY:
  """Return the centre of a well as taught, without any plate-model offset.

  This is where a depth map is measured. Use :func:`well_sampling_xy` for sampling.

  Raises:
    CalibrationError: If the well is not on the plate or the plate format is not calibrated.
  """
  rows, columns = _check_well(plate_format, row, column)
  entry = calibration.plate_type(plate_format)
  if not entry.is_calibrated:
    raise CalibrationError(f"the corners of plate type {plate_format} are not calibrated")
  return interpolate_well_xy(entry.corners, rows, columns, row, column)


def well_sampling_xy(
  calibration: Calibration, plate_format: PlateFormat, plate_model: str, row: int, column: int
) -> XY:
  """Return where the probe samples a well: its centre plus the plate model's XY offset.

  Args:
    calibration: The calibration.
    plate_format: The format of the plate.
    plate_model: The name of the plate's model.
    row: Zero-based row.
    column: Zero-based column.

  Raises:
    CalibrationError: If the well is not on the plate, the plate format is not calibrated or
      the plate model is unknown.
  """
  centre = well_xy(calibration, plate_format, row, column)
  model = calibration.plate_model(plate_model)
  return _moved(centre, _exact(model.x_offset), _exact(model.y_offset))


def _interval(nodes: Sequence[int], position: int) -> int:
  """Return the index of the node interval that contains a position."""
  for index in range(len(nodes) - 2, 0, -1):
    if nodes[index] <= position:
      return index
  return 0


def depth_map_value(depth_map: DepthMap, row: int, column: int) -> float:
  """Return the mapped height of a well's bottom above the deck, in mm.

  The value is interpolated linearly in both directions between the four measured wells
  around the well. The measured grid spans the plate, so nothing is extrapolated.

  Raises:
    CalibrationError: If the well is not on the mapped plate format.
  """
  _check_well(depth_map.plate_format, row, column)
  xs, ys = depth_map_nodes(depth_map.plate_format, len(depth_map.values))
  i, j = _interval(xs, column), _interval(ys, row)
  t = (column - xs[i]) / (xs[i + 1] - xs[i])
  u = (row - ys[j]) / (ys[j + 1] - ys[j])
  upper, lower = j * len(xs) + i, (j + 1) * len(xs) + i
  values = depth_map.values
  return (
    (1 - t) * (1 - u) * values[upper]
    + t * (1 - u) * values[upper + 1]
    + (1 - t) * u * values[lower]
    + t * u * values[lower + 1]
  )


def travel_height(calibration: Calibration) -> float:
  """Return the Z the probe retracts to before any horizontal move.

  Raises:
    CalibrationError: If the travel height is 0, which is how a record that never held one
      states it.
  """
  if calibration.instrument.travel_height == 0:
    raise CalibrationError("the travel height is not set")
  return calibration.instrument.travel_height


def _deck_height(calibration: Calibration) -> decimal.Decimal:
  """Return the deck height.

  Raises:
    CalibrationError: If the deck height is not calibrated.
  """
  if not calibration.instrument.deck_height_is_calibrated:
    raise CalibrationError("the deck height is not calibrated")
  return _exact(calibration.instrument.deck_height)


def well_sampling_z(
  calibration: Calibration,
  plate_format: PlateFormat,
  plate_model: str,
  row: int,
  column: int,
  sampling_height: float,
) -> float:
  """Return the Z at which the probe samples a well.

  With a depth map, that is deck height + mapped height of the well's bottom + sampling
  height. A plate model without a map (instruments without a contact sensor) has one taught
  depth for all wells, and the result is deck height + that depth offset; the sampling height
  is not used then.

  Args:
    calibration: The calibration.
    plate_format: The format of the plate.
    plate_model: The name of the plate's model.
    row: Zero-based row.
    column: Zero-based column.
    sampling_height: How far above the well bottom to sample, in mm; see
      `DEFAULT_SAMPLING_HEIGHTS`.

  Raises:
    CalibrationError: If the well is not on the plate, the deck height is not calibrated, the
      plate model is unknown, its map is of another plate format, it has neither a map nor a
      taught depth offset, or the sampling height is negative.
  """
  _check_well(plate_format, row, column)
  if sampling_height < 0:
    raise CalibrationError(f"a sampling height of {sampling_height} mm is below the well bottom")
  deck = _deck_height(calibration)
  model = calibration.plate_model(plate_model)
  if model.depth_map is None:
    if not model.is_calibrated:
      raise CalibrationError(f"plate model {plate_model!r} has no depth map and no taught depth")
    return _total(deck, _exact(model.well_depth_offset))
  if model.depth_map.plate_format != plate_format:
    raise CalibrationError(
      f"the depth map of plate model {plate_model!r} is for {model.depth_map.plate_format}"
    )
  mapped = _from_double(depth_map_value(model.depth_map, row, column))
  return _total(deck, mapped, _exact(sampling_height))


def _calibrated_station(calibration: Calibration, name: str) -> Station:
  """Return a station that has been taught.

  Raises:
    CalibrationError: If the station is unknown or not calibrated.
  """
  station = calibration.station(name)
  if not station.is_calibrated:
    raise CalibrationError(f"station {name!r} is not calibrated")
  return station


def station_xy(calibration: Calibration, name: str) -> XY:
  """Return the probe position over a station.

  Raises:
    CalibrationError: If the station is unknown or not calibrated.
  """
  station = _calibrated_station(calibration, name)
  return XY(station.x, station.y)


def station_dip_z(calibration: Calibration, name: str) -> float:
  """Return the Z the probe dips to at a station: deck height + the station's depth.

  Raises:
    CalibrationError: If the station is unknown, or it or the deck height is not calibrated.
  """
  station = _calibrated_station(calibration, name)
  return _total(_deck_height(calibration), _exact(station.depth))


def station_draw_z(calibration: Calibration, name: str) -> float:
  """Return the Z for drawing continuously from a station: 2 mm above its dip depth.

  Raises:
    CalibrationError: As for :func:`station_dip_z`.
  """
  station = _calibrated_station(calibration, name)
  return _total(_deck_height(calibration), _exact(station.depth), _exact(TUBE_SAMPLING_HEIGHT))


def tube_xy(calibration: Calibration) -> XY:
  """Return the probe position over the tube holder.

  Raises:
    CalibrationError: If the tube holder is not calibrated.
  """
  return station_xy(calibration, TUBE_STATION)


def tube_contact_z(calibration: Calibration) -> float:
  """Return the Z at which the probe touches the bottom of the empty tube holder.

  A tube is present when the probe touches something above this height.

  Raises:
    CalibrationError: If the tube holder or the deck height is not calibrated.
  """
  return station_dip_z(calibration, TUBE_STATION)


def tube_sampling_z(calibration: Calibration) -> float:
  """Return the Z at which the probe samples a tube: 2 mm above the empty holder's bottom.

  Raises:
    CalibrationError: If the tube holder or the deck height is not calibrated.
  """
  return station_draw_z(calibration, TUBE_STATION)


def deck_height_test_point(calibration: Calibration) -> XY:
  """Return the spot on the plate holder where the deck height is measured and checked.

  Raises:
    CalibrationError: If the deck height is not calibrated or the point is not set.
  """
  instrument = calibration.instrument
  x, y = instrument.deck_height_test_x, instrument.deck_height_test_y
  if not instrument.deck_height_is_calibrated or x is None or y is None:
    raise CalibrationError("the deck height and its test point are not calibrated")
  return XY(x, y)


def with_deck_height(
  calibration: Calibration,
  deck_height: float,
  measured_on: datetime.datetime,
  test_point: Optional[XY] = None,
) -> Calibration:
  """Return the calibration with a newly measured deck height.

  Only the deck height and its date change. Station depths, plate-model depth offsets and
  depth maps are stored relative to the deck height and are not rewritten, so every well,
  station and tube depth moves by the same amount as the deck height did. Depth maps were
  measured against a deck height taken during their own mapping, which may differ slightly
  from the stored one.

  Args:
    calibration: The calibration to start from.
    deck_height: The measured contact height at the test point.
    measured_on: When it was measured.
    test_point: The spot it was measured at, when that is being taught too; otherwise the
      stored test point is kept.

  Returns:
    A new calibration; the given one is not changed.
  """
  instrument = dataclasses.replace(
    calibration.instrument, deck_height=deck_height, deck_height_calibrated_on=measured_on
  )
  if test_point is not None:
    instrument = dataclasses.replace(
      instrument, deck_height_test_x=test_point.x, deck_height_test_y=test_point.y
    )
  return dataclasses.replace(calibration, instrument=instrument)


# ---------------------------------------------------------------------------------------------
# Nominal values
# ---------------------------------------------------------------------------------------------

NOMINAL_CORNERS_384 = Corners(
  top_left=XY(18.0, 111.7),
  top_right=XY(121.8, 111.35),
  bottom_left=XY(17.8, 44.1),
  bottom_right=XY(121.65, 43.8),
)
NOMINAL_DECK_HEIGHT_TEST_POINT = XY(3.0, 105.0)
NOMINAL_STATION_POSITIONS: Mapping[str, XY] = {
  "Buffer": XY(27.25, 1.5),
  "Decon": XY(75.75, 1.5),
  "Clean": XY(89.15, 1.5),
  "Water": XY(109.65, 1.5),
  "Markers": XY(54.85, 1.5),
  "Tube": XY(0.0, 13.5),
}
NOMINAL_PLATE_MODELS: Tuple[str, ...] = (
  "Generic 96 Well Plate",
  "Generic 384 Well Plate",
  "Generic 1536 Well Plate",
)
NOMINAL_PROBE_CHANGE_REMINDER_HOURS = 20


def _nominal_plate_type(plate_type: PlateType) -> PlateTypeCalibration:
  """Return the nominal, uncalibrated entry of one plate type."""
  for plate_format in PLATE_FORMATS:
    if plate_format == plate_type:
      return PlateTypeCalibration(plate_type, derive_corners(NOMINAL_CORNERS_384, plate_format))
  return PlateTypeCalibration(plate_type, _NO_CORNERS)


def nominal_calibration(
  system_kind: SystemKind = "iQue3",
  name: Optional[str] = None,
  device_id: Optional[str] = None,
  serial_number: Optional[str] = None,
) -> Calibration:
  """Return a calibration made of nominal starting values. It does not replace calibrating.

  Nothing in it is taught: no entry has a calibration date, so the coordinate functions of
  this module refuse to work from it. The values are where to move the probe before fine
  positioning it by hand, or markers for "not calibrated":

  - travel height at the top of the Z range, deck height at the bottom of it;
  - the deck-height test point, the 384-well corners and the station positions where teaching
    usually starts, with the 96- and 1536-well corners derived from the 384-well ones;
  - station depths at the marker a new instrument carries (stations have no nominal depth);
  - one generic plate model per plate format, without depth offset or depth map.

  Every instrument differs from these by a millimetre or more; sampling with them would
  miss wells or drive the probe into a plate.

  Args:
    system_kind: The instrument family member.
    name: The instrument's display name.
    device_id: The controller's system identifier.
    serial_number: The instrument's serial number.

  Returns:
    The nominal calibration.
  """
  instrument = Instrument(
    travel_height=SAMPLER_Z_RANGE[1],
    deck_height=SAMPLER_Z_RANGE[0],
    pump_location="Right",
    system_kind=system_kind,
    deck_height_test_x=NOMINAL_DECK_HEIGHT_TEST_POINT.x,
    deck_height_test_y=NOMINAL_DECK_HEIGHT_TEST_POINT.y,
    name=name,
    device_id=device_id,
    serial_number=serial_number,
    probe_change_reminder_hours=NOMINAL_PROBE_CHANGE_REMINDER_HOURS,
  )
  stations = tuple(
    Station(name=station, x=position.x, y=position.y, depth=UNCALIBRATED_STATION_DEPTH)
    for station, position in NOMINAL_STATION_POSITIONS.items()
  )
  return Calibration(
    instrument=instrument,
    plate_types=tuple(_nominal_plate_type(plate_type) for plate_type in PLATE_TYPES),
    stations=stations,
    plate_models=tuple(PlateModel(name=model) for model in NOMINAL_PLATE_MODELS),
    detector_configuration=DetectorConfiguration(),
    consumables_odometer=ConsumablesOdometer(),
  )
