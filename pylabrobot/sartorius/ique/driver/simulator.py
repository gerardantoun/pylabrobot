"""An in-process stand-in for the iQue Screener PLUS controller.

:class:`SimulatedController` has the methods of
:class:`~pylabrobot.sartorius.ique.driver.client.ControllerClient` that the driver uses and
behaves as the controller does: devices that are disconnected until a reset and initialised
after it, the controller's own refusals (raised as
:class:`~pylabrobot.sartorius.ique.driver.client.ControllerFault`), device faults reported
through the device state instead of an error, callbacks sent before a call returns, and a
worklist runner.

The runner executes worklists on a simulated clock that runs `speed` times faster than real
time. It stamps every command with start and end times on that clock, sends the command
callbacks, and turns every sampling dip into a burst of events: Poisson counts per 5 ms tick
while the probe draws, reaching the detector after the tubing transit delay (25 s by default,
so the first burst comes more than 20 s after acquisition starts), separated by the quiet gaps
the probe's time in air leaves. Events stream as float chunks of the instrument's size with the
controller's running value count, and the controller's FCS file is written when the fluidics
stop. Everything random comes from one generator seeded with `seed`, so a run is repeatable.

What it does not model: collisions (nothing the controller would not check is checked here
either), liquid in stations and wells, the Gilson's real motion profile, the instrument's
fluid and laser status texts, firmware, demo files, and the controller's archive of finalised
worklists.
"""

from __future__ import annotations

import array
import asyncio
import dataclasses
import datetime
import decimal
import logging
import math
import random
import uuid
from typing import (
  Any,
  AsyncIterator,
  Awaitable,
  Callable,
  Dict,
  FrozenSet,
  List,
  Literal,
  Optional,
  Sequence,
  Set,
  Tuple,
  Union,
)

from pylabrobot.sartorius.ique.driver import contract as wire
from pylabrobot.sartorius.ique.driver.calibration import (
  NOMINAL_STATION_POSITIONS,
  Calibration,
  SystemKind,
  nominal_calibration,
  write_calibration,
)
from pylabrobot.sartorius.ique.driver.client import ControllerFault
from pylabrobot.sartorius.ique.driver.contract import (
  ConnectionInfo,
  ContactToleranceType,
  ContactZResult,
  ControllerServiceException,
  DeviceAlertMessage,
  DeviceConfiguration,
  DeviceInfo,
  DeviceKind,
  DeviceMessage,
  DeviceResetOptions,
  DeviceState,
  EnabledLasers,
  IQue3InstallationPrimeStep,
  IQue3PurgeStep,
  MovementRange,
  PumpDirection,
  SystemState,
  SystemType,
  Version,
  Worklist,
  WorklistCommand,
  WorklistStatus,
  WorklistStopReason,
)
from pylabrobot.sartorius.ique.driver.fcs import (
  TIME_STEP,
  EventData,
  Laser,
  ParameterTable,
  file_crc32,
  instrument_keywords,
  parameter_table_for_system_kind,
  write_fcs,
)
from pylabrobot.sartorius.ique.driver.wcf import SoapFault

logger = logging.getLogger(__name__)

SIMULATED_VERSION = Version(7, 2, 6010, 25563)
"""The controller version the simulator reports."""
SIMULATED_SYSTEM_ID = "00155D010203"
"""The system ID the simulator reports (the instrument computer's network address)."""
SIMULATED_DECK_HEIGHT = 200.0
"""Where the probe touches the plate holder in the simulator, and the simulated calibration's
deck height."""
CLOCK_START = datetime.datetime(2026, 1, 5, 8, 0, tzinfo=datetime.timezone.utc)
"""The simulated controller clock at the simulator's start."""

_SOAP_ENVELOPE = "http://www.w3.org/2003/05/soap-envelope"
_UNSET_TIME = datetime.datetime(1, 1, 1)  # a .NET DateTime that was never set
_RUNNING_END_TIME = datetime.datetime(1900, 1, 1)  # the controller's end time while running
_CHUNK_BYTES = 84000  # the cytometer forwards at most this many bytes of values per chunk
_WAIT_STEP = 0.5  # the controller polls for cancellation every 500 ms during waits
_TIME_TOLERANCE = 1e-6  # simulated seconds below which a remaining wait is nothing
_PIECE = 4096  # bytes per piece of a streamed download
_MAX_VALUE = 16_777_215.0

_X_RANGE = (0.0, 131.0)
_Y_RANGE = (0.0, 195.0)
_Z_RANGE = (190.0, 250.0)
_CONTACT_DEPTH = 3.0  # how far below z_low a contact search goes
_TOLERANCES: Dict[ContactToleranceType, str] = {
  "NewProbeAndTubing": "4",
  "DeckHeightValidation": "0.25",
  "PlateMapping": "2",
  "MicrofugeTubeValidation": "10",
}
_PUMP_SPEEDS = (160, 16000, 2860)  # hundredths of rpm: minimum, maximum, default

_MODELS: Dict[DeviceKind, str] = {
  "Sampler": "Gilson GX-271",
  "Pump": "Ismatec REGLO Digital",
  "Shaker": "BioShake 3000 elm",
  "Cytometer": "iQue3 Cytometer",
  "PlateRail": "iQue3 Rail 1.5",
  "ContactSensor": "IntelliCyt Smart Probe",
}
_DEFAULT_DEVICES: Tuple[DeviceKind, ...] = ("Sampler", "Pump", "Shaker", "Cytometer")
_OPTIONAL_DEVICES: FrozenSet[DeviceKind] = frozenset(
  ("Shaker", "Cytometer", "PlateRail", "ContactSensor")
)
_DEVICE_ORDER: Tuple[DeviceKind, ...] = (
  "Sampler",
  "Pump",
  "Shaker",
  "Cytometer",
  "PlateRail",
  "ContactSensor",
)
_PLUS_TYPES: FrozenSet[str] = frozenset(("iQue3", "iQue3HD", "iQuePlusVYB", "iQuePlusBR"))

_LASERS: Dict[EnabledLasers, FrozenSet[Laser]] = {
  "VioletBlueRed": frozenset(("violet", "blue", "red")),
  "VioletBlue": frozenset(("violet", "blue")),
  "BlueRed": frozenset(("blue", "red")),
  "Blue": frozenset(("blue",)),
  "VioletYellowBlue": frozenset(("violet", "yellow", "blue")),
  "VioletYellow": frozenset(("violet", "yellow")),
  "YellowBlue": frozenset(("yellow", "blue")),
  "Yellow": frozenset(("yellow",)),
}
_INSTRUMENT_NAMES: Dict[str, str] = {
  "iQue3": "iQue Screener PLUS - VBR",
  "iQue3HD": "iQue Screener PLUS HD - VBR",
  "iQuePlusVYB": "iQue Screener PLUS - VYB",
  "iQuePlusBR": "iQue Screener PLUS - BR",
}
# Seconds each cleaning method takes, by its name in lower case.
_CLEAN_DURATIONS: Dict[str, float] = {
  "dailyclean": 600.0,
  "quickclean": 660.0,
  "longclean": 1800.0,
  "unclog": 630.0,
  "debbubble": 480.0,
  "priming": 780.0,
  "shutdown": 30.0,
}
_PURGE_STEPS: Tuple[IQue3PurgeStep, ...] = ("FlushSystem", "PurgeSystem", "PurgeSensor")
_PRIMING_STEPS: Tuple[IQue3InstallationPrimeStep, ...] = (
  "Start",
  "PrimingSystem",
  "PrimingSensor",
  "Priming",
  "End",
)
_STEP_DURATION = 60.0  # simulated seconds a purge or priming step takes

Executor = Callable[[Any], Awaitable[Tuple[bool, str]]]


def controller_fault(operation: str, message: str) -> ControllerFault:
  """Return the error the client raises when the controller refuses a call.

  Args:
    operation: The operation's name, such as ``Worklist_Run``.
    message: The controller's message.
  """
  fault = SoapFault(
    code=(_SOAP_ENVELOPE, "Sender"),
    subcodes=(),
    reason=message,
    detail=None,
    action=f"http://tempuri.org/IController/{operation}ControllerServiceExceptionFault",
  )
  detail = ControllerServiceException(
    inner_exception=None, message=message, type="System.Exception"
  )
  return ControllerFault(operation, fault, detail)


def simulated_calibration(
  system_kind: SystemKind = "iQue3",
  deck_height: float = SIMULATED_DECK_HEIGHT,
  device_id: Optional[str] = SIMULATED_SYSTEM_ID,
) -> Calibration:
  """Return a calibration as if every position had been taught at the nominal places.

  Deck height `deck_height`, travel height at the top of Z, plate corners at the nominal
  positions, stations at their nominal positions with dip depths of 6 to 8 mm above the deck,
  the tube holder 3 mm above it, and the generic plate models with a taught depth of 4 mm. It
  suits the simulator; on an instrument it would miss wells.

  Args:
    system_kind: The instrument family member.
    deck_height: The deck height.
    device_id: The system ID the file names.
  """
  taught = datetime.datetime(2026, 1, 5, 7, 0, tzinfo=datetime.timezone.utc)
  base = nominal_calibration(system_kind, name="Simulated iQue", device_id=device_id)
  instrument = dataclasses.replace(
    base.instrument, deck_height=deck_height, deck_height_calibrated_on=taught
  )
  depths = {"Buffer": 8.0, "Markers": 8.0, "Decon": 6.0, "Clean": 6.0, "Water": 8.0, "Tube": 3.0}
  stations = tuple(
    dataclasses.replace(station, depth=depths[station.name], calibrated_on=taught)
    if station.name in NOMINAL_STATION_POSITIONS
    else station
    for station in base.stations
  )
  plate_types = tuple(
    dataclasses.replace(entry, calibrated_on=taught)
    if entry.plate_type in ("Well96", "Well384", "Well1536")
    else entry
    for entry in base.plate_types
  )
  models = tuple(dataclasses.replace(model, well_depth_offset=4.0) for model in base.plate_models)
  return dataclasses.replace(
    base, instrument=instrument, stations=stations, plate_types=plate_types, plate_models=models
  )


class _WorklistError(Exception):
  """An executor failed: the worklist ends with stop reason WorklistError."""


class _Acquisition:
  """The data of one acquisition: events waiting to arrive, events recorded, the stream."""

  def __init__(self, table: ParameterTable, start: float, delay: float, maximum: int) -> None:
    """Start an acquisition at a simulated time.

    Args:
      table: The instrument's parameters.
      start: Simulated time at which recording started.
      delay: Seconds of events to drop at the start.
      maximum: Event count at which the cytometer reports the event limit.
    """
    self.table = table
    self.start = start
    self.delay_ticks = round(delay / TIME_STEP)
    self.maximum = maximum
    self.threshold = 2_147_483_647
    self.recording = True
    self.values = array.array("f")
    self.count = 0
    self.unsent_from = 0


class SimulatedController:
  """An in-process controller with the methods of ControllerClient that the driver uses.

  Attributes:
    calls: Every operation called, in order, as (name, arguments); tests read it.
    calibration_file: The stored calibration file, or None.
    deck_height: The height at which the probe touches the plate holder while the rail is in.
  """

  def __init__(
    self,
    *,
    system_type: SystemType = "iQue3",
    seed: int = 0,
    speed: float = 1.0,
    version: Optional[Version] = SIMULATED_VERSION,
    system_id: str = SIMULATED_SYSTEM_ID,
    calibration: Union[bytes, Literal["simulated"], None] = "simulated",
    detector_powered: bool = True,
    other_client_connected: bool = False,
    system_id_mismatch: bool = False,
    transit_delay: float = 25.0,
    events_per_second: float = 400.0,
    noise_per_second: float = 0.2,
    deck_height: float = SIMULATED_DECK_HEIGHT,
    drop_data_chunks: Sequence[int] = (),
    drop_worklist_completed: bool = False,
  ) -> None:
    """Create the simulator.

    Args:
      system_type: The system type it reports.
      seed: Seed of every random choice.
      speed: How many simulated seconds pass per real second during a worklist.
      version: The controller version it reports.
      system_id: The system ID it reports.
      calibration: The stored calibration file: ``"simulated"`` for
        :func:`simulated_calibration`, None for none.
      detector_powered: Whether the detector is on at the start.
      other_client_connected: Report another registered client at connect.
      system_id_mismatch: Make the device reset find that its stored system ID is stale.
      transit_delay: Seconds the sample takes from the probe to the detector.
      events_per_second: Event rate while the probe draws from a well, before each well's own
        random factor.
      noise_per_second: Rate of stray events throughout an acquisition.
      deck_height: Height of the plate holder's surface while the rail is in.
      drop_data_chunks: Indices of event chunks whose callback is lost, as when the client is
        too slow; the running value count still advances.
      drop_worklist_completed: Lose the worklist-completed callback.
    """
    self.calls: List[Tuple[str, Tuple[Any, ...]]] = []
    self._system_type = system_type
    self._rng = random.Random(seed)
    self.speed = speed
    self._version = version
    self._system_id = system_id
    if calibration == "simulated":
      kind: SystemKind = system_type
      calibration = write_calibration(simulated_calibration(kind, deck_height, system_id))
    self.calibration_file: Optional[bytes] = calibration
    self._other_client = other_client_connected
    self._system_id_mismatch = system_id_mismatch
    self.transit_delay = transit_delay
    self.events_per_second = events_per_second
    self.noise_per_second = noise_per_second
    self.deck_height = deck_height
    self._drop_chunks: Set[int] = set(drop_data_chunks)
    self._drop_worklist_completed = drop_worklist_completed
    self._table = parameter_table_for_system_kind(system_type)

    self._open = False
    self._registered = False
    self._client_offset = datetime.timedelta(0)
    self._handlers: Dict[str, Callable[..., None]] = {}
    self._virtual = 0.0
    self._pace_origin: Optional[float] = None
    self._demo = False
    self.verbose_logging = True

    self._configured: List[DeviceKind] = list(_DEFAULT_DEVICES)
    self._states: Dict[DeviceKind, DeviceState] = {kind: "Disconnected" for kind in _DEVICE_ORDER}
    self._messages: Dict[DeviceKind, List[DeviceMessage]] = {kind: [] for kind in _DEVICE_ORDER}
    self._system_state: SystemState = "Red"
    self._resetting = False

    self._x, self._y, self._z = _X_RANGE[1], _Y_RANGE[1], _Z_RANGE[1]
    self._contact = False
    self._contact_changes = 0
    self._pump_speed = 0
    self._pump_direction: PumpDirection = "CW"
    hd = system_type == "iQue3HD"
    self._shaker_range = (200, 5000) if hd else (100, 3000)
    self._shaker_rpm = 0
    self._lock_closed = True
    self._rail: Optional[Literal["loaded", "ejected"]] = None

    self._powered = detector_powered
    self._powering_on = False
    self._lasers: FrozenSet[Laser] = frozenset()
    self._leak = False
    self._operation_end = 0.0
    self._purge: Tuple[IQue3PurgeStep, bool] = ("None", True)
    self._priming: Tuple[IQue3InstallationPrimeStep, bool] = ("None", True)
    self._acquisition: Optional[_Acquisition] = None
    self._event_limit = False
    self._user_name: Optional[str] = None
    self._metadata: Dict[str, str] = {}
    self._fcs_file: Optional[bytes] = None
    self._fcs_file_name: Optional[str] = None
    self._values_sent = 0
    self._chunks_sent = 0
    self._pending: List[Tuple[float, List[float]]] = []
    self._fluidics_started: Optional[float] = None
    self._fluidics_minutes = 0.0
    self._pump_started: Optional[float] = None
    self._pump_minutes = 0.0

    self._worklist_status: WorklistStatus = "NoActiveWorklist"
    self._worklist: Optional[Worklist] = None
    self._cancel = False
    self._runner: Optional[asyncio.Task[None]] = None
    self._background: Set[asyncio.Task[None]] = set()
    self._executors: Dict[type, Executor] = {
      wire.SampleWellCommand: self._sample_well,
      wire.BeadSipCommand: self._sample_well,
      wire.SampleTestTubeCommand: self._sample_test_tube,
      wire.SampleVialCommand: self._sample_vial,
      wire.SampleWellContinuouslyCommand: self._sample_continuously,
      wire.SampleContinuouslyCommand: self._sample_continuously,
      wire.RinseCommand: self._rinse,
      wire.CyclingRinseCommand: self._cycling_rinse,
      wire.TimedContinuousRinseCommand: self._timed_continuous_rinse,
      wire.TimedCyclingRinseCommand: self._timed_cycling_rinse,
      wire.FlushCommand: self._timed_cycling_rinse,
      wire.FlushMatryoshkaCommand: self._flush_and_eject,
      wire.StartPumpCommand: self._start_pump,
      wire.StopPumpCommand: self._stop_pump,
      wire.StartShakerCommand: self._start_shaker,
      wire.StopShakerCommand: self._stop_shaker,
      wire.TimedShakeCommand: self._timed_shake,
      wire.CombinedShakeRinseCommand: self._combined_shake_rinse,
      wire.CombinedShakeRinseAtLocationCommand: self._combined_shake_rinse,
      wire.ValidateDeckHeightCommand: self._validate_deck_height,
      wire.StartFlowCytometerFluidicsCommand: self._start_fluidics,
      wire.StartMatryoshkaFluidicsCommand: self._start_fluidics_and_load,
      wire.StopFlowCytometerFluidicsCommand: self._stop_fluidics_command,
      wire.RunFlowCytometerFluidicsForDurationCommand: self._fluidics_for_duration,
      wire.FlowCytometerAcquireCommand: self._acquire,
      wire.FlowCytometerStopAcquireCommand: self._stop_acquire,
      wire.CleanFlowCytometerCommand: self._clean,
      wire.ShutdownFlowCytometerCommand: self._shutdown_command,
    }

  # -------------------------------------------------------------------------------------------
  # Session, clock and callbacks
  # -------------------------------------------------------------------------------------------

  @property
  def is_open(self) -> bool:
    """Whether the session is open."""
    return self._open

  async def open(self) -> None:
    """Open the session."""
    self.calls.append(("open", ()))
    self._open = True

  async def close(self) -> None:
    """End the session; the controller then sends no more callbacks to it. A running worklist
    keeps running, as on the instrument."""
    self.calls.append(("close", ()))
    self._open = False
    self._registered = False

  async def shutdown(self) -> None:
    """Stop the worklist runner and background tasks; for the end of a test."""
    tasks = list(self._background) + ([self._runner] if self._runner is not None else [])
    for task in tasks:
      task.cancel()
    for task in tasks:
      try:
        await task
      except asyncio.CancelledError:
        pass  # the task was cancelled just above

  def _now(self) -> datetime.datetime:
    """Return the controller's clock."""
    return CLOCK_START + datetime.timedelta(seconds=self._virtual)

  def _emit(self, callback: str, *arguments: Any) -> None:
    """Send a callback to the registered session, as the controller does, if there is one.

    A handler's error is logged, as the transport does, and does not affect the controller.
    """
    if not (self._open and self._registered):
      return
    handler = self._handlers.get(callback)
    if handler is None:
      return
    try:
      handler(*arguments)
    except Exception:  # the transport logs a handler's error and carries on; so does this
      logger.exception("the handler for %s raised", callback)

  def _set_handler(self, callback: str, handler: Optional[Callable[..., None]]) -> None:
    """Set or remove a callback handler."""
    if handler is None:
      self._handlers.pop(callback, None)
    else:
      self._handlers[callback] = handler

  def on_test_connection(self, handler: Optional[Callable[[], None]]) -> None:
    """Set the handler of TestConnection."""
    self._set_handler("TestConnection", handler)

  def on_cytometer_has_uint_fcs_data(
    self, handler: Optional[Callable[[Optional[array.array[int]], int], None]]
  ) -> None:
    """Set the handler of Cytometer_OnHasUIntFCSData; this model never sends it."""
    self._set_handler("Cytometer_OnHasUIntFCSData", handler)

  def on_cytometer_has_float_fcs_data(
    self, handler: Optional[Callable[[Optional[array.array[float]], int], None]]
  ) -> None:
    """Set the handler of Cytometer_OnHasFloatFCSData."""
    self._set_handler("Cytometer_OnHasFloatFCSData", handler)

  def on_cytometer_long_running_operation_completed(
    self, handler: Optional[Callable[[Optional[str]], None]]
  ) -> None:
    """Set the handler of OnCytometerLongRunningOperationCompleted."""
    self._set_handler("OnCytometerLongRunningOperationCompleted", handler)

  def on_system_state_changed(self, handler: Optional[Callable[[SystemState], None]]) -> None:
    """Set the handler of OnSystemStateChanged."""
    self._set_handler("OnSystemStateChanged", handler)

  def on_device_state_changed(
    self, handler: Optional[Callable[[Optional[DeviceInfo]], None]]
  ) -> None:
    """Set the handler of OnDeviceStateChanged."""
    self._set_handler("OnDeviceStateChanged", handler)

  def on_contact_sensor_state_changed(self, handler: Optional[Callable[[bool], None]]) -> None:
    """Set the handler of OnContactSensorStateChanged."""
    self._set_handler("OnContactSensorStateChanged", handler)

  def on_device_alert(
    self, handler: Optional[Callable[[Optional[DeviceAlertMessage]], None]]
  ) -> None:
    """Set the handler of OnDeviceAlert."""
    self._set_handler("OnDeviceAlert", handler)

  def on_command_started(
    self, handler: Optional[Callable[[Optional[WorklistCommand]], None]]
  ) -> None:
    """Set the handler of OnCommandStarted."""
    self._set_handler("OnCommandStarted", handler)

  def on_command_completed(
    self, handler: Optional[Callable[[Optional[WorklistCommand]], None]]
  ) -> None:
    """Set the handler of OnCommandCompleted."""
    self._set_handler("OnCommandCompleted", handler)

  def on_worklist_completed(self, handler: Optional[Callable[[Optional[Worklist]], None]]) -> None:
    """Set the handler of OnWorklistCompleted."""
    self._set_handler("OnWorklistCompleted", handler)

  def _record(self, operation: str, *arguments: Any) -> None:
    """Log a call, and refuse it unless the session is open and registered.

    Raises:
      ControllerFault: No client has connected, as the controller answers.
    """
    self.calls.append((operation, arguments))
    if not self._open:
      raise ConnectionError(f"{operation}: the session is not open")
    if not self._registered:
      raise controller_fault(operation, "Client must call connect before using other API methods.")

  def _spawn(self, work: Awaitable[None]) -> None:
    """Run a piece of the controller's background work."""
    task: asyncio.Task[None] = asyncio.ensure_future(work)
    self._background.add(task)
    task.add_done_callback(self._background.discard)

  async def _sleep(self, seconds: float) -> None:
    """Let simulated seconds pass in real time scaled by the speed."""
    await asyncio.sleep(seconds / self.speed)

  async def _pace(self) -> None:
    """Keep the runner's simulated clock in step with real time scaled by the speed.

    Sleeping is measured against where real time should be, so that a coarse timer only makes
    the sleeps fewer and longer, not the run slower.
    """
    loop = asyncio.get_running_loop()
    if self._pace_origin is None:
      self._pace_origin = loop.time() - self._virtual / self.speed
    delay = self._pace_origin + self._virtual / self.speed - loop.time()
    await asyncio.sleep(max(0.0, delay))

  # -------------------------------------------------------------------------------------------
  # Device state
  # -------------------------------------------------------------------------------------------

  def _info(self, kind: DeviceKind) -> DeviceInfo:
    """Return a device's state as the controller reports it."""
    state = self._states[kind] if kind in self._configured else "Disconnected"
    connected = state != "Disconnected"
    if kind == "Cytometer" and not connected:
      text = "Powering Device" if self._powering_on else "Device Offline"
      messages = [DeviceMessage(detail="", message=text, message_level="Error")]
    elif not connected:
      messages = [DeviceMessage(detail="", message="Device Disconnected", message_level="Error")]
    else:
      messages = list(self._messages[kind])
    return DeviceInfo(
      kind=kind,
      messages=list(messages),
      model=_MODELS[kind] if connected else "",
      state=state,
    )

  def _update_system_state(self) -> None:
    """Recompute the system state and report a change."""
    red = any(self._states[kind] in ("Disconnected", "Faulted") for kind in self._configured)
    warning = any(
      message.message_level == "Warning"
      for kind in self._configured
      for message in self._messages[kind]
    )
    state: SystemState = "Red" if red else ("Yellow" if self._resetting or warning else "Green")
    if state != self._system_state:
      self._system_state = state
      self._emit("OnSystemStateChanged", state)

  def _set_state(
    self, kind: DeviceKind, state: DeviceState, messages: Optional[List[DeviceMessage]] = None
  ) -> None:
    """Change a device's state or messages and report it."""
    changed = self._states[kind] != state or (
      messages is not None and messages != self._messages[kind]
    )
    self._states[kind] = state
    if messages is not None:
      self._messages[kind] = messages
    if changed and kind in self._configured:
      self._emit("OnDeviceStateChanged", self._info(kind))
    self._update_system_state()

  def fault_device(self, kind: DeviceKind, message: str) -> None:
    """Fault a device as a failed internal command would; for tests.

    Args:
      kind: The device.
      message: The fault message.
    """
    self._set_state(
      kind, "Faulted", [DeviceMessage(detail="", message=message, message_level="Error")]
    )

  def set_contact(self, has_contact: bool) -> None:
    """Set the probe's contact sensor and report the change; for tests."""
    if has_contact != self._contact:
      self._contact = has_contact
      self._contact_changes += 1
      self._emit("OnContactSensorStateChanged", has_contact)

  def raise_alert(self, kind: DeviceKind, alert: wire.DeviceAlert) -> None:
    """Report a device alert; for tests."""
    self._emit("OnDeviceAlert", DeviceAlertMessage(alert=alert, kind=kind))

  def set_leak(self, leak: bool) -> None:
    """Make the detector report a leak, which makes it refuse to shut down; for tests."""
    self._leak = leak

  def _ready(self, kind: DeviceKind) -> bool:
    """Whether a configured device is ready."""
    return kind in self._configured and self._states[kind] == "Ready"

  def _state(self, kind: DeviceKind) -> Optional[DeviceState]:
    """A configured device's state, or None for a device not configured."""
    return self._states[kind] if kind in self._configured else None

  def _monitored_red(self) -> bool:
    """Whether a device the running worklist monitors is missing, disconnected or faulted."""
    worklist = self._worklist
    kinds = list(worklist.devices_to_monitor or []) if worklist is not None else []
    if not kinds:
      return self._system_state == "Red"
    return any(
      kind not in self._configured or self._states[kind] in ("Disconnected", "Faulted")
      for kind in kinds
    )

  def _should_stop(self) -> bool:
    """Whether a cancellable wait of the runner ends early: cancel, or a monitored fault."""
    return self._cancel or self._monitored_red()

  # -------------------------------------------------------------------------------------------
  # Motion and devices, as the device layer does them
  # -------------------------------------------------------------------------------------------

  def _surface(self) -> Optional[float]:
    """The height of what the probe would touch at its XY: the deck while the rail is in."""
    return self.deck_height if self._rail == "loaded" else None

  def _move_z(self, z: float) -> None:
    """Move the probe in Z; it stops on a surface and the contact sensor reports it."""
    surface = self._surface()
    if surface is not None and z < surface:
      self._z = surface
      self.set_contact(True)
    else:
      self._z = z
      if surface is None or z > surface + 0.5:
        self.set_contact(False)

  def _park(self) -> None:
    """Park the probe: Z to the top, then XY to the largest X and Y; refused on contact."""
    if self._contact:
      self.fault_device("Sampler", "Park Faulted")
      return
    self._move_z(_Z_RANGE[1])
    self._x, self._y = _X_RANGE[1], _Y_RANGE[1]

  def _xy_time(self, x: float, y: float, fast: bool) -> float:
    """Simulated seconds an XY move takes."""
    distance = math.hypot(x - self._x, y - self._y)
    return 0.15 + distance / (250.0 if fast else 100.0)

  def _pump_start(self, speed: int, direction: PumpDirection) -> None:
    """Start the pump when it stands still; the speed is clamped."""
    if self._state("Pump") != "Ready":
      return
    self._pump_speed = max(_PUMP_SPEEDS[0], min(_PUMP_SPEEDS[1], speed))
    self._pump_direction = direction
    self._pump_started = self._virtual
    self._set_state("Pump", "Running")

  def _pump_update(self, speed: int, direction: PumpDirection) -> None:
    """Change the speed of the running pump; the speed is clamped."""
    if self._state("Pump") != "Running":
      return
    self._pump_speed = max(_PUMP_SPEEDS[0], min(_PUMP_SPEEDS[1], speed))
    self._pump_direction = direction

  def _pump_stop(self) -> None:
    """Stop the running pump."""
    if self._state("Pump") != "Running":
      return
    if self._pump_started is not None:
      self._pump_minutes = (self._virtual - self._pump_started) / 60
    self._set_state("Pump", "Ready")

  def _confirm_pump_running(self) -> None:
    """Fault the pump unless it runs, as its inquiry does."""
    if self._state("Pump") != "Running":
      self.fault_device("Pump", "Confirm Running Faulted")

  def _shaker_start(self, rpm: int) -> None:
    """Start the shaker or change its speed; the speed is clamped."""
    if self._state("Shaker") not in ("Ready", "Running"):
      return
    self._shaker_rpm = max(self._shaker_range[0], min(self._shaker_range[1], rpm))
    self._set_state("Shaker", "Running")

  def _shaker_stop(self) -> None:
    """Stop the shaker at its home position."""
    if self._state("Shaker") != "Running":
      return
    self._shaker_rpm = 0
    self._set_state("Shaker", "Ready")

  def _rail_move(self, position: Literal["loaded", "ejected"]) -> None:
    """Move the plate rail when it is ready."""
    if self._state("PlateRail") != "Ready":
      return
    self._rail = position
    surface = self._surface()
    if surface is None or self._z > surface + 0.5:
      self.set_contact(False)

  def _close_lock_and_load(self) -> None:
    """Close the plate lock, then load the rail, as the controller does before a plate."""
    if self._state("Shaker") == "Ready":
      self._lock_closed = True
    self._rail_move("loaded")

  # -------------------------------------------------------------------------------------------
  # Connection, identity, device management
  # -------------------------------------------------------------------------------------------

  async def controller_connect(
    self, client_time: datetime.datetime, *, timeout: Optional[float] = None
  ) -> Optional[ConnectionInfo]:
    """Register the session unless another client is registered."""
    self.calls.append(("controller_connect", (client_time,)))
    if not self._open:
      raise ConnectionError("controller_connect: the session is not open")
    comments = "[WorklistIsRunning]" if self._worklist_status == "RunningWorklist" else ""
    if self._other_client:
      return ConnectionInfo(
        comments=comments, is_client_already_connected=True, version=self._version
      )
    self._registered = True
    self._client_offset = client_time - self._now()
    return ConnectionInfo(
      comments=comments, is_client_already_connected=False, version=self._version
    )

  async def controller_set_verbose_logging(
    self, verbose_logging: bool, *, timeout: Optional[float] = None
  ) -> None:
    """Set the log level."""
    self._record("controller_set_verbose_logging", verbose_logging)
    self.verbose_logging = verbose_logging

  async def controller_enable_demo_mode(
    self, demo_mode: bool, *, timeout: Optional[float] = None
  ) -> None:
    """Switch demo mode."""
    self._record("controller_enable_demo_mode", demo_mode)
    self._demo = demo_mode

  async def controller_get_system_id(
    self, force_refresh: bool, *, timeout: Optional[float] = None
  ) -> Optional[str]:
    """Return the system ID."""
    self._record("controller_get_system_id", force_refresh)
    return self._system_id

  async def controller_determine_system_type(
    self, *, timeout: Optional[float] = None
  ) -> SystemType:
    """Return the system type."""
    self._record("controller_determine_system_type")
    return self._system_type

  async def controller_get_summary(self, *, timeout: Optional[float] = None) -> Optional[str]:
    """Return the controller computer's summary."""
    self._record("controller_get_summary")
    return "Simulated controller (no operating system), 64 bit, .NET 4.8"

  async def controller_get_device_summary(
    self, device_kind: DeviceKind, *, timeout: Optional[float] = None
  ) -> Optional[str]:
    """Return a device's summary."""
    self._record("controller_get_device_summary", device_kind)
    if self._state(device_kind) in (None, "Disconnected"):
      return f"{device_kind.upper()} OFFLINE"
    if device_kind == "Cytometer":
      lasers = ", ".join(f"{laser} laser" for laser in self._table.lasers)
      return f"Model: {_MODELS['Cytometer']}; Serial: SIM-0001; Lasers: {lasers}"
    return _MODELS[device_kind]

  async def controller_get_system_state(self, *, timeout: Optional[float] = None) -> SystemState:
    """Return the system state."""
    self._record("controller_get_system_state")
    return self._system_state

  async def controller_configure_devices(
    self,
    configuration: Optional[Dict[DeviceKind, Optional[DeviceConfiguration]]],
    *,
    timeout: Optional[float] = None,
  ) -> None:
    """Add or remove the optional devices; refused while a worklist runs."""
    self._record("controller_configure_devices", configuration)
    if self._worklist_status == "RunningWorklist":
      raise controller_fault("Controller_ConfigureDevices", "A worklist is running")
    for kind, entry in (configuration or {}).items():
      if kind not in _OPTIONAL_DEVICES or entry is None:
        continue
      if entry.include_this_device and kind not in self._configured:
        self._configured.append(kind)
        self._states[kind] = "Disconnected"
      elif not entry.include_this_device and kind in self._configured:
        self._configured.remove(kind)
    self._update_system_state()

  async def controller_reset_devices(
    self, options: Optional[DeviceResetOptions], *, timeout: Optional[float] = None
  ) -> bool:
    """Reconnect every configured device; False when the stored system ID is stale."""
    self._record("controller_reset_devices", options)
    if self._worklist_status == "RunningWorklist":
      raise controller_fault(
        "Controller_ResetDevices",
        "There is currently a running worklist that prevents devices from resetting",
      )
    self._resetting = True
    self._update_system_state()
    for kind in self._configured:
      if kind == "Cytometer" and not self._powered:
        self._set_state(kind, "Disconnected", [])
      else:
        self._set_state(kind, "Ready", [])
    self._resetting = False
    if self._system_id_mismatch:
      self._system_id_mismatch = False
      self._set_state("Sampler", "Disconnected", [])
      return False
    self._update_system_state()
    return True

  async def controller_initialize_devices(
    self, device_initialization_list: Optional[List[DeviceKind]], *, timeout: Optional[float] = None
  ) -> None:
    """Initialise devices: park the probe, stop the pump, home the shaker, home and eject the
    rail."""
    self._record("controller_initialize_devices", device_initialization_list)
    kinds = list(device_initialization_list or []) or [
      kind for kind in self._configured if self._states[kind] != "Disconnected"
    ]
    for kind in kinds:
      if self._states[kind] == "Disconnected":
        continue
      if kind == "Sampler":
        self._park()
      elif kind == "Pump":
        self._pump_stop()
      elif kind == "Shaker":
        self._shaker_stop()
        self._lock_closed = True
      elif kind == "PlateRail":
        self._rail = "ejected"

  async def controller_get_device_statuses(
    self, *, timeout: Optional[float] = None
  ) -> Optional[List[Optional[DeviceInfo]]]:
    """Return every configured device's state."""
    self._record("controller_get_device_statuses")
    return [self._info(kind) for kind in _DEVICE_ORDER if kind in self._configured]

  # -------------------------------------------------------------------------------------------
  # Detector
  # -------------------------------------------------------------------------------------------

  async def _power_on_scan(self) -> None:
    """Look for the detector after its power relay was pulsed."""
    await self._sleep(10.0)
    self._powered = True
    self._powering_on = False
    if "Cytometer" in self._configured:
      self._set_state("Cytometer", "Ready", [])

  async def controller_power_on_cytometer(self, *, timeout: Optional[float] = None) -> None:
    """Pulse the power relay and look for the detector in the background."""
    self._record("controller_power_on_cytometer")
    if self._demo:
      raise controller_fault("Controller_PowerOnCytometer", "Not supported in demo mode")
    if self._state("PlateRail") in (None, "Disconnected"):
      raise controller_fault("Controller_PowerOnCytometer", "No system controller available")
    if self._powered or self._powering_on:
      return
    self._powering_on = True
    self._spawn(self._power_on_scan())

  async def controller_cytometer_has_power(self, *, timeout: Optional[float] = None) -> bool:
    """Whether the detector is connected."""
    self._record("controller_cytometer_has_power")
    return self._state("Cytometer") not in (None, "Disconnected")

  async def cytometer_is_powering_on(self, *, timeout: Optional[float] = None) -> bool:
    """Whether the power-on scan runs."""
    self._record("cytometer_is_powering_on")
    return self._powering_on

  async def cytometer_is_ready_to_run(self, *, timeout: Optional[float] = None) -> bool:
    """Whether a measurement can start."""
    self._record("cytometer_is_ready_to_run")
    return self._ready("Cytometer") and self._purge[0] == "None" and self._priming[0] == "None"

  async def cytometer_is_ready_to_clean(self, *, timeout: Optional[float] = None) -> bool:
    """Whether fluidics maintenance can start."""
    self._record("cytometer_is_ready_to_clean")
    return self._ready("Cytometer")

  async def cytometer_get_operation_duration(
    self, *, timeout: Optional[float] = None
  ) -> datetime.timedelta:
    """Remaining time of the detector's operation."""
    self._record("cytometer_get_operation_duration")
    return datetime.timedelta(seconds=max(0.0, self._operation_end - self._virtual))

  def _power_off(self) -> None:
    """Switch the detector off."""
    self._powered = False
    self._lasers = frozenset()
    if "Cytometer" in self._configured:
      self._set_state("Cytometer", "Disconnected", [])

  async def cytometer_shutdown(self, *, timeout: Optional[float] = None) -> None:
    """Shut the detector down; refused on a leak."""
    self._record("cytometer_shutdown")
    if self._leak:
      raise controller_fault("Cytometer_Shutdown", "Unable to shutdown: low pressure detected")
    if self._state("Cytometer") in (None, "Disconnected"):
      return
    self._power_off()

  async def _report_long_running(self, error: str) -> None:
    """Report the end of a long-running detector operation after a second."""
    await self._sleep(1.0)
    self._emit("OnCytometerLongRunningOperationCompleted", error)

  async def cytometer_calibrate_fluid_stations(self, *, timeout: Optional[float] = None) -> None:
    """Calibrate the bottle station; the result arrives as a callback."""
    self._record("cytometer_calibrate_fluid_stations")
    error = "" if self._ready("Cytometer") else "The cytometer is not connected"
    self._spawn(self._report_long_running(error))

  async def cytometer_reset_consumables_odometer(self, *, timeout: Optional[float] = None) -> None:
    """Clear the consumables odometer; the result arrives as a callback."""
    self._record("cytometer_reset_consumables_odometer")
    error = "" if self._ready("Cytometer") else "The cytometer is not connected"
    self._spawn(self._report_long_running(error))

  async def _complete_step(self, kind: Literal["purge", "priming"]) -> None:
    """Complete the current purge or priming step after its duration."""
    await self._sleep(_STEP_DURATION)
    if kind == "purge":
      self._purge = (self._purge[0], True)
      if self._purge[0] == _PURGE_STEPS[-1]:
        self._power_off()
    else:
      self._priming = (self._priming[0], True)
      if self._priming[0] == _PRIMING_STEPS[-1]:
        self._priming = ("None", True)
        self.raise_alert("Cytometer", "None")

  async def ique3_run_next_purge_step(self, *, timeout: Optional[float] = None) -> None:
    """Start the next purge step; the previous one must be completed."""
    self._record("ique3_run_next_purge_step")
    step, completed = self._purge
    if not completed:
      raise controller_fault("IQue3_RunNextPurgeStep", "The current purge step is not complete")
    if step == _PURGE_STEPS[-1]:
      raise controller_fault("IQue3_RunNextPurgeStep", "The purge is complete")
    index = 0 if step == "None" else _PURGE_STEPS.index(step) + 1
    self._purge = (_PURGE_STEPS[index], False)
    self._operation_end = self._virtual + _STEP_DURATION
    self.raise_alert("Cytometer", "CYT_PurgingFluidics")
    self._spawn(self._complete_step("purge"))

  async def ique3_get_current_purge_step(
    self, *, timeout: Optional[float] = None
  ) -> Tuple[IQue3PurgeStep, bool]:
    """Return the purge step and whether it is completed."""
    self._record("ique3_get_current_purge_step")
    return self._purge

  async def ique3_run_next_installation_priming_step(
    self, *, timeout: Optional[float] = None
  ) -> None:
    """Start the next installation priming step; the previous one must be completed."""
    self._record("ique3_run_next_installation_priming_step")
    step, completed = self._priming
    if not completed:
      raise controller_fault(
        "IQue3_RunNextInstallationPrimingStep", "The current priming step is not complete"
      )
    index = 0 if step == "None" else _PRIMING_STEPS.index(step) + 1
    self._priming = (_PRIMING_STEPS[index], False)
    self._operation_end = self._virtual + _STEP_DURATION
    self.raise_alert("Cytometer", "CYT_InstallationPriming")
    self._spawn(self._complete_step("priming"))

  async def ique3_get_current_installation_priming_step(
    self, *, timeout: Optional[float] = None
  ) -> Tuple[IQue3InstallationPrimeStep, bool]:
    """Return the installation priming step and whether it is completed."""
    self._record("ique3_get_current_installation_priming_step")
    return self._priming

  # -------------------------------------------------------------------------------------------
  # Direct operations
  # -------------------------------------------------------------------------------------------

  async def contact_sensor_has_contact(self, *, timeout: Optional[float] = None) -> bool:
    """Read the contact sensor."""
    self._record("contact_sensor_has_contact")
    return self._contact if "ContactSensor" in self._configured else False

  async def plate_rail_load(self, *, timeout: Optional[float] = None) -> None:
    """Move the rail in; the controller checks neither the probe nor a plate."""
    self._record("plate_rail_load")
    self._rail_move("loaded")

  async def plate_rail_eject(self, *, timeout: Optional[float] = None) -> None:
    """Move the rail out; the controller does not check the probe."""
    self._record("plate_rail_eject")
    self._rail_move("ejected")

  async def pump_get_min_speed(self, *, timeout: Optional[float] = None) -> int:
    """The pump's lowest speed, in hundredths of rpm."""
    self._record("pump_get_min_speed")
    return _PUMP_SPEEDS[0]

  async def pump_get_max_speed(self, *, timeout: Optional[float] = None) -> int:
    """The pump's highest speed, in hundredths of rpm."""
    self._record("pump_get_max_speed")
    return _PUMP_SPEEDS[1]

  async def pump_get_default_speed(self, *, timeout: Optional[float] = None) -> int:
    """The pump's default speed, in hundredths of rpm."""
    self._record("pump_get_default_speed")
    return _PUMP_SPEEDS[2]

  async def pump_start(
    self, speed: int, direction: PumpDirection, *, timeout: Optional[float] = None
  ) -> None:
    """Start the pump when it stands still."""
    self._record("pump_start", speed, direction)
    self._pump_start(speed, direction)

  async def pump_update(
    self, speed: int, direction: PumpDirection, *, timeout: Optional[float] = None
  ) -> None:
    """Change the running pump's speed."""
    self._record("pump_update", speed, direction)
    self._pump_update(speed, direction)

  async def pump_stop(self, *, timeout: Optional[float] = None) -> None:
    """Stop the pump."""
    self._record("pump_stop")
    self._pump_stop()

  async def sampler_home(self, *, timeout: Optional[float] = None) -> None:
    """Park the probe; refused, faulting the sampler, while the probe reports contact."""
    self._record("sampler_home")
    if self._state("Sampler") == "Ready":
      self._park()

  def _position(self, value: float) -> decimal.Decimal:
    """Return a position as the controller reports it."""
    return decimal.Decimal(repr(round(value, 3)))

  async def sampler_get_x(self, *, timeout: Optional[float] = None) -> decimal.Decimal:
    """The probe's X."""
    self._record("sampler_get_x")
    return self._position(self._x)

  async def sampler_get_y(self, *, timeout: Optional[float] = None) -> decimal.Decimal:
    """The probe's Y."""
    self._record("sampler_get_y")
    return self._position(self._y)

  async def sampler_get_z(self, *, timeout: Optional[float] = None) -> decimal.Decimal:
    """The probe's Z."""
    self._record("sampler_get_z")
    return self._position(self._z)

  def _range(self, axis: Tuple[float, float]) -> MovementRange:
    """Return an axis's travel."""
    return MovementRange(max=decimal.Decimal(repr(axis[1])), min=decimal.Decimal(repr(axis[0])))

  async def sampler_get_x_range(
    self, *, timeout: Optional[float] = None
  ) -> Optional[MovementRange]:
    """X travel."""
    self._record("sampler_get_x_range")
    return self._range(_X_RANGE)

  async def sampler_get_y_range(
    self, *, timeout: Optional[float] = None
  ) -> Optional[MovementRange]:
    """Y travel."""
    self._record("sampler_get_y_range")
    return self._range(_Y_RANGE)

  async def sampler_get_z_range(
    self, *, timeout: Optional[float] = None
  ) -> Optional[MovementRange]:
    """Z travel."""
    self._record("sampler_get_z_range")
    return self._range(_Z_RANGE)

  async def sampler_get_z_contact_tolerance(
    self, activity: ContactToleranceType, *, timeout: Optional[float] = None
  ) -> decimal.Decimal:
    """The allowed contact deviation of an activity."""
    self._record("sampler_get_z_contact_tolerance", activity)
    return decimal.Decimal(_TOLERANCES[activity])

  async def sampler_get_smallest_supported_move(
    self, *, timeout: Optional[float] = None
  ) -> decimal.Decimal:
    """The smallest step."""
    self._record("sampler_get_smallest_supported_move")
    return decimal.Decimal("0.05")

  async def sampler_move_xy(
    self,
    x: decimal.Decimal,
    y: decimal.Decimal,
    fast: bool,
    *,
    timeout: Optional[float] = None,
  ) -> None:
    """Move XY wherever told; the controller checks nothing."""
    self._record("sampler_move_xy", x, y, fast)
    if self._state("Sampler") != "Ready":
      return
    self._x, self._y = float(x), float(y)

  async def sampler_move_z(self, z: decimal.Decimal, *, timeout: Optional[float] = None) -> None:
    """Move Z wherever told."""
    self._record("sampler_move_z", z)
    if self._state("Sampler") != "Ready":
      return
    self._move_z(float(z))

  def _find_contact(self, z_high: float, z_low: float, retract: bool) -> ContactZResult:
    """Run a contact-sensing descent at the current XY."""
    bottom = max(z_low - _CONTACT_DEPTH, _Z_RANGE[0])
    surface = self._surface()
    found = surface is not None and bottom <= surface <= z_high
    contact_z = surface if found and surface is not None else z_high
    self._z = z_high if retract else (contact_z if found else bottom)
    return ContactZResult(
      contact_time=self._now(),
      contact_z_location=self._position(contact_z),
      error_message="",
      found_contact=found,
      has_error=False,
      id=uuid.UUID(int=self._rng.getrandbits(128), version=4),
      is_simulated=False,
      z_low=self._position(z_low),
    )

  async def sampler_find_z_contact(
    self,
    z_high: decimal.Decimal,
    z_low: decimal.Decimal,
    retract_after_find: bool,
    *,
    timeout: Optional[float] = None,
  ) -> Optional[ContactZResult]:
    """Search for contact at the current XY."""
    self._record("sampler_find_z_contact", z_high, z_low, retract_after_find)
    if self._state("Sampler") != "Ready" or self._contact:
      message = "Device not ready for contact z." if not self._contact else "Probe is in contact."
      return ContactZResult(
        contact_time=self._now(),
        contact_z_location=decimal.Decimal(0),
        error_message=message,
        found_contact=False,
        has_error=True,
        id=uuid.UUID(int=self._rng.getrandbits(128), version=4),
        is_simulated=False,
        z_low=z_low,
      )
    return self._find_contact(float(z_high), float(z_low), retract_after_find)

  async def shaker_min_rpm(self, *, timeout: Optional[float] = None) -> int:
    """The shaker's lowest speed."""
    self._record("shaker_min_rpm")
    return self._shaker_range[0]

  async def shaker_max_rpm(self, *, timeout: Optional[float] = None) -> int:
    """The shaker's highest speed."""
    self._record("shaker_max_rpm")
    return self._shaker_range[1]

  async def shaker_start(self, rpm: int, *, timeout: Optional[float] = None) -> None:
    """Start the shaker; returns once at speed."""
    self._record("shaker_start", rpm)
    self._shaker_start(rpm)

  async def shaker_stop(self, *, timeout: Optional[float] = None) -> None:
    """Stop the shaker; returns once home."""
    self._record("shaker_stop")
    self._shaker_stop()

  async def shaker_home(self, *, timeout: Optional[float] = None) -> None:
    """Close the plate lock and home the shaker."""
    self._record("shaker_home")
    if self._state("Shaker") == "Ready":
      self._lock_closed = True

  async def shaker_open_plate_lock(self, *, timeout: Optional[float] = None) -> None:
    """Open the plate lock."""
    self._record("shaker_open_plate_lock")
    if self._state("Shaker") == "Ready":
      self._lock_closed = False

  async def shaker_close_plate_lock(self, *, timeout: Optional[float] = None) -> None:
    """Close the plate lock."""
    self._record("shaker_close_plate_lock")
    if self._state("Shaker") == "Ready":
      self._lock_closed = True

  @property
  def plate_lock_closed(self) -> bool:
    """Whether the shaker's plate lock is closed."""
    return self._lock_closed

  @property
  def rail_position(self) -> Optional[Literal["loaded", "ejected"]]:
    """Where the plate rail is, or None before it was initialised."""
    return self._rail

  @property
  def probe_position(self) -> Tuple[float, float, float]:
    """The probe's X, Y and Z."""
    return self._x, self._y, self._z

  # -------------------------------------------------------------------------------------------
  # Calibration file
  # -------------------------------------------------------------------------------------------

  async def controller_download_calibration_settings(self) -> bytes:
    """Return the stored calibration file."""
    self.calls.append(("controller_download_calibration_settings", ()))
    if self.calibration_file is None:
      raise controller_fault(
        "Controller_DownloadCalibrationSettings", "No calibration settings stored"
      )
    return self.calibration_file

  async def controller_upload_calibration_settings(self, data: bytes) -> None:
    """Store a calibration file."""
    self.calls.append(("controller_upload_calibration_settings", (len(data),)))
    if len(data) > 5000 * 1024:
      raise controller_fault("Controller_UploadCalibrationSettings", "The file is too large")
    self.calibration_file = bytes(data)

  # -------------------------------------------------------------------------------------------
  # Worklists
  # -------------------------------------------------------------------------------------------

  async def worklist_run(
    self, worklist: Optional[Worklist], *, timeout: Optional[float] = None
  ) -> None:
    """Park the probe, home the shaker for a plate, and start the worklist runner."""
    self._record("worklist_run", worklist)
    if self._worklist_status != "NoActiveWorklist":
      raise controller_fault("Worklist_Run", "There is already an active worklist")
    if worklist is None or not worklist.commands:
      raise controller_fault("Worklist_Run", "The worklist has no commands")
    if self._state("Sampler") == "Ready":
      self._park()
    name = worklist.name or ""
    plate = worklist.plate_id is not None and worklist.plate_id.int != 0
    if plate and "Test Tube" not in name and "Vial" not in name:
      if self._state("Shaker") == "Ready":
        self._lock_closed = True
    self._values_sent = 0
    self._chunks_sent = 0
    self._cancel = False
    self._worklist_status = "RunningWorklist"
    self._worklist = worklist
    self._runner = asyncio.ensure_future(self._run(worklist))

  async def worklist_cancel(self, *, timeout: Optional[float] = None) -> None:
    """Ask the runner to stop."""
    self._record("worklist_cancel")
    self._cancel = True

  async def worklist_get_current(self, *, timeout: Optional[float] = None) -> Optional[Worklist]:
    """Return the current or last worklist."""
    self._record("worklist_get_current")
    return self._worklist

  async def worklist_finalize(self, *, timeout: Optional[float] = None) -> None:
    """Acknowledge a finished worklist."""
    self._record("worklist_finalize")
    if self._worklist_status == "RunningWorklist":
      raise controller_fault("Worklist_Finalize", "A worklist is running")
    self._worklist_status = "NoActiveWorklist"

  async def worklist_get_status(self, *, timeout: Optional[float] = None) -> WorklistStatus:
    """Return the worklist status."""
    self._record("worklist_get_status")
    return self._worklist_status

  async def worklist_is_fcs_file_available(self, *, timeout: Optional[float] = None) -> bool:
    """Whether the current FCS file exists."""
    self._record("worklist_is_fcs_file_available")
    return self._fcs_file is not None

  async def worklist_get_current_fcs_file(self) -> AsyncIterator[bytes]:
    """Return the current FCS file in pieces."""
    self.calls.append(("worklist_get_current_fcs_file", ()))
    if self._fcs_file is None:
      raise controller_fault("Worklist_GetCurrentFCSFile", "There is no current FCS file")
    data = self._fcs_file
    for start in range(0, len(data), _PIECE):
      yield data[start : start + _PIECE]

  async def wait_for_worklist(self) -> None:
    """Wait until the runner has ended; for tests."""
    if self._runner is not None:
      await self._runner

  # -------------------------------------------------------------------------------------------
  # The runner
  # -------------------------------------------------------------------------------------------

  async def _elapse(self, seconds: float, *, cancellable: bool) -> bool:
    """Let simulated time pass, delivering events as they reach the detector.

    Args:
      seconds: How long.
      cancellable: Whether a cancel or a monitored fault ends the wait early; as on the
        controller, a wait of 500 ms or less is not interruptible.

    Returns:
      False when the wait ended early.
    """
    interruptible = cancellable and seconds > _WAIT_STEP
    remaining = seconds
    while remaining > _TIME_TOLERANCE:
      if interruptible and self._should_stop():
        return False
      step = min(_WAIT_STEP, remaining)
      self._noise(step)
      self._virtual += step
      remaining -= step
      self._deliver_events()
      await self._pace()
    if cancellable and self._should_stop():
      return False
    return True

  def _poisson(self, mean: float) -> int:
    """Draw a Poisson-distributed count."""
    limit = math.exp(-mean)
    count = 0
    product = self._rng.random()
    while product > limit:
      count += 1
      product *= self._rng.random()
    return count

  def _event_values(self, brightness: float) -> List[float]:
    """Return the values of one event with its time left at 0."""
    lasers = self._lasers
    parameters = self._table.parameters
    channel_count = (len(parameters) - 2) // 2
    heights = []
    for parameter in parameters[:channel_count]:
      if parameter.laser is not None and parameter.laser not in lasers:
        heights.append(0.0)
        continue
      if parameter.name == "FSC-H":
        height = self._rng.lognormvariate(math.log(300_000.0), 0.25)
      elif parameter.name == "SSC-H":
        height = self._rng.lognormvariate(math.log(80_000.0), 0.35)
      else:
        height = self._rng.lognormvariate(math.log(2_000.0 * brightness), 0.6)
      heights.append(min(height, _MAX_VALUE))
    areas = [min(height * 1.25, _MAX_VALUE) for height in heights]
    width = max(0.0, self._rng.gauss(50.0, 8.0))
    return heights + areas + [width, 0.0]

  def _draw(self, start: float, end: float) -> None:
    """Turn the probe drawing from a well between two simulated times into events that reach
    the detector after the transit delay."""
    if self._acquisition is None or self._state("Pump") != "Running":
      return
    rate = self._rng.uniform(0.6, 1.4) * self.events_per_second * TIME_STEP
    brightness = self._rng.uniform(0.5, 3.0)
    arrival = start + self.transit_delay + self._rng.gauss(0.0, 0.02)
    for tick in range(int(round((end - start) / TIME_STEP))):
      for _ in range(self._poisson(rate)):
        self._pending.append((arrival + tick * TIME_STEP, self._event_values(brightness)))
    self._pending.sort(key=lambda pending: pending[0])

  def _noise(self, step: float) -> None:
    """Add the stray events of a stretch of simulated time."""
    if self._acquisition is None or not self._acquisition.recording:
      return
    for _ in range(self._poisson(self.noise_per_second * step)):
      at = self._virtual + self._rng.uniform(0.0, step)
      self._pending.append((at, self._event_values(1.0)))
    self._pending.sort(key=lambda pending: pending[0])

  def _deliver_events(self) -> None:
    """Record the events that have reached the detector and stream them."""
    acquisition = self._acquisition
    if acquisition is None:
      self._pending.clear()
      return
    due = 0
    while due < len(self._pending) and self._pending[due][0] <= self._virtual:
      due += 1
    arrived, self._pending = self._pending[:due], self._pending[due:]
    count = len(self._table.parameters)
    for at, values in arrived:
      if not acquisition.recording:
        continue
      tick = round((at - acquisition.start) / TIME_STEP) - acquisition.delay_ticks
      if tick < 0 or acquisition.count >= acquisition.threshold:
        continue
      values[count - 1] = float(tick)
      acquisition.values.extend(values)
      acquisition.count += 1
      if acquisition.count >= acquisition.maximum:
        acquisition.recording = False
        self._event_limit = True
        self.fault_device("Cytometer", "Acquisition Event Limit Exceeded")
    self._stream(acquisition)

  def _stream(self, acquisition: _Acquisition) -> None:
    """Send the recorded values not yet sent, in chunks of the instrument's size."""
    count = len(self._table.parameters)
    per_chunk = _CHUNK_BYTES // (count * 4) * count
    values = acquisition.values
    while acquisition.unsent_from < len(values):
      chunk = values[acquisition.unsent_from : acquisition.unsent_from + per_chunk]
      acquisition.unsent_from += len(chunk)
      if not (self._open and self._registered):
        continue
      self._values_sent += len(chunk)
      index = self._chunks_sent
      self._chunks_sent += 1
      if index in self._drop_chunks:
        continue
      self._emit("Cytometer_OnHasFloatFCSData", array.array("f", chunk), self._values_sent)

  def _close_fcs_file(self) -> None:
    """Write the controller's FCS file from the recorded events."""
    acquisition = self._acquisition
    if acquisition is None:
      return
    self._deliver_events()
    self._acquisition = None
    self._pending.clear()
    if acquisition.count == 0:
      self._fcs_file = None
      self._fcs_file_name = None
      return
    events = EventData(self._table.names, acquisition.values, self._table.long_names)
    metadata = {key: value for key, value in self._metadata.items() if key and value}
    keywords = instrument_keywords(
      self._table,
      operator=self._user_name or "UNK",
      acquired_on=(self._now() + self._client_offset).date(),
      events=events,
      metadata=metadata,
    )
    self._fcs_file = write_fcs(events, keywords)

  def _stop_fluidics(self) -> None:
    """Stop the detector's fluidics and close the FCS file."""
    if self._acquisition is not None:
      self._close_fcs_file()
    if self._fluidics_started is not None:
      self._fluidics_minutes = (self._virtual - self._fluidics_started) / 60
      self._fluidics_started = None
    self._lasers = frozenset()
    if self._state("Cytometer") == "Running":
      self._set_state("Cytometer", "Ready")

  async def _run(self, worklist: Worklist) -> None:
    """Run a worklist as the controller's worklist thread does."""
    commands: List[WorklistCommand] = [c for c in worklist.commands or [] if c is not None]
    estimate = sum(command.estimated_duration.total_seconds() for command in commands)
    notes = f"Estimate={estimate:g}|" + "|".join(
      f"{kind}={_MODELS[kind]}" for kind in self._configured if self._states[kind] != "Disconnected"
    )
    current = dataclasses.replace(
      worklist,
      id=uuid.UUID(int=self._rng.getrandbits(128), version=4),
      start_time=self._now(),
      end_time=_RUNNING_END_TIME,
      client_time_offset=self._client_offset,
      execution_notes=notes,
      commands=list(commands),
    )
    self._worklist = current
    self._fluidics_minutes = 0.0
    self._pump_minutes = 0.0
    self._event_limit = False
    self._pace_origin = None
    stop_reason: WorklistStopReason = "Unspecified"
    error = ""
    reached_end = False
    try:
      for command in commands:
        if type(command) not in self._executors:
          raise _WorklistError("Dev Error: Unsupported worklist command")
      for index, command in enumerate(commands):
        if self._cancel:
          stop_reason = "Cancelled"
          break
        if self._monitored_red():
          stop_reason = "DeviceFaulted"
          break
        self._emit("OnCommandStarted", command)
        start = self._now()
        completed, command_notes = await self._executors[type(command)](command)
        updated = dataclasses.replace(
          command,
          start_time=start,
          end_time=self._now() if completed else _UNSET_TIME,
          completed=completed,
          execution_notes=command_notes,
        )
        commands[index] = updated
        current = dataclasses.replace(current, commands=list(commands))
        self._worklist = current
        self._emit("OnCommandCompleted", updated)
      else:
        reached_end = True
    except _WorklistError as failure:
      stop_reason = "WorklistError"
      error = str(failure)
    if self._event_limit:
      stop_reason = "EventLimit"
    await self._stop_all_and_move_home(worklist.leave_fluidics_running_on_complete)
    all_completed = reached_end and commands[-1].completed
    self._worklist_status = "CompletedUnfinalizedWorklist"
    current = dataclasses.replace(
      current,
      commands=list(commands),
      all_commands_completed=all_completed,
      error=error,
      stop_reason=stop_reason,
      end_time=self._now(),
      fcs_file_name=self._fcs_file_name,
      fcs_data_written=self._values_sent,
      fcs_file_crc=file_crc32(self._fcs_file) if self._fcs_file is not None else 0,
      fluidics_run_time_minutes=decimal.Decimal(repr(round(self._fluidics_minutes, 3))),
      pump_run_time_minutes=decimal.Decimal(repr(round(self._pump_minutes, 3))),
    )
    self._worklist = current
    if not self._drop_worklist_completed:
      self._emit("OnWorklistCompleted", current)

  async def _stop_all_and_move_home(self, leave_fluidics_running: bool) -> None:
    """Stop the pump and the shaker, park the probe, stop the fluidics, eject the rail."""
    if self._state("Pump") != "Faulted":
      self._pump_stop()
    if self._state("Shaker") != "Faulted":
      self._shaker_stop()
    if self._state("Sampler") not in (None, "Faulted", "Disconnected"):
      self._park()
    if not leave_fluidics_running and self._state("Cytometer") is not None:
      await self._elapse(2.0, cancellable=False)
      self._stop_fluidics()
    elif self._acquisition is not None:
      self._close_fcs_file()
    if self._state("PlateRail") is not None:
      self._rail_move("ejected")

  @staticmethod
  def _notes(**steps: float) -> str:
    """Return a sampling command's notes: seconds per step and the contact count."""
    return " ".join(f"{name}={value:.3f}" for name, value in steps.items())

  def _contact_count(self) -> int:
    """Contact sensor changes since the worklist started, halved, as the notes report."""
    return self._contact_changes // 2

  async def _dip(
    self, x: float, y: float, fast: bool, z_down: float, z_up: float, sip_ms: int, up_ms: int
  ) -> Tuple[bool, str]:
    """One dip: XY, down, draw, up, wait in air."""
    move = self._xy_time(x, y, fast)
    await self._elapse(move, cancellable=False)
    self._x, self._y = x, y
    await self._elapse(0.1, cancellable=False)
    self._move_z(z_down)
    start = self._virtual
    drew = await self._elapse(sip_ms / 1000, cancellable=True)
    self._draw(start, self._virtual)
    await self._elapse(0.1, cancellable=False)
    self._move_z(z_up)
    waited = drew and await self._elapse(up_ms / 1000, cancellable=True)
    notes = self._notes(
      BeginMoveSequence=0.0,
      MoveXY=move,
      MoveZDown=0.1,
      WaitForSip=self._virtual - start,
      MoveZUp=0.1,
      WaitForUp=up_ms / 1000 if waited else 0.0,
      EndMoveSequence=0.0,
      WriteLog=0.0,
    )
    return drew and waited, f"{notes} ContactCount={self._contact_count()}"

  async def _sample_well(self, command: wire.SampleCommand) -> Tuple[bool, str]:
    """SampleWellCommand and BeadSipCommand: one dip."""
    return await self._dip(
      float(command.x),
      float(command.y),
      command.fast_move,
      float(command.z_down),
      float(command.z_up),
      command.sip_time,
      command.up_time,
    )

  async def _sample_vial(self, command: wire.SampleVialCommand) -> Tuple[bool, str]:
    """SampleVialCommand: one slow dip."""
    return await self._dip(
      float(command.x),
      float(command.y),
      False,
      float(command.z_down),
      float(command.z_up),
      command.sip_time,
      command.up_time,
    )

  async def _sample_test_tube(self, command: wire.SampleTestTubeCommand) -> Tuple[bool, str]:
    """SampleTestTubeCommand: wait for an event count or a time; nothing moves."""
    waited = 0.0
    while waited < command.sip_time / 1000:
      acquisition = self._acquisition
      if acquisition is not None and acquisition.count >= command.event_threshold:
        break
      await self._elapse(0.1, cancellable=False)
      if self._should_stop():
        return False, f"WaitForEvents={waited:.3f}"
      waited += 0.1
    return True, f"WaitForEvents={waited:.3f}"

  async def _sample_continuously(self, command: Any) -> Tuple[bool, str]:
    """SampleContinuouslyCommand and SampleWellContinuouslyCommand: dip with the pump."""
    move = self._xy_time(float(command.x), float(command.y), command.fast_move)
    await self._elapse(move, cancellable=False)
    self._x, self._y = float(command.x), float(command.y)
    self._move_z(float(command.z_low))
    await self._elapse(1.0, cancellable=True)
    self._pump_update(command.pump_speed, command.pump_direction)
    self._pump_start(command.pump_speed, command.pump_direction)
    start = self._virtual
    drew = await self._elapse(command.sip_time / 1000, cancellable=True)
    self._draw(start, self._virtual)
    self._pump_stop()
    self._move_z(float(command.z_high))
    return drew, f"MoveXY={move:.3f} WaitForSip={self._virtual - start:.3f}"

  async def _cycle(
    self,
    x: float,
    y: float,
    fast: bool,
    z_down: float,
    z_up: float,
    sip_ms: int,
    up_ms: int,
    duration: float,
  ) -> bool:
    """Dip repeatedly at a station for a duration; the pump must run."""
    self._confirm_pump_running()
    self._move_z(z_up)
    move = self._xy_time(x, y, fast)
    await self._elapse(move, cancellable=False)
    self._x, self._y = x, y
    start = self._virtual
    while duration - (self._virtual - start) > _TIME_TOLERANCE:
      self._move_z(z_down)
      left = duration - (self._virtual - start)
      if not await self._elapse(min(sip_ms / 1000, left), cancellable=True):
        self._move_z(z_up)
        return False
      self._move_z(z_up)
      left = duration - (self._virtual - start)
      if left > _TIME_TOLERANCE and not await self._elapse(
        min(up_ms / 1000, left), cancellable=True
      ):
        return False
    self._move_z(z_up)
    return True

  async def _rinse(self, command: wire.RinseCommand) -> Tuple[bool, str]:
    """RinseCommand: one dip at a station; the pump must run."""
    self._confirm_pump_running()
    completed, notes = await self._dip(
      float(command.x),
      float(command.y),
      command.fast_move,
      float(command.z_down),
      float(command.z_up),
      command.sip_time,
      command.up_time,
    )
    return completed, notes

  async def _cycling_rinse(self, command: wire.CyclingRinseCommand) -> Tuple[bool, str]:
    """CyclingRinseCommand: repeated dips for a duration in ms."""
    completed = await self._cycle(
      float(command.x),
      float(command.y),
      command.fast_move,
      float(command.z_down),
      float(command.z_up),
      command.sip_time,
      command.up_time,
      command.rinse_duration / 1000,
    )
    return completed, f"CyclingRinseDuration={command.rinse_duration}"

  async def _timed_cycling_rinse(self, command: wire.TimedCyclingRinseCommand) -> Tuple[bool, str]:
    """TimedCyclingRinseCommand and FlushCommand: repeated dips for a duration in s."""
    completed = await self._cycle(
      float(command.x),
      float(command.y),
      command.fast_move,
      float(command.z_down),
      float(command.z_up),
      command.sip_time,
      command.up_time,
      float(command.rinse_duration),
    )
    return completed, f"RinseDuration={command.rinse_duration * 1000}"

  async def _flush_and_eject(self, command: wire.FlushMatryoshkaCommand) -> Tuple[bool, str]:
    """FlushMatryoshkaCommand: eject the rail while flushing."""
    self._rail_move("ejected")
    return await self._timed_cycling_rinse(command)

  async def _timed_continuous_rinse(
    self, command: wire.TimedContinuousRinseCommand
  ) -> Tuple[bool, str]:
    """TimedContinuousRinseCommand: the probe stays down for a duration in s."""
    self._confirm_pump_running()
    self._move_z(float(command.z_up))
    move = self._xy_time(float(command.x), float(command.y), command.sample_speed == "Fast")
    await self._elapse(move, cancellable=False)
    self._x, self._y = float(command.x), float(command.y)
    self._move_z(float(command.z_down))
    completed = await self._elapse(float(command.rinse_duration), cancellable=True)
    self._move_z(float(command.z_up))
    if completed and command.up_time > 0:
      completed = await self._elapse(command.up_time / 1000, cancellable=True)
    return completed, f"RinseDuration={command.rinse_duration * 1000}"

  async def _start_pump(self, command: wire.StartPumpCommand) -> Tuple[bool, str]:
    """StartPumpCommand: update a running pump, or start a stopped one."""
    self._pump_update(command.speed, command.direction)
    self._pump_start(command.speed, command.direction)
    return True, ""

  async def _stop_pump(self, command: wire.StopPumpCommand) -> Tuple[bool, str]:
    """StopPumpCommand."""
    self._pump_stop()
    return True, ""

  async def _start_shaker(self, command: wire.StartShakerCommand) -> Tuple[bool, str]:
    """StartShakerCommand: returns once at speed."""
    self._shaker_start(command.rpm)
    await self._elapse(2.0, cancellable=False)
    return True, ""

  async def _stop_shaker(self, command: wire.StopShakerCommand) -> Tuple[bool, str]:
    """StopShakerCommand: returns once home."""
    self._shaker_stop()
    await self._elapse(2.0, cancellable=False)
    return True, ""

  async def _timed_shake(self, command: wire.TimedShakeCommand) -> Tuple[bool, str]:
    """TimedShakeCommand: shake with the probe parked in a rinse station."""
    self._confirm_pump_running()
    self._move_z(float(command.rinse_z_up))
    self._x, self._y = float(command.rinse_x), float(command.rinse_y)
    self._move_z(float(command.rinse_z_down))
    self._shaker_start(command.rpm)
    completed = await self._elapse(float(command.shake_duration), cancellable=True)
    self._shaker_stop()
    self._move_z(float(command.rinse_z_up))
    return completed, f"ShakeDuration={command.shake_duration * 1000}"

  async def _combined_shake_rinse(
    self, command: wire.CombinedShakeRinseCommand
  ) -> Tuple[bool, str]:
    """CombinedShakeRinseCommand(AtLocation): shake while the probe cycles at a station."""
    self._confirm_pump_running()
    self._shaker_start(command.rpm)
    await self._elapse(2.0, cancellable=False)
    completed = await self._cycle(
      float(command.rinse_x),
      float(command.rinse_y),
      command.fast_move,
      float(command.rinse_z_down),
      float(command.rinse_z_up),
      command.sip_time,
      command.up_time,
      float(command.shake_duration),
    )
    self._shaker_stop()
    return completed, f"ShakeDuration={command.shake_duration * 1000}"

  async def _validate_deck_height(
    self, command: wire.ValidateDeckHeightCommand
  ) -> Tuple[bool, str]:
    """ValidateDeckHeightCommand: load the rail, touch the deck, check the height."""
    if not command.is_microfuge_tube_check:
      self._close_lock_and_load()
    self._x, self._y = float(command.x), float(command.y)
    result = self._find_contact(float(command.z_high), float(command.z_low), True)
    low, tolerance = float(command.z_low), float(command.z_tolerance)
    contact = float(result.contact_z_location)
    if not result.found_contact:
      raise _WorklistError(f"Contact not found at {low}")
    if not low - tolerance <= contact <= low + tolerance:
      raise _WorklistError(f"Contact at {contact} is out of range")
    return True, f"ZLow={low} ZContact={contact} ZTolerance={tolerance}"

  def _start_detector_fluidics(
    self,
    command: Union[wire.StartFlowCytometerFluidicsCommand, wire.StartMatryoshkaFluidicsCommand],
  ) -> None:
    """Configure the detector and start its fluidics, or fault it when it cannot."""
    if not self._ready("Cytometer"):
      if self._state("Cytometer") not in (None, "Disconnected"):
        self.fault_device("Cytometer", "Start Fluidics Faulted")
      return
    with_lasers = isinstance(command, wire.StartMatryoshkaFluidicsCommand) or (
      command.prepare_for_acquisition
    )
    self._lasers = _LASERS[command.enabled_lasers] if with_lasers else frozenset()
    self._user_name = command.user_name
    self._metadata = {
      key: value
      for key, value in command.fcs_metadata or []
      if key is not None and value is not None
    }
    self._fluidics_started = self._virtual
    self._set_state("Cytometer", "Running")

  async def _start_fluidics(
    self, command: wire.StartFlowCytometerFluidicsCommand
  ) -> Tuple[bool, str]:
    """StartFlowCytometerFluidicsCommand."""
    self._start_detector_fluidics(command)
    return await self._elapse(5.0, cancellable=True), ""

  async def _start_fluidics_and_load(
    self, command: wire.StartMatryoshkaFluidicsCommand
  ) -> Tuple[bool, str]:
    """StartMatryoshkaFluidicsCommand: load the plate and start the fluidics for acquisition."""
    self._close_lock_and_load()
    self._start_detector_fluidics(command)
    return await self._elapse(5.0, cancellable=True), ""

  async def _stop_fluidics_command(
    self, command: wire.StopFlowCytometerFluidicsCommand
  ) -> Tuple[bool, str]:
    """StopFlowCytometerFluidicsCommand."""
    self._stop_fluidics()
    return await self._elapse(3.0, cancellable=True), ""

  async def _fluidics_for_duration(
    self, command: wire.RunFlowCytometerFluidicsForDurationCommand
  ) -> Tuple[bool, str]:
    """RunFlowCytometerFluidicsForDurationCommand."""
    if not self._ready("Cytometer"):
      return True, ""
    self._fluidics_started = self._virtual
    self._set_state("Cytometer", "Running")
    completed = await self._elapse(float(command.fluidics_duration), cancellable=True)
    self._stop_fluidics()
    return completed, ""

  async def _acquire(self, command: wire.FlowCytometerAcquireCommand) -> Tuple[bool, str]:
    """FlowCytometerAcquireCommand: create the FCS file and start recording; returns at once."""
    if self._state("Cytometer") != "Running" or not self._lasers:
      if self._state("Cytometer") not in (None, "Disconnected"):
        self.fault_device("Cytometer", "Acquire Faulted")
      return True, ""
    name = _INSTRUMENT_NAMES.get(self._system_type, "iQue Screener PLUS")
    stamp = (self._now() + self._client_offset).strftime("%Y%m%d-%H%M%S")
    self._fcs_file_name = f"{stamp}-{name}.fcs"
    self._fcs_file = None
    acquisition = _Acquisition(
      self._table, self._virtual, float(command.acquistion_delay), command.event_maximum
    )
    acquisition.threshold = command.event_threshold
    self._acquisition = acquisition
    return True, ""

  async def _stop_acquire(self, command: wire.FlowCytometerStopAcquireCommand) -> Tuple[bool, str]:
    """FlowCytometerStopAcquireCommand: stop recording; the instrument keeps acquiring."""
    self._deliver_events()
    if self._acquisition is not None:
      self._acquisition.recording = False
    return True, ""

  async def _clean(self, command: wire.CleanFlowCytometerCommand) -> Tuple[bool, str]:
    """CleanFlowCytometerCommand: run a maintenance method of the detector."""
    method = (command.clean_method or "").lower()
    if not self._ready("Cytometer"):
      return True, ""
    if method not in _CLEAN_DURATIONS:
      self.fault_device("Cytometer", "Clean Faulted")
      return True, ""
    duration = _CLEAN_DURATIONS[method]
    self._operation_end = self._virtual + duration
    self._set_state("Cytometer", "Running")
    completed = await self._elapse(duration, cancellable=True)
    self._operation_end = self._virtual
    if method == "shutdown":
      self._power_off()
    else:
      self._stop_fluidics()
    return completed, ""

  async def _shutdown_command(self, command: wire.ShutdownFlowCytometerCommand) -> Tuple[bool, str]:
    """ShutdownFlowCytometerCommand: shut the detector down; a leak fails the worklist."""
    if self._leak:
      raise _WorklistError("Unable to shutdown")
    self._power_off()
    return True, ""
