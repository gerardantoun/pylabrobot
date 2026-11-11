"""Parsing of the instrument's status replies (``STATUS?``, ``RTSTATUS?``, ``ERRORLIST``)."""

from dataclasses import dataclass
from typing import List, Literal, Tuple, cast

from pylabrobot.biorad.cfx_opus.framing import CFXProtocolError

LidPosition = Literal[
  "OPEN", "CLOSED", "OPENING", "CLOSING", "MANUAL", "STOP", "ERROR", "NONE", "UNKNOWN"
]
_LID_POSITIONS: Tuple[str, ...] = (
  "OPEN",
  "CLOSED",
  "OPENING",
  "CLOSING",
  "MANUAL",
  "STOP",
  "ERROR",
  "NONE",
  "UNKNOWN",
)

#: A run that has been started but has not yet appeared reports ``"idle"``: the instrument shows
#: nothing for the first seconds after ``RemoteRun``.
RunState = Literal["idle", "running", "paused", "finished", "aborted"]

#: Fields in one block's record of a ``STATUS?`` reply. A reply holds one record per block.
STATUS_FIELDS_PER_BLOCK = 19

# Bits of the status register (field 7).
_AT_TARGET = 1 << 0
_PAUSED = 1 << 1
_LID_PREHEATING = 1 << 3
_INCUBATING = 1 << 4
_PROTOCOL_RUNNING = 1 << 5
_BLOCK_ACTIVE = 1 << 6
_PROTOCOL_CANCELLED = 1 << 7


@dataclass(frozen=True)
class BlockStatus:
  """One block's state, from one ``STATUS?`` record.

  Temperatures are in °C and times in seconds.
  """

  block_temperature: float
  lid_temperature: float
  #: The running loop's total cycles. Only valid once the loop's ``GOTO`` has executed once.
  cycle_count: int
  #: The current cycle, 1-based; 0 when no protocol runs.
  cycle: int
  #: The current step's command text, e.g. ``"TEMP 95.0,10"``, or ``"IDLE"``.
  step_text: str
  #: The current step's 1-based number in the authored step list; 0 when no protocol runs.
  step: int
  #: The run's name as the instrument shows it: upper case, at most 8 characters.
  run_name: str
  method: str
  lid_heater_on: bool
  register: int
  elapsed_time: float
  step_elapsed_time: float
  #: The instrument's estimate of the time left. It does not tick while the lid preheats or
  #: during plate reads, so it runs out before the run ends.
  remaining_time: float
  ramp_time: float
  hold_time: float
  paused_field: int
  #: Block error codes. Empty when the block reports none.
  errors: Tuple[str, ...]
  #: Steps in the authored step list, including ``END``; 0 when no protocol runs.
  step_count: int
  #: The modelled sample temperature under method ``CALC``; the block temperature under ``BLOCK``.
  sample_temperature: float
  lid_position: LidPosition
  sensor: str

  @property
  def at_target(self) -> bool:
    """The block is at the current step's setpoint right now."""

    return bool(self.register & _AT_TARGET)

  @property
  def lid_preheating(self) -> bool:
    """The heated lid is warming up before the protocol's first step."""

    return bool(self.register & _LID_PREHEATING)

  @property
  def paused(self) -> bool:
    return bool(self.register & _PAUSED) or self.paused_field != 0

  @property
  def run_state(self) -> RunState:
    """What the block is doing, derived from the status register and the run name.

    A finished run stays attached (step ``IDLE`` with the run's name) until it is acknowledged.
    """

    if self.run_name and self.step_text == "IDLE":
      return "aborted" if self.register & _PROTOCOL_CANCELLED else "finished"
    if self.register & _PROTOCOL_RUNNING and not self.register & _BLOCK_ACTIVE:
      return "aborted" if self.register & _PROTOCOL_CANCELLED else "finished"
    if self.paused:
      return "paused"
    if self.register & (_PROTOCOL_RUNNING | _BLOCK_ACTIVE | _INCUBATING) or self.run_name:
      return "running"
    return "idle"


def _float(text: str, name: str) -> float:
  try:
    return float(text)
  except ValueError as error:
    raise CFXProtocolError(f"Status field {name} is not a number: {text!r}") from error


def _int(text: str, name: str) -> int:
  try:
    return int(text)
  except ValueError as error:
    raise CFXProtocolError(f"Status field {name} is not an integer: {text!r}") from error


def _parse_run_descriptor(text: str) -> Tuple[str, str, bool]:
  """Parse ``"<NAME>",<method>,<ON|OFF>``."""

  if not text.startswith('"'):
    raise CFXProtocolError(f"Run descriptor does not start with a quoted name: {text!r}")
  closing = text.find('"', 1)
  if closing < 0:
    raise CFXProtocolError(f"Run descriptor has an unterminated name: {text!r}")
  name = text[1:closing]
  rest = text[closing + 1 :].split(",")
  if len(rest) != 3 or rest[0] != "" or rest[2] not in ("ON", "OFF"):
    raise CFXProtocolError(f"Run descriptor is not '\"name\",method,ON|OFF': {text!r}")
  return name, rest[1], rest[2] == "ON"


def _parse_block_record(fields: List[str]) -> BlockStatus:
  name, method, lid_heater_on = _parse_run_descriptor(fields[6])
  lid_position = fields[17]
  if lid_position not in _LID_POSITIONS:
    raise CFXProtocolError(f"Unknown lid position: {lid_position!r}")
  errors = tuple(code for code in fields[14].split(":") if code not in ("", "0"))
  return BlockStatus(
    block_temperature=_float(fields[0], "block temperature"),
    lid_temperature=_float(fields[1], "lid temperature"),
    cycle_count=_int(fields[2], "cycle count"),
    cycle=_int(fields[3], "cycle"),
    step_text=fields[4],
    step=_int(fields[5], "step"),
    run_name=name,
    method=method,
    lid_heater_on=lid_heater_on,
    register=_int(fields[7], "status register"),
    elapsed_time=_float(fields[8], "elapsed time"),
    step_elapsed_time=_float(fields[9], "step elapsed time"),
    remaining_time=_float(fields[10], "remaining time"),
    ramp_time=_float(fields[11], "ramp time"),
    hold_time=_float(fields[12], "hold time"),
    paused_field=_int(fields[13], "paused"),
    errors=errors,
    step_count=_int(fields[15], "step count"),
    sample_temperature=_float(fields[16], "sample temperature"),
    lid_position=cast(LidPosition, lid_position),
    sensor=fields[18],
  )


def parse_status(value: str) -> List[BlockStatus]:
  """Parse the value of a ``STATUS?`` reply (without its result code) into one record per block."""

  fields = value.split(";")
  if len(fields) == 0 or len(fields) % STATUS_FIELDS_PER_BLOCK != 0:
    raise CFXProtocolError(
      f"STATUS? reply has {len(fields)} fields, not a multiple of {STATUS_FIELDS_PER_BLOCK}: "
      f"{value!r}"
    )
  return [
    _parse_block_record(fields[i : i + STATUS_FIELDS_PER_BLOCK])
    for i in range(0, len(fields), STATUS_FIELDS_PER_BLOCK)
  ]


@dataclass(frozen=True)
class OpticsStatus:
  """The optical head's state, from ``RTSTATUS?``. Temperatures in °C."""

  shuttle_temperature: float
  ambient_temperature: float
  #: Fault entries, ``<hex code>[,<extra>]``. Empty when healthy.
  faults: Tuple[str, ...]


def parse_optics_status(value: str) -> OpticsStatus:
  """Parse the value of an ``RTSTATUS?`` reply (without its result code)."""

  fields = value.split(";")
  if len(fields) < 2:
    raise CFXProtocolError(f"RTSTATUS? reply has fewer than 2 fields: {value!r}")
  return OpticsStatus(
    shuttle_temperature=_float(fields[0], "shuttle temperature"),
    ambient_temperature=_float(fields[1], "ambient temperature"),
    faults=tuple(fault for fault in fields[2:] if fault),
  )


def parse_error_list(value: str) -> Tuple[str, ...]:
  """Parse the value of an ``ERRORLIST`` reply (without its result code)."""

  return tuple(entry for entry in value.split(";") if entry)
