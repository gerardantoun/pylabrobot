"""qPCR protocols for the CFX command language.

A protocol is authored on the instrument one directive at a time: the header (``METHOD``,
``HOTLID``, ``VOLUME``), then numbered steps (``TEMP``, ``GRAD``, ``PLATEREAD``, ``GOTO``) with
unnumbered modifiers (``INC``, ``RATE``, ``EXT``, ``BEEP``) after the step they modify, then
``END``. ``GOTO <step>,<repeats>`` jumps back to a 1-based step number and runs the loop
``repeats`` more times.

This module models a protocol as plain steps plus two compound steps, :class:`Cycle` (a ``GOTO``
loop) and :class:`MeltCurve`, and compiles it to the directive list.
"""

import math
from dataclasses import dataclass
from typing import List, Literal, Optional, Sequence, Union

#: Named plate-read configurations and their scan masks. Bits 0-5 select optical channels 1-6;
#: bit 7 sweeps the plate continuously instead of stopping over each well.
PlateReadScan = Literal["all", "fast", "fret"]
SCAN_MASKS = {"all": 0x3F, "fast": 0x81, "fret": 0xA0}

MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 100.0
MIN_GRADIENT_TEMPERATURE = 30.0
MAX_GRADIENT_TEMPERATURE = 100.0
MIN_GRADIENT_SPREAD = 1.0
MAX_GRADIENT_SPREAD = 24.0
MAX_HOLD = 64800
MAX_LID_TEMPERATURE = 110.0
MAX_VOLUME = 125.0
MAX_INCREMENT = 10.0
MIN_RAMP_RATE = 0.1
MAX_RAMP_RATE = 5.0
MAX_EXTEND = 60
MAX_REPEATS = 9999
MAX_STEPS = 98

#: The lid heater turns off once the block falls below this temperature, in °C.
LID_SHUTOFF_TEMPERATURE = 30

#: A melt curve's equilibration hold. The instrument recognizes a melt by this 31 s hold at the
#: melt's start temperature, immediately before the per-rung hold.
MELT_EQUILIBRATION_HOLD = 31


class ProtocolError(ValueError):
  """A protocol that the instrument cannot run as written."""


@dataclass(frozen=True)
class Hold:
  """Hold the block at one temperature.

  Args:
    temperature: Target temperature in °C. Under a protocol with a sample volume this is the
      calculated sample temperature, otherwise the block temperature.
    duration: Hold time in whole seconds. ``0`` holds indefinitely, until the run is stopped.
    ramp_rate: Maximum ramp rate toward the target, in °C/s. ``None`` ramps at full speed.
    increment: Change of the target, in °C, on each later pass through the enclosing cycle.
    extend: Change of the hold time, in seconds, on each later pass through the enclosing cycle.
    beep: Sound the instrument's beeper when the step completes.
  """

  temperature: float
  duration: int
  ramp_rate: Optional[float] = None
  increment: Optional[float] = None
  extend: Optional[int] = None
  beep: bool = False


@dataclass(frozen=True)
class Gradient:
  """Hold a temperature gradient across the block's rows, ``lower`` at one edge, ``upper`` at the
  other.

  Args:
    lower: Temperature in °C at the cold edge.
    upper: Temperature in °C at the hot edge.
    duration: Hold time in whole seconds.
    extend: Change of the hold time, in seconds, on each later pass through the enclosing cycle.
  """

  lower: float
  upper: float
  duration: int
  extend: Optional[int] = None


@dataclass(frozen=True)
class PlateRead:
  """Read the plate's fluorescence.

  Args:
    scan: ``"all"`` reads every optical channel, stopping over each well; ``"fast"`` reads channel
      1 only (FAM / SYBR Green) in one continuous sweep; ``"fret"`` reads the FRET channel only in
      one continuous sweep.
  """

  scan: PlateReadScan = "all"


@dataclass(frozen=True)
class Cycle:
  """Run ``steps`` ``count`` times in a row, e.g. the denature-anneal-read loop of a qPCR."""

  steps: Sequence[Union[Hold, Gradient, PlateRead]]
  count: int


@dataclass(frozen=True)
class MeltCurve:
  """Raise the temperature from ``start`` to ``end`` in steps of ``increment``, reading the plate
  at every step.

  Args:
    start: First read temperature in °C.
    end: Last read temperature in °C. ``end - start`` must be a whole number of increments.
    increment: Temperature step in °C. Negative to melt downward.
    hold: Time in whole seconds the block holds at each step before its read.
    scan: Which optical channels each read covers, as for :class:`PlateRead`.
  """

  start: float
  end: float
  increment: float = 0.5
  hold: int = 5
  scan: PlateReadScan = "all"


ProtocolStep = Union[Hold, Gradient, PlateRead, Cycle, MeltCurve]


@dataclass(frozen=True)
class Protocol:
  """A thermal protocol with optional plate reads.

  Args:
    steps: The steps in run order.
    volume: Sample volume per well in µL. With a volume the instrument controls the calculated
      sample temperature; ``0`` controls the block temperature directly.
    lid_temperature: Heated lid temperature in °C, or ``0`` to leave the lid heater off.
  """

  steps: Sequence[ProtocolStep]
  volume: float = 0
  lid_temperature: float = 105

  @property
  def method(self) -> Literal["CALC", "BLOCK"]:
    """The thermal control method the volume implies."""

    return "CALC" if self.volume > 0 else "BLOCK"


def _format_temperature(value: float) -> str:
  return f"{value:.1f}"


def _check_range(name: str, value: float, low: float, high: float) -> None:
  if not low <= value <= high:
    raise ProtocolError(f"{name} {value} is outside {low}-{high}")


def _check_whole(name: str, value: float) -> None:
  if not float(value).is_integer():
    raise ProtocolError(f"{name} must be a whole number, got {value}")


def _check_tenths(name: str, value: float) -> None:
  if not math.isclose(round(value, 1), value, abs_tol=1e-9):
    raise ProtocolError(f"{name} {value} has more than one decimal place")


def _hold_directives(step: Hold) -> List[str]:
  _check_range("Hold temperature", step.temperature, MIN_TEMPERATURE, MAX_TEMPERATURE)
  _check_tenths("Hold temperature", step.temperature)
  _check_whole("Hold duration", step.duration)
  _check_range("Hold duration", step.duration, 0, MAX_HOLD)
  directives = [f"TEMP {_format_temperature(step.temperature)},{int(step.duration)}"]
  if step.increment is not None:
    _check_range("Increment", step.increment, -MAX_INCREMENT, MAX_INCREMENT)
    _check_tenths("Increment", step.increment)
    directives.append(f"INC {_format_temperature(step.increment)}")
  if step.ramp_rate is not None:
    _check_range("Ramp rate", step.ramp_rate, MIN_RAMP_RATE, MAX_RAMP_RATE)
    _check_tenths("Ramp rate", step.ramp_rate)
    directives.append(f"RATE {_format_temperature(step.ramp_rate)}")
  if step.extend is not None:
    _check_whole("Extend", step.extend)
    _check_range("Extend", step.extend, -MAX_EXTEND, MAX_EXTEND)
    directives.append(f"EXT {int(step.extend)}")
  if step.beep:
    directives.append("BEEP")
  return directives


def _gradient_directives(step: Gradient) -> List[str]:
  for name, value in (("Gradient lower", step.lower), ("Gradient upper", step.upper)):
    _check_range(name + " temperature", value, MIN_GRADIENT_TEMPERATURE, MAX_GRADIENT_TEMPERATURE)
    _check_tenths(name + " temperature", value)
  _check_range("Gradient spread", step.upper - step.lower, MIN_GRADIENT_SPREAD, MAX_GRADIENT_SPREAD)
  _check_whole("Gradient duration", step.duration)
  _check_range("Gradient duration", step.duration, 1, MAX_HOLD)
  directives = [
    f"GRAD {_format_temperature(step.lower)},{_format_temperature(step.upper)},{int(step.duration)}"
  ]
  if step.extend is not None:
    _check_whole("Extend", step.extend)
    _check_range("Extend", step.extend, -MAX_EXTEND, MAX_EXTEND)
    directives.append(f"EXT {int(step.extend)}")
  return directives


def _plate_read_directive(scan: PlateReadScan) -> str:
  return f"PLATEREAD #h{SCAN_MASKS[scan]:X}"


def melt_rungs(melt: MeltCurve) -> int:
  """The number of temperatures a melt curve reads at, ``start`` and ``end`` included."""

  if melt.increment == 0:
    raise ProtocolError("A melt curve's increment cannot be 0")
  rungs = (melt.end - melt.start) / melt.increment
  if rungs < 1 or not math.isclose(rungs, round(rungs), abs_tol=1e-6):
    raise ProtocolError(
      f"A melt from {melt.start} to {melt.end} is not a whole number (at least 1) of "
      f"{melt.increment} °C increments"
    )
  return round(rungs) + 1


class _Compiler:
  """Emits numbered step directives and keeps the step count for ``GOTO`` targets."""

  def __init__(self) -> None:
    self.directives: List[str] = []
    self.steps = 0

  def emit_step(self, directives: List[str]) -> int:
    """Emit one numbered step with its modifiers and return its step number."""

    self.steps += 1
    self.directives.extend(directives)
    return self.steps

  def emit_simple(self, step: Union[Hold, Gradient, PlateRead]) -> int:
    if isinstance(step, Hold):
      return self.emit_step(_hold_directives(step))
    if isinstance(step, Gradient):
      return self.emit_step(_gradient_directives(step))
    return self.emit_step([_plate_read_directive(step.scan)])

  def emit_cycle(self, cycle: Cycle) -> None:
    if not cycle.steps:
      raise ProtocolError("A cycle needs at least one step")
    _check_whole("Cycle count", cycle.count)
    _check_range("Cycle count", cycle.count, 2, MAX_REPEATS + 1)
    first = None
    for step in cycle.steps:
      if not isinstance(step, (Hold, Gradient, PlateRead)):
        raise ProtocolError(f"A cycle cannot contain {type(step).__name__}")
      number = self.emit_simple(step)
      first = number if first is None else first
    assert first is not None
    self.emit_step([f"GOTO {first},{int(cycle.count) - 1}"])

  def emit_melt(self, melt: MeltCurve) -> None:
    rungs = melt_rungs(melt)
    for name, value in (("Melt start", melt.start), ("Melt end", melt.end)):
      _check_range(name + " temperature", value, MIN_TEMPERATURE, MAX_TEMPERATURE)
      _check_tenths(name + " temperature", value)
    _check_range("Melt increment", melt.increment, -MAX_INCREMENT, MAX_INCREMENT)
    _check_tenths("Melt increment", melt.increment)
    _check_whole("Melt hold", melt.hold)
    _check_range("Melt hold", melt.hold, 1, MAX_HOLD)
    start = _format_temperature(melt.start)
    rate = min(max(abs(melt.increment), MIN_RAMP_RATE), MAX_RAMP_RATE)
    self.emit_step([f"TEMP {start},{MELT_EQUILIBRATION_HOLD}"])
    rung = self.emit_step(
      [
        f"TEMP {start},{int(melt.hold)}",
        f"INC {_format_temperature(melt.increment)}",
        f"RATE {_format_temperature(rate)}",
      ]
    )
    self.emit_step([_plate_read_directive(melt.scan)])
    self.emit_step([f"GOTO {rung},{rungs - 1}"])


def compile_protocol(protocol: Protocol) -> List[str]:
  """Compile a protocol to the directives the instrument is sent, one command each.

  Returns:
    ``METHOD``, ``HOTLID`` and ``VOLUME``, the step directives, then ``END``.

  Raises:
    ProtocolError: A value is outside the instrument's limits or the structure is not expressible.
  """

  if not protocol.steps:
    raise ProtocolError("A protocol needs at least one step")
  _check_range("Volume", protocol.volume, 0, MAX_VOLUME)
  _check_whole("Volume", protocol.volume)
  _check_range("Lid temperature", protocol.lid_temperature, 0, MAX_LID_TEMPERATURE)
  _check_whole("Lid temperature", protocol.lid_temperature)

  compiler = _Compiler()
  for step in protocol.steps:
    if isinstance(step, Cycle):
      compiler.emit_cycle(step)
    elif isinstance(step, MeltCurve):
      compiler.emit_melt(step)
    else:
      compiler.emit_simple(step)
  if compiler.steps > MAX_STEPS:
    raise ProtocolError(f"Protocol has {compiler.steps} steps, more than {MAX_STEPS}")

  return [
    f"METHOD {protocol.method}",
    f"HOTLID {int(protocol.lid_temperature)},{LID_SHUTOFF_TEMPERATURE}",
    f"VOLUME {int(protocol.volume)}",
    *compiler.directives,
    "END",
  ]


def expected_plate_reads(protocol: Protocol) -> int:
  """The number of plate reads a protocol produces when it runs to completion."""

  reads = 0
  for step in protocol.steps:
    if isinstance(step, PlateRead):
      reads += 1
    elif isinstance(step, Cycle):
      reads += sum(isinstance(s, PlateRead) for s in step.steps) * int(step.count)
    elif isinstance(step, MeltCurve):
      reads += melt_rungs(step)
  return reads
