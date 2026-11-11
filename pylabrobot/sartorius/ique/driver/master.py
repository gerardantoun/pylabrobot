"""The session with the iQue Screener PLUS controller: setup, live state, worklists, calibration.

The instrument's internal computer runs the controller, a service that drives the sampler, the
peristaltic pump, the shaker, the plate rail, the probe's contact sensor and the cytometer (the
detector). :class:`IQueDriver` holds the one session the controller serves at a time. It
configures, resets and initialises the instrument's devices, keeps a picture of the instrument's
state from the callbacks the controller sends, runs worklists and collects their event data, and
keeps the calibration file. The user-facing direct operations are its features (``plate_rail``,
``shaker``, ``pump``, ``sampler``, ``detector``).

The controller handles one call at a time, so every call goes through one lock. A broken
session is raised to the caller as the wcf package's error and never retried.
"""

from __future__ import annotations

import array
import asyncio
import datetime
import decimal
import logging
import pathlib
from dataclasses import dataclass
from typing import (
  AsyncIterator,
  Awaitable,
  Callable,
  Dict,
  List,
  Literal,
  Mapping,
  NamedTuple,
  Optional,
  ParamSpec,
  Protocol,
  Sequence,
  Tuple,
  Type,
  TypeVar,
  runtime_checkable,
)

from pylabrobot.sartorius.ique.driver import contract as wire
from pylabrobot.sartorius.ique.driver.calibration import (
  MAX_FILE_SIZE,
  Calibration,
  CalibrationError,
  read_calibration,
)
from pylabrobot.sartorius.ique.driver.client import (
  CONTROLLER_PORT,
  CONTROLLER_STREAMING_PORT,
  ControllerClient,
  ControllerFault,
)
from pylabrobot.sartorius.ique.driver.contract import (
  ConnectionInfo,
  ContactToleranceType,
  ContactZResult,
  DeviceAlert,
  DeviceAlertMessage,
  DeviceConfiguration,
  DeviceInfo,
  DeviceKind,
  DeviceResetOptions,
  DeviceState,
  IQue3InstallationPrimeStep,
  IQue3PurgeStep,
  MovementRange,
  PumpDirection,
  SystemState,
  SystemType,
  Worklist,
  WorklistCommand,
  WorklistStatus,
  WorklistStopReason,
)
from pylabrobot.sartorius.ique.driver.errors import (
  CalibrationMissingError,
  CalibrationRefusedError,
  ControllerVersionError,
  DeviceFaultedError,
  DeviceNotReadyError,
  DeviceOperationError,
  DeviceResetError,
  EventDataError,
  IQueError,
  IQueTimeoutError,
  NotSetUpError,
  SessionRefusedError,
  UnsupportedSystemError,
  WorklistActiveError,
  WorklistCancelledError,
  WorklistFailedError,
  WorklistTimeoutError,
  describe_device,
)
from pylabrobot.sartorius.ique.driver.fcs import (
  EventData,
  FcsError,
  ParameterTable,
  file_crc32,
  parameter_table_for_system_kind,
  read_fcs,
)
from pylabrobot.sartorius.ique.driver.features.detector import Detector
from pylabrobot.sartorius.ique.driver.features.plate_rail import PlatePosition, PlateRail
from pylabrobot.sartorius.ique.driver.features.pump import Pump
from pylabrobot.sartorius.ique.driver.features.sampler import Sampler
from pylabrobot.sartorius.ique.driver.features.shaker import Shaker

logger = logging.getLogger(__name__)

_P = ParamSpec("_P")
_T = TypeVar("_T")

PLUS_SYSTEM_TYPES: Tuple[SystemType, ...] = ("iQue3", "iQue3HD", "iQuePlusVYB", "iQuePlusBR")
"""The system types of the iQue Screener PLUS family, the only ones this driver runs."""

SUPPORTED_CONTROLLER_VERSIONS: Tuple[Tuple[int, int], ...] = ((7, 2),)
"""Controller software versions (major, minor) whose contract this driver was written against."""

ALL_DEVICES: Tuple[DeviceKind, ...] = (
  "Sampler",
  "Pump",
  "Shaker",
  "Cytometer",
  "PlateRail",
  "ContactSensor",
)
"""Every device slot of an iQue Screener PLUS."""

MECHANICAL_DEVICES: Tuple[DeviceKind, ...] = (
  "Sampler",
  "Pump",
  "Shaker",
  "PlateRail",
  "ContactSensor",
)
"""The devices that move or carry the plate and the probe: every device but the cytometer."""

# The controller starts with the sampler, pump, shaker and cytometer; the optional devices are
# set with Controller_ConfigureDevices. An iQue Screener PLUS has all four; they are included
# with no configuration text, in this order.
PLUS_DEVICE_CONFIGURATION: Dict[DeviceKind, Optional[DeviceConfiguration]] = {
  "Shaker": DeviceConfiguration(configuration=None, include_this_device=True),
  "Cytometer": DeviceConfiguration(configuration=None, include_this_device=True),
  "PlateRail": DeviceConfiguration(configuration=None, include_this_device=True),
  "ContactSensor": DeviceConfiguration(configuration=None, include_this_device=True),
}

# The reset options for an instrument with factory settings. Every value is outside the range
# the controller applies, so the controller uses its own defaults:
# Z-arm power 70 %, liquid-level detection 2 %, an extra 3 mm of descent when finding contact,
# and the plate rail's stored run current.
DEFAULT_RESET_OPTIONS = DeviceResetOptions(
  is_in_dev_mode=False,
  is_sorpc6=False,
  override_lld_percentage=0.0,
  override_z_arm_additional_contact_depth=0.0,
  override_z_arm_percentage=0,
  plate_rail_rc=0,
)

# Commands the controller has an executor for. It chooses executors by exact type, and one
# command without an executor stops the whole worklist before anything runs.
EXECUTABLE_COMMANDS: Tuple[Type[WorklistCommand], ...] = (
  wire.SampleWellCommand,
  wire.BeadSipCommand,
  wire.SampleTestTubeCommand,
  wire.SampleVialCommand,
  wire.SampleWellContinuouslyCommand,
  wire.SampleContinuouslyCommand,
  wire.RinseCommand,
  wire.CyclingRinseCommand,
  wire.TimedContinuousRinseCommand,
  wire.TimedCyclingRinseCommand,
  wire.FlushCommand,
  wire.FlushMatryoshkaCommand,
  wire.StartPumpCommand,
  wire.StopPumpCommand,
  wire.StartShakerCommand,
  wire.StopShakerCommand,
  wire.TimedShakeCommand,
  wire.CombinedShakeRinseCommand,
  wire.CombinedShakeRinseAtLocationCommand,
  wire.ValidateDeckHeightCommand,
  wire.StartFlowCytometerFluidicsCommand,
  wire.StartMatryoshkaFluidicsCommand,
  wire.StopFlowCytometerFluidicsCommand,
  wire.RunFlowCytometerFluidicsForDurationCommand,
  wire.FlowCytometerAcquireCommand,
  wire.FlowCytometerStopAcquireCommand,
  wire.CleanFlowCytometerCommand,
  wire.ShutdownFlowCytometerCommand,
)

# Commands that need the cytometer; before a worklist with one of them, the cytometer must say it
# is ready to run: a run, a prime and a clean all need that.
CYTOMETER_COMMANDS: Tuple[Type[WorklistCommand], ...] = (
  wire.StartFlowCytometerFluidicsCommand,
  wire.StartMatryoshkaFluidicsCommand,
  wire.StopFlowCytometerFluidicsCommand,
  wire.RunFlowCytometerFluidicsForDurationCommand,
  wire.FlowCytometerAcquireCommand,
  wire.FlowCytometerStopAcquireCommand,
  wire.CleanFlowCytometerCommand,
  wire.ShutdownFlowCytometerCommand,
  wire.SampleTestTubeCommand,
)

# The controller's EndTime while a worklist runs; a completed worklist carries a later one.
_RUNNING_END_TIME_YEAR = 1900


@runtime_checkable
class ControllerApi(Protocol):
  """The operations of :class:`ControllerClient` the driver uses.

  :class:`~pylabrobot.sartorius.ique.driver.simulator.SimulatedController` implements them too,
  and tests may pass anything else that does. Units and meanings are those of the client.
  """

  @property
  def is_open(self) -> bool:
    """Whether the session is open for calls."""
    ...

  async def open(self) -> None:
    """Open the session."""
    ...

  async def close(self) -> None:
    """End the session."""
    ...

  def on_test_connection(self, handler: Optional[Callable[[], None]]) -> None:
    """Set the handler of TestConnection."""
    ...

  def on_cytometer_has_uint_fcs_data(
    self, handler: Optional[Callable[[Optional[array.array[int]], int], None]]
  ) -> None:
    """Set the handler of Cytometer_OnHasUIntFCSData."""
    ...

  def on_cytometer_has_float_fcs_data(
    self, handler: Optional[Callable[[Optional[array.array[float]], int], None]]
  ) -> None:
    """Set the handler of Cytometer_OnHasFloatFCSData."""
    ...

  def on_cytometer_long_running_operation_completed(
    self, handler: Optional[Callable[[Optional[str]], None]]
  ) -> None:
    """Set the handler of OnCytometerLongRunningOperationCompleted."""
    ...

  def on_system_state_changed(self, handler: Optional[Callable[[SystemState], None]]) -> None:
    """Set the handler of OnSystemStateChanged."""
    ...

  def on_device_state_changed(
    self, handler: Optional[Callable[[Optional[DeviceInfo]], None]]
  ) -> None:
    """Set the handler of OnDeviceStateChanged."""
    ...

  def on_contact_sensor_state_changed(self, handler: Optional[Callable[[bool], None]]) -> None:
    """Set the handler of OnContactSensorStateChanged."""
    ...

  def on_device_alert(
    self, handler: Optional[Callable[[Optional[DeviceAlertMessage]], None]]
  ) -> None:
    """Set the handler of OnDeviceAlert."""
    ...

  def on_command_started(
    self, handler: Optional[Callable[[Optional[WorklistCommand]], None]]
  ) -> None:
    """Set the handler of OnCommandStarted."""
    ...

  def on_command_completed(
    self, handler: Optional[Callable[[Optional[WorklistCommand]], None]]
  ) -> None:
    """Set the handler of OnCommandCompleted."""
    ...

  def on_worklist_completed(self, handler: Optional[Callable[[Optional[Worklist]], None]]) -> None:
    """Set the handler of OnWorklistCompleted."""
    ...

  async def controller_connect(
    self, client_time: datetime.datetime, *, timeout: Optional[float] = None
  ) -> Optional[ConnectionInfo]:
    """Controller_Connect."""
    ...

  async def controller_set_verbose_logging(
    self, verbose_logging: bool, *, timeout: Optional[float] = None
  ) -> None:
    """Controller_SetVerboseLogging."""
    ...

  async def controller_enable_demo_mode(
    self, demo_mode: bool, *, timeout: Optional[float] = None
  ) -> None:
    """Controller_EnableDemoMode."""
    ...

  async def controller_get_system_id(
    self, force_refresh: bool, *, timeout: Optional[float] = None
  ) -> Optional[str]:
    """Controller_GetSystemID."""
    ...

  async def controller_determine_system_type(
    self, *, timeout: Optional[float] = None
  ) -> SystemType:
    """Controller_DetermineSystemType."""
    ...

  async def controller_get_summary(self, *, timeout: Optional[float] = None) -> Optional[str]:
    """Controller_GetSummary."""
    ...

  async def controller_get_device_summary(
    self, device_kind: DeviceKind, *, timeout: Optional[float] = None
  ) -> Optional[str]:
    """Controller_GetDeviceSummary."""
    ...

  async def controller_get_system_state(self, *, timeout: Optional[float] = None) -> SystemState:
    """Controller_GetSystemState."""
    ...

  async def controller_configure_devices(
    self,
    configuration: Optional[Dict[DeviceKind, Optional[DeviceConfiguration]]],
    *,
    timeout: Optional[float] = None,
  ) -> None:
    """Controller_ConfigureDevices."""
    ...

  async def controller_reset_devices(
    self, options: Optional[DeviceResetOptions], *, timeout: Optional[float] = None
  ) -> bool:
    """Controller_ResetDevices."""
    ...

  async def controller_initialize_devices(
    self, device_initialization_list: Optional[List[DeviceKind]], *, timeout: Optional[float] = None
  ) -> None:
    """Controller_InitializeDevices."""
    ...

  async def controller_get_device_statuses(
    self, *, timeout: Optional[float] = None
  ) -> Optional[List[Optional[DeviceInfo]]]:
    """Controller_GetDeviceStatuses."""
    ...

  async def controller_power_on_cytometer(self, *, timeout: Optional[float] = None) -> None:
    """Controller_PowerOnCytometer."""
    ...

  async def controller_cytometer_has_power(self, *, timeout: Optional[float] = None) -> bool:
    """Controller_CytometerHasPower."""
    ...

  async def cytometer_is_powering_on(self, *, timeout: Optional[float] = None) -> bool:
    """Cytometer_IsPoweringOn."""
    ...

  async def cytometer_is_ready_to_run(self, *, timeout: Optional[float] = None) -> bool:
    """Cytometer_IsReadyToRun."""
    ...

  async def cytometer_is_ready_to_clean(self, *, timeout: Optional[float] = None) -> bool:
    """Cytometer_IsReadyToClean."""
    ...

  async def cytometer_get_operation_duration(
    self, *, timeout: Optional[float] = None
  ) -> datetime.timedelta:
    """Cytometer_GetOperationDuration."""
    ...

  async def cytometer_shutdown(self, *, timeout: Optional[float] = None) -> None:
    """Cytometer_Shutdown."""
    ...

  async def cytometer_calibrate_fluid_stations(self, *, timeout: Optional[float] = None) -> None:
    """Cytometer_CalibrateFluidStations."""
    ...

  async def cytometer_reset_consumables_odometer(self, *, timeout: Optional[float] = None) -> None:
    """Cytometer_ResetConsumablesOdometer."""
    ...

  async def ique3_run_next_purge_step(self, *, timeout: Optional[float] = None) -> None:
    """IQue3_RunNextPurgeStep."""
    ...

  async def ique3_get_current_purge_step(
    self, *, timeout: Optional[float] = None
  ) -> Tuple[IQue3PurgeStep, bool]:
    """IQue3_GetCurrentPurgeStep."""
    ...

  async def ique3_run_next_installation_priming_step(
    self, *, timeout: Optional[float] = None
  ) -> None:
    """IQue3_RunNextInstallationPrimingStep."""
    ...

  async def ique3_get_current_installation_priming_step(
    self, *, timeout: Optional[float] = None
  ) -> Tuple[IQue3InstallationPrimeStep, bool]:
    """IQue3_GetCurrentInstallationPrimingStep."""
    ...

  async def contact_sensor_has_contact(self, *, timeout: Optional[float] = None) -> bool:
    """ContactSensor_HasContact."""
    ...

  async def plate_rail_load(self, *, timeout: Optional[float] = None) -> None:
    """PlateRail_Load."""
    ...

  async def plate_rail_eject(self, *, timeout: Optional[float] = None) -> None:
    """PlateRail_Eject."""
    ...

  async def pump_get_min_speed(self, *, timeout: Optional[float] = None) -> int:
    """Pump_GetMinSpeed."""
    ...

  async def pump_get_max_speed(self, *, timeout: Optional[float] = None) -> int:
    """Pump_GetMaxSpeed."""
    ...

  async def pump_get_default_speed(self, *, timeout: Optional[float] = None) -> int:
    """Pump_GetDefaultSpeed."""
    ...

  async def pump_start(
    self, speed: int, direction: PumpDirection, *, timeout: Optional[float] = None
  ) -> None:
    """Pump_Start."""
    ...

  async def pump_update(
    self, speed: int, direction: PumpDirection, *, timeout: Optional[float] = None
  ) -> None:
    """Pump_Update."""
    ...

  async def pump_stop(self, *, timeout: Optional[float] = None) -> None:
    """Pump_Stop."""
    ...

  async def sampler_home(self, *, timeout: Optional[float] = None) -> None:
    """Sampler_Home."""
    ...

  async def sampler_get_x(self, *, timeout: Optional[float] = None) -> decimal.Decimal:
    """Sampler_GetX."""
    ...

  async def sampler_get_y(self, *, timeout: Optional[float] = None) -> decimal.Decimal:
    """Sampler_GetY."""
    ...

  async def sampler_get_z(self, *, timeout: Optional[float] = None) -> decimal.Decimal:
    """Sampler_GetZ."""
    ...

  async def sampler_get_x_range(
    self, *, timeout: Optional[float] = None
  ) -> Optional[MovementRange]:
    """Sampler_GetXRange."""
    ...

  async def sampler_get_y_range(
    self, *, timeout: Optional[float] = None
  ) -> Optional[MovementRange]:
    """Sampler_GetYRange."""
    ...

  async def sampler_get_z_range(
    self, *, timeout: Optional[float] = None
  ) -> Optional[MovementRange]:
    """Sampler_GetZRange."""
    ...

  async def sampler_get_z_contact_tolerance(
    self, activity: ContactToleranceType, *, timeout: Optional[float] = None
  ) -> decimal.Decimal:
    """Sampler_GetZContactTolerance."""
    ...

  async def sampler_get_smallest_supported_move(
    self, *, timeout: Optional[float] = None
  ) -> decimal.Decimal:
    """Sampler_GetSmallestSupportedMove."""
    ...

  async def sampler_move_xy(
    self,
    x: decimal.Decimal,
    y: decimal.Decimal,
    fast: bool,
    *,
    timeout: Optional[float] = None,
  ) -> None:
    """Sampler_MoveXY."""
    ...

  async def sampler_move_z(self, z: decimal.Decimal, *, timeout: Optional[float] = None) -> None:
    """Sampler_MoveZ."""
    ...

  async def sampler_find_z_contact(
    self,
    z_high: decimal.Decimal,
    z_low: decimal.Decimal,
    retract_after_find: bool,
    *,
    timeout: Optional[float] = None,
  ) -> Optional[ContactZResult]:
    """Sampler_FindZContact."""
    ...

  async def shaker_min_rpm(self, *, timeout: Optional[float] = None) -> int:
    """Shaker_MinRPM."""
    ...

  async def shaker_max_rpm(self, *, timeout: Optional[float] = None) -> int:
    """Shaker_MaxRPM."""
    ...

  async def shaker_start(self, rpm: int, *, timeout: Optional[float] = None) -> None:
    """Shaker_Start."""
    ...

  async def shaker_stop(self, *, timeout: Optional[float] = None) -> None:
    """Shaker_Stop."""
    ...

  async def shaker_home(self, *, timeout: Optional[float] = None) -> None:
    """Shaker_Home."""
    ...

  async def shaker_open_plate_lock(self, *, timeout: Optional[float] = None) -> None:
    """Shaker_OpenPlateLock."""
    ...

  async def shaker_close_plate_lock(self, *, timeout: Optional[float] = None) -> None:
    """Shaker_ClosePlateLock."""
    ...

  async def worklist_run(
    self, worklist: Optional[Worklist], *, timeout: Optional[float] = None
  ) -> None:
    """Worklist_Run."""
    ...

  async def worklist_cancel(self, *, timeout: Optional[float] = None) -> None:
    """Worklist_Cancel."""
    ...

  async def worklist_get_current(self, *, timeout: Optional[float] = None) -> Optional[Worklist]:
    """Worklist_GetCurrent."""
    ...

  async def worklist_finalize(self, *, timeout: Optional[float] = None) -> None:
    """Worklist_Finalize."""
    ...

  async def worklist_get_status(self, *, timeout: Optional[float] = None) -> WorklistStatus:
    """Worklist_GetStatus."""
    ...

  async def worklist_is_fcs_file_available(self, *, timeout: Optional[float] = None) -> bool:
    """Worklist_IsFCSFileAvailable."""
    ...

  async def controller_download_calibration_settings(self) -> bytes:
    """Controller_DownloadCalibrationSettings (streamed)."""
    ...

  async def controller_upload_calibration_settings(self, data: bytes) -> None:
    """Controller_UploadCalibrationSettings (streamed)."""
    ...

  def worklist_get_current_fcs_file(self) -> AsyncIterator[bytes]:
    """Worklist_GetCurrentFCSFile (streamed), in pieces."""
    ...


class AxisRange(NamedTuple):
  """The travel of one sampler axis, in mm."""

  min: float
  max: float


@dataclass(frozen=True)
class DeviceLimits:
  """What the sampler and the pump report about themselves, read at setup.

  Attributes:
    x_range: Sampler X travel.
    y_range: Sampler Y travel.
    z_range: Sampler Z travel; the maximum is the raised position.
    smallest_move: The smallest sampler step, in mm.
    pump_min_speed: Lowest pump speed, in rpm.
    pump_max_speed: Highest pump speed, in rpm.
    pump_default_speed: The pump's default speed, in rpm.
  """

  x_range: AxisRange
  y_range: AxisRange
  z_range: AxisRange
  smallest_move: float
  pump_min_speed: float
  pump_max_speed: float
  pump_default_speed: float


@dataclass(frozen=True)
class InstrumentStatus:
  """A picture of the instrument's state.

  Attributes:
    system_state: Red, Yellow or Green, or None before the controller reported one.
    devices: The last reported state and messages of each device.
    contact: Whether the probe's contact sensor reports contact, or None before it was read.
    alerts: The last alert of each device that raised one; ``None`` means cleared.
    worklist_status: The controller's worklist status, or None before it was read.
    current_worklist: The worklist running, or the last one that ran, as last reported.
    current_command: The worklist command running, as last reported.
    last_long_running_result: The error text of the last long-running detector operation; empty
      for success, None before one ended.
  """

  system_state: Optional[SystemState]
  devices: Mapping[DeviceKind, DeviceInfo]
  contact: Optional[bool]
  alerts: Mapping[DeviceKind, DeviceAlert]
  worklist_status: Optional[WorklistStatus]
  current_worklist: Optional[Worklist]
  current_command: Optional[WorklistCommand]
  last_long_running_result: Optional[str]


RunPhase = Literal["started", "completed"]


@dataclass(frozen=True)
class RunProgress:
  """A worklist command started or ended.

  Attributes:
    phase: ``started`` just before the command executes; ``completed`` after it ran or was
      skipped.
    index: Position of the command in the worklist.
    total: Number of commands in the worklist.
    command: The command as the controller reported it; a completed one carries its start and
      end times and notes.
    events_received: Events streamed so far in this run.
  """

  phase: RunPhase
  index: int
  total: int
  command: WorklistCommand
  events_received: int


@dataclass(frozen=True)
class CompletedRun:
  """What a worklist run produced.

  Attributes:
    worklist: The worklist as the controller completed it, with the start and end time of every
      command on the controller's clock.
    events: The events from the controller's FCS file, the authoritative copy; None when the
      run did not acquire.
    fcs_keywords: The keywords of that file, or None.
    fcs_file: The bytes of that file, or None.
    stream_event_count: How many events arrived through the event stream during the run.
    stream_consistent: Whether the stream arrived without gaps and in whole events.
    client_time_offset: This computer's clock minus the controller's, as the controller measured
      it when the session started; add it to a controller time to get local time.
  """

  worklist: Worklist
  events: Optional[EventData]
  fcs_keywords: Optional[Dict[str, str]]
  fcs_file: Optional[bytes]
  stream_event_count: int
  stream_consistent: bool
  client_time_offset: datetime.timedelta

  @property
  def stop_reason(self) -> WorklistStopReason:
    """Why the controller ended the run; ``Unspecified`` for a normal end."""
    return self.worklist.stop_reason

  @property
  def commands(self) -> List[WorklistCommand]:
    """The commands as the controller completed them."""
    return [command for command in self.worklist.commands or [] if command is not None]


class _RunDeadline(Exception):
  """A worklist run reached its timeout."""


class _ActiveRun:
  """The run of one worklist: what the callbacks report while it runs."""

  def __init__(
    self, worklist: Worklist, commands: Sequence[WorklistCommand], table: ParameterTable
  ):
    """Create the record of a run about to start.

    Args:
      worklist: The worklist as sent.
      commands: Its commands.
      table: The parameter table of the instrument's events.
    """
    self.worklist = worklist
    self.commands = list(commands)
    self.index_by_id = {command.id: index for index, command in enumerate(commands)}
    self.stream = table.new_events()
    self.values_received = 0
    self.stream_consistent = True
    self.pending_progress: List[RunProgress] = []
    self.completed: Optional[Worklist] = None
    self.problem: Optional[str] = None
    self.wakeup = asyncio.Event()

  def command_event(self, phase: RunPhase, command: Optional[WorklistCommand]) -> None:
    """Record that a command started or ended."""
    if command is None:
      self.problem = f"the controller reported a command {phase} without the command"
    elif command.id not in self.index_by_id:
      self.problem = f"the controller reported command {command.id} {phase}, not in this worklist"
    else:
      self.pending_progress.append(
        RunProgress(
          phase=phase,
          index=self.index_by_id[command.id],
          total=len(self.commands),
          command=command,
          events_received=self.stream.event_count,
        )
      )
    self.wakeup.set()

  def add_chunk(self, values: Optional[array.array[float]], data_index: int) -> None:
    """Add a chunk of streamed event values, checking that the running count follows on."""
    chunk = values if values is not None else array.array("f")
    expected = self.values_received + len(chunk)
    if data_index != expected:
      if self.stream_consistent:
        logger.warning(
          "event stream: running count %d after a chunk of %d values, expected %d; values were "
          "lost, the controller's FCS file is used",
          data_index,
          len(chunk),
          expected,
        )
      self.stream_consistent = False
    self.values_received = data_index
    if len(chunk) % self.stream.parameter_count:
      logger.warning(
        "event stream: a chunk of %d values is not a whole number of %d-value events; dropped",
        len(chunk),
        self.stream.parameter_count,
      )
      self.stream_consistent = False
      return
    self.stream.extend(chunk)

  def finish(self, worklist: Optional[Worklist]) -> None:
    """Record the completed worklist from the completion callback."""
    if worklist is None:
      self.problem = "the controller reported the worklist completed without the worklist"
    else:
      self.completed = worklist
    self.wakeup.set()


def _millimetres(value: float) -> decimal.Decimal:
  """Return a length in mm as the decimal the controller takes, to a thousandth of a mm."""
  return decimal.Decimal(value).quantize(decimal.Decimal("0.001"))


def _axis(movement: Optional[MovementRange], axis: str) -> AxisRange:
  """Convert a reported axis range.

  Raises:
    DeviceResetError: The controller reported no range.
  """
  if movement is None:
    raise DeviceResetError(f"the controller reported no {axis} range for the sampler")
  return AxisRange(float(movement.min), float(movement.max))


def _acquired(worklist: Worklist) -> bool:
  """Whether a completed worklist ran an acquisition command."""
  return any(
    isinstance(command, wire.FlowCytometerAcquireCommand) and command.completed
    for command in worklist.commands or []
  )


class IQueDriver:
  """The session with the iQue Screener PLUS controller, and its features.

  Attributes:
    plate_rail: Loading and ejecting the plate.
    shaker: The plate shaker and its plate lock.
    pump: The peristaltic sample pump.
    sampler: The probe's XYZ sampler and its contact sensor.
    detector: The cytometer.
    rail_position: Where the plate rail is, as far as the driver knows: ``loaded`` (in),
      ``ejected`` (out), or None when it is moving or not known.
  """

  def __init__(
    self,
    host: Optional[str] = None,
    *,
    client: Optional[ControllerApi] = None,
    port: int = CONTROLLER_PORT,
    streaming_port: int = CONTROLLER_STREAMING_PORT,
    request_timeout: float = 60.0,
    reset_timeout: float = 180.0,
    initialize_timeout: float = 120.0,
    ready_timeout: float = 60.0,
    stop_timeout: float = 180.0,
    status_poll_interval: float = 5.0,
    ready_poll_interval: float = 0.5,
    calibration_backup_dir: Optional[pathlib.Path] = None,
    controller_verbose_logging: bool = True,
    supported_controller_versions: Sequence[Tuple[int, int]] = SUPPORTED_CONTROLLER_VERSIONS,
    reset_options: DeviceResetOptions = DEFAULT_RESET_OPTIONS,
  ) -> None:
    """Create the driver; :meth:`setup` connects it.

    Args:
      host: Host name or address of the instrument's internal computer.
      client: The controller client to use instead of connecting to `host`, such as a
        :class:`~pylabrobot.sartorius.ique.driver.simulator.SimulatedController`.
      port: Port of the controller's session endpoint.
      streaming_port: Port of the controller's streamed endpoint.
      request_timeout: Seconds to wait for the reply to an ordinary call.
      reset_timeout: Seconds to wait for Controller_ResetDevices, which reconnects every device.
      initialize_timeout: Seconds to wait for Controller_InitializeDevices; the controller itself
        gives the devices 50 s.
      ready_timeout: Seconds to wait after initialisation for every device to be ready.
      stop_timeout: Seconds to wait for a cancelled worklist to stop; the controller still parks
        the probe, stops the fluidics and ejects the plate.
      status_poll_interval: Seconds between status queries while a worklist runs, so that a lost
        completion callback cannot make a run wait for ever.
      ready_poll_interval: Seconds between device status queries while waiting for readiness.
      calibration_backup_dir: Where :meth:`upload_calibration` keeps the instrument's previous
        calibration file; uploads are refused without it.
      controller_verbose_logging: The controller's log level to set at setup (debug when True).
        The controller stores it.
      supported_controller_versions: Controller versions (major, minor) setup accepts.
      reset_options: The device options sent with the device reset.

    Raises:
      ValueError: Neither or both of `host` and `client` are given.
    """
    if (host is None) == (client is None):
      raise ValueError("give either the instrument's host or a client")
    self.host = host
    if client is None:
      assert host is not None
      client = ControllerClient(
        host, port=port, streaming_port=streaming_port, request_timeout=request_timeout
      )
    self._client: ControllerApi = client
    self._reset_timeout = reset_timeout
    self._initialize_timeout = initialize_timeout
    self._ready_timeout = ready_timeout
    self.stop_timeout = stop_timeout
    self.status_poll_interval = status_poll_interval
    self.ready_poll_interval = ready_poll_interval
    self.calibration_backup_dir = calibration_backup_dir
    self._controller_verbose_logging = controller_verbose_logging
    self._supported_versions = tuple(supported_controller_versions)
    self._reset_options = reset_options
    self._lock = asyncio.Lock()
    self._set_up = False

    self.connection_info: Optional[ConnectionInfo] = None
    self.system_type: Optional[SystemType] = None
    self.system_id: Optional[str] = None
    self.summary: Optional[str] = None
    self.limits: Optional[DeviceLimits] = None
    self.calibration_bytes: Optional[bytes] = None
    self.calibration: Optional[Calibration] = None
    self.calibration_problem: Optional[str] = None
    self.rail_position: Optional[PlatePosition] = None

    self._system_state: Optional[SystemState] = None
    self._devices: Dict[DeviceKind, DeviceInfo] = {}
    self._contact: Optional[bool] = None
    self._contact_changes = 0
    self._alerts: Dict[DeviceKind, DeviceAlert] = {}
    self._worklist_status: Optional[WorklistStatus] = None
    self._current_worklist: Optional[Worklist] = None
    self._current_command: Optional[WorklistCommand] = None
    self._last_long_running_result: Optional[str] = None
    self._long_running: Optional[asyncio.Future[str]] = None
    self._run: Optional[_ActiveRun] = None

    self.plate_rail = PlateRail(self)
    self.shaker = Shaker(self)
    self.pump = Pump(self)
    self.sampler = Sampler(self)
    self.detector = Detector(self)

  # -------------------------------------------------------------------------------------------
  # Calls
  # -------------------------------------------------------------------------------------------

  @property
  def client(self) -> ControllerApi:
    """The controller client; make calls through :meth:`call`."""
    return self._client

  async def call(
    self, method: Callable[_P, Awaitable[_T]], *args: _P.args, **kwargs: _P.kwargs
  ) -> _T:
    """Make one call to the controller, after any call in progress has returned.

    Args:
      method: A method of :attr:`client`.
      *args: Its arguments.
      **kwargs: Its keyword arguments.

    Returns:
      What the method returned.
    """
    async with self._lock:
      return await method(*args, **kwargs)

  async def _download(self, pieces: Callable[[], AsyncIterator[bytes]]) -> bytes:
    """Download a streamed file, holding the call lock until it has arrived."""
    async with self._lock:
      return b"".join([piece async for piece in pieces()])

  @property
  def is_set_up(self) -> bool:
    """Whether :meth:`setup` completed and :meth:`stop` has not been called since."""
    return self._set_up

  def require_setup(self) -> None:
    """Raise unless the driver is set up.

    Raises:
      NotSetUpError: :meth:`setup` has not completed.
    """
    if not self._set_up:
      raise NotSetUpError("the driver is not set up; call setup() first")

  def require_calibration(self) -> Calibration:
    """Return the calibration.

    Raises:
      CalibrationMissingError: The instrument's calibration file was missing or unreadable.
    """
    if self.calibration is None:
      problem = f": {self.calibration_problem}" if self.calibration_problem else ""
      raise CalibrationMissingError(f"no calibration is loaded{problem}")
    return self.calibration

  def require_limits(self) -> DeviceLimits:
    """Return the device limits read at setup.

    Raises:
      NotSetUpError: The limits were not read.
    """
    if self.limits is None:
      raise NotSetUpError("the sampler and pump limits were not read; call setup() first")
    return self.limits

  # -------------------------------------------------------------------------------------------
  # Setup
  # -------------------------------------------------------------------------------------------

  def _register_handlers(self) -> None:
    """Set a handler for every callback the controller sends to the session."""
    client = self._client
    client.on_test_connection(self._on_test_connection)
    client.on_cytometer_has_float_fcs_data(self._on_float_data)
    client.on_cytometer_has_uint_fcs_data(self._on_uint_data)
    client.on_cytometer_long_running_operation_completed(self._on_long_running_completed)
    client.on_system_state_changed(self._on_system_state_changed)
    client.on_device_state_changed(self._on_device_state_changed)
    client.on_contact_sensor_state_changed(self._on_contact_sensor_state_changed)
    client.on_device_alert(self._on_device_alert)
    client.on_command_started(self._on_command_started)
    client.on_command_completed(self._on_command_completed)
    client.on_worklist_completed(self._on_worklist_completed)

  async def setup(self, *, require_calibration: bool = True) -> None:
    """Connect to the controller and bring the instrument up: configure, reset and initialise
    its devices.

    Steps, in order:

    1. Set a handler for every callback, then open the session, so no callback is missed.
    2. ``Controller_Connect`` with this computer's local time; the controller stores the
       difference to its clock as the client time offset of later runs. Refuse when another
       client is registered, and when the controller's version is not supported.
    3. ``Controller_DetermineSystemType``; refuse anything outside the PLUS family.
    4. ``Controller_GetSystemID(False)``, and ``(True)`` when that returns nothing; then
       ``Controller_GetSummary``. Version, system type, ID and summary are logged.
    5. ``Worklist_GetStatus``, and ``Worklist_GetCurrent`` unless no worklist is active.
    6. Download the calibration file over the streamed endpoint and parse it. Nothing has moved
       yet, so a missing file stops setup here (unless `require_calibration` is False). The
       driver never clears the instrument's calibration file.
    7. If a worklist is running (another session started it), attach without resetting: the
       controller refuses a reset while a worklist runs. Otherwise:

       a. ``Controller_SetVerboseLogging``;
       b. ``Controller_ConfigureDevices`` including the shaker, cytometer, plate rail and
          contact sensor;
       c. ``Controller_GetDeviceStatuses``;
       d. ``Controller_EnableDemoMode(False)``, so the controller drives the real devices;
       e. ``Controller_ResetDevices`` with the reset options; it reconnects every device and
          answers False when the system ID it stored does not match the instrument, in which
          case it disconnects the sampler and setup fails;
       f. ``Controller_InitializeDevices([])``: home and park the sampler, stop the pump, close
          the plate lock and home the shaker, home the plate rail and eject it, start the
          contact sensor.

    8. Read the sampler's ranges and smallest step and the pump's speed limits; a reset may
       have connected other devices.
    9. After a reset, wait until every device but the cytometer is ready; a faulted device
       fails setup. A cytometer that is switched off is reported and left off.
    10. Read the system state, the contact sensor and the cytometer's power.

    A worklist that finished but was not finalised is left as it is; :meth:`run_worklist`
    refuses to start until it is finalised (``finalize_previous=True``).

    Args:
      require_calibration: Fail when the calibration file is missing or unreadable. When False,
        such a problem is logged, :attr:`calibration` stays None and operations that need it
        refuse.

    Raises:
      SessionRefusedError: Another client is registered with the controller.
      ControllerVersionError: The controller's version is not supported.
      UnsupportedSystemError: The instrument is not an iQue Screener PLUS.
      CalibrationMissingError: The calibration file is missing or unreadable.
      DeviceResetError: The reset failed or devices did not become ready in time.
      DeviceFaultedError: A device is faulted after initialisation.
      ControllerFault: The controller refused a call.
    """
    if self._set_up:
      raise IQueError("the driver is already set up")
    self._register_handlers()
    await self._client.open()
    try:
      await self._setup_session(require_calibration)
    except BaseException:
      await self._client.close()
      raise
    self._set_up = True
    logger.warning(
      "the iQue Screener PLUS driver has not been verified on an instrument; supervise it"
    )

  async def _setup_session(self, require_calibration: bool) -> None:
    """Run steps 2 to 10 of :meth:`setup` on the open session."""
    client = self._client
    now = datetime.datetime.now().astimezone()
    info = await self.call(client.controller_connect, now)
    if info is None:
      raise IQueError("the controller answered Controller_Connect without connection information")
    self.connection_info = info
    if info.is_client_already_connected:
      raise SessionRefusedError(
        "another client is connected to the controller; close it before connecting"
      )
    version = info.version
    if version is None or (version.major, version.minor) not in self._supported_versions:
      raise ControllerVersionError(version, self._supported_versions)

    system_type = await self.call(client.controller_determine_system_type)
    if system_type not in PLUS_SYSTEM_TYPES:
      raise UnsupportedSystemError(system_type)
    self.system_type = system_type
    system_id = await self.call(client.controller_get_system_id, False)
    if not system_id:
      system_id = await self.call(client.controller_get_system_id, True)
    if not system_id:
      raise DeviceResetError("the controller cannot determine the instrument's system ID")
    self.system_id = system_id
    self.summary = await self.call(client.controller_get_summary)
    logger.info(
      "connected to the iQue controller %s: system type %s, system ID %s%s; %s",
      version,
      system_type,
      system_id,
      f", comments {info.comments!r}" if info.comments else "",
      self.summary,
    )

    status = await self.call(client.worklist_get_status)
    self._worklist_status = status
    if status != "NoActiveWorklist":
      self._current_worklist = await self.call(client.worklist_get_current)

    await self._load_calibration(require_calibration)

    if status == "RunningWorklist":
      name = self._current_worklist.name if self._current_worklist is not None else None
      logger.warning(
        "a worklist (%r) is running on the instrument; attached without resetting the devices. "
        "Wait for it to end or cancel it with cancel_worklist().",
        name,
      )
    else:
      if status == "CompletedUnfinalizedWorklist":
        name = self._current_worklist.name if self._current_worklist is not None else None
        logger.warning(
          "the instrument holds a finished worklist (%r) that was not finalised; run_worklist "
          "needs finalize_previous=True",
          name,
        )
      await self.call(client.controller_set_verbose_logging, self._controller_verbose_logging)
      await self.call(client.controller_configure_devices, dict(PLUS_DEVICE_CONFIGURATION))
      await self._refresh_devices()
      await self.call(client.controller_enable_demo_mode, False)
      initialize = await self.call(
        client.controller_reset_devices, self._reset_options, timeout=self._reset_timeout
      )
      if not initialize:
        raise DeviceResetError(
          "the controller's stored system ID does not match the instrument; it disconnected the "
          "sampler and cleared the stored ID. Run setup again."
        )
      await self.call(client.controller_initialize_devices, [], timeout=self._initialize_timeout)

    x_range = _axis(await self.call(client.sampler_get_x_range), "X")
    y_range = _axis(await self.call(client.sampler_get_y_range), "Y")
    z_range = _axis(await self.call(client.sampler_get_z_range), "Z")
    smallest = await self.call(client.sampler_get_smallest_supported_move)
    pump_min = await self.call(client.pump_get_min_speed)
    pump_max = await self.call(client.pump_get_max_speed)
    pump_default = await self.call(client.pump_get_default_speed)
    self.limits = DeviceLimits(
      x_range=x_range,
      y_range=y_range,
      z_range=z_range,
      smallest_move=float(smallest),
      pump_min_speed=pump_min / 100,
      pump_max_speed=pump_max / 100,
      pump_default_speed=pump_default / 100,
    )

    if status == "RunningWorklist":
      await self._refresh_devices()
    else:
      await self._wait_until_devices_ready()
      self.rail_position = "ejected"
    self._system_state = await self.call(client.controller_get_system_state)
    self._contact = await self.call(client.contact_sensor_has_contact)
    if not await self.call(client.controller_cytometer_has_power):
      logger.warning("the detector is switched off; detector.power_on() switches it on")

  async def _load_calibration(self, require_calibration: bool) -> None:
    """Download and parse the calibration file (setup step 6).

    Raises:
      CalibrationMissingError: The file is missing or unreadable and `require_calibration`.
    """
    try:
      await self.download_calibration()
    except (ControllerFault, CalibrationMissingError) as error:
      if require_calibration:
        raise CalibrationMissingError(
          f"the instrument's calibration file could not be loaded: {error}"
        ) from error
      self.calibration_problem = str(error)
      logger.warning(
        "the instrument's calibration file could not be loaded (%s); operations that need "
        "calibration will refuse",
        error,
      )

  async def _refresh_devices(self) -> Dict[DeviceKind, DeviceInfo]:
    """Read every device's state and replace what the callbacks reported.

    Returns:
      The devices the controller reported.
    """
    reported = await self.call(self._client.controller_get_device_statuses)
    devices = {info.kind: info for info in reported or [] if info is not None}
    self._devices.update(devices)
    return devices

  async def _wait_until_devices_ready(self) -> None:
    """Wait until every device but the cytometer is ready (setup step 9).

    Raises:
      DeviceFaultedError: A device is faulted.
      DeviceResetError: A device is missing, or not ready within the ready timeout.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + self._ready_timeout
    while True:
      devices = await self._refresh_devices()
      missing = [kind for kind in ALL_DEVICES if kind not in devices]
      if missing:
        raise DeviceResetError(f"the controller does not report these devices: {missing}")
      faulted = {kind: info for kind, info in devices.items() if info.state == "Faulted"}
      if faulted:
        raise DeviceFaultedError("finish setup", faulted)
      waiting = [devices[kind] for kind in MECHANICAL_DEVICES if devices[kind].state != "Ready"]
      if not waiting:
        return
      if loop.time() >= deadline:
        states = "; ".join(describe_device(info) for info in waiting)
        raise DeviceResetError(f"devices are not ready after initialisation: {states}")
      await asyncio.sleep(self.ready_poll_interval)

  # -------------------------------------------------------------------------------------------
  # Live state
  # -------------------------------------------------------------------------------------------

  def _on_test_connection(self) -> None:
    """Answer the controller's liveness probe; receiving it is the answer."""
    logger.debug("the controller tested the connection")

  def _on_system_state_changed(self, state: SystemState) -> None:
    """Record the system state."""
    if state != self._system_state:
      logger.info("system state %s", state)
    self._system_state = state

  def _on_device_state_changed(self, info: Optional[DeviceInfo]) -> None:
    """Record a device's state and messages."""
    if info is None:
      logger.warning("the controller reported a device state change without the device")
      return
    previous = self._devices.get(info.kind)
    self._devices[info.kind] = info
    if info.state == "Faulted" and (previous is None or previous.state != "Faulted"):
      logger.error("device fault: %s", describe_device(info))
    if info.kind == "PlateRail" and info.state != "Ready":
      self.rail_position = None

  def _on_contact_sensor_state_changed(self, has_contact: bool) -> None:
    """Record the probe's contact sensor."""
    self._contact = has_contact
    self._contact_changes += 1

  def _on_device_alert(self, alert: Optional[DeviceAlertMessage]) -> None:
    """Record an alert."""
    if alert is None:
      logger.warning("the controller reported an alert without its content")
      return
    self._alerts[alert.kind] = alert.alert
    if alert.alert == "CYT_KillCytometerPower":
      logger.error(
        "the controller lost communication with the detector and is switching it off (%s)",
        alert.kind,
      )
    elif alert.alert != "None":
      logger.warning("device alert from %s: %s", alert.kind, alert.alert)

  def _on_command_started(self, command: Optional[WorklistCommand]) -> None:
    """Record the command that is about to run."""
    self._current_command = command
    if self._run is not None:
      self._run.command_event("started", command)

  def _on_command_completed(self, command: Optional[WorklistCommand]) -> None:
    """Record a command that ended."""
    if self._run is not None:
      self._run.command_event("completed", command)

  def _note_worklist_ended(self, worklist: Optional[Worklist]) -> None:
    """Record that a worklist ended: the controller ejected the plate rail if it could."""
    self._worklist_status = "CompletedUnfinalizedWorklist"
    self._current_command = None
    if worklist is not None:
      self._current_worklist = worklist
    rail = self._devices.get("PlateRail")
    if rail is not None and rail.state == "Ready":
      self.rail_position = "ejected"

  def _on_worklist_completed(self, worklist: Optional[Worklist]) -> None:
    """Record the end of a worklist."""
    self._note_worklist_ended(worklist)
    if self._run is not None:
      self._run.finish(worklist)

  def _on_float_data(self, values: Optional[array.array[float]], data_index: int) -> None:
    """Add streamed event values to the running worklist."""
    if self._run is None:
      logger.warning("event data arrived while no worklist of this driver runs; dropped")
      return
    self._run.add_chunk(values, data_index)

  def _on_uint_data(self, values: Optional[array.array[int]], data_index: int) -> None:
    """Note integer event data, which only older cytometer models send."""
    logger.warning("integer event data arrived; only older cytometer models send it; dropped")
    if self._run is not None:
      self._run.stream_consistent = False

  def _on_long_running_completed(self, error: Optional[str]) -> None:
    """Record the end of a long-running detector operation."""
    result = error or ""
    self._last_long_running_result = result
    if self._long_running is not None and not self._long_running.done():
      self._long_running.set_result(result)

  @property
  def system_state(self) -> Optional[SystemState]:
    """The system state as last reported."""
    return self._system_state

  @property
  def devices(self) -> Mapping[DeviceKind, DeviceInfo]:
    """Every device's state and messages as last reported."""
    return dict(self._devices)

  def device_state(self, kind: DeviceKind) -> Optional[DeviceState]:
    """Return a device's state as last reported, or None when it was never reported."""
    info = self._devices.get(kind)
    return None if info is None else info.state

  @property
  def contact(self) -> Optional[bool]:
    """Whether the probe's contact sensor reported contact last."""
    return self._contact

  @property
  def status(self) -> InstrumentStatus:
    """The instrument's state as last reported, without asking the controller."""
    return InstrumentStatus(
      system_state=self._system_state,
      devices=dict(self._devices),
      contact=self._contact,
      alerts=dict(self._alerts),
      worklist_status=self._worklist_status,
      current_worklist=self._current_worklist,
      current_command=self._current_command,
      last_long_running_result=self._last_long_running_result,
    )

  async def request_status(self) -> InstrumentStatus:
    """Ask the controller for the system state, every device, the contact sensor and the
    worklist status, and return the refreshed picture.

    Raises:
      NotSetUpError: The driver is not set up.
    """
    self.require_setup()
    client = self._client
    self._system_state = await self.call(client.controller_get_system_state)
    await self._refresh_devices()
    self._contact = await self.call(client.contact_sensor_has_contact)
    self._worklist_status = await self.call(client.worklist_get_status)
    if self._worklist_status != "NoActiveWorklist":
      self._current_worklist = await self.call(client.worklist_get_current)
    return self.status

  # -------------------------------------------------------------------------------------------
  # Guards for direct operations
  # -------------------------------------------------------------------------------------------

  async def require_idle(self, action: str) -> None:
    """Raise unless no worklist runs; the status is read afresh.

    Args:
      action: What is about to be done, for the error message.

    Raises:
      NotSetUpError: The driver is not set up.
      WorklistActiveError: A worklist runs.
    """
    self.require_setup()
    if self._run is not None:
      raise WorklistActiveError(f"cannot {action}: a worklist runs", "RunningWorklist")
    status = await self.call(self._client.worklist_get_status)
    self._worklist_status = status
    if status == "RunningWorklist":
      raise WorklistActiveError(f"cannot {action}: a worklist runs on the instrument", status)

  async def require_direct_operation(
    self,
    action: str,
    device: DeviceKind,
    states: Sequence[DeviceState] = ("Ready",),
    *,
    check_other_devices: bool = True,
  ) -> DeviceInfo:
    """Raise unless a direct operation on a device may run now.

    Nothing may run while a worklist runs, and the device must be in one of `states`: the
    controller would otherwise do nothing, without an error. For an operation on a mechanical
    device, no mechanical device may be faulted either, unless `check_other_devices` is False,
    which stopping and parking use. A faulted cytometer never blocks mechanical operations, so
    the probe can be parked and the plate ejected. Device states are read afresh.

    Args:
      action: What is about to be done, for the error message.
      device: The device it acts on.
      states: The states in which it may act.
      check_other_devices: Refuse while another mechanical device is faulted.

    Returns:
      The device's state.

    Raises:
      NotSetUpError: The driver is not set up.
      WorklistActiveError: A worklist runs.
      DeviceFaultedError: The device, or another mechanical device, is faulted.
      DeviceNotReadyError: The device is in another state.
    """
    await self.require_idle(action)
    devices = await self._refresh_devices()
    if check_other_devices and device in MECHANICAL_DEVICES:
      faulted = {
        kind: devices[kind]
        for kind in MECHANICAL_DEVICES
        if kind in devices and devices[kind].state == "Faulted"
      }
      if faulted:
        raise DeviceFaultedError(action, faulted)
    info = devices.get(device)
    if info is not None and info.state == "Faulted":
      raise DeviceFaultedError(action, {device: info})
    if info is None or info.state not in states:
      raise DeviceNotReadyError(action, device, info)
    return info

  async def confirm_operation(self, action: str, device: DeviceKind) -> DeviceInfo:
    """Raise if a device faulted during an operation.

    The controller turns a device's failure into a fault of the device and answers the call
    normally, so every direct operation is followed by this check.

    Args:
      action: What was done, for the error message.
      device: The device it acted on.

    Returns:
      The device's state.

    Raises:
      DeviceFaultedError: The device is faulted.
      DeviceNotReadyError: The device is not reported, or disconnected.
    """
    devices = await self._refresh_devices()
    info = devices.get(device)
    if info is None or info.state == "Disconnected":
      raise DeviceNotReadyError(action, device, info)
    if info.state == "Faulted":
      raise DeviceFaultedError(action, {device: info})
    return info

  async def run_long_running_operation(
    self, operation: str, start: Callable[[], Awaitable[None]], timeout: float
  ) -> None:
    """Start a detector operation whose result arrives as a callback, and wait for it.

    Args:
      operation: Its name, for messages.
      start: Makes the call that starts it.
      timeout: Seconds to wait for the result.

    Raises:
      IQueError: Another such operation is in progress.
      DeviceOperationError: The controller reported an error.
      IQueTimeoutError: No result arrived in time.
    """
    if self._long_running is not None:
      raise IQueError(f"cannot start {operation}: another detector operation is in progress")
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    self._long_running = future
    try:
      await self.call(start)
      # asyncio.wait, unlike asyncio.wait_for on Python 3.10, never swallows a cancellation.
      done, _ = await asyncio.wait({future}, timeout=timeout)
      if not done:
        raise IQueTimeoutError(f"{operation} did not report a result within {timeout} s")
      error = future.result()
    finally:
      self._long_running = None
    if error:
      raise DeviceOperationError(operation, error)

  # -------------------------------------------------------------------------------------------
  # Worklists
  # -------------------------------------------------------------------------------------------

  @staticmethod
  def _check_worklist(worklist: Worklist) -> List[WorklistCommand]:
    """Check a worklist before it is sent.

    Returns:
      Its commands.

    Raises:
      ValueError: It has no command, holds None, repeats a command ID, or holds a command the
        controller cannot execute.
    """
    commands = list(worklist.commands or [])
    if not commands:
      raise ValueError("the worklist has no commands")
    present = [command for command in commands if command is not None]
    if len(present) != len(commands):
      raise ValueError("the worklist holds None in place of a command")
    ids = [command.id for command in present]
    if len(set(ids)) != len(ids):
      raise ValueError("command IDs repeat; every command needs its own ID")
    for command in present:
      if type(command) not in EXECUTABLE_COMMANDS:
        raise ValueError(
          f"the controller cannot execute {type(command).__name__} (command {command.id}); it "
          "would stop the whole worklist before running anything"
        )
    return present

  async def _check_ready_for_run(
    self, worklist: Worklist, commands: Sequence[WorklistCommand], allow_warnings: bool
  ) -> None:
    """Raise unless the monitored devices are ready and, when needed, the cytometer.

    Raises:
      DeviceFaultedError: A monitored device is faulted.
      DeviceNotReadyError: A monitored device is not ready, or the cytometer is not ready to
        run a worklist that needs it.
      IQueError: Devices report warnings and `allow_warnings` is False.
    """
    devices = await self._refresh_devices()
    self._system_state = await self.call(self._client.controller_get_system_state)
    monitored = list(worklist.devices_to_monitor or []) or list(ALL_DEVICES)
    faulted = {
      kind: devices[kind]
      for kind in monitored
      if kind in devices and devices[kind].state == "Faulted"
    }
    if faulted:
      raise DeviceFaultedError("run the worklist", faulted)
    for kind in monitored:
      info = devices.get(kind)
      if info is None or info.state != "Ready":
        raise DeviceNotReadyError("run the worklist", kind, info)
    if any(isinstance(command, CYTOMETER_COMMANDS) for command in commands):
      if not await self.call(self._client.cytometer_is_ready_to_run):
        raise DeviceNotReadyError(
          "run the worklist: the cytometer is not ready to run",
          "Cytometer",
          devices.get("Cytometer"),
        )
    if self._system_state == "Yellow" and not allow_warnings:
      warnings = [
        describe_device(info)
        for info in devices.values()
        if any(m is not None and m.message_level == "Warning" for m in info.messages or [])
      ]
      raise IQueError(
        "devices report warnings (pass allow_warnings=True to run anyway): " + "; ".join(warnings)
      )

  async def run_worklist(
    self,
    worklist: Worklist,
    *,
    on_progress: Optional[Callable[[RunProgress], None]] = None,
    timeout: Optional[float] = None,
    finalize_previous: bool = False,
    allow_warnings: bool = False,
  ) -> CompletedRun:
    """Run a worklist and collect what it produced.

    Before sending, the worklist is checked (commands present, unique IDs, every command one
    the controller executes), no worklist may be running, an unfinalised one is finalised only
    with `finalize_previous`, every monitored device must be ready, and the cytometer must be
    ready to run when the worklist uses it.

    While it runs, `on_progress` is called for every command that starts or ends, and the
    worklist status is polled so that a lost completion callback cannot hang the run. The
    streamed event values are collected and their running count checked. At the end, the
    controller's FCS file is downloaded for a run that acquired and becomes the authoritative
    event data: its checksum must match what the controller reported, its parameters must be
    the instrument's, and it must hold at least the streamed events. Then the worklist is
    finalised.

    If the waiting task is cancelled, the run times out or `on_progress` raises, the worklist is
    cancelled, the driver waits for the controller to stop it (it parks the probe, stops the
    pump, the shaker and the fluidics and ejects the plate) and finalises it before raising.

    Args:
      worklist: The worklist; the controller gives it a new ID when it starts.
      on_progress: Called in the waiting task for every command that starts or ends.
      timeout: Seconds after which the run is cancelled; None waits as long as it takes.
      finalize_previous: Finalise a worklist that finished earlier and was not finalised.
      allow_warnings: Run although devices report warnings (system state Yellow).

    Returns:
      The completed run.

    Raises:
      ValueError: The worklist is malformed.
      WorklistActiveError: A worklist runs, or one is unfinalised without `finalize_previous`.
      DeviceFaultedError: A monitored device is faulted.
      DeviceNotReadyError: A monitored device or the cytometer is not ready.
      WorklistCancelledError: The run was cancelled with :meth:`cancel_worklist`.
      WorklistTimeoutError: The run exceeded `timeout`.
      WorklistFailedError: The controller stopped the run for another reason.
      EventDataError: The event data of a run that acquired is missing or inconsistent.
    """
    self.require_setup()
    commands = self._check_worklist(worklist)
    if self._run is not None:
      raise WorklistActiveError("a worklist of this driver runs", "RunningWorklist")
    client = self._client
    status = await self.call(client.worklist_get_status)
    self._worklist_status = status
    if status == "RunningWorklist":
      raise WorklistActiveError("a worklist runs on the instrument", status)
    if status == "CompletedUnfinalizedWorklist":
      if not finalize_previous:
        raise WorklistActiveError(
          "the instrument holds a finished worklist that was not finalised; pass "
          "finalize_previous=True to finalise it",
          status,
        )
      logger.info("finalising the previous worklist")
      await self._finalize()
    await self._check_ready_for_run(worklist, commands, allow_warnings)

    assert self.system_type is not None
    run = _ActiveRun(worklist, commands, parameter_table_for_system_kind(self.system_type))
    self._run = run
    self.rail_position = None
    try:
      return await self._execute(run, on_progress, timeout)
    finally:
      self._run = None

  async def _execute(
    self,
    run: _ActiveRun,
    on_progress: Optional[Callable[[RunProgress], None]],
    timeout: Optional[float],
  ) -> CompletedRun:
    """Send the worklist, wait for it and collect its results."""
    client = self._client
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    await self.call(client.worklist_run, run.worklist)
    self._worklist_status = "RunningWorklist"
    self._current_worklist = run.worklist
    logger.info("worklist %r started: %d commands", run.worklist.name, len(run.commands))
    try:
      completed = await self._await_completion(run, on_progress, deadline)
    except _RunDeadline:
      logger.error(
        "worklist %r exceeded its timeout of %s s; cancelling", run.worklist.name, timeout
      )
      completed = await self._cancel_and_wait(run)
      try:
        partial = await self._results(run, completed, require_file=False)
      finally:
        await self._finalize()
      raise WorklistTimeoutError(
        f"the worklist did not finish within {timeout} s and was cancelled",
        completed.stop_reason,
        completed.error or "",
        partial,
      ) from None
    except BaseException:
      if client.is_open:
        logger.error("the run was interrupted; cancelling the worklist and finalising it")
        await self._cancel_and_wait(run)
        await self._finalize()
      raise

    succeeded = (
      completed.stop_reason == "Unspecified"
      and completed.all_commands_completed
      and not completed.error
    )
    try:
      results = await self._results(run, completed, require_file=succeeded)
    finally:
      await self._finalize()
    if succeeded:
      logger.info("worklist %r completed", run.worklist.name)
      return results
    message = (
      f"the worklist stopped: stop reason {completed.stop_reason}"
      + (f", error {completed.error!r}" if completed.error else "")
      + ("" if completed.all_commands_completed else ", not every command completed")
    )
    error_type = (
      WorklistCancelledError if completed.stop_reason == "Cancelled" else (WorklistFailedError)
    )
    raise error_type(message, completed.stop_reason, completed.error or "", results)

  def _deliver_progress(
    self, run: _ActiveRun, on_progress: Optional[Callable[[RunProgress], None]]
  ) -> None:
    """Pass the recorded command events to the progress callback."""
    pending, run.pending_progress = run.pending_progress, []
    for progress in pending:
      logger.debug(
        "command %d/%d %s: %s",
        progress.index + 1,
        progress.total,
        progress.phase,
        type(progress.command).__name__,
      )
      if on_progress is not None:
        on_progress(progress)

  async def _await_completion(
    self,
    run: _ActiveRun,
    on_progress: Optional[Callable[[RunProgress], None]],
    deadline: Optional[float],
    *,
    stopping: bool = False,
  ) -> Worklist:
    """Wait for the completion callback, polling the status in case it is lost.

    Args:
      run: The run.
      on_progress: The progress callback.
      deadline: Event-loop time at which to give up, or None.
      stopping: The run is being cancelled; inconsistent callbacks are only logged.

    Raises:
      _RunDeadline: The deadline passed.
      IQueError: The callbacks described another worklist.
    """
    loop = asyncio.get_running_loop()
    next_poll = loop.time() + self.status_poll_interval
    while True:
      self._deliver_progress(run, on_progress)
      if run.problem is not None:
        if not stopping:
          raise IQueError(run.problem)
        logger.error("while the worklist stops: %s", run.problem)
        run.problem = None
      if run.completed is not None:
        return run.completed
      now = loop.time()
      if deadline is not None and now >= deadline:
        raise _RunDeadline()
      wake = next_poll if deadline is None else min(next_poll, deadline)
      # A timer sets the event at the next poll or the deadline; unlike asyncio.wait_for on
      # Python 3.10, waiting on the event never swallows a cancellation of this task.
      timer = loop.call_later(max(0.0, wake - now), run.wakeup.set)
      try:
        await run.wakeup.wait()
      finally:
        timer.cancel()
      run.wakeup.clear()
      if loop.time() >= next_poll and run.completed is None:
        next_poll = loop.time() + self.status_poll_interval
        status = await self.call(self._client.worklist_get_status)
        if status != "RunningWorklist" and run.completed is None:
          recovered = await self._recover_completion(run)
          if recovered is not None:
            return recovered

  async def _recover_completion(self, run: _ActiveRun) -> Optional[Worklist]:
    """Read the completed worklist when the controller says it ended and no callback came.

    The controller marks the run ended slightly before it fills in the stop reason and end time
    and sends the callback, so a worklist still showing the running end time is not taken.

    Returns:
      The completed worklist, or None when it is not complete yet.

    Raises:
      IQueError: The controller's current worklist is not this one.
    """
    current = await self.call(self._client.worklist_get_current)
    if run.completed is not None:
      return run.completed
    if current is None:
      raise IQueError("the worklist ended, but the controller reports no current worklist")
    ids = [command.id for command in current.commands or [] if command is not None]
    if ids != [command.id for command in run.commands]:
      raise IQueError("the controller's current worklist is not the one this driver started")
    if current.end_time.year <= _RUNNING_END_TIME_YEAR:
      return None
    logger.warning(
      "the worklist ended without its completion callback; read it with Worklist_GetCurrent"
    )
    await self._refresh_devices()
    self._note_worklist_ended(current)
    return current

  async def _cancel_and_wait(self, run: _ActiveRun) -> Worklist:
    """Cancel the running worklist and wait for the controller to stop it.

    Raises:
      IQueTimeoutError: It did not stop within the stop timeout.
    """
    await self.call(self._client.worklist_cancel)
    deadline = asyncio.get_running_loop().time() + self.stop_timeout
    try:
      return await self._await_completion(run, None, deadline, stopping=True)
    except _RunDeadline:
      raise IQueTimeoutError(
        f"the controller did not stop the cancelled worklist within {self.stop_timeout} s"
      ) from None

  async def _finalize(self) -> None:
    """Acknowledge the finished worklist so that another can run."""
    await self.call(self._client.worklist_finalize)
    self._worklist_status = "NoActiveWorklist"

  async def _results(
    self, run: _ActiveRun, completed: Worklist, *, require_file: bool
  ) -> CompletedRun:
    """Collect the event data of a finished run.

    Args:
      run: The run.
      completed: The worklist as the controller completed it.
      require_file: Raise when a run that acquired has no FCS file.

    Raises:
      EventDataError: The data is missing (with `require_file`) or inconsistent.
    """
    events: Optional[EventData] = None
    keywords: Optional[Dict[str, str]] = None
    data: Optional[bytes] = None
    streamed = run.stream.event_count
    if _acquired(completed):
      available = await self.call(self._client.worklist_is_fcs_file_available)
      if available:
        data = await self._download(self._client.worklist_get_current_fcs_file)
        events, keywords = self._check_fcs_file(run, completed, data)
      elif require_file:
        raise EventDataError("the run acquired events, but the controller has no FCS file")
      else:
        logger.warning("the run acquired, but the controller has no FCS file")
    if run.values_received != completed.fcs_data_written:
      logger.warning(
        "the controller sent %d event values, %d arrived",
        completed.fcs_data_written,
        run.values_received,
      )
    return CompletedRun(
      worklist=completed,
      events=events,
      fcs_keywords=keywords,
      fcs_file=data,
      stream_event_count=streamed,
      stream_consistent=run.stream_consistent,
      client_time_offset=completed.client_time_offset,
    )

  @staticmethod
  def _check_fcs_file(
    run: _ActiveRun, completed: Worklist, data: bytes
  ) -> Tuple[EventData, Dict[str, str]]:
    """Read the controller's FCS file and check it against the run.

    Raises:
      EventDataError: Wrong checksum, unreadable, other parameters, or fewer events than were
        streamed.
    """
    crc = file_crc32(data)
    if crc != completed.fcs_file_crc:
      raise EventDataError(
        f"the FCS file {completed.fcs_file_name!r} has checksum {crc:08x}; the controller "
        f"reported {completed.fcs_file_crc:08x}"
      )
    try:
      events, keywords = read_fcs(data)
    except FcsError as error:
      raise EventDataError(f"the controller's FCS file is unreadable: {error}") from error
    if events.parameter_names != run.stream.parameter_names:
      raise EventDataError(
        f"the FCS file has parameters {events.parameter_names}, expected "
        f"{run.stream.parameter_names}"
      )
    streamed = run.stream.event_count
    if events.event_count < streamed:
      raise EventDataError(
        f"the FCS file holds {events.event_count} events, fewer than the {streamed} streamed"
      )
    if events.event_count > streamed:
      logger.warning(
        "the FCS file holds %d events, %d were streamed; the file is used",
        events.event_count,
        streamed,
      )
    else:
      logger.info("the FCS file holds the %d streamed events", streamed)
    return events, keywords

  async def cancel_worklist(self) -> None:
    """Ask the controller to cancel the running worklist; it stops within about half a second
    of the command in progress and then parks and ejects. :meth:`run_worklist` raises
    :class:`WorklistCancelledError` when it ends. Nothing is sent when no worklist runs.

    Raises:
      NotSetUpError: The driver is not set up.
    """
    self.require_setup()
    status = await self.call(self._client.worklist_get_status)
    self._worklist_status = status
    if status != "RunningWorklist":
      logger.info("no worklist runs; nothing to cancel")
      return
    await self.call(self._client.worklist_cancel)
    logger.info("cancel requested")

  # -------------------------------------------------------------------------------------------
  # Calibration
  # -------------------------------------------------------------------------------------------

  async def download_calibration(self) -> bytes:
    """Download the instrument's calibration file and keep it with its parsed content.

    Returns:
      The file's bytes.

    Raises:
      ControllerFault: The controller has no calibration file.
      CalibrationMissingError: The file is unreadable; its bytes are kept all the same.
    """
    data = await self.call(self._client.controller_download_calibration_settings)
    self.calibration_bytes = data
    try:
      self.calibration = read_calibration(data)
    except CalibrationError as error:
      self.calibration = None
      self.calibration_problem = str(error)
      raise CalibrationMissingError(
        f"the instrument's calibration file is unreadable: {error}"
      ) from (error)
    self.calibration_problem = None
    return data

  def _write_backup(self, data: bytes) -> pathlib.Path:
    """Write the instrument's calibration file to the backup folder under a new name.

    The name holds the system ID and the UTC time; a number is appended when a file of that name
    exists already, so no backup is ever overwritten.
    """
    assert self.calibration_backup_dir is not None
    self.calibration_backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    stem = f"calibration-{self.system_id or 'instrument'}-{stamp}".replace(":", "-")
    number = 1
    while True:
      path = self.calibration_backup_dir / (
        f"{stem}.cbr" if number == 1 else f"{stem}-{number}.cbr"
      )
      try:
        with path.open("xb") as backup:
          backup.write(data)
      except FileExistsError:
        number += 1
        continue
      logger.info("backed up the instrument's calibration file to %s", path)
      return path

  async def upload_calibration(
    self, data: bytes, *, allow_no_existing_file: bool = False
  ) -> Optional[pathlib.Path]:
    """Store a calibration file on the instrument, after backing up the one it holds.

    The data must be a readable calibration file for this instrument family and, when it names
    an instrument, for this instrument. The instrument's current file is downloaded first and
    written to :attr:`calibration_backup_dir` under a timestamped name; the new file is then
    uploaded, read back and compared.

    Args:
      data: The calibration file.
      allow_no_existing_file: Upload although the instrument holds no file to back up.

    Returns:
      The backup's path, or None when there was nothing to back up.

    Raises:
      CalibrationRefusedError: No backup folder is set, the data is unreadable, too large, or
        for another instrument, the current file cannot be backed up, or the file read back
        differs.
    """
    self.require_setup()
    if self.calibration_backup_dir is None:
      raise CalibrationRefusedError(
        "uploading a calibration file needs calibration_backup_dir, where the instrument's "
        "current file is kept first"
      )
    if len(data) > MAX_FILE_SIZE:
      raise CalibrationRefusedError(
        f"the file is {len(data)} bytes; the instrument accepts {MAX_FILE_SIZE}"
      )
    try:
      parsed = read_calibration(data)
    except CalibrationError as error:
      raise CalibrationRefusedError(f"the data is not a readable calibration file: {error}") from (
        error
      )
    instrument = parsed.instrument
    if instrument.system_kind != self.system_type:
      raise CalibrationRefusedError(
        f"the file is for system kind {instrument.system_kind}; the instrument is "
        f"{self.system_type}"
      )
    if instrument.device_id and instrument.device_id != self.system_id:
      raise CalibrationRefusedError(
        f"the file is for instrument {instrument.device_id!r}; this is {self.system_id!r}"
      )
    backup: Optional[pathlib.Path] = None
    try:
      current = await self.call(self._client.controller_download_calibration_settings)
    except ControllerFault as fault:
      if not allow_no_existing_file:
        raise CalibrationRefusedError(
          f"the instrument's current calibration file cannot be backed up ({fault}); pass "
          "allow_no_existing_file=True if it holds none"
        ) from fault
      logger.warning("the instrument holds no calibration file to back up (%s)", fault)
    else:
      backup = self._write_backup(current)
    await self.call(self._client.controller_upload_calibration_settings, data)
    stored = await self.call(self._client.controller_download_calibration_settings)
    if stored != data:
      raise CalibrationRefusedError(
        f"the instrument returned a different file after the upload; the previous one is at "
        f"{backup}"
      )
    self.calibration_bytes = data
    self.calibration = parsed
    self.calibration_problem = None
    logger.info("stored a new calibration file on the instrument")
    return backup

  # -------------------------------------------------------------------------------------------
  # Stop
  # -------------------------------------------------------------------------------------------

  async def stop(self, *, cancel: bool = False) -> None:
    """Leave the instrument at rest and end the session.

    Stops the pump and the shaker and parks the probe, then closes the session. A device that
    is faulted is skipped and reported after the session is closed.

    Args:
      cancel: Cancel a running worklist first and wait for it to stop; without it, a running
        worklist makes stop refuse.

    Raises:
      WorklistActiveError: A worklist runs and `cancel` is False.
      UnsafeMotionError: The probe reports contact and cannot be parked.
      DeviceFaultedError: A device was faulted and was skipped; the session is closed.
    """
    if not self._set_up:
      return
    status = await self.call(self._client.worklist_get_status)
    if status == "RunningWorklist" or self._run is not None:
      if not cancel:
        raise WorklistActiveError(
          "a worklist runs; wait for it, or pass cancel=True to cancel it", "RunningWorklist"
        )
      await self._cancel_for_stop()
    skipped: Dict[DeviceKind, DeviceInfo] = {}
    try:
      devices = await self._refresh_devices()
      for kind in ("Pump", "Shaker", "Sampler"):
        if devices.get(kind) is not None and devices[kind].state == "Faulted":
          skipped[kind] = devices[kind]
          logger.error("not stopping the faulted %s", kind)
      if "Pump" not in skipped:
        await self.pump.stop()
      if "Shaker" not in skipped:
        await self.shaker.stop()
      if "Sampler" not in skipped:
        await self.sampler.home()
    finally:
      self._set_up = False
      await self._client.close()
      logger.info("session with the iQue controller closed")
    if skipped:
      raise DeviceFaultedError("stop every device", skipped)

  async def _cancel_for_stop(self) -> None:
    """Cancel the running worklist and wait until the controller no longer runs it."""
    await self.call(self._client.worklist_cancel)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + self.stop_timeout
    while True:
      if self._run is None:
        status = await self.call(self._client.worklist_get_status)
        if status != "RunningWorklist":
          return
      if loop.time() >= deadline:
        raise IQueTimeoutError(
          f"the controller did not stop the cancelled worklist within {self.stop_timeout} s"
        )
      await asyncio.sleep(self.ready_poll_interval)
