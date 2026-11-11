"""Worklists of the iQue Screener PLUS: building them, and reading a finished one.

The instrument's controller runs a plate read, a tube read or a maintenance routine as a worklist:
an ordered list of commands that it executes on its own, one after the other, and hands back with
the start and end time of every command. This module builds those worklists from run settings and
the instrument's calibration, and turns the worklist of a finished plate read into the sampled
wells that well identification needs. It does no I/O.

Settings are in PyLabRobot units: seconds, mm, rpm. Commands carry the controller's units, and the
conversion happens in one place (the ``_milliseconds``, ``_whole_seconds``, ``_pump_speed`` and
``_position`` helpers): positions in mm as decimals with three places, sip and up times in ms,
timed rinse and shake durations in whole seconds, cycling rinse durations in ms, pump speeds in
hundredths of an rpm, shaker speeds in rpm.

A plate read (:func:`plate_read_worklist`) does this:

1. load the plate and start the cytometer fluidics for acquisition;
2. touch the deck-height test point with the probe; the run stops when the deck is not within the
   tolerance of the calibrated height;
3. start the pump, then optionally prime in Buffer and shake the plate;
4. start recording events, and the shaker when it shakes throughout;
5. sample each well with one dip of the probe; between wells, shake the plate every so many wells
   while the probe dips in a station, and rinse the probe every so many wells;
6. flush the line in Buffer while the plate rail ejects the plate, clean the probe in a series of
   stations, stop the pump and the fluidics.

The probe draws air between dips, so wells reach the detector as bursts separated by gaps. A
plate is one continuous acquisition; nothing in the event stream marks a well, which is why
:func:`sampled_wells` reads the times of the sampling commands back from the finished worklist.

Whatever happens, the controller ends every worklist by stopping the pump and shaker, parking the
probe, stopping the fluidics (unless the worklist asks it to leave them running) and ejecting the
plate rail.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import logging
import math
import uuid
from dataclasses import dataclass
from typing import (
  Dict,
  FrozenSet,
  List,
  Literal,
  NamedTuple,
  Optional,
  Sequence,
  Set,
  Tuple,
  Type,
  TypedDict,
)

from pylabrobot.sartorius.ique.driver.calibration import (
  DEFAULT_SAMPLING_HEIGHTS,
  PLATE_DIMENSIONS,
  SAMPLER_X_RANGE,
  SAMPLER_Y_RANGE,
  SAMPLER_Z_RANGE,
  Calibration,
  PlateFormat,
  deck_height_test_point,
  format_well_name,
  parse_well_name,
  station_dip_z,
  station_draw_z,
  station_xy,
  travel_height,
  tube_contact_z,
  tube_sampling_z,
  tube_xy,
  well_sampling_xy,
  well_sampling_z,
)
from pylabrobot.sartorius.ique.driver.contract import (
  CleanFlowCytometerCommand,
  CombinedShakeRinseAtLocationCommand,
  CommandCategory,
  CyclingRinseCommand,
  DeviceKind,
  EnabledLasers,
  FlowCytometerAcquireCommand,
  FlowCytometerStopAcquireCommand,
  FlushCommand,
  FlushMatryoshkaCommand,
  PumpDirection,
  RinseStationPump,
  SampleContinuouslyCommand,
  SampleSpeed,
  SampleVialCommand,
  SampleWellCommand,
  ShutdownFlowCytometerCommand,
  StartFlowCytometerFluidicsCommand,
  StartMatryoshkaFluidicsCommand,
  StartPumpCommand,
  StartShakerCommand,
  StopFlowCytometerFluidicsCommand,
  StopPumpCommand,
  StopShakerCommand,
  Threshold,
  TimedContinuousRinseCommand,
  TimedCyclingRinseCommand,
  ValidateDeckHeightCommand,
  Worklist,
  WorklistCommand,
)
from pylabrobot.sartorius.ique.driver.fcs import (
  PARAMETER_TABLES,
  SYSTEM_KIND_CONFIGURATIONS,
  Laser,
  OpticalConfiguration,
  Parameter,
  ParameterTable,
)
from pylabrobot.sartorius.ique.driver.well_identification import SampledWell

logger = logging.getLogger(__name__)

WashStation = Literal["Buffer", "Decon", "Clean", "Water"]
ShakeMode = Literal["none", "continuous", "after_wells"]
WellOrder = Literal["row", "serpentine_row", "column", "serpentine_column"]
CleanMethod = Literal["QuickClean", "LongClean", "Unclog", "Priming"]

WASH_STATIONS: Tuple[WashStation, ...] = ("Buffer", "Decon", "Clean", "Water")
SHAKE_MODES: Tuple[ShakeMode, ...] = ("none", "continuous", "after_wells")
WELL_ORDERS: Tuple[WellOrder, ...] = ("row", "serpentine_row", "column", "serpentine_column")
LASERS: Tuple[Laser, ...] = ("violet", "blue", "yellow", "red")

# The station the probe primes in, flushes in and waits in during the pre-plate shake.
BUFFER = "Buffer"
# The station the probe draws water from for the cleanliness test.
WATER = "Water"

# ---------------------------------------------------------------------------------------------
# Wire units
# ---------------------------------------------------------------------------------------------

_MILLISECONDS_PER_SECOND = decimal.Decimal(1000)
# The controller takes pump speeds in hundredths of an rpm.
_PUMP_SPEED_SCALE = decimal.Decimal(100)
# The sampler is sent positions rounded to a thousandth of a mm.
POSITION_RESOLUTION = decimal.Decimal("0.001")
# The event count the controller reads as "no limit": the largest 32-bit signed integer.
NO_EVENT_LIMIT = 2_147_483_647
# A command time the controller has not filled in: the .NET default date.
NOT_RUN = datetime.datetime(1, 1, 1)

# ---------------------------------------------------------------------------------------------
# Instrument values
# ---------------------------------------------------------------------------------------------

# A plate read is refused when the probe touches the deck further than this from the calibrated
# deck height, in mm. The controller states this tolerance (contact tolerance for deck height
# validation); this is the value it reports.
DECK_HEIGHT_TOLERANCE = 0.25
# A tube is present when the probe touches between 0.2 mm and this many mm above the bottom of the
# empty tube holder; the controller's contact tolerance for tube validation.
TUBE_TOLERANCE = 10.0
# The user name the controller writes into the FCS file when none is given.
UNKNOWN_USER = "UNK"
# The controller's default pump speed in rpm, used for priming and cleaning outside a plate read.
MAINTENANCE_PUMP_SPEED = 28.6
# Pump speeds the instrument uses: 10 rpm is the slowest sampling speed, 53 rpm the priming speed.
PUMP_SPEED_RANGE = (10.0, 32.0)
MAINTENANCE_PUMP_SPEED_RANGE = (10.0, 53.0)
PRIME_PUMP_SPEED = 53.0
# Shaker speeds in rpm: the BioShake 5000 of the HD model and the BioShake 3000 of the others.
SHAKER_SPEED_RANGES: Dict[str, Tuple[int, int]] = {
  "iQue3": (200, 3000),
  "iQue3HD": (200, 5000),
  "iQuePlusVYB": (200, 3000),
  "iQuePlusBR": (200, 3000),
}
DEFAULT_SHAKE_SPEEDS: Dict[str, Dict[PlateFormat, int]] = {
  "iQue3": {"Well96": 2400, "Well384": 3000, "Well1536": 3000},
  "iQue3HD": {"Well96": 2000, "Well384": 2500, "Well1536": 5000},
  "iQuePlusVYB": {"Well96": 2400, "Well384": 3000, "Well1536": 3000},
  "iQuePlusBR": {"Well96": 2400, "Well384": 3000, "Well1536": 3000},
}
# Between wells and in stations the probe stays up at least this long per dip, in seconds, so
# that each dip is followed by an air gap.
MIN_STATION_UP_TIME = 0.5
# Settings ranges, in the units of the settings.
SIP_TIME_RANGE = (0.5, 59.9)
UP_TIME_RANGE = (0.0, 59.9)
PRIME_DURATION_RANGE = (5.0, 600.0)
PRE_PLATE_SHAKE_DURATION_RANGE = (1.0, 180.0)
SHAKE_DURATION_RANGE = (1.0, 180.0)
RINSE_DURATION_RANGE = (0.5, 60.0)
FLUSH_DURATION_RANGE = (30.0, 600.0)
CLEAN_DURATION_RANGE = (5.0, 600.0)
MANUAL_PRIME_DURATION_RANGE = (1.0, 3600.0)
TUBE_MIN_SIP_TIME = 5.0
THRESHOLD_RANGE = (1000, 16_700_000)
MAX_EVENT_LIMIT = 40_000_000
MAX_THRESHOLDS = 2
PRIMARY_THRESHOLD_PARAMETERS = ("FSC-H", "SSC-H")
# The probe and tubing clean lasts a whole number of these, from 4 to 32 minutes: a quarter of it
# in Decon, a quarter in Clean and half in Water.
CLEAN_DURATION_STEP = 240
PROBE_CLEAN_DURATION_RANGE = (240.0, 1920.0)
PROBE_CLEAN_DURATION = 1200.0
# The probe holds a tube sample, and draws from a station continuously, 2 mm above the taught
# depth (the calibration functions apply it). In a tube read the pump draws this volume at most,
# in uL, and the instrument's flow model is uL/min = rpm / 0.265.
TUBE_MAX_DRAW = decimal.Decimal(320)
PUMP_RPM_PER_UL_PER_MINUTE = decimal.Decimal("0.265")
# The cleanliness test draws water this long, in seconds, at the default sampling speed in rpm.
CLEAN_TEST_DURATION = 60.0
CLEAN_TEST_PUMP_SPEED = 29.0
# The one-minute fluidics (leak) check: water for this long, with these dip and up times, in s.
FLUIDICS_CHECK_DURATION = 60.0
FLUIDICS_CHECK_SIP_TIME = 59.0
FLUIDICS_CHECK_UP_TIME = 2.0
# Dip and up times of the probe and tubing clean and of the clean after a tube, in seconds.
CLEAN_SIP_TIME = 1.0
CLEAN_UP_TIME = 0.5
# Devices that can stop a maintenance worklist by failing: all but the shaker, which a clean does
# not use; and when the worklist shuts the cytometer down, not the cytometer either.
MAINTENANCE_DEVICES: Tuple[DeviceKind, ...] = (
  "Sampler",
  "Pump",
  "Cytometer",
  "PlateRail",
  "ContactSensor",
)
SHUTDOWN_DEVICES: Tuple[DeviceKind, ...] = ("Sampler", "Pump", "PlateRail", "ContactSensor")
# The controller acts on these worklist names: "Prime" makes the run stop when the client goes
# away, and a name containing "Test Tube" or "Vial" skips homing the shaker before a plate run.
PRIME_WORKLIST_NAME = "Prime"
_SHAKER_HOME_SUPPRESSORS = ("test tube", "vial")

_STATION_PUMPS: Dict[str, RinseStationPump] = {
  "Buffer": "S1",
  "Decon": "S2",
  "Clean": "S3",
  "Water": "S4",
  "Markers": "MB",
}


class _LaserOption(NamedTuple):
  """A set of lasers an instrument can acquire with.

  Attributes:
    configuration: The optical configuration that offers it.
    lasers: The lasers switched on.
    enabled: How the controller names the set.
    event_maximum: The most events the instrument records with it; it stops the run there.
  """

  configuration: OpticalConfiguration
  lasers: FrozenSet[Laser]
  enabled: EnabledLasers
  event_maximum: int


def _laser_set(*lasers: Laser) -> FrozenSet[Laser]:
  """Return lasers as a set."""
  return frozenset(lasers)


_LASER_OPTIONS: Tuple[_LaserOption, ...] = (
  _LaserOption("VBR", _laser_set("violet", "blue", "red"), "VioletBlueRed", 17_500_000),
  _LaserOption("VBR", _laser_set("blue", "red"), "BlueRed", 28_000_000),
  _LaserOption("VBR", _laser_set("violet", "blue"), "VioletBlue", 20_000_000),
  _LaserOption("VBR", _laser_set("blue"), "Blue", 35_000_000),
  _LaserOption("VYB", _laser_set("violet", "yellow", "blue"), "VioletYellowBlue", 17_500_000),
  _LaserOption("VYB", _laser_set("yellow", "blue"), "YellowBlue", 28_000_000),
  _LaserOption("VYB", _laser_set("violet", "yellow"), "VioletYellow", 21_000_000),
  _LaserOption("VYB", _laser_set("yellow"), "Yellow", 40_000_000),
  _LaserOption("BR", _laser_set("blue", "red"), "BlueRed", 30_000_000),
  _LaserOption("BR", _laser_set("blue"), "Blue", 40_000_000),
)

# ---------------------------------------------------------------------------------------------
# Duration estimates, in seconds. The controller only adds them up for its notes; they are for
# progress display and for the expected acquisition time that sets the high-rate warning.
# ---------------------------------------------------------------------------------------------

_START_FLUIDICS_ESTIMATE = decimal.Decimal(8)
_STOP_FLUIDICS_ESTIMATE = decimal.Decimal(5)
_DECK_CHECK_ESTIMATE = decimal.Decimal("6.35")
_PUMP_ESTIMATE = decimal.Decimal(1)
_START_SHAKER_ESTIMATE = decimal.Decimal(1)
_STOP_SHAKER_ESTIMATE = decimal.Decimal(2)
_ACQUIRE_ESTIMATE = decimal.Decimal(1)
_STOP_ACQUIRE_ESTIMATE = decimal.Decimal(1)
_SHUTDOWN_ESTIMATE = decimal.Decimal(1)
# Added to a timed rinse for moving to the station, and to a shake for spinning up and down.
_RINSE_PADDING = decimal.Decimal("1.8")
_SHAKE_PADDING = decimal.Decimal("3.8")
_CYCLING_RINSE_PADDING = decimal.Decimal(1)
# Added to a continuous rinse whose move to the station is slow.
_SLOW_MOVE_PADDING = decimal.Decimal(3)
# Moving to a station, lowering and raising for a continuous draw.
_CONTINUOUS_DRAW_PADDING = decimal.Decimal(4)
_TUBE_SAMPLE_PADDING = decimal.Decimal("1.4")
# Moving to a well: short when neither X nor Y changes by this many mm or more, else long; a
# probe that does not wait up after the dip needs longer.
_LONG_MOVE_DISTANCE = decimal.Decimal(50)
_SHORT_MOVE = decimal.Decimal("0.63")
_LONG_MOVE = decimal.Decimal("1.0")
_SHORT_MOVE_WITHOUT_UP_TIME = decimal.Decimal("0.9")
_LONG_MOVE_WITHOUT_UP_TIME = decimal.Decimal("1.2")
# How long the cytometer's own maintenance routines take, as the instrument states them.
_CLEAN_ESTIMATES: Dict[CleanMethod, decimal.Decimal] = {
  "QuickClean": decimal.Decimal(660),
  "LongClean": decimal.Decimal(1800),
  "Unclog": decimal.Decimal(630),
  "Priming": decimal.Decimal(780),
}

# Well types that are not sampled wells: an empty position, and a well of rinse fluid.
_NOT_SAMPLED_WELL_TYPES = ("Empty", "Rinse")
_SAMPLE_WELL_TYPE = "Sample"


class IncompleteWorklistError(Exception):
  """A worklist did not run to its end."""


# ---------------------------------------------------------------------------------------------
# Unit conversion
# ---------------------------------------------------------------------------------------------


def _exact(value: float, what: str) -> decimal.Decimal:
  """Return a number as the decimal it prints as, so 0.3 is exactly 0.3.

  Raises:
    ValueError: If the value is not a finite int or float.
  """
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    raise ValueError(f"{what} must be a number, not {value!r}")
  if isinstance(value, int):
    return decimal.Decimal(value)
  if not math.isfinite(value):
    raise ValueError(f"{what} must be finite, not {value!r}")
  return decimal.Decimal(repr(value))


def _scaled(value: float, scale: decimal.Decimal, what: str, unit: str) -> int:
  """Return a number times a scale, which must come out whole.

  Raises:
    ValueError: If the value is not a number or the product is not whole.
  """
  product = _exact(value, what) * scale
  if product != product.to_integral_value():
    raise ValueError(f"{what} ({value!r}) is not a whole number of {unit}")
  return int(product)


def _milliseconds(seconds: float, what: str) -> int:
  """Convert a time in s to the whole milliseconds the controller takes.

  Raises:
    ValueError: If the time is not a whole number of milliseconds.
  """
  return _scaled(seconds, _MILLISECONDS_PER_SECOND, what, "milliseconds")


def _whole_seconds(seconds: float, what: str) -> int:
  """Convert a time in s to the whole seconds the controller takes.

  Raises:
    ValueError: If the time is not a whole number of seconds.
  """
  return _scaled(seconds, decimal.Decimal(1), what, "seconds")


def _pump_speed(rpm: float, what: str) -> int:
  """Convert a pump speed in rpm to the hundredths of an rpm the controller takes.

  Raises:
    ValueError: If the speed is not a whole number of hundredths of an rpm.
  """
  return _scaled(rpm, _PUMP_SPEED_SCALE, what, "hundredths of an rpm")


_AXIS_RANGES: Dict[str, Tuple[float, float]] = {
  "X": SAMPLER_X_RANGE,
  "Y": SAMPLER_Y_RANGE,
  "Z": SAMPLER_Z_RANGE,
}


def _position(mm: float, axis: Literal["X", "Y", "Z"], what: str) -> decimal.Decimal:
  """Convert a sampler coordinate in mm to the decimal the controller takes.

  Raises:
    ValueError: If the coordinate lies outside the sampler's travel in that axis.
  """
  low, high = _AXIS_RANGES[axis]
  if not low <= mm <= high:
    raise ValueError(f"{what}: {axis} {mm} mm is outside the sampler's {low} to {high} mm")
  return _exact(mm, what).quantize(POSITION_RESOLUTION, rounding=decimal.ROUND_HALF_EVEN)


def _millimetres(mm: float, what: str) -> decimal.Decimal:
  """Convert a positive length in mm, such as a tolerance, to a decimal.

  Raises:
    ValueError: If the length is not positive.
  """
  if _exact(mm, what) <= 0:
    raise ValueError(f"{what} must be positive, not {mm}")
  return _exact(mm, what).quantize(POSITION_RESOLUTION, rounding=decimal.ROUND_HALF_EVEN)


def _timedelta(seconds: decimal.Decimal) -> datetime.timedelta:
  """Convert a duration in s to a timedelta, to the microsecond."""
  return datetime.timedelta(microseconds=int((seconds * 1_000_000).to_integral_value()))


def _seconds_decimal(milliseconds: int) -> decimal.Decimal:
  """Convert whole milliseconds to seconds."""
  return decimal.Decimal(milliseconds) / _MILLISECONDS_PER_SECOND


# ---------------------------------------------------------------------------------------------
# Pump timing
# ---------------------------------------------------------------------------------------------


def _line_flush_time(pump_speed: float) -> int:
  """Return how many seconds the pump needs to clear the sample line at a speed.

  The instrument's model is ceil(1200 * rpm^-0.997) + 2 s, with the speed in whole rpm (rounded
  down): 44 s at 29 rpm, 25 s at 53 rpm.
  """
  rpm = math.floor(pump_speed)
  return math.ceil(math.pow(rpm, -0.997) * 1200.0) + 2


def transit_delay(pump_speed: float) -> int:
  """Return how many seconds a sample takes from the probe to the detector at a speed.

  The instrument's model is ceil(1100 * rpm^-0.98) - 2 s, with the speed in whole rpm (rounded
  down): 39 s at 29 rpm.
  """
  rpm = math.floor(pump_speed)
  return math.ceil(math.pow(rpm, -0.98) * 1100.0) - 2


def _minimum_flush_and_clean(pump_speed: float) -> int:
  """Return how long the flush and the post-plate clean must last together, in seconds.

  It is the time to clear the line, rounded up to 30, 60, 90, 120 or 150 s; a longer time stays.
  """
  needed = _line_flush_time(pump_speed)
  for step in (30, 60, 90, 120, 150):
    if needed <= step:
      return step
  return needed


def _tube_sip_limit(pump_speed: float) -> decimal.Decimal:
  """Return the longest sip from a tube at a pump speed, in seconds.

  It is the time to draw 320 uL at the pump's flow, rounded to 10 s (halves to even), at least
  10 s: 180 s at 29 rpm.
  """
  flow = _exact(pump_speed, "pump speed") / PUMP_RPM_PER_UL_PER_MINUTE
  seconds = TUBE_MAX_DRAW / flow * 60
  rounded = (seconds / 10).quantize(decimal.Decimal(1), rounding=decimal.ROUND_HALF_EVEN) * 10
  return max(rounded, decimal.Decimal(10))


# ---------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------


def _check_range(value: float, bounds: Tuple[float, float], what: str, unit: str) -> None:
  """Check that a number lies within bounds, both included.

  Raises:
    ValueError: If it is not a number or lies outside.
  """
  _exact(value, what)
  low, high = bounds
  if not low <= value <= high:
    raise ValueError(f"{what} must be from {low:g} to {high:g} {unit}, not {value!r}")


def _check_integer(value: int, low: int, high: Optional[int], what: str) -> None:
  """Check that a value is an int within bounds, both included; ``high`` None is unbounded.

  Raises:
    ValueError: If it is not an int or lies outside.
  """
  if isinstance(value, bool) or not isinstance(value, int):
    raise ValueError(f"{what} must be an integer, not {value!r}")
  if value < low or (high is not None and value > high):
    upper = "" if high is None else f" to {high}"
    raise ValueError(f"{what} must be from {low}{upper}, not {value}")


def _check_station(station: str, what: str) -> None:
  """Check that a station is one the probe can rinse, shake or clean in.

  Raises:
    ValueError: If it is not Buffer, Decon, Clean or Water.
  """
  if station not in WASH_STATIONS:
    raise ValueError(f"{what} must be one of {', '.join(WASH_STATIONS)}, not {station!r}")


@dataclass(frozen=True)
class TriggerThreshold:
  """An event trigger: a pulse must exceed this height on one channel to count as an event.

  Attributes:
    parameter: The pulse-height parameter, such as ``FSC-H`` or ``BL1-H``.
    value: The height in instrument units, 1,000 to 16,700,000.
  """

  parameter: str
  value: int

  def __post_init__(self) -> None:
    """Check the parameter name and the value.

    Raises:
      ValueError: If the parameter is not a pulse height or the value is out of range.
    """
    if not isinstance(self.parameter, str) or not self.parameter.endswith("-H"):
      raise ValueError(f"a threshold applies to a pulse height such as FSC-H, not {self.parameter}")
    _check_integer(
      self.value, THRESHOLD_RANGE[0], THRESHOLD_RANGE[1], f"{self.parameter} threshold"
    )


@dataclass(frozen=True)
class RinseStep:
  """A time with the probe dipping repeatedly in one station.

  Attributes:
    station: The station.
    duration: How long, in seconds.
  """

  station: WashStation
  duration: float

  def __post_init__(self) -> None:
    """Check the station and that the duration is a number.

    Raises:
      ValueError: If the station is not one the probe rinses in, or the duration not a number.
    """
    _check_station(self.station, "a rinse step's station")
    _exact(self.duration, "a rinse step's duration")


DEFAULT_THRESHOLDS: Tuple[TriggerThreshold, ...] = (TriggerThreshold("FSC-H", 100_000),)
DEFAULT_CLEANS: Tuple[RinseStep, ...] = (
  RinseStep("Decon", 30.0),
  RinseStep("Clean", 30.0),
  RinseStep("Water", 60.0),
)


def _check_steps(
  steps: Tuple[RinseStep, ...], bounds: Tuple[float, float], to_wire_unit: bool, what: str
) -> None:
  """Check a list of rinse steps and the duration of each.

  Args:
    steps: The steps.
    bounds: The allowed duration of one step, in s.
    to_wire_unit: True when the controller takes the duration in ms, False in whole seconds.
    what: What the steps are, for messages.

  Raises:
    ValueError: If the steps are not a tuple of steps, or a duration is out of range or does not
      convert.
  """
  if not isinstance(steps, tuple) or not all(isinstance(step, RinseStep) for step in steps):
    raise ValueError(f"{what} must be a tuple of RinseStep")
  for step in steps:
    label = f"{what}: {step.station}"
    _check_range(step.duration, bounds, label, "s")
    if to_wire_unit:
      _milliseconds(step.duration, label)
    else:
      _whole_seconds(step.duration, label)


@dataclass(frozen=True, kw_only=True)
class AcquisitionSettings:
  """What the cytometer records: triggers, lasers and event limits.

  Attributes:
    thresholds: One or two triggers. The first must be on forward or side scatter (``FSC-H``,
      ``SSC-H``); the second may be on any other parameter whose laser is on.
    lasers: The lasers to acquire with, such as ``("blue", "red")``; None for all the
      instrument has. Instruments support only some combinations.
    event_limit: Record at most this many events, then keep running without recording; None
      for no limit. At most 40,000,000, and no more than the event maximum.
    event_maximum: Stop the run when this many events are recorded; None for the most the
      instrument can record with its lasers (17,500,000 to 40,000,000).
  """

  thresholds: Tuple[TriggerThreshold, ...] = DEFAULT_THRESHOLDS
  lasers: Optional[Tuple[Laser, ...]] = None
  event_limit: Optional[int] = None
  event_maximum: Optional[int] = None

  def __post_init__(self) -> None:
    """Check the triggers, lasers and limits on their own.

    Whether the instrument has the parameters and lasers is checked when a worklist is built.

    Raises:
      ValueError: If a value is out of range or inconsistent.
    """
    thresholds = self.thresholds
    if not isinstance(thresholds, tuple) or not all(
      isinstance(threshold, TriggerThreshold) for threshold in thresholds
    ):
      raise ValueError("thresholds must be a tuple of TriggerThreshold")
    if not 1 <= len(thresholds) <= MAX_THRESHOLDS:
      raise ValueError(f"a read takes one or two thresholds, not {len(thresholds)}")
    if thresholds[0].parameter not in PRIMARY_THRESHOLD_PARAMETERS:
      raise ValueError(
        f"the first threshold must be on FSC-H or SSC-H, not {thresholds[0].parameter}"
      )
    if len({threshold.parameter for threshold in thresholds}) != len(thresholds):
      raise ValueError("the two thresholds must be on different parameters")
    if self.lasers is not None:
      if not isinstance(self.lasers, tuple) or not self.lasers:
        raise ValueError("lasers must be None or a non-empty tuple")
      for laser in self.lasers:
        if laser not in LASERS:
          raise ValueError(f"{laser!r} is not a laser; lasers are {', '.join(LASERS)}")
      if len(set(self.lasers)) != len(self.lasers):
        raise ValueError(f"lasers {self.lasers} name a laser twice")
    if self.event_limit is not None:
      _check_integer(self.event_limit, 1, MAX_EVENT_LIMIT, "event_limit")
    if self.event_maximum is not None:
      _check_integer(self.event_maximum, 1, None, "event_maximum")


@dataclass(frozen=True, kw_only=True)
class PlateReadSettings(AcquisitionSettings):
  """How a plate is read. Times are in seconds, speeds in rpm, heights in mm.

  It has the attributes of :class:`AcquisitionSettings` and the following. Which wells are read,
  and in what order, is not a setting: :func:`plate_read_worklist` takes them as a list, and
  :func:`well_order` gives the standard orders.

  Attributes:
    sip_time: How long the probe stays down in each well, 0.5 to 59.9 s.
    up_time: How long it then waits up, drawing the air gap that separates wells, 0 to 59.9 s.
      In stations it waits at least 0.5 s.
    pump_speed: Pump speed while sampling, 10 to 32 rpm; 19 rpm is the instrument's "slow".
    sampling_height: How far above the mapped well bottom the probe samples; None for the
      default of the plate format (0.5 mm for 96 and 1536 wells, 0.2 mm for 384).
    pre_plate_prime: Whether to prime the line in Buffer before the first well.
    pre_plate_prime_duration: How long, 5 to 600 s in whole seconds.
    pre_plate_shake: Whether to shake the plate before the first well, the probe dipping in
      Buffer meanwhile.
    pre_plate_shake_duration: How long, 1 to 180 s in whole seconds.
    pre_plate_shake_speed: Shaker speed for it; None for ``shake_speed``.
    shake_mode: ``"after_wells"`` shakes the plate every ``shake_every`` wells, the probe dipping
      in ``shake_station`` meanwhile; ``"continuous"`` (96-well plates only) shakes it throughout
      the read; ``"none"`` does not shake.
    shake_every: Shake after every this many sampled wells, 1 to the wells of the plate.
    shake_duration: How long each shake lasts, 1 to 180 s in whole seconds.
    shake_speed: Shaker speed, 200 rpm to the shaker's maximum (3000 rpm, 5000 rpm on the HD
      model); None for the instrument's default for the plate format.
    shake_station: The station the probe dips in during the shakes between wells.
    probe_rinse: Whether to rinse the probe every ``rinse_every`` sampled wells.
    rinse_every: Rinse after every this many sampled wells, 1 to the wells of the plate. When a
      shake and a rinse fall after the same well, the shake comes first.
    rinses: The rinse steps, in order, each 0.5 to 60 s.
    flush_duration: How long to flush the line in Buffer after the last well, 30 to 600 s in
      whole seconds. Flush and post-plate clean together must clear the line at the pump speed
      (in whole rpm): at least 60 s at 21 to 32 rpm, 90 s at 14 to 20, 120 s at 11 to 13 and
      150 s at 10.
    post_plate_clean: Whether to clean the probe after the flush.
    cleans: The clean steps, in order, each 5 to 600 s in whole seconds.
  """

  sip_time: float = 1.0
  up_time: float = 0.5
  pump_speed: float = 29.0
  sampling_height: Optional[float] = None
  pre_plate_prime: bool = False
  pre_plate_prime_duration: float = 60.0
  pre_plate_shake: bool = False
  pre_plate_shake_duration: float = 15.0
  pre_plate_shake_speed: Optional[int] = None
  shake_mode: ShakeMode = "after_wells"
  shake_every: int = 12
  shake_duration: float = 4.0
  shake_speed: Optional[int] = None
  shake_station: WashStation = "Buffer"
  probe_rinse: bool = False
  rinse_every: int = 12
  rinses: Tuple[RinseStep, ...] = ()
  flush_duration: float = 30.0
  post_plate_clean: bool = True
  cleans: Tuple[RinseStep, ...] = DEFAULT_CLEANS

  def __post_init__(self) -> None:
    """Check every setting against its range and the settings against each other.

    Raises:
      ValueError: If a value is out of range, does not convert to the controller's units, or
        the settings contradict each other.
    """
    super().__post_init__()
    _check_range(self.sip_time, SIP_TIME_RANGE, "sip_time", "s")
    _milliseconds(self.sip_time, "sip_time")
    _check_range(self.up_time, UP_TIME_RANGE, "up_time", "s")
    _milliseconds(self.up_time, "up_time")
    _check_range(self.pump_speed, PUMP_SPEED_RANGE, "pump_speed", "rpm")
    _pump_speed(self.pump_speed, "pump_speed")
    if self.sampling_height is not None and _exact(self.sampling_height, "sampling_height") < 0:
      raise ValueError(f"sampling_height must not be negative, not {self.sampling_height}")
    _check_range(
      self.pre_plate_prime_duration, PRIME_DURATION_RANGE, "pre_plate_prime_duration", "s"
    )
    _whole_seconds(self.pre_plate_prime_duration, "pre_plate_prime_duration")
    _check_range(
      self.pre_plate_shake_duration,
      PRE_PLATE_SHAKE_DURATION_RANGE,
      "pre_plate_shake_duration",
      "s",
    )
    _whole_seconds(self.pre_plate_shake_duration, "pre_plate_shake_duration")
    lowest, highest = SHAKER_SPEED_RANGES["iQue3HD"]
    if self.pre_plate_shake_speed is not None:
      _check_integer(self.pre_plate_shake_speed, lowest, highest, "pre_plate_shake_speed")
    if self.shake_mode not in SHAKE_MODES:
      raise ValueError(
        f"shake_mode must be one of {', '.join(SHAKE_MODES)}, not {self.shake_mode!r}"
      )
    _check_integer(self.shake_every, 1, None, "shake_every")
    _check_range(self.shake_duration, SHAKE_DURATION_RANGE, "shake_duration", "s")
    _whole_seconds(self.shake_duration, "shake_duration")
    if self.shake_speed is not None:
      _check_integer(self.shake_speed, lowest, highest, "shake_speed")
    _check_station(self.shake_station, "shake_station")
    _check_integer(self.rinse_every, 1, None, "rinse_every")
    _check_steps(self.rinses, RINSE_DURATION_RANGE, True, "rinses")
    if self.probe_rinse and not self.rinses:
      raise ValueError("probe_rinse is on but rinses is empty")
    _check_range(self.flush_duration, FLUSH_DURATION_RANGE, "flush_duration", "s")
    _whole_seconds(self.flush_duration, "flush_duration")
    _check_steps(self.cleans, CLEAN_DURATION_RANGE, False, "cleans")
    if self.post_plate_clean and not self.cleans:
      raise ValueError("post_plate_clean is on but cleans is empty")
    cleaning = sum(step.duration for step in self.cleans) if self.post_plate_clean else 0.0
    needed = _minimum_flush_and_clean(self.pump_speed)
    if self.flush_duration < max(FLUSH_DURATION_RANGE[0], needed - cleaning):
      raise ValueError(
        f"at {self.pump_speed:g} rpm the flush and the post-plate clean must last {needed} s "
        f"together to clear the line; flush_duration {self.flush_duration:g} s and "
        f"{cleaning:g} s of cleaning are not enough"
      )


@dataclass(frozen=True, kw_only=True)
class TubeReadSettings(AcquisitionSettings):
  """How a 1.5 mL microcentrifuge tube in the tube holder is read. Times in s, speeds in rpm.

  It has the attributes of :class:`AcquisitionSettings` and the following.

  Attributes:
    sip_time: How long the probe draws from the tube: 5 s up to the time the pump takes to draw
      320 uL (180 s at 29 rpm), in whole milliseconds.
    pump_speed: Pump speed while sampling, 10 to 32 rpm.
    station: The station the line is primed and flushed from.
    post_tube_clean: Whether to clean the probe after the flush.
    cleans: The clean steps, in order, each 5 to 600 s in whole seconds.
  """

  sip_time: float = 10.0
  pump_speed: float = 29.0
  station: WashStation = "Buffer"
  post_tube_clean: bool = True
  cleans: Tuple[RinseStep, ...] = DEFAULT_CLEANS

  def __post_init__(self) -> None:
    """Check every setting against its range.

    Raises:
      ValueError: If a value is out of range or does not convert to the controller's units.
    """
    super().__post_init__()
    _check_range(self.pump_speed, PUMP_SPEED_RANGE, "pump_speed", "rpm")
    _pump_speed(self.pump_speed, "pump_speed")
    limit = float(_tube_sip_limit(self.pump_speed))
    _check_range(self.sip_time, (TUBE_MIN_SIP_TIME, limit), "sip_time", "s")
    _milliseconds(self.sip_time, "sip_time")
    _check_station(self.station, "station")
    _check_steps(self.cleans, CLEAN_DURATION_RANGE, False, "cleans")
    if self.post_tube_clean and not self.cleans:
      raise ValueError("post_tube_clean is on but cleans is empty")


# ---------------------------------------------------------------------------------------------
# What the instrument has
# ---------------------------------------------------------------------------------------------


def _system_kind(calibration: Calibration) -> str:
  """Return the instrument's system kind, which must be a member of the PLUS family.

  Raises:
    ValueError: If the calibration is for another instrument.
  """
  kind = calibration.instrument.system_kind
  if kind not in SYSTEM_KIND_CONFIGURATIONS:
    raise ValueError(
      f"worklists are built for the iQue Screener PLUS family "
      f"({', '.join(SYSTEM_KIND_CONFIGURATIONS)}), not for system kind {kind!r}"
    )
  return kind


def _laser_option(calibration: Calibration, lasers: Optional[Tuple[Laser, ...]]) -> _LaserOption:
  """Return the laser set to acquire with: the one asked for, or all the instrument has.

  Raises:
    ValueError: If the instrument cannot acquire with that combination of lasers.
  """
  configuration = SYSTEM_KIND_CONFIGURATIONS[_system_kind(calibration)]
  wanted = frozenset(PARAMETER_TABLES[configuration].lasers if lasers is None else lasers)
  supported = [option for option in _LASER_OPTIONS if option.configuration == configuration]
  for option in supported:
    if option.lasers == wanted:
      return option
  offered = "; ".join(", ".join(sorted(option.lasers)) for option in supported)
  raise ValueError(
    f"the {configuration} instrument cannot acquire with lasers {', '.join(sorted(wanted))}; "
    f"it offers: {offered}"
  )


def _wire_thresholds(
  table: ParameterTable, option: _LaserOption, thresholds: Sequence[TriggerThreshold]
) -> List[Optional[Threshold]]:
  """Return the triggers as the controller takes them: the index of the channel and the value.

  A channel's index is its place among the pulse heights the instrument reports, FSC-H first.

  Raises:
    ValueError: If the instrument has no such parameter or its laser is off.
  """
  heights: List[Parameter] = [p for p in table.parameters if p.measurement == "height"]
  names = [parameter.name for parameter in heights]
  wire: List[Optional[Threshold]] = []
  for threshold in thresholds:
    if threshold.parameter not in names:
      raise ValueError(
        f"the {table.configuration} instrument has no parameter {threshold.parameter}; "
        f"thresholds can be on {', '.join(names)}"
      )
    index = names.index(threshold.parameter)
    laser = heights[index].laser
    if laser is not None and laser not in option.lasers:
      raise ValueError(f"a threshold on {threshold.parameter} needs the {laser} laser on")
    wire.append(Threshold(id=index, maximum=0, minimum=threshold.value))
  return wire


class _Acquisition(NamedTuple):
  """What the start-fluidics and acquire commands carry.

  Attributes:
    lasers: The lasers, as the controller names the set.
    thresholds: The triggers.
    event_threshold: The event limit; NO_EVENT_LIMIT for none.
    event_maximum: The event count that stops the run.
  """

  lasers: EnabledLasers
  thresholds: List[Optional[Threshold]]
  event_threshold: int
  event_maximum: int


def _acquisition(calibration: Calibration, settings: AcquisitionSettings) -> _Acquisition:
  """Resolve the acquisition settings against the instrument.

  Raises:
    ValueError: If the instrument lacks a laser or parameter, or a limit exceeds what it records.
  """
  option = _laser_option(calibration, settings.lasers)
  table = PARAMETER_TABLES[option.configuration]
  thresholds = _wire_thresholds(table, option, settings.thresholds)
  maximum = option.event_maximum
  if settings.event_maximum is not None:
    if settings.event_maximum > option.event_maximum:
      raise ValueError(
        f"event_maximum {settings.event_maximum} exceeds the {option.event_maximum} events the "
        f"instrument records with these lasers"
      )
    maximum = settings.event_maximum
  limit = NO_EVENT_LIMIT
  if settings.event_limit is not None:
    if settings.event_limit > maximum:
      raise ValueError(f"event_limit {settings.event_limit} exceeds the event maximum {maximum}")
    limit = settings.event_limit
  return _Acquisition(option.enabled, thresholds, limit, maximum)


def _all_lasers(calibration: Calibration) -> EnabledLasers:
  """Return the set of all lasers of the instrument, as the controller names it."""
  return _laser_option(calibration, None).enabled


def _metadata(pairs: Sequence[Tuple[str, str]]) -> List[Tuple[Optional[str], Optional[str]]]:
  """Check FCS metadata: key and value text, keys unique and not empty.

  Raises:
    ValueError: If an entry is not a pair of strings, a key is empty, or a key repeats.
  """
  checked: List[Tuple[Optional[str], Optional[str]]] = []
  seen: Set[str] = set()
  for pair in pairs:
    if not (isinstance(pair, tuple) and len(pair) == 2):
      raise ValueError(f"metadata entries are (key, value) pairs, not {pair!r}")
    key, value = pair
    if not isinstance(key, str) or not isinstance(value, str) or not key:
      raise ValueError(f"a metadata entry needs a key and a value text, not {pair!r}")
    if key in seen:
      raise ValueError(f"metadata key {key!r} appears twice")
    seen.add(key)
    checked.append((key, value))
  return checked


def _pump_direction(calibration: Calibration) -> PumpDirection:
  """Return the pump's direction: clockwise when it sits on the right."""
  return "CW" if calibration.instrument.pump_location == "Right" else "CCW"


def _shake_speed(calibration: Calibration, plate_format: PlateFormat, speed: Optional[int]) -> int:
  """Return a shaker speed the instrument's shaker can do: the one asked for, or its default.

  Raises:
    ValueError: If the speed is beyond the shaker's range.
  """
  kind = _system_kind(calibration)
  if speed is None:
    return DEFAULT_SHAKE_SPEEDS[kind][plate_format]
  low, high = SHAKER_SPEED_RANGES[kind]
  if not low <= speed <= high:
    raise ValueError(f"the shaker of system kind {kind} runs at {low} to {high} rpm, not {speed}")
  return speed


class _Spot(NamedTuple):
  """Where the probe goes at a station: XY, and the Z it dips to.

  Attributes:
    name: The station name.
    x: X in mm.
    y: Y in mm.
    z: The Z the probe goes down to, in mm.
  """

  name: str
  x: decimal.Decimal
  y: decimal.Decimal
  z: decimal.Decimal


def _station(calibration: Calibration, name: str, draw: bool = False) -> _Spot:
  """Return where the probe dips at a station, or draws continuously from it when ``draw``.

  Raises:
    CalibrationError: If the calibration lacks the station, or it or the deck height is not
      calibrated.
    ValueError: If the position is outside the sampler's travel.
  """
  xy = station_xy(calibration, name)
  z = station_draw_z(calibration, name) if draw else station_dip_z(calibration, name)
  what = f"station {name}"
  return _Spot(
    name, _position(xy.x, "X", what), _position(xy.y, "Y", what), _position(z, "Z", what)
  )


def _travel(calibration: Calibration) -> decimal.Decimal:
  """Return the travel height as the controller takes it.

  Raises:
    CalibrationError: If the travel height is not set.
  """
  return _position(travel_height(calibration), "Z", "travel height")


def _check_dip(spot: _Spot, travel: decimal.Decimal) -> None:
  """Check that a dip goes down from the travel height.

  Raises:
    ValueError: If the dip height is above the travel height.
  """
  if spot.z > travel:
    raise ValueError(f"{spot.name}: the dip height {spot.z} mm is above the travel height {travel}")


# ---------------------------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------------------------


class _CommandFields(TypedDict):
  """The members every command has: the client's labels and estimate, the controller's results."""

  category: CommandCategory
  completed: bool
  countdown_duration: int
  description: Optional[str]
  end_time: datetime.datetime
  estimated_duration: datetime.timedelta
  execution_notes: Optional[str]
  id: uuid.UUID
  name: Optional[str]
  start_time: datetime.datetime


def _fields(
  category: CommandCategory, description: str, estimate: decimal.Decimal, countdown: int = 0
) -> _CommandFields:
  """Return the common members of a new command, with a fresh identifier.

  Args:
    category: The display group.
    description: Text for people; nothing reads it.
    estimate: Expected duration in s.
    countdown: Seconds to count down while it runs, for display; 0 for none.
  """
  return _CommandFields(
    category=category,
    completed=False,
    countdown_duration=countdown,
    description=description,
    end_time=NOT_RUN,
    estimated_duration=_timedelta(estimate),
    execution_notes=None,
    id=uuid.uuid4(),
    name=None,
    start_time=NOT_RUN,
  )


def _start_plate_fluidics(
  acquisition: _Acquisition, metadata: List[Tuple[Optional[str], Optional[str]]], user_name: str
) -> StartMatryoshkaFluidicsCommand:
  """Load the plate and start the fluidics with lasers and triggers for acquisition."""
  return StartMatryoshkaFluidicsCommand(
    **_fields("FlowCytometer", "Load plate and start fluidics", _START_FLUIDICS_ESTIMATE),
    enabled_lasers=acquisition.lasers,
    fcs_metadata=metadata,
    laser_configuration="Normal",
    speed="Medium",
    thresholds=acquisition.thresholds,
    user_name=user_name,
  )


def _start_fluidics(
  lasers: EnabledLasers,
  thresholds: List[Optional[Threshold]],
  metadata: List[Tuple[Optional[str], Optional[str]]],
  user_name: str,
  for_acquisition: bool,
) -> StartFlowCytometerFluidicsCommand:
  """Start the fluidics, with the lasers on and ready to record when ``for_acquisition``."""
  description = "Start fluidics for acquisition" if for_acquisition else "Start fluidics"
  return StartFlowCytometerFluidicsCommand(
    **_fields("FlowCytometer", description, _START_FLUIDICS_ESTIMATE),
    enabled_lasers=lasers,
    fcs_metadata=metadata,
    laser_configuration="Normal",
    prepare_for_acquisition=for_acquisition,
    speed="Medium",
    thresholds=thresholds,
    user_name=user_name,
  )


def _maintenance_fluidics(calibration: Calibration) -> StartFlowCytometerFluidicsCommand:
  """Start the fluidics without acquisition: no triggers, no metadata, all lasers."""
  return _start_fluidics(_all_lasers(calibration), [], [], UNKNOWN_USER, False)


def _stop_fluidics() -> StopFlowCytometerFluidicsCommand:
  """Stop the fluidics; this also ends acquisition and closes the controller's FCS file."""
  return StopFlowCytometerFluidicsCommand(
    **_fields("FlowCytometer", "Stop fluidics", _STOP_FLUIDICS_ESTIMATE)
  )


def _check_deck(
  calibration: Calibration, travel: decimal.Decimal, tolerance: float
) -> Tuple[ValidateDeckHeightCommand, Tuple[decimal.Decimal, decimal.Decimal]]:
  """Load the plate rail and touch the deck-height test point; fail unless at the deck height.

  Returns:
    The command and the XY it leaves the probe at.

  Raises:
    CalibrationError: If the deck height or its test point is not calibrated.
  """
  point = deck_height_test_point(calibration)
  x = _position(point.x, "X", "deck-height test point")
  y = _position(point.y, "Y", "deck-height test point")
  deck = _position(calibration.instrument.deck_height, "Z", "deck height")
  command = ValidateDeckHeightCommand(
    **_fields("Info", "Check the deck height", _DECK_CHECK_ESTIMATE),
    is_microfuge_tube_check=False,
    x=x,
    y=y,
    z_high=travel,
    z_low=deck,
    z_tolerance=_millimetres(tolerance, "deck height tolerance"),
  )
  return command, (x, y)


def _start_pump(speed: int, direction: PumpDirection, description: str) -> StartPumpCommand:
  """Start the pump, or change its speed when it runs."""
  return StartPumpCommand(
    **_fields("Pump", description, _PUMP_ESTIMATE), direction=direction, speed=speed
  )


def _start_pump_at(rpm: float, direction: PumpDirection) -> StartPumpCommand:
  """Start the pump at a speed in rpm."""
  return _start_pump(_pump_speed(rpm, "pump speed"), direction, f"Start pump at {rpm:g} rpm")


def _stop_pump() -> StopPumpCommand:
  """Stop the pump."""
  return StopPumpCommand(**_fields("Pump", "Stop pump", _PUMP_ESTIMATE))


def _timed_rinse(
  spot: _Spot,
  travel: decimal.Decimal,
  duration: int,
  sip_time: int,
  up_time: int,
  fast: bool,
  description: str,
  kind: Type[TimedCyclingRinseCommand] = TimedCyclingRinseCommand,
) -> TimedCyclingRinseCommand:
  """Dip the probe repeatedly in a station for a time.

  Args:
    spot: The station.
    travel: The travel height.
    duration: Total time in whole seconds.
    sip_time: Time down per dip, in ms.
    up_time: Time up per dip, in ms.
    fast: Whether the move to the station is fast.
    description: Text for people.
    kind: The command class; the flush class also ejects the plate meanwhile.
  """
  _check_dip(spot, travel)
  return kind(
    **_fields("Rinse", description, decimal.Decimal(duration) + _RINSE_PADDING, duration),
    fast_move=fast,
    rinse_description=description,
    rinse_duration=duration,
    rinse_station_name=spot.name,
    rinse_station_pump=_STATION_PUMPS[spot.name],
    sip_time=sip_time,
    up_time=up_time,
    x=spot.x,
    y=spot.y,
    z_down=spot.z,
    z_up=travel,
  )


def _shake(
  spot: _Spot,
  travel: decimal.Decimal,
  duration: int,
  rpm: int,
  sip_time: int,
  up_time: int,
) -> CombinedShakeRinseAtLocationCommand:
  """Shake the plate for a time while the probe dips repeatedly in a station.

  The shake time starts once the shaker is at speed; the command ends when it has stopped.
  """
  _check_dip(spot, travel)
  description = f"Shake {duration} s at {rpm} rpm, probe in {spot.name}"
  return CombinedShakeRinseAtLocationCommand(
    **_fields("Shake", description, decimal.Decimal(duration) + _SHAKE_PADDING, duration),
    fast_move=True,
    rpm=rpm,
    rinse_station_pump=_STATION_PUMPS[spot.name],
    rinse_x=spot.x,
    rinse_y=spot.y,
    rinse_z_down=spot.z,
    rinse_z_up=travel,
    shake_duration=duration,
    sip_time=sip_time,
    up_time=up_time,
    location_name=spot.name,
  )


def _cycling_rinse(
  spot: _Spot, travel: decimal.Decimal, duration: int, sip_time: int, up_time: int
) -> CyclingRinseCommand:
  """Rinse the probe between wells: dip repeatedly in a station for a time in ms."""
  _check_dip(spot, travel)
  seconds = _seconds_decimal(duration)
  estimate = seconds + _seconds_decimal(up_time) + _CYCLING_RINSE_PADDING
  description = f"Rinse in {spot.name} for {seconds.normalize():f} s"
  return CyclingRinseCommand(
    **_fields("Rinse", description, estimate, duration // 1000),
    fast_move=True,
    sip_time=sip_time,
    up_time=up_time,
    x=spot.x,
    y=spot.y,
    z_down=spot.z,
    z_up=travel,
    operation="Rinse",
    rinse_duration=duration,
    rinse_station_name=spot.name,
    rinse_station_pump=_STATION_PUMPS[spot.name],
  )


def _acquire(
  acquisition: _Acquisition, delay: int, description: str
) -> FlowCytometerAcquireCommand:
  """Start recording events; the expected acquisition time is filled in later."""
  return FlowCytometerAcquireCommand(
    **_fields("FlowCytometer", description, _ACQUIRE_ESTIMATE),
    acquistion_delay=delay,
    estimated_acquisition_time=datetime.timedelta(0),
    event_maximum=acquisition.event_maximum,
    event_threshold=acquisition.event_threshold,
  )


def _with_acquisition_time(commands: List[WorklistCommand]) -> List[WorklistCommand]:
  """Fill in the expected acquisition time of the acquire command.

  It is the sum of the estimates from the acquire command up to the flush, or to the end of the
  worklist when there is no flush. The controller uses it with the event maximum to warn of an
  event rate that would reach the maximum before the end.
  """
  start = next(
    i for i, command in enumerate(commands) if type(command) is FlowCytometerAcquireCommand
  )
  total = datetime.timedelta(0)
  for command in commands[start:]:
    if isinstance(command, (FlushMatryoshkaCommand, FlushCommand)):
      break
    total += command.estimated_duration
  acquire = commands[start]
  if not isinstance(acquire, FlowCytometerAcquireCommand):
    raise TypeError(f"command {start} is a {type(acquire).__name__}")
  filled = dataclasses.replace(acquire, estimated_acquisition_time=total)
  return commands[:start] + [filled] + commands[start + 1 :]


def _move_padding(
  x: decimal.Decimal, y: decimal.Decimal, prior: Tuple[decimal.Decimal, decimal.Decimal], up: int
) -> decimal.Decimal:
  """Return the expected time to move to a well, by the distance from the previous position."""
  short = max(abs(x - prior[0]), abs(y - prior[1])) < _LONG_MOVE_DISTANCE
  if up == 0:
    return _SHORT_MOVE_WITHOUT_UP_TIME if short else _LONG_MOVE_WITHOUT_UP_TIME
  return _SHORT_MOVE if short else _LONG_MOVE


def _sample_well(
  row: int,
  column: int,
  x: decimal.Decimal,
  y: decimal.Decimal,
  z: decimal.Decimal,
  travel: decimal.Decimal,
  sip_time: int,
  up_time: int,
  prior: Tuple[decimal.Decimal, decimal.Decimal],
) -> SampleWellCommand:
  """Sample one well: move over it, dip for the sip time, wait up for the up time.

  The command names the well by its row letters and its column number counted from 1.
  """
  well = format_well_name(row, column)
  if z > travel:
    raise ValueError(f"well {well}: the sampling height {z} mm is above the travel height {travel}")
  letters = well.rstrip("0123456789")
  seconds = _seconds_decimal(sip_time)
  estimate = seconds + _seconds_decimal(up_time) + _move_padding(x, y, prior, up_time)
  return SampleWellCommand(
    **_fields("Sample", f"Sample {well} for {seconds.normalize():f} s", estimate),
    fast_move=True,
    sip_time=sip_time,
    up_time=up_time,
    x=x,
    y=y,
    z_down=z,
    z_up=travel,
    column=column + 1,
    row=letters,
    well_type=_SAMPLE_WELL_TYPE,
  )


def _timed_continuous_rinse(
  spot: _Spot, travel: decimal.Decimal, duration: int, speed: SampleSpeed, description: str
) -> TimedContinuousRinseCommand:
  """Keep the probe down in a station for a time while the pump runs."""
  _check_dip(spot, travel)
  estimate = decimal.Decimal(duration) + _RINSE_PADDING
  if speed == "Slow":
    estimate += _SLOW_MOVE_PADDING
  return TimedContinuousRinseCommand(
    **_fields("Rinse", description, estimate, duration),
    rinse_description=description,
    rinse_duration=duration,
    rinse_station_name=spot.name,
    rinse_station_pump=_STATION_PUMPS[spot.name],
    sample_speed=speed,
    up_time=0,
    x=spot.x,
    y=spot.y,
    z_down=spot.z,
    z_up=travel,
  )


def _draw_continuously(
  spot: _Spot,
  travel: decimal.Decimal,
  duration: int,
  pump_speed: int,
  direction: PumpDirection,
  description: str,
) -> SampleContinuouslyCommand:
  """Lower the probe in a station, run the pump at a speed for a time, stop it, raise the probe.

  The pump is left stopped.
  """
  _check_dip(spot, travel)
  estimate = _PUMP_ESTIMATE * 2 + _CONTINUOUS_DRAW_PADDING + _seconds_decimal(duration)
  return SampleContinuouslyCommand(
    **_fields("Rinse", description, estimate),
    fast_move=False,
    pump_direction=direction,
    pump_speed=pump_speed,
    sip_time=duration,
    x=spot.x,
    y=spot.y,
    z_high=travel,
    z_low=spot.z,
  )


def _clean_after_tube(
  calibration: Calibration, travel: decimal.Decimal, steps: Sequence[RinseStep]
) -> List[WorklistCommand]:
  """Return the timed rinses of a clean step list, with the slow moves of a tube read."""
  sip = _milliseconds(CLEAN_SIP_TIME, "clean sip time")
  up = _milliseconds(CLEAN_UP_TIME, "clean up time")
  commands: List[WorklistCommand] = []
  for step in steps:
    seconds = _whole_seconds(step.duration, "clean duration")
    spot = _station(calibration, step.station)
    description = f"Clean in {step.station} for {seconds} s"
    commands.append(_timed_rinse(spot, travel, seconds, sip, up, False, description))
  return commands


def _new_worklist(
  name: str,
  commands: List[WorklistCommand],
  calibration: Optional[Calibration],
  devices_to_monitor: Sequence[DeviceKind] = (),
  leave_fluidics_running: bool = False,
  experiment_id: Optional[uuid.UUID] = None,
  plate_id: Optional[uuid.UUID] = None,
) -> Worklist:
  """Return a worklist ready to run: the controller fills in every result member.

  Args:
    name: The name; see PRIME_WORKLIST_NAME for the names the controller acts on.
    commands: The commands in order.
    calibration: The calibration it was built from, for the instrument's ID; None when it uses
      none.
    devices_to_monitor: The devices whose failure stops it; empty for all.
    leave_fluidics_running: Whether the fluidics stay on after the last command.
    experiment_id: When set, finalizing the run archives its files on the instrument.
    plate_id: When set, the controller homes the shaker, which also locks the plate, first.
  """
  device_id = None if calibration is None else calibration.instrument.device_id
  worklist = Worklist(
    all_commands_completed=False,
    client_time_offset=datetime.timedelta(0),
    commands=list(commands),
    device_id=device_id,
    devices_to_monitor=list(devices_to_monitor),
    end_time=NOT_RUN,
    error=None,
    execution_notes=None,
    experiment_id=experiment_id,
    experiment_name=None,
    fcs_data_written=0,
    fcs_file_crc=0,
    fcs_file_name=None,
    fluidics_run_time_minutes=decimal.Decimal(0),
    id=uuid.uuid4(),
    leave_fluidics_running_on_complete=leave_fluidics_running,
    name=name,
    plate_id=plate_id,
    plate_number=None,
    pump_run_time_minutes=decimal.Decimal(0),
    start_time=NOT_RUN,
    stop_reason="Unspecified",
  )
  logger.debug("built worklist %r with %d commands", name, len(commands))
  return worklist


# ---------------------------------------------------------------------------------------------
# Well order
# ---------------------------------------------------------------------------------------------


def _plate_dimensions(plate_format: PlateFormat) -> Tuple[int, int]:
  """Return (rows, columns) of a plate format.

  Raises:
    ValueError: If it is not a plate format with wells.
  """
  if plate_format not in PLATE_DIMENSIONS:
    raise ValueError(
      f"{plate_format!r} is not a plate format; formats are {list(PLATE_DIMENSIONS)}"
    )
  return PLATE_DIMENSIONS[plate_format]


def _parse_wells(plate_format: PlateFormat, wells: Sequence[str]) -> List[Tuple[int, int]]:
  """Return the zero-based (row, column) of each well, in order.

  Raises:
    ValueError: If a name is not a well of the plate, or a well appears twice (``A1`` and
      ``A01`` are the same well).
  """
  _plate_dimensions(plate_format)
  if isinstance(wells, str):
    raise ValueError(f"wells must be a sequence of well names, not the text {wells!r}")
  positions: List[Tuple[int, int]] = []
  seen: Set[Tuple[int, int]] = set()
  for name in wells:
    position = parse_well_name(name, plate_format)
    if position in seen:
      raise ValueError(f"well {format_well_name(*position)} appears twice")
    seen.add(position)
    positions.append(position)
  return positions


def well_order(
  plate_format: PlateFormat, order: WellOrder, wells: Optional[Sequence[str]] = None
) -> List[str]:
  """Return wells in one of the instrument's standard sampling orders.

  Args:
    plate_format: The plate format.
    order: ``"row"``: A01, A02, ... then B01, B02, ...; ``"serpentine_row"``: row A left to
      right, row B right to left, and so on; ``"column"``: A01, B01, ... then A02, B02, ...;
      ``"serpentine_column"``: column 1 top to bottom, column 2 bottom to top, and so on.
    wells: The wells to order, in any order and spelling (``A1`` or ``A01``); None for every
      well. Wells left out are skipped, and the direction of a serpentine row or column depends
      on its place on the plate, not among the wells given.

  Returns:
    The well names, such as ``A01``, in sampling order.

  Raises:
    ValueError: If the order is unknown, or a well is not on the plate or given twice.
  """
  rows, columns = _plate_dimensions(plate_format)
  grid: List[Tuple[int, int]]
  if order == "row":
    grid = [(row, column) for row in range(rows) for column in range(columns)]
  elif order == "serpentine_row":
    grid = [
      (row, column if row % 2 == 0 else columns - 1 - column)
      for row in range(rows)
      for column in range(columns)
    ]
  elif order == "column":
    grid = [(row, column) for column in range(columns) for row in range(rows)]
  elif order == "serpentine_column":
    grid = [
      (row if column % 2 == 0 else rows - 1 - row, column)
      for column in range(columns)
      for row in range(rows)
    ]
  else:
    raise ValueError(f"order must be one of {', '.join(WELL_ORDERS)}, not {order!r}")
  if wells is None:
    return [format_well_name(row, column) for row, column in grid]
  selected = set(_parse_wells(plate_format, wells))
  return [format_well_name(row, column) for row, column in grid if (row, column) in selected]


# ---------------------------------------------------------------------------------------------
# Run worklists
# ---------------------------------------------------------------------------------------------


def _check_not_prime(name: str) -> None:
  """Check that a run is not named as a prime.

  Raises:
    ValueError: If the name is "Prime": the controller would stop the run when the client goes
      away.
  """
  if name.strip().lower() == PRIME_WORKLIST_NAME.lower():
    raise ValueError(f"a run cannot be named {name!r}: the controller treats that as a prime")


def _check_plate_read_name(name: str) -> None:
  """Check that a plate read's name is not one the controller treats specially.

  Raises:
    ValueError: If the name is "Prime", or holds "Test Tube" or "Vial": the controller would
      then not home the shaker, which locks the plate.
  """
  _check_not_prime(name)
  for word in _SHAKER_HOME_SUPPRESSORS:
    if word in name.lower():
      raise ValueError(
        f"a plate read's name cannot contain {word!r}: the plate would not be locked"
      )


def plate_read_worklist(
  calibration: Calibration,
  plate_format: PlateFormat,
  wells: Sequence[str],
  settings: PlateReadSettings,
  *,
  plate_model: str,
  name: str = "Plate read",
  metadata: Sequence[Tuple[str, str]] = (),
  user_name: str = UNKNOWN_USER,
  deck_height_tolerance: float = DECK_HEIGHT_TOLERANCE,
) -> Worklist:
  """Build the worklist of a plate read on an iQue Screener PLUS.

  The commands, in order:

  1. load the plate and start the fluidics with the lasers and triggers;
  2. check the deck height at its test point (the run fails when the contact is off by more than
     the tolerance);
  3. start the pump;
  4. prime in Buffer, when ``pre_plate_prime``;
  5. shake with the probe dipping in Buffer, when ``pre_plate_shake``;
  6. start recording events;
  7. start the shaker, in continuous mode;
  8. per well, one dip; after the n-th sampled well (n counts only the wells given, and nothing
     follows the last one): a shake when n is a multiple of ``shake_every``, then the rinse
     steps when it is a multiple of ``rinse_every``;
  9. stop the shaker, in continuous mode;
  10. flush in Buffer, while the plate rail ejects the plate;
  11. the clean steps, when ``post_plate_clean``;
  12. stop the pump and the fluidics.

  Args:
    calibration: The instrument's calibration.
    plate_format: The plate's format.
    wells: The wells to sample, in sampling order, such as from :func:`well_order`.
    settings: How to read.
    plate_model: The name of the plate's model in the calibration; it gives the XY offset and
      the depth map.
    name: The worklist's name.
    metadata: (key, value) pairs the instrument writes into its FCS file, each as keyword
      ``IC$<key>``.
    user_name: The operator name written into the FCS file.
    deck_height_tolerance: How far in mm the deck may be from its calibrated height; the
      controller states it (contact tolerance for deck height validation).

  Returns:
    The worklist, ready to run.

  Raises:
    ValueError: If a well is not on the plate or given twice, no well is given, the name is one
      the controller treats specially, or a setting does not suit the instrument or the plate.
    CalibrationError: If the calibration lacks something the read needs: the plate format's
      corners, the plate model or its depth, the deck height and its test point, the travel
      height, or a station.
  """
  _check_plate_read_name(name)
  positions = _parse_wells(plate_format, wells)
  if not positions:
    raise ValueError("a plate read needs at least one well")
  rows, columns = _plate_dimensions(plate_format)
  if settings.shake_mode == "continuous" and plate_format != "Well96":
    raise ValueError("continuous shaking is only for 96-well plates")
  intervals = (("shake_every", settings.shake_every), ("rinse_every", settings.rinse_every))
  for setting, every in intervals:
    if every > rows * columns:
      raise ValueError(f"{setting} {every} exceeds the {rows * columns} wells of the plate")
  shake_speed = _shake_speed(calibration, plate_format, settings.shake_speed)
  pre_shake_speed = shake_speed
  if settings.pre_plate_shake_speed is not None:
    pre_shake_speed = _shake_speed(calibration, plate_format, settings.pre_plate_shake_speed)
  acquisition = _acquisition(calibration, settings)
  pairs = _metadata(metadata)
  travel = _travel(calibration)
  direction = _pump_direction(calibration)
  sip = _milliseconds(settings.sip_time, "sip_time")
  up = _milliseconds(settings.up_time, "up_time")
  station_up = max(up, _milliseconds(MIN_STATION_UP_TIME, "station up time"))
  sampling_height = settings.sampling_height
  if sampling_height is None:
    sampling_height = DEFAULT_SAMPLING_HEIGHTS[plate_format]
  spots: Dict[str, _Spot] = {}

  def spot(station: str) -> _Spot:
    """Return a station's position, reading it from the calibration once."""
    if station not in spots:
      spots[station] = _station(calibration, station)
    return spots[station]

  commands: List[WorklistCommand] = [_start_plate_fluidics(acquisition, pairs, user_name)]
  deck_check, prior = _check_deck(calibration, travel, deck_height_tolerance)
  commands.append(deck_check)
  commands.append(_start_pump_at(settings.pump_speed, direction))
  if settings.pre_plate_prime:
    seconds = _whole_seconds(settings.pre_plate_prime_duration, "pre_plate_prime_duration")
    buffer = spot(BUFFER)
    description = f"Prime in {BUFFER} for {seconds} s"
    commands.append(_timed_rinse(buffer, travel, seconds, sip, station_up, True, description))
    prior = (buffer.x, buffer.y)
  if settings.pre_plate_shake:
    seconds = _whole_seconds(settings.pre_plate_shake_duration, "pre_plate_shake_duration")
    buffer = spot(BUFFER)
    commands.append(_shake(buffer, travel, seconds, pre_shake_speed, sip, station_up))
    prior = (buffer.x, buffer.y)
  commands.append(_acquire(acquisition, 0, "Start recording events"))
  if settings.shake_mode == "continuous":
    commands.append(
      StartShakerCommand(
        **_fields("Shake", f"Start shaking at {shake_speed} rpm", _START_SHAKER_ESTIMATE),
        rpm=shake_speed,
      )
    )
  shake_seconds = _whole_seconds(settings.shake_duration, "shake_duration")
  for count, (row, column) in enumerate(positions, start=1):
    what = f"well {format_well_name(row, column)}"
    xy = well_sampling_xy(calibration, plate_format, plate_model, row, column)
    z = well_sampling_z(calibration, plate_format, plate_model, row, column, sampling_height)
    x, y = _position(xy.x, "X", what), _position(xy.y, "Y", what)
    depth = _position(z, "Z", what)
    commands.append(_sample_well(row, column, x, y, depth, travel, sip, up, prior))
    prior = (x, y)
    if count == len(positions):
      break
    if settings.shake_mode == "after_wells" and count % settings.shake_every == 0:
      station = spot(settings.shake_station)
      commands.append(_shake(station, travel, shake_seconds, shake_speed, sip, station_up))
      prior = (station.x, station.y)
    if settings.probe_rinse and count % settings.rinse_every == 0:
      for step in settings.rinses:
        station = spot(step.station)
        duration = _milliseconds(step.duration, "rinse duration")
        commands.append(_cycling_rinse(station, travel, duration, sip, up))
        prior = (station.x, station.y)
  if settings.shake_mode == "continuous":
    commands.append(StopShakerCommand(**_fields("Shake", "Stop shaking", _STOP_SHAKER_ESTIMATE)))
  flush = _whole_seconds(settings.flush_duration, "flush_duration")
  description = f"Flush in {BUFFER} for {flush} s and eject the plate"
  commands.append(
    _timed_rinse(
      spot(BUFFER), travel, flush, sip, station_up, True, description, FlushMatryoshkaCommand
    )
  )
  if settings.post_plate_clean:
    for step in settings.cleans:
      seconds = _whole_seconds(step.duration, "clean duration")
      description = f"Clean in {step.station} for {seconds} s"
      commands.append(
        _timed_rinse(spot(step.station), travel, seconds, sip, station_up, True, description)
      )
  commands.append(_stop_pump())
  commands.append(_stop_fluidics())
  return _new_worklist(
    name,
    _with_acquisition_time(commands),
    calibration,
    experiment_id=uuid.uuid4(),
    plate_id=uuid.uuid4(),
  )


def _tube_check(
  calibration: Calibration, travel: decimal.Decimal, tolerance: float
) -> ValidateDeckHeightCommand:
  """Feel for a tube in the tube holder: contact must be 0.2 mm to the tolerance above its bottom.

  Raises:
    CalibrationError: If the tube holder or the deck height is not calibrated.
  """
  xy = tube_xy(calibration)
  return ValidateDeckHeightCommand(
    **_fields("Info", "Check that a tube is in the tube holder", _DECK_CHECK_ESTIMATE),
    is_microfuge_tube_check=True,
    x=_position(xy.x, "X", "tube holder"),
    y=_position(xy.y, "Y", "tube holder"),
    z_high=travel,
    z_low=_position(tube_contact_z(calibration), "Z", "tube holder"),
    z_tolerance=_millimetres(tolerance, "tube tolerance"),
  )


def tube_read_worklist(
  calibration: Calibration,
  settings: TubeReadSettings,
  *,
  name: str = "Tube read",
  metadata: Sequence[Tuple[str, str]] = (),
  user_name: str = UNKNOWN_USER,
  tube_tolerance: float = TUBE_TOLERANCE,
) -> Worklist:
  """Build the worklist of a read of the 1.5 mL microcentrifuge tube in the tube holder.

  The commands, in order:

  1. feel for the tube (the run fails when the holder is empty or obstructed);
  2. start the fluidics with the lasers and triggers;
  3. prime: draw from the prime station at 53 rpm until the line is clear (25 s);
  4. start the pump at the sampling speed;
  5. start recording events, after the time the sample takes to reach the detector at that
     speed (39 s at 29 rpm);
  6. draw from the tube for the sip time, moving slowly;
  7. flush from the station with the probe down until the line is clear (44 s at 29 rpm);
  8. when ``post_tube_clean``: stop recording, raise the pump to 53 rpm, the clean steps;
  9. stop the pump and the fluidics.

  The same worklist with ``post_tube_clean`` on is the instrument's bead (peak) QC test.

  Args:
    calibration: The instrument's calibration.
    settings: How to read.
    name: The worklist's name.
    metadata: (key, value) pairs the instrument writes into its FCS file.
    user_name: The operator name written into the FCS file.
    tube_tolerance: How far in mm above the empty holder's bottom a tube may be touched; the
      controller states it (contact tolerance for tube validation).

  Returns:
    The worklist, ready to run.

  Raises:
    ValueError: If the name is one the controller treats as a prime, or a setting does not
      suit the instrument.
    CalibrationError: If the calibration lacks the tube holder, the station, a clean station,
      the deck height or the travel height.
  """
  _check_not_prime(name)
  acquisition = _acquisition(calibration, settings)
  pairs = _metadata(metadata)
  travel = _travel(calibration)
  direction = _pump_direction(calibration)
  station = settings.station
  prime_seconds = _line_flush_time(PRIME_PUMP_SPEED)
  flush_seconds = _line_flush_time(settings.pump_speed)
  delay = transit_delay(settings.pump_speed)
  prime_speed = _pump_speed(PRIME_PUMP_SPEED, "prime pump speed")
  commands: List[WorklistCommand] = [
    _tube_check(calibration, travel, tube_tolerance),
    _start_fluidics(acquisition.lasers, acquisition.thresholds, pairs, user_name, True),
    _draw_continuously(
      _station(calibration, station, draw=True),
      travel,
      _milliseconds(prime_seconds, "prime time"),
      prime_speed,
      direction,
      f"Prime from {station} for {prime_seconds} s at {PRIME_PUMP_SPEED:g} rpm",
    ),
    _start_pump_at(settings.pump_speed, direction),
    _acquire(acquisition, delay, f"Start recording events after {delay} s"),
  ]
  sip = _milliseconds(settings.sip_time, "sip_time")
  tube = tube_xy(calibration)
  depth = _position(tube_sampling_z(calibration), "Z", "tube")
  if depth > travel:
    raise ValueError(f"the tube's sampling height {depth} mm is above the travel height {travel}")
  sip_seconds = _seconds_decimal(sip)
  commands.append(
    SampleVialCommand(
      **_fields(
        "Sample",
        f"Sample the tube for {sip_seconds.normalize():f} s",
        sip_seconds + _TUBE_SAMPLE_PADDING,
        math.ceil(sip_seconds),
      ),
      fast_move=False,
      sip_time=sip,
      up_time=0,
      x=_position(tube.x, "X", "tube"),
      y=_position(tube.y, "Y", "tube"),
      z_down=depth,
      z_up=travel,
      column=1,
      row="A",
      well_type=_SAMPLE_WELL_TYPE,
      event_threshold=NO_EVENT_LIMIT,
      z_tolerance=_millimetres(tube_tolerance, "tube tolerance"),
    )
  )
  flush_spot = _station(calibration, station)
  description = f"Flush in {station} for {flush_seconds} s"
  commands.append(_timed_continuous_rinse(flush_spot, travel, flush_seconds, "Fast", description))
  if settings.post_tube_clean:
    commands.append(
      FlowCytometerStopAcquireCommand(
        **_fields("FlowCytometer", "Stop recording events", _STOP_ACQUIRE_ESTIMATE)
      )
    )
    description = f"Raise pump speed to {PRIME_PUMP_SPEED:g} rpm"
    commands.append(_start_pump(prime_speed, direction, description))
    commands.extend(_clean_after_tube(calibration, travel, settings.cleans))
  commands.append(_stop_pump())
  commands.append(_stop_fluidics())
  return _new_worklist(
    name, _with_acquisition_time(commands), calibration, experiment_id=uuid.uuid4()
  )


def qc_clean_test_worklist(
  calibration: Calibration,
  settings: TubeReadSettings,
  *,
  name: str = "Clean test",
  metadata: Sequence[Tuple[str, str]] = (),
  user_name: str = UNKNOWN_USER,
) -> Worklist:
  """Build the instrument's cleanliness QC test: record events while drawing clean water.

  A clean line records few events. The commands, in order: start the fluidics with the lasers and
  triggers; start the pump at the sampling speed; start recording after the transit delay of that
  speed; draw from Water for 60 s at 29 rpm; start the pump at the sampling speed again; flush
  from the settings' station with the probe down until the line is clear; stop the pump and the
  fluidics.

  Of the settings it uses the triggers, lasers, event limits, pump speed and station; the sip
  time and clean steps are for tube reads.

  Args:
    calibration: The instrument's calibration.
    settings: The acquisition settings, pump speed and flush station.
    name: The worklist's name.
    metadata: (key, value) pairs the instrument writes into its FCS file.
    user_name: The operator name written into the FCS file.

  Returns:
    The worklist, ready to run.

  Raises:
    ValueError: If the name is one the controller treats as a prime, or a setting does not
      suit the instrument.
    CalibrationError: If the calibration lacks Water, the station, the deck height or the travel
      height.
  """
  _check_not_prime(name)
  acquisition = _acquisition(calibration, settings)
  pairs = _metadata(metadata)
  travel = _travel(calibration)
  direction = _pump_direction(calibration)
  delay = transit_delay(settings.pump_speed)
  flush_seconds = _line_flush_time(settings.pump_speed)
  water_seconds = _whole_seconds(CLEAN_TEST_DURATION, "clean test duration")
  commands: List[WorklistCommand] = [
    _start_fluidics(acquisition.lasers, acquisition.thresholds, pairs, user_name, True),
    _start_pump_at(settings.pump_speed, direction),
    _acquire(acquisition, delay, f"Start recording events after {delay} s"),
    _draw_continuously(
      _station(calibration, WATER, draw=True),
      travel,
      _milliseconds(CLEAN_TEST_DURATION, "clean test duration"),
      _pump_speed(CLEAN_TEST_PUMP_SPEED, "clean test pump speed"),
      direction,
      f"Draw from {WATER} for {water_seconds} s at {CLEAN_TEST_PUMP_SPEED:g} rpm",
    ),
    _start_pump_at(settings.pump_speed, direction),
    _timed_continuous_rinse(
      _station(calibration, settings.station),
      travel,
      flush_seconds,
      "Fast",
      f"Flush in {settings.station} for {flush_seconds} s",
    ),
    _stop_pump(),
    _stop_fluidics(),
  ]
  return _new_worklist(
    name, _with_acquisition_time(commands), calibration, experiment_id=uuid.uuid4()
  )


# ---------------------------------------------------------------------------------------------
# Maintenance worklists
# ---------------------------------------------------------------------------------------------


def _check_maintenance_pump_speed(pump_speed: float) -> None:
  """Check a pump speed for priming or cleaning.

  Raises:
    ValueError: If it is outside 10 to 53 rpm.
  """
  _check_range(pump_speed, MAINTENANCE_PUMP_SPEED_RANGE, "pump_speed", "rpm")


def prime_worklist(
  calibration: Calibration,
  *,
  duration: float = 3600.0,
  pump_speed: float = MAINTENANCE_PUMP_SPEED,
  sip_time: float = 1.0,
  up_time: float = 1.0,
) -> Worklist:
  """Build the manual prime: fill the line from Buffer with the probe dipping repeatedly.

  The commands: start the fluidics; start the pump; dip in Buffer for the duration. The
  instrument's prime runs an hour and is ended by cancelling the worklist; a 60 s prime clears
  sample left in the line by a run that was stopped. The worklist is named "Prime", which makes
  the controller stop it when the client disconnects.

  Args:
    calibration: The instrument's calibration.
    duration: How long to prime, 1 to 3600 s in whole seconds.
    pump_speed: Pump speed in rpm, 10 to 53; the default is the controller's default speed.
      Before a plate read, prime at the read's pump speed.
    sip_time: Time down per dip, 0.5 to 59.9 s.
    up_time: Time up per dip, 0.5 to 59.9 s.

  Returns:
    The worklist, ready to run.

  Raises:
    ValueError: If a value is out of range.
    CalibrationError: If Buffer, the deck height or the travel height is not calibrated.
  """
  _check_range(duration, MANUAL_PRIME_DURATION_RANGE, "duration", "s")
  _check_maintenance_pump_speed(pump_speed)
  _check_range(sip_time, SIP_TIME_RANGE, "sip_time", "s")
  _check_range(up_time, (MIN_STATION_UP_TIME, UP_TIME_RANGE[1]), "up_time", "s")
  seconds = _whole_seconds(duration, "duration")
  travel = _travel(calibration)
  commands: List[WorklistCommand] = [
    _maintenance_fluidics(calibration),
    _start_pump_at(pump_speed, _pump_direction(calibration)),
    _timed_rinse(
      _station(calibration, BUFFER),
      travel,
      seconds,
      _milliseconds(sip_time, "sip_time"),
      _milliseconds(up_time, "up_time"),
      True,
      f"Prime in {BUFFER} for {seconds} s",
    ),
  ]
  return _new_worklist(PRIME_WORKLIST_NAME, commands, calibration)


def _probe_clean_commands(
  calibration: Calibration, duration: float, pump_speed: float
) -> List[WorklistCommand]:
  """Return the commands of the probe and tubing clean.

  Raises:
    ValueError: If the duration is not 4 to 32 minutes in steps of 4, or the speed is out of
      range.
    CalibrationError: If Decon, Clean, Water, the deck height or the travel height is not
      calibrated.
  """
  _check_range(duration, PROBE_CLEAN_DURATION_RANGE, "duration", "s")
  if _whole_seconds(duration, "duration") % CLEAN_DURATION_STEP != 0:
    raise ValueError(f"the clean lasts 4 to 32 minutes in steps of 4, not {duration:g} s")
  _check_maintenance_pump_speed(pump_speed)
  quarter = _whole_seconds(duration, "duration") // 4
  travel = _travel(calibration)
  sip = _milliseconds(CLEAN_SIP_TIME, "clean sip time")
  up = _milliseconds(CLEAN_UP_TIME, "clean up time")
  commands: List[WorklistCommand] = [
    _maintenance_fluidics(calibration),
    _start_pump_at(pump_speed, _pump_direction(calibration)),
  ]
  for station, seconds in (("Decon", quarter), ("Clean", quarter), ("Water", 2 * quarter)):
    description = f"Rinse in {station} for {seconds} s"
    commands.append(
      _timed_rinse(_station(calibration, station), travel, seconds, sip, up, True, description)
    )
  commands.append(_stop_pump())
  commands.append(_stop_fluidics())
  return commands


def probe_and_tubing_clean_worklist(
  calibration: Calibration,
  duration: float = PROBE_CLEAN_DURATION,
  *,
  pump_speed: float = MAINTENANCE_PUMP_SPEED,
) -> Worklist:
  """Build the probe and tubing clean: the probe dips in Decon, then Clean, then Water.

  The commands: start the fluidics; start the pump; dip in Decon for a quarter of the duration,
  in Clean for a quarter and in Water for half (1 s down, 0.5 s up); stop the pump and the
  fluidics. The Decon, Clean and Water troughs must be filled.

  Args:
    calibration: The instrument's calibration.
    duration: How long, 4 to 32 minutes in steps of 4, in seconds (240 to 1920).
    pump_speed: Pump speed in rpm, 10 to 53; the default is the controller's default speed.

  Returns:
    The worklist, ready to run.

  Raises:
    ValueError: If a value is out of range.
    CalibrationError: If Decon, Clean, Water, the deck height or the travel height is not
      calibrated.
  """
  commands = _probe_clean_commands(calibration, duration, pump_speed)
  return _new_worklist("Probe and Tubing Clean", commands, calibration, MAINTENANCE_DEVICES)


def startup_worklist(
  calibration: Calibration,
  *,
  clean_duration: float = PROBE_CLEAN_DURATION,
  pump_speed: float = MAINTENANCE_PUMP_SPEED,
) -> Worklist:
  """Build the worklist of the daily start-up: the probe and tubing clean.

  Starting up is first a sequence of controller calls, then this worklist: power the cytometer
  on and wait up to 30 s for it to have power; skip the clean while an installation prime or a
  purge is pending; wait until the cytometer is ready to clean (up to its operation duration plus
  a minute, or 25 minutes when it states none), failing if it leaves the ready state; check that
  it is ready to run; run this worklist. The Waste bottle must be empty, the Sheath, PLUS One and
  PLUS Two bottles and the Decon, Clean and Water troughs filled.

  Args:
    calibration: The instrument's calibration.
    clean_duration: How long the clean lasts, as for :func:`probe_and_tubing_clean_worklist`.
    pump_speed: Pump speed in rpm, 10 to 53.

  Returns:
    The worklist, ready to run.

  Raises:
    ValueError: If a value is out of range.
    CalibrationError: As for :func:`probe_and_tubing_clean_worklist`.
  """
  commands = _probe_clean_commands(calibration, clean_duration, pump_speed)
  return _new_worklist("Start-up Clean", commands, calibration, MAINTENANCE_DEVICES)


def shutdown_worklist(
  calibration: Calibration,
  *,
  clean_duration: float = PROBE_CLEAN_DURATION,
  pump_speed: float = MAINTENANCE_PUMP_SPEED,
) -> Worklist:
  """Build the daily shut-down: the probe and tubing clean, then shut the cytometer down.

  The controller must report the cytometer ready to run first. Shutting down without the clean
  is a single controller call (cytometer shutdown), not a worklist. A leak (low pressure) makes
  the shut-down fail and stops the worklist.

  Args:
    calibration: The instrument's calibration.
    clean_duration: How long the clean lasts, as for :func:`probe_and_tubing_clean_worklist`.
    pump_speed: Pump speed in rpm, 10 to 53.

  Returns:
    The worklist, ready to run. The cytometer is not among the devices whose failure stops it,
    since it goes off at the end.

  Raises:
    ValueError: If a value is out of range.
    CalibrationError: As for :func:`probe_and_tubing_clean_worklist`.
  """
  commands = _probe_clean_commands(calibration, clean_duration, pump_speed)
  commands.append(
    ShutdownFlowCytometerCommand(
      **_fields("FlowCytometer", "Shut the cytometer down", _SHUTDOWN_ESTIMATE)
    )
  )
  return _new_worklist("Clean and Shutdown", commands, calibration, SHUTDOWN_DEVICES)


def _detector_clean(method: CleanMethod, name: str) -> Worklist:
  """Build a worklist that runs one of the cytometer's own maintenance routines.

  The cytometer runs the routine by itself; cancelling the worklist only stops waiting for it.
  The fluidics are left running afterwards, as the routine leaves them.
  """
  estimate = _CLEAN_ESTIMATES[method]
  minutes = (estimate / 60).normalize()
  command = CleanFlowCytometerCommand(
    **_fields("FlowCytometer", f"{name} (about {minutes:f} min)", estimate),
    clean_method=method,
  )
  return _new_worklist(name, [command], None, MAINTENANCE_DEVICES, leave_fluidics_running=True)


def detector_quick_clean_worklist() -> Worklist:
  """Build the detector quick clean, about 11 minutes. The sampler tubing must be installed.

  Returns:
    The worklist, ready to run once the controller reports the cytometer ready to run.
  """
  return _detector_clean("QuickClean", "Detector Quick Clean")


def detector_long_clean_worklist() -> Worklist:
  """Build the detector long clean, about 30 minutes. The sampler tubing must be installed.

  Returns:
    The worklist, ready to run once the controller reports the cytometer ready to run.
  """
  return _detector_clean("LongClean", "Detector Long Clean")


def detector_unclog_worklist() -> Worklist:
  """Build the detector unclog, about 10.5 minutes: for clogs, and monthly maintenance.

  The sampler tubing must be installed.

  Returns:
    The worklist, ready to run once the controller reports the cytometer ready to run.
  """
  return _detector_clean("Unclog", "Detector Unclog")


def detector_debubble_worklist() -> Worklist:
  """Build the detector debubble, about 13 minutes: the cytometer primes from its bottles.

  It also ends the fluidics maintenance procedure. The sampler tubing must be installed.

  Returns:
    The worklist, ready to run once the controller reports the cytometer ready to run.
  """
  return _detector_clean("Priming", "Detector Debubble")


def fluidics_check_worklist(
  calibration: Calibration, *, pump_speed: float = MAINTENANCE_PUMP_SPEED
) -> Worklist:
  """Build the one-minute fluidics (leak) check, run after changing filters, FluidLink or probe.

  The commands: start the fluidics; start the pump; keep the probe in Water for 60 s (59 s down,
  2 s up); stop the pump and the fluidics. Meanwhile the user looks for leaks at the pump clamp,
  the tubing port and both ends of the FluidLink. The Water trough must be filled.

  Args:
    calibration: The instrument's calibration.
    pump_speed: Pump speed in rpm, 10 to 53; the default is the controller's default speed.

  Returns:
    The worklist, ready to run.

  Raises:
    ValueError: If the speed is out of range.
    CalibrationError: If Water, the deck height or the travel height is not calibrated.
  """
  _check_maintenance_pump_speed(pump_speed)
  travel = _travel(calibration)
  seconds = _whole_seconds(FLUIDICS_CHECK_DURATION, "fluidics check duration")
  commands: List[WorklistCommand] = [
    _maintenance_fluidics(calibration),
    _start_pump_at(pump_speed, _pump_direction(calibration)),
    _timed_rinse(
      _station(calibration, WATER),
      travel,
      seconds,
      _milliseconds(FLUIDICS_CHECK_SIP_TIME, "fluidics check sip time"),
      _milliseconds(FLUIDICS_CHECK_UP_TIME, "fluidics check up time"),
      True,
      f"Rinse in {WATER} for {seconds} s",
    ),
    _stop_pump(),
    _stop_fluidics(),
  ]
  return _new_worklist("Fluidics Check", commands, calibration, MAINTENANCE_DEVICES)


def deck_height_validation_worklist(
  calibration: Calibration, *, tolerance: float = DECK_HEIGHT_TOLERANCE
) -> Worklist:
  """Build a check of the deck height on its own, as every plate read starts with.

  The controller loads the plate rail, touches the test point and fails the worklist unless the
  contact is within the tolerance of the calibrated deck height. Measuring a new deck height is
  done with direct sampler calls, not a worklist.

  Args:
    calibration: The instrument's calibration.
    tolerance: How far in mm the deck may be from its calibrated height; the controller states
      it (contact tolerance for deck height validation).

  Returns:
    The worklist, ready to run.

  Raises:
    CalibrationError: If the deck height, its test point or the travel height is not calibrated.
  """
  command, _ = _check_deck(calibration, _travel(calibration), tolerance)
  return _new_worklist("Validate Deck Height", [command], calibration)


# ---------------------------------------------------------------------------------------------
# Finished worklists
# ---------------------------------------------------------------------------------------------


def _commands(worklist: Worklist) -> List[WorklistCommand]:
  """Return the commands of a worklist.

  Raises:
    ValueError: If it has none, or an entry is missing.
  """
  if not worklist.commands:
    raise ValueError(f"worklist {worklist.name!r} has no commands")
  commands: List[WorklistCommand] = []
  for index, command in enumerate(worklist.commands):
    if command is None:
      raise ValueError(f"command {index + 1} of worklist {worklist.name!r} is missing")
    commands.append(command)
  return commands


def _has_started(command: WorklistCommand) -> bool:
  """Return whether the controller started a command."""
  return command.start_time.replace(tzinfo=None) != NOT_RUN


def _incomplete(worklist: Worklist, index: int, command: WorklistCommand, count: int) -> str:
  """Describe the first command of a worklist that did not complete."""
  state = "started but did not finish" if _has_started(command) else "did not run"
  if command.execution_notes == "Skipped":
    state = "was skipped"
  message = (
    f"worklist {worklist.name!r} did not complete: command {index + 1} of {count}, "
    f"{type(command).__name__} ({command.description!r}), {state}; "
    f"stop reason {worklist.stop_reason}"
  )
  if worklist.error:
    message += f", error {worklist.error!r}"
  return message


def _sampled(command: WorklistCommand) -> Optional[SampleWellCommand]:
  """Return a command when it sampled a well and finished, else None.

  Only the plain sampling command samples a well of a plate; its subclasses sample tubes.
  """
  if not isinstance(command, SampleWellCommand) or type(command) is not SampleWellCommand:
    return None
  if not command.completed or command.well_type in _NOT_SAMPLED_WELL_TYPES:
    return None
  return command


def _well_name(command: SampleWellCommand) -> str:
  """Return the name of the well a sampling command sampled, from its row and column.

  Raises:
    ValueError: If the row and column do not name a well.
  """
  if command.row is None:
    raise ValueError(f"sampling command {command.id} has no row")
  return format_well_name(*parse_well_name(f"{command.row}{command.column}"))


def sampled_wells(completed: Worklist, *, allow_incomplete: bool = False) -> List[SampledWell]:
  """Return the wells a finished plate read sampled, with when each sampling command ran.

  Sampling commands are recognised by their class and named by their row and column. Wells of
  type Empty or Rinse are not sampled wells. Times are in seconds since the first command of the
  worklist started, on the controller's clock.

  ``after_pause`` is True for a well when anything happened between it and the previous sampled
  well: a shake, a rinse, or a well that was skipped, empty or not finished. The first well has
  no previous well, and its ``after_pause`` is False.

  Args:
    completed: The worklist as the controller handed it back at the end of the run.
    allow_incomplete: Return the wells whose commands finished even if the run did not
      complete; otherwise an incomplete run raises.

  Returns:
    The sampled wells in sampling order.

  Raises:
    IncompleteWorklistError: If a command did not complete and ``allow_incomplete`` is False;
      the message names the first such command.
    ValueError: If the worklist has no commands, a command is missing, or a finished sampling
      command has no valid well or ends before it starts.
  """
  commands = _commands(completed)
  for index, command in enumerate(commands):
    if not command.completed:
      message = _incomplete(completed, index, command, len(commands))
      if not allow_incomplete:
        raise IncompleteWorklistError(message)
      logger.info("%s", message)
      break
  if not _has_started(commands[0]):
    return []
  origin = commands[0].start_time
  wells: List[SampledWell] = []
  pause = False
  for command in commands:
    sample = _sampled(command)
    if sample is None:
      # Anything between two sampled wells interrupts the regular rhythm of dips.
      pause = bool(wells)
      continue
    well = _well_name(sample)
    start = (sample.start_time - origin).total_seconds()
    end = (sample.end_time - origin).total_seconds()
    if end < start:
      raise ValueError(f"the command sampling {well} ends before it starts")
    wells.append(SampledWell(well, start, end, after_pause=pause))
    pause = False
  return wells


def estimated_duration(worklist: Worklist) -> datetime.timedelta:
  """Return how long a worklist is expected to run: the sum of its commands' estimates.

  It leaves out what the controller does around every worklist (parking, homing, ejecting).

  Raises:
    ValueError: If the worklist has no commands or one is missing.
  """
  total = datetime.timedelta(0)
  for command in _commands(worklist):
    total += command.estimated_duration
  return total


def flush_target(
  worklist: Worklist, *, running: Optional[uuid.UUID] = None
) -> Optional[WorklistCommand]:
  """Return the command to skip to when a plate read is stopped early, so the line is flushed.

  It is the first command not yet completed that stops a continuous shake or flushes the line:
  skipping to it runs it and everything after it (flush, clean, pump and fluidics off). Skip
  only once recording has started; before that, cancel. A skip does not interrupt the command
  running, and the commands after a skip target that already runs would all be skipped, so
  there is no target while that command runs.

  Args:
    worklist: The worklist as last reported by the controller.
    running: The ID of the command running now, if any.

  Returns:
    The command, or None when the worklist has none left to skip to.

  Raises:
    ValueError: If the worklist has no commands or one is missing.
  """
  for command in _commands(worklist):
    if isinstance(command, (StopShakerCommand, FlushMatryoshkaCommand, FlushCommand)):
      if command.completed:
        continue
      return None if command.id == running else command
  return None
