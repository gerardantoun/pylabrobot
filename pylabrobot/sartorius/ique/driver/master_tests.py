"""Tests of the driver against a scripted fake controller: the setup sequence, the live state,
worklist runs, calibration, stop, and every refusal."""

import array
import asyncio
import dataclasses
import datetime
import decimal
import logging
import pathlib
import tempfile
import unittest
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Sequence, Tuple

from pylabrobot.sartorius.ique.driver import contract as wire
from pylabrobot.sartorius.ique.driver.calibration import write_calibration
from pylabrobot.sartorius.ique.driver.client import ControllerClient, ControllerFault
from pylabrobot.sartorius.ique.driver.contract import (
  ConnectionInfo,
  DeviceInfo,
  DeviceKind,
  DeviceMessage,
  DeviceState,
  MovementRange,
  Version,
  Worklist,
  WorklistStatus,
  WorklistStopReason,
)
from pylabrobot.sartorius.ique.driver.errors import (
  CalibrationMissingError,
  CalibrationRefusedError,
  ControllerVersionError,
  DeviceFaultedError,
  DeviceNotReadyError,
  DeviceResetError,
  EventDataError,
  IQueError,
  NotSetUpError,
  SessionRefusedError,
  UnsafeMotionError,
  UnsupportedSystemError,
  WorklistActiveError,
  WorklistCancelledError,
  WorklistFailedError,
  WorklistTimeoutError,
)
from pylabrobot.sartorius.ique.driver.fcs import (
  EventData,
  file_crc32,
  instrument_keywords,
  parameter_table_for_system_kind,
  write_fcs,
)
from pylabrobot.sartorius.ique.driver.master import (
  ALL_DEVICES,
  DEFAULT_RESET_OPTIONS,
  PLUS_DEVICE_CONFIGURATION,
  ControllerApi,
  IQueDriver,
  RunProgress,
)
from pylabrobot.sartorius.ique.driver.simulator import controller_fault, simulated_calibration
from pylabrobot.sartorius.ique.driver.simulator_tests import (
  CommandIds,
  acquire,
  make_worklist,
  sample_well,
  start_pump,
  stop_fluidics,
  stop_pump,
)

D = decimal.Decimal
TABLE = parameter_table_for_system_kind("iQue3")
CALIBRATION = write_calibration(simulated_calibration())
VERSION = Version(7, 2, 6010, 25563)
START = datetime.datetime(2026, 1, 5, 8, 0, tzinfo=datetime.timezone.utc)
CALLBACKS = (
  "TestConnection",
  "Cytometer_OnHasFloatFCSData",
  "Cytometer_OnHasUIntFCSData",
  "OnCytometerLongRunningOperationCompleted",
  "OnSystemStateChanged",
  "OnDeviceStateChanged",
  "OnContactSensorStateChanged",
  "OnDeviceAlert",
  "OnCommandStarted",
  "OnCommandCompleted",
  "OnWorklistCompleted",
)


def events(count: int, first_tick: int = 0) -> EventData:
  """Return `count` events of the instrument's 32 parameters, one 5 ms tick apart."""
  values: List[float] = []
  for index in range(count):
    values += [float(index + 1)] * (len(TABLE.parameters) - 1) + [float(first_tick + index)]
  return TABLE.new_events(values)


def fcs_file(data: EventData) -> bytes:
  """Return the controller's FCS file of some events."""
  return write_fcs(data, instrument_keywords(TABLE, events=data))


def chunks(data: EventData, size: int) -> List[array.array]:
  """Cut events into stream chunks of `size` events."""
  width = data.parameter_count
  step = size * width
  return [data.values[start : start + step] for start in range(0, len(data.values), step)]


@dataclass
class RunPlan:
  """How the fake controller plays a worklist.

  Attributes:
    stream: The event chunks streamed after the first command.
    file_events: The events of the controller's FCS file; None for no file.
    stop_reason: The stop reason reported.
    error: The error text reported.
    stop_before: Index of the first command not run (the run stops there).
    wait_for_cancel: Stall at the second command until cancelled.
    lose_completion: Do not send the completion callback.
    provisional_reads: How many times Worklist_GetCurrent still shows the running end time.
    crc_offset: Added to the reported checksum.
    index_gap: Extra values added to the running count of the first chunk.
    foreign_command: Report this command as started instead of the worklist's own.
  """

  stream: Sequence[array.array] = ()
  file_events: Optional[EventData] = None
  stop_reason: WorklistStopReason = "Unspecified"
  error: str = ""
  stop_before: Optional[int] = None
  wait_for_cancel: bool = False
  lose_completion: bool = False
  provisional_reads: int = 0
  crc_offset: int = 0
  index_gap: int = 0
  foreign_command: Optional[wire.WorklistCommand] = None


class FakeController:
  """A scripted controller: records every call and answers from `replies`.

  A reply is a value, an exception to raise, or a function of the call's arguments.
  """

  def __init__(self) -> None:
    """Script the answers of a healthy iQue3 with nothing running."""
    self.calls: List[Tuple[str, Tuple[Any, ...]]] = []
    self.timeouts: Dict[str, Optional[float]] = {}
    self.handlers: Dict[str, Callable[..., None]] = {}
    self.handlers_at_open: List[str] = []
    self.is_open_flag = False
    self.states: Dict[DeviceKind, DeviceState] = {kind: "Ready" for kind in ALL_DEVICES}
    self.messages: Dict[DeviceKind, List[DeviceMessage]] = {}
    self.status: WorklistStatus = "NoActiveWorklist"
    self.current: Optional[Worklist] = None
    self.cancelled = False
    self.fcs: Optional[bytes] = None
    self.calibration: Optional[bytes] = CALIBRATION
    self.position = [131.0, 195.0, 250.0]
    self.contact = False
    self.in_call = 0
    self.overlaps = 0
    self.plan = RunPlan()
    self.player: Optional["asyncio.Task[None]"] = None
    self.waiting = asyncio.Event()
    self.replies: Dict[str, Any] = {
      "controller_connect": ConnectionInfo(
        comments="", is_client_already_connected=False, version=VERSION
      ),
      "controller_set_verbose_logging": None,
      "controller_enable_demo_mode": None,
      "controller_get_system_id": "00155D010203",
      "controller_determine_system_type": "iQue3",
      "controller_get_summary": "Windows 10, 64 bit, .NET 4.8",
      "controller_get_device_summary": "summary",
      "controller_get_system_state": "Green",
      "controller_configure_devices": None,
      "controller_reset_devices": True,
      "controller_initialize_devices": None,
      "controller_get_device_statuses": lambda: [self.info(kind) for kind in ALL_DEVICES],
      "controller_power_on_cytometer": None,
      "controller_cytometer_has_power": True,
      "cytometer_is_powering_on": False,
      "cytometer_is_ready_to_run": True,
      "cytometer_is_ready_to_clean": True,
      "cytometer_get_operation_duration": datetime.timedelta(0),
      "cytometer_shutdown": None,
      "cytometer_calibrate_fluid_stations": None,
      "cytometer_reset_consumables_odometer": None,
      "ique3_run_next_purge_step": None,
      "ique3_get_current_purge_step": ("None", True),
      "ique3_run_next_installation_priming_step": None,
      "ique3_get_current_installation_priming_step": ("None", True),
      "contact_sensor_has_contact": lambda: self.contact,
      "plate_rail_load": None,
      "plate_rail_eject": None,
      "pump_get_min_speed": 160,
      "pump_get_max_speed": 16000,
      "pump_get_default_speed": 2860,
      "pump_start": None,
      "pump_update": None,
      "pump_stop": lambda: self.set_state("Pump", "Ready"),
      "sampler_home": self.park,
      "sampler_get_x": lambda: D(repr(self.position[0])),
      "sampler_get_y": lambda: D(repr(self.position[1])),
      "sampler_get_z": lambda: D(repr(self.position[2])),
      "sampler_get_x_range": MovementRange(max=D(131), min=D(0)),
      "sampler_get_y_range": MovementRange(max=D(195), min=D(0)),
      "sampler_get_z_range": MovementRange(max=D(250), min=D(190)),
      "sampler_get_z_contact_tolerance": D("0.25"),
      "sampler_get_smallest_supported_move": D("0.05"),
      "sampler_move_xy": None,
      "sampler_move_z": None,
      "sampler_find_z_contact": None,
      "shaker_min_rpm": 100,
      "shaker_max_rpm": 3000,
      "shaker_start": None,
      "shaker_stop": lambda: self.set_state("Shaker", "Ready"),
      "shaker_home": None,
      "shaker_open_plate_lock": None,
      "shaker_close_plate_lock": None,
      "worklist_run": self.start_run,
      "worklist_cancel": self.cancel,
      "worklist_get_current": self.get_current,
      "worklist_finalize": self.finalize,
      "worklist_get_status": lambda: self.status,
      "worklist_is_fcs_file_available": lambda: self.fcs is not None,
    }

  # Scripting helpers.

  def info(self, kind: DeviceKind) -> DeviceInfo:
    """Return a device's state as the controller would report it."""
    return DeviceInfo(
      kind=kind, messages=list(self.messages.get(kind, [])), model="model", state=self.states[kind]
    )

  def set_state(self, kind: DeviceKind, state: DeviceState) -> None:
    """Change a device's state."""
    self.states[kind] = state

  def park(self) -> None:
    """Park the probe."""
    self.position = [131.0, 195.0, 250.0]

  def fire(self, callback: str, *arguments: Any) -> None:
    """Send a callback to the driver."""
    self.handlers[callback](*arguments)

  def names(self) -> List[str]:
    """The names of the calls made, in order."""
    return [name for name, _ in self.calls]

  def arguments(self, name: str) -> Tuple[Any, ...]:
    """The arguments of the first call of an operation."""
    return next(arguments for called, arguments in self.calls if called == name)

  def start_run(self, worklist: Worklist) -> None:
    """Start playing the run plan, as Worklist_Run starts the worklist thread."""
    self.status = "RunningWorklist"
    self.cancelled = False
    self.player = asyncio.ensure_future(self.play(worklist, self.plan))

  def cancel(self) -> None:
    """Request cancellation."""
    self.cancelled = True

  def finalize(self) -> None:
    """Acknowledge the finished worklist."""
    self.status = "NoActiveWorklist"

  def get_current(self) -> Optional[Worklist]:
    """Return the current worklist, with the running end time while reads are provisional."""
    if self.current is not None and self.plan.provisional_reads > 0:
      self.plan.provisional_reads -= 1
      return dataclasses.replace(self.current, end_time=datetime.datetime(1900, 1, 1))
    return self.current

  async def play(self, worklist: Worklist, plan: RunPlan) -> None:
    """Play a worklist: command callbacks, data after the first command, completion."""
    await asyncio.sleep(0)
    commands = list(worklist.commands or [])
    done: List[Optional[wire.WorklistCommand]] = list(commands)
    sent = 0
    stop_reason = plan.stop_reason
    for index, command in enumerate(commands):
      assert command is not None
      if plan.stop_before is not None and index == plan.stop_before:
        break
      self.fire("OnCommandStarted", plan.foreign_command or command)
      await asyncio.sleep(0)
      if plan.wait_for_cancel and index == 1:
        self.waiting.set()
        while not self.cancelled:
          await asyncio.sleep(0.001)
        stop_reason = "Cancelled"
        break
      begin = START + datetime.timedelta(seconds=index)
      done[index] = dataclasses.replace(
        command,
        start_time=begin,
        end_time=begin + datetime.timedelta(seconds=1),
        completed=True,
        execution_notes="",
      )
      self.fire("OnCommandCompleted", done[index])
      if index == 0:
        for number, chunk in enumerate(plan.stream):
          sent += len(chunk) + (plan.index_gap if number == 0 else 0)
          self.fire("Cytometer_OnHasFloatFCSData", chunk, sent)
          await asyncio.sleep(0)
    finished = all(c is not None and c.completed for c in done)
    self.fcs = None if plan.file_events is None else fcs_file(plan.file_events)
    completed = dataclasses.replace(
      worklist,
      commands=done,
      all_commands_completed=finished and stop_reason == "Unspecified",
      stop_reason=stop_reason,
      error=plan.error,
      start_time=START,
      end_time=START + datetime.timedelta(seconds=len(commands) + 5),
      client_time_offset=datetime.timedelta(seconds=2),
      fcs_file_name="20260105-080000-iQue Screener PLUS - VBR.fcs",
      fcs_data_written=sent,
      fcs_file_crc=(file_crc32(self.fcs) + plan.crc_offset) & 0xFFFFFFFF if self.fcs else 0,
    )
    self.current = completed
    self.status = "CompletedUnfinalizedWorklist"
    if not plan.lose_completion:
      self.fire("OnWorklistCompleted", completed)

  # The interface.

  async def _answer(self, name: str, *arguments: Any, timeout: Optional[float] = None) -> Any:
    """Record a call and answer it from the script."""
    self.calls.append((name, arguments))
    self.timeouts[name] = timeout
    if self.in_call:
      self.overlaps += 1
    self.in_call += 1
    try:
      await asyncio.sleep(0)
      reply = self.replies[name]
      if isinstance(reply, BaseException):
        raise reply
      if callable(reply):
        return reply(*arguments)
      return reply
    finally:
      self.in_call -= 1

  @property
  def is_open(self) -> bool:
    """Whether the session is open."""
    return self.is_open_flag

  async def open(self) -> None:
    """Open the session."""
    self.calls.append(("open", ()))
    self.handlers_at_open = sorted(self.handlers)
    self.is_open_flag = True

  async def close(self) -> None:
    """Close the session."""
    self.calls.append(("close", ()))
    self.is_open_flag = False

  def _handler(self, callback: str, handler: Optional[Callable[..., None]]) -> None:
    """Set a callback handler."""
    assert handler is not None
    self.handlers[callback] = handler

  def on_test_connection(self, handler: Optional[Callable[[], None]]) -> None:
    """Set the handler."""
    self._handler("TestConnection", handler)

  def on_cytometer_has_uint_fcs_data(
    self, handler: Optional[Callable[[Optional["array.array[int]"], int], None]]
  ) -> None:
    """Set the handler."""
    self._handler("Cytometer_OnHasUIntFCSData", handler)

  def on_cytometer_has_float_fcs_data(
    self, handler: Optional[Callable[[Optional["array.array[float]"], int], None]]
  ) -> None:
    """Set the handler."""
    self._handler("Cytometer_OnHasFloatFCSData", handler)

  def on_cytometer_long_running_operation_completed(
    self, handler: Optional[Callable[[Optional[str]], None]]
  ) -> None:
    """Set the handler."""
    self._handler("OnCytometerLongRunningOperationCompleted", handler)

  def on_system_state_changed(self, handler: Optional[Callable[[Any], None]]) -> None:
    """Set the handler."""
    self._handler("OnSystemStateChanged", handler)

  def on_device_state_changed(self, handler: Optional[Callable[[Any], None]]) -> None:
    """Set the handler."""
    self._handler("OnDeviceStateChanged", handler)

  def on_contact_sensor_state_changed(self, handler: Optional[Callable[[bool], None]]) -> None:
    """Set the handler."""
    self._handler("OnContactSensorStateChanged", handler)

  def on_device_alert(self, handler: Optional[Callable[[Any], None]]) -> None:
    """Set the handler."""
    self._handler("OnDeviceAlert", handler)

  def on_command_started(self, handler: Optional[Callable[[Any], None]]) -> None:
    """Set the handler."""
    self._handler("OnCommandStarted", handler)

  def on_command_completed(self, handler: Optional[Callable[[Any], None]]) -> None:
    """Set the handler."""
    self._handler("OnCommandCompleted", handler)

  def on_worklist_completed(self, handler: Optional[Callable[[Any], None]]) -> None:
    """Set the handler."""
    self._handler("OnWorklistCompleted", handler)

  async def controller_connect(
    self, client_time: datetime.datetime, *, timeout: Optional[float] = None
  ) -> Any:
    """Scripted."""
    return await self._answer("controller_connect", client_time, timeout=timeout)

  async def controller_set_verbose_logging(
    self, verbose_logging: bool, *, timeout: Optional[float] = None
  ) -> Any:
    """Scripted."""
    return await self._answer("controller_set_verbose_logging", verbose_logging, timeout=timeout)

  async def controller_enable_demo_mode(
    self, demo_mode: bool, *, timeout: Optional[float] = None
  ) -> Any:
    """Scripted."""
    return await self._answer("controller_enable_demo_mode", demo_mode, timeout=timeout)

  async def controller_get_system_id(
    self, force_refresh: bool, *, timeout: Optional[float] = None
  ) -> Any:
    """Scripted."""
    return await self._answer("controller_get_system_id", force_refresh, timeout=timeout)

  async def controller_determine_system_type(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("controller_determine_system_type", timeout=timeout)

  async def controller_get_summary(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("controller_get_summary", timeout=timeout)

  async def controller_get_device_summary(
    self, device_kind: DeviceKind, *, timeout: Optional[float] = None
  ) -> Any:
    """Scripted."""
    return await self._answer("controller_get_device_summary", device_kind, timeout=timeout)

  async def controller_get_system_state(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("controller_get_system_state", timeout=timeout)

  async def controller_configure_devices(
    self, configuration: Any, *, timeout: Optional[float] = None
  ) -> Any:
    """Scripted."""
    return await self._answer("controller_configure_devices", configuration, timeout=timeout)

  async def controller_reset_devices(self, options: Any, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("controller_reset_devices", options, timeout=timeout)

  async def controller_initialize_devices(
    self, device_initialization_list: Any, *, timeout: Optional[float] = None
  ) -> Any:
    """Scripted."""
    return await self._answer(
      "controller_initialize_devices", device_initialization_list, timeout=timeout
    )

  async def controller_get_device_statuses(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("controller_get_device_statuses", timeout=timeout)

  async def controller_power_on_cytometer(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("controller_power_on_cytometer", timeout=timeout)

  async def controller_cytometer_has_power(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("controller_cytometer_has_power", timeout=timeout)

  async def cytometer_is_powering_on(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("cytometer_is_powering_on", timeout=timeout)

  async def cytometer_is_ready_to_run(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("cytometer_is_ready_to_run", timeout=timeout)

  async def cytometer_is_ready_to_clean(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("cytometer_is_ready_to_clean", timeout=timeout)

  async def cytometer_get_operation_duration(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("cytometer_get_operation_duration", timeout=timeout)

  async def cytometer_shutdown(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("cytometer_shutdown", timeout=timeout)

  async def cytometer_calibrate_fluid_stations(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("cytometer_calibrate_fluid_stations", timeout=timeout)

  async def cytometer_reset_consumables_odometer(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("cytometer_reset_consumables_odometer", timeout=timeout)

  async def ique3_run_next_purge_step(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("ique3_run_next_purge_step", timeout=timeout)

  async def ique3_get_current_purge_step(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("ique3_get_current_purge_step", timeout=timeout)

  async def ique3_run_next_installation_priming_step(
    self, *, timeout: Optional[float] = None
  ) -> Any:
    """Scripted."""
    return await self._answer("ique3_run_next_installation_priming_step", timeout=timeout)

  async def ique3_get_current_installation_priming_step(
    self, *, timeout: Optional[float] = None
  ) -> Any:
    """Scripted."""
    return await self._answer("ique3_get_current_installation_priming_step", timeout=timeout)

  async def contact_sensor_has_contact(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("contact_sensor_has_contact", timeout=timeout)

  async def plate_rail_load(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("plate_rail_load", timeout=timeout)

  async def plate_rail_eject(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("plate_rail_eject", timeout=timeout)

  async def pump_get_min_speed(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("pump_get_min_speed", timeout=timeout)

  async def pump_get_max_speed(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("pump_get_max_speed", timeout=timeout)

  async def pump_get_default_speed(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("pump_get_default_speed", timeout=timeout)

  async def pump_start(self, speed: int, direction: Any, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("pump_start", speed, direction, timeout=timeout)

  async def pump_update(
    self, speed: int, direction: Any, *, timeout: Optional[float] = None
  ) -> Any:
    """Scripted."""
    return await self._answer("pump_update", speed, direction, timeout=timeout)

  async def pump_stop(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("pump_stop", timeout=timeout)

  async def sampler_home(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("sampler_home", timeout=timeout)

  async def sampler_get_x(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("sampler_get_x", timeout=timeout)

  async def sampler_get_y(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("sampler_get_y", timeout=timeout)

  async def sampler_get_z(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("sampler_get_z", timeout=timeout)

  async def sampler_get_x_range(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("sampler_get_x_range", timeout=timeout)

  async def sampler_get_y_range(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("sampler_get_y_range", timeout=timeout)

  async def sampler_get_z_range(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("sampler_get_z_range", timeout=timeout)

  async def sampler_get_z_contact_tolerance(
    self, activity: Any, *, timeout: Optional[float] = None
  ) -> Any:
    """Scripted."""
    return await self._answer("sampler_get_z_contact_tolerance", activity, timeout=timeout)

  async def sampler_get_smallest_supported_move(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("sampler_get_smallest_supported_move", timeout=timeout)

  async def sampler_move_xy(
    self, x: decimal.Decimal, y: decimal.Decimal, fast: bool, *, timeout: Optional[float] = None
  ) -> Any:
    """Scripted."""
    return await self._answer("sampler_move_xy", x, y, fast, timeout=timeout)

  async def sampler_move_z(self, z: decimal.Decimal, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("sampler_move_z", z, timeout=timeout)

  async def sampler_find_z_contact(
    self,
    z_high: decimal.Decimal,
    z_low: decimal.Decimal,
    retract_after_find: bool,
    *,
    timeout: Optional[float] = None,
  ) -> Any:
    """Scripted."""
    return await self._answer(
      "sampler_find_z_contact", z_high, z_low, retract_after_find, timeout=timeout
    )

  async def shaker_min_rpm(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("shaker_min_rpm", timeout=timeout)

  async def shaker_max_rpm(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("shaker_max_rpm", timeout=timeout)

  async def shaker_start(self, rpm: int, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("shaker_start", rpm, timeout=timeout)

  async def shaker_stop(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("shaker_stop", timeout=timeout)

  async def shaker_home(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("shaker_home", timeout=timeout)

  async def shaker_open_plate_lock(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("shaker_open_plate_lock", timeout=timeout)

  async def shaker_close_plate_lock(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("shaker_close_plate_lock", timeout=timeout)

  async def worklist_run(self, worklist: Any, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("worklist_run", worklist, timeout=timeout)

  async def worklist_cancel(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("worklist_cancel", timeout=timeout)

  async def worklist_get_current(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("worklist_get_current", timeout=timeout)

  async def worklist_finalize(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("worklist_finalize", timeout=timeout)

  async def worklist_get_status(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("worklist_get_status", timeout=timeout)

  async def worklist_is_fcs_file_available(self, *, timeout: Optional[float] = None) -> Any:
    """Scripted."""
    return await self._answer("worklist_is_fcs_file_available", timeout=timeout)

  async def controller_download_calibration_settings(self) -> bytes:
    """Return the stored calibration file, or refuse when there is none."""
    self.calls.append(("controller_download_calibration_settings", ()))
    if self.calibration is None:
      raise controller_fault(
        "Controller_DownloadCalibrationSettings", "No calibration settings stored"
      )
    return self.calibration

  async def controller_upload_calibration_settings(self, data: bytes) -> None:
    """Store a calibration file."""
    self.calls.append(("controller_upload_calibration_settings", (data,)))
    self.calibration = data

  async def worklist_get_current_fcs_file(self) -> AsyncIterator[bytes]:
    """Return the current FCS file."""
    self.calls.append(("worklist_get_current_fcs_file", ()))
    if self.fcs is None:
      raise controller_fault("Worklist_GetCurrentFCSFile", "no file")
    yield self.fcs[:100]
    yield self.fcs[100:]


SETUP_CALLS = [
  "open",
  "controller_connect",
  "controller_determine_system_type",
  "controller_get_system_id",
  "controller_get_summary",
  "worklist_get_status",
  "controller_download_calibration_settings",
  "controller_set_verbose_logging",
  "controller_configure_devices",
  "controller_get_device_statuses",
  "controller_enable_demo_mode",
  "controller_reset_devices",
  "controller_initialize_devices",
  "sampler_get_x_range",
  "sampler_get_y_range",
  "sampler_get_z_range",
  "sampler_get_smallest_supported_move",
  "pump_get_min_speed",
  "pump_get_max_speed",
  "pump_get_default_speed",
  "controller_get_device_statuses",
  "controller_get_system_state",
  "contact_sensor_has_contact",
  "controller_cytometer_has_power",
]


def make_driver(fake: FakeController, **options: Any) -> IQueDriver:
  """Return a driver on a fake controller with short poll intervals."""
  options.setdefault("status_poll_interval", 0.01)
  options.setdefault("ready_poll_interval", 0.001)
  return IQueDriver(client=fake, **options)


async def set_up(
  fake: Optional[FakeController] = None, **options: Any
) -> Tuple[IQueDriver, FakeController]:
  """Return a set-up driver on a fake controller, with the setup calls cleared."""
  fake = fake or FakeController()
  driver = make_driver(fake, **options)
  await driver.setup()
  fake.calls.clear()
  return driver, fake


def acquiring_worklist(wells: int = 2) -> Worklist:
  """A worklist that acquires: acquire, pump, wells, stop pump, stop fluidics."""
  ids = CommandIds()
  commands: List[wire.WorklistCommand] = [acquire(ids), start_pump(ids)]
  commands += [sample_well(ids, column) for column in range(1, wells + 1)]
  commands += [stop_pump(ids), stop_fluidics(ids)]
  return make_worklist(commands)


class SetupTests(unittest.IsolatedAsyncioTestCase):
  """The setup sequence and its refusals."""

  async def test_sequence(self) -> None:
    """Setup makes the instrument client's calls in its order, with its values."""
    fake = FakeController()
    driver = make_driver(fake)
    with self.assertLogs("pylabrobot.sartorius.ique.driver.master", logging.WARNING) as logs:
      await driver.setup()
    self.assertEqual(fake.names(), SETUP_CALLS)
    self.assertIn("not been verified", "\n".join(logs.output))
    (client_time,) = fake.arguments("controller_connect")
    self.assertIsNotNone(client_time.utcoffset())
    self.assertEqual(fake.arguments("controller_get_system_id"), (False,))
    self.assertEqual(fake.arguments("controller_set_verbose_logging"), (True,))
    (configuration,) = fake.arguments("controller_configure_devices")
    self.assertEqual(list(configuration), ["Shaker", "Cytometer", "PlateRail", "ContactSensor"])
    self.assertEqual(configuration, PLUS_DEVICE_CONFIGURATION)
    self.assertTrue(all(entry.include_this_device for entry in configuration.values()))
    self.assertEqual(fake.arguments("controller_enable_demo_mode"), (False,))
    self.assertEqual(fake.arguments("controller_reset_devices"), (DEFAULT_RESET_OPTIONS,))
    self.assertEqual(fake.timeouts["controller_reset_devices"], 180.0)
    self.assertEqual(fake.arguments("controller_initialize_devices"), ([],))
    self.assertEqual(fake.timeouts["controller_initialize_devices"], 120.0)
    self.assertEqual(sorted(CALLBACKS), fake.handlers_at_open)
    self.assertTrue(driver.is_set_up)
    self.assertEqual((driver.system_type, driver.system_id), ("iQue3", "00155D010203"))
    assert driver.limits is not None
    self.assertEqual(driver.limits.pump_default_speed, 28.6)
    self.assertEqual(driver.limits.z_range, (190.0, 250.0))
    self.assertEqual(driver.calibration_bytes, CALIBRATION)
    self.assertIsNotNone(driver.calibration)
    self.assertEqual(driver.rail_position, "ejected")

  async def test_reset_options_are_the_controller_defaults(self) -> None:
    """Every reset option lies outside the range the controller applies."""
    options = DEFAULT_RESET_OPTIONS
    self.assertFalse(40 <= options.override_z_arm_percentage <= 100)
    self.assertFalse(0 < options.override_lld_percentage < 10)
    self.assertFalse(0 < options.override_z_arm_additional_contact_depth < 10)
    self.assertFalse(13 <= options.plate_rail_rc <= 18)
    self.assertFalse(options.is_in_dev_mode or options.is_sorpc6)

  async def test_system_id_is_refreshed_when_empty(self) -> None:
    """An empty stored system ID is read again with a refresh."""
    fake = FakeController()
    fake.replies["controller_get_system_id"] = lambda refresh: "AA" if refresh else ""
    driver, _ = await set_up(fake)
    self.assertEqual(driver.system_id, "AA")

  async def test_another_client(self) -> None:
    """Another registered client: refused, the session closed, nothing more sent."""
    fake = FakeController()
    fake.replies["controller_connect"] = ConnectionInfo(
      comments="", is_client_already_connected=True, version=VERSION
    )
    with self.assertRaises(SessionRefusedError):
      await make_driver(fake).setup()
    self.assertEqual(fake.names(), ["open", "controller_connect", "close"])

  async def test_unsupported_version(self) -> None:
    """A controller version other than 7.2, or none, is refused."""
    for version in (Version(7, 3, 0, 0), None):
      fake = FakeController()
      fake.replies["controller_connect"] = ConnectionInfo(
        comments="", is_client_already_connected=False, version=version
      )
      with self.assertRaises(ControllerVersionError):
        await make_driver(fake).setup()
      self.assertEqual(fake.names()[-1], "close")

  async def test_unsupported_system_type(self) -> None:
    """A system type outside the PLUS family is refused before anything moves."""
    for system_type in ("HTFC", "Unknown", "iQueHD"):
      fake = FakeController()
      fake.replies["controller_determine_system_type"] = system_type
      with self.assertRaises(UnsupportedSystemError):
        await make_driver(fake).setup()
      self.assertNotIn("controller_reset_devices", fake.names())

  async def test_stale_system_id(self) -> None:
    """A reset that answers False fails setup without initialising."""
    fake = FakeController()
    fake.replies["controller_reset_devices"] = False
    with self.assertRaises(DeviceResetError):
      await make_driver(fake).setup()
    self.assertNotIn("controller_initialize_devices", fake.names())
    self.assertFalse(fake.is_open)

  async def test_faulted_after_initialisation(self) -> None:
    """A device faulted after initialisation fails setup."""
    fake = FakeController()
    fake.states["Shaker"] = "Faulted"
    with self.assertRaises(DeviceFaultedError) as caught:
      await make_driver(fake).setup()
    self.assertEqual(list(caught.exception.devices), ["Shaker"])

  async def test_devices_not_ready_in_time(self) -> None:
    """A mechanical device that stays busy fails setup after the ready timeout."""
    fake = FakeController()
    fake.states["PlateRail"] = "Running"
    with self.assertRaises(DeviceResetError):
      await make_driver(fake, ready_timeout=0.02).setup()

  async def test_detector_off_is_reported(self) -> None:
    """A switched-off detector does not stop setup; it is reported."""
    fake = FakeController()
    fake.states["Cytometer"] = "Disconnected"
    fake.replies["controller_cytometer_has_power"] = False
    driver = make_driver(fake)
    with self.assertLogs("pylabrobot.sartorius.ique.driver.master", logging.WARNING) as logs:
      await driver.setup()
    self.assertIn("detector is switched off", "\n".join(logs.output))

  async def test_missing_calibration(self) -> None:
    """Without a calibration file setup fails before the devices are reset, unless allowed."""
    fake = FakeController()
    fake.calibration = None
    with self.assertRaises(CalibrationMissingError):
      await make_driver(fake).setup()
    self.assertNotIn("controller_reset_devices", fake.names())
    self.assertNotIn("controller_clear_calibration_settings", fake.names())
    fake = FakeController()
    fake.calibration = b"not a calibration file"
    driver = make_driver(fake)
    with self.assertLogs("pylabrobot.sartorius.ique.driver.master", logging.WARNING):
      await driver.setup(require_calibration=False)
    self.assertIsNone(driver.calibration)
    self.assertEqual(driver.calibration_bytes, b"not a calibration file")
    with self.assertRaises(CalibrationMissingError):
      driver.require_calibration()

  async def test_running_worklist_attaches_without_reset(self) -> None:
    """With a worklist running, setup attaches: no configuration, reset or initialisation."""
    fake = FakeController()
    fake.status = "RunningWorklist"
    fake.current = acquiring_worklist()
    driver = make_driver(fake)
    with self.assertLogs("pylabrobot.sartorius.ique.driver.master", logging.WARNING) as logs:
      await driver.setup()
    names = fake.names()
    for name in ("controller_configure_devices", "controller_reset_devices"):
      self.assertNotIn(name, names)
    self.assertNotIn("controller_initialize_devices", names)
    self.assertIn("worklist_get_current", names)
    self.assertIn("is running", "\n".join(logs.output))
    self.assertIsNone(driver.rail_position)
    with self.assertRaises(WorklistActiveError):
      await driver.pump.stop()

  async def test_unfinalised_worklist_is_left(self) -> None:
    """A finished, unfinalised worklist is not finalised by setup; a run then needs consent."""
    fake = FakeController()
    fake.status = "CompletedUnfinalizedWorklist"
    driver = make_driver(fake)
    await driver.setup()
    self.assertIn("controller_reset_devices", fake.names())
    self.assertNotIn("worklist_finalize", fake.names())
    with self.assertRaises(WorklistActiveError):
      await driver.run_worklist(acquiring_worklist())
    self.assertNotIn("worklist_run", fake.names())

  async def test_twice(self) -> None:
    """A driver is set up once."""
    driver, _ = await set_up()
    with self.assertRaises(IQueError):
      await driver.setup()

  def test_host_or_client(self) -> None:
    """The driver needs exactly one of a host and a client."""
    with self.assertRaises(ValueError):
      IQueDriver()
    with self.assertRaises(ValueError):
      IQueDriver("192.0.2.1", client=FakeController())

  def test_client_satisfies_the_protocol(self) -> None:
    """ControllerClient and the fake have the interface the driver uses."""
    client: ControllerApi = ControllerClient("192.0.2.1")
    fake: ControllerApi = FakeController()
    self.assertIsInstance(client, ControllerApi)
    self.assertIsInstance(fake, ControllerApi)


class StateTests(unittest.IsolatedAsyncioTestCase):
  """The live state from callbacks, and its refresh."""

  async def test_callbacks(self) -> None:
    """Every state callback updates the driver's picture."""
    driver, fake = await set_up()
    fake.fire("OnSystemStateChanged", "Yellow")
    fault = DeviceMessage(detail="d", message="Start Faulted", message_level="Error")
    with self.assertLogs("pylabrobot.sartorius.ique.driver.master", logging.ERROR) as logs:
      fake.fire(
        "OnDeviceStateChanged",
        DeviceInfo(kind="Pump", messages=[fault], model="m", state="Faulted"),
      )
    self.assertIn("Start Faulted", "\n".join(logs.output))
    fake.fire("OnContactSensorStateChanged", True)
    fake.fire(
      "OnDeviceAlert", wire.DeviceAlertMessage(alert="CYT_PurgingFluidics", kind="Cytometer")
    )
    fake.fire("OnCytometerLongRunningOperationCompleted", None)
    fake.fire("TestConnection")
    status = driver.status
    self.assertEqual(status.system_state, "Yellow")
    self.assertEqual(status.devices["Pump"].state, "Faulted")
    self.assertTrue(status.contact)
    self.assertEqual(status.alerts["Cytometer"], "CYT_PurgingFluidics")
    self.assertEqual(status.last_long_running_result, "")

  async def test_rail_position_is_forgotten_while_it_moves(self) -> None:
    """A rail that is not ready has no known position."""
    driver, fake = await set_up()
    fake.fire(
      "OnDeviceStateChanged", DeviceInfo(kind="PlateRail", messages=[], model="m", state="Running")
    )
    self.assertIsNone(driver.rail_position)

  async def test_request_status(self) -> None:
    """request_status asks for every state afresh."""
    driver, fake = await set_up()
    fake.replies["controller_get_system_state"] = "Yellow"
    fake.contact = True
    status = await driver.request_status()
    self.assertEqual(status.system_state, "Yellow")
    self.assertTrue(status.contact)
    self.assertEqual(status.worklist_status, "NoActiveWorklist")
    self.assertEqual(
      fake.names(),
      [
        "controller_get_system_state",
        "controller_get_device_statuses",
        "contact_sensor_has_contact",
        "worklist_get_status",
      ],
    )

  async def test_not_set_up(self) -> None:
    """Operations before setup are refused."""
    driver = make_driver(FakeController())
    with self.assertRaises(NotSetUpError):
      await driver.request_status()
    with self.assertRaises(NotSetUpError):
      await driver.run_worklist(acquiring_worklist())

  async def test_calls_are_serialised(self) -> None:
    """Concurrent calls reach the controller one at a time."""
    driver, fake = await set_up()
    await asyncio.gather(*(driver.call(fake.worklist_get_status) for _ in range(20)))
    self.assertEqual(fake.overlaps, 0)


class RunTests(unittest.IsolatedAsyncioTestCase):
  """run_worklist on the fake controller."""

  async def asyncSetUp(self) -> None:
    """Set up a driver."""
    self.driver, self.fake = await set_up()
    self.data = events(25, first_tick=4000)

  def plan(self, **fields: Any) -> None:
    """Script the next run; by default 25 events streamed in chunks of 10 and in the file."""
    fields.setdefault("stream", chunks(self.data, 10))
    fields.setdefault("file_events", self.data)
    self.fake.plan = RunPlan(**fields)

  async def test_success(self) -> None:
    """A run reports progress, returns the file's events and is finalised after the download."""
    self.plan()
    worklist = acquiring_worklist()
    progress: List[RunProgress] = []
    run = await self.driver.run_worklist(worklist, on_progress=progress.append)
    commands = worklist.commands or []
    self.assertEqual(
      [(p.phase, p.index) for p in progress],
      [(phase, index) for index in range(len(commands)) for phase in ("started", "completed")],
    )
    self.assertTrue(all(p.total == len(commands) for p in progress))
    self.assertEqual(run.events, self.data)
    self.assertEqual(run.stream_event_count, 25)
    self.assertTrue(run.stream_consistent)
    self.assertEqual(run.fcs_file, self.fake.fcs)
    assert run.fcs_keywords is not None
    self.assertEqual(run.fcs_keywords["$CYT"], "IntelliCyt iQue3")
    self.assertEqual(run.client_time_offset, datetime.timedelta(seconds=2))
    self.assertTrue(all(command.completed for command in run.commands))
    names = self.fake.names()
    self.assertLess(names.index("worklist_get_current_fcs_file"), names.index("worklist_finalize"))
    self.assertEqual(self.fake.status, "NoActiveWorklist")
    self.assertEqual(self.driver.rail_position, "ejected")

  async def test_without_acquisition(self) -> None:
    """A worklist that acquires nothing downloads no file."""
    self.plan(stream=(), file_events=self.data)
    ids = CommandIds()
    run = await self.driver.run_worklist(make_worklist([start_pump(ids), stop_pump(ids)]))
    self.assertIsNone(run.events)
    self.assertNotIn("worklist_get_current_fcs_file", self.fake.names())

  async def test_refused_while_active(self) -> None:
    """A run is refused while one runs, and while one is unfinalised unless allowed."""
    self.fake.status = "RunningWorklist"
    with self.assertRaises(WorklistActiveError):
      await self.driver.run_worklist(acquiring_worklist())
    self.fake.status = "CompletedUnfinalizedWorklist"
    with self.assertRaises(WorklistActiveError):
      await self.driver.run_worklist(acquiring_worklist())
    self.assertNotIn("worklist_run", self.fake.names())
    self.plan()
    await self.driver.run_worklist(acquiring_worklist(), finalize_previous=True)
    names = self.fake.names()
    self.assertLess(names.index("worklist_finalize"), names.index("worklist_run"))

  async def test_malformed_worklists(self) -> None:
    """Empty worklists, repeated IDs, None and commands without an executor are refused."""
    ids = CommandIds()
    pump = start_pump(ids)
    for commands in (
      [],
      [pump, pump],
      [pump, None],
      [pump, wire.StopFCAutomaterCommand(**ids.fields("Info"))],
      [pump, wire.SampleCommand(**{**ids.fields("Sample"), **_sample_fields()})],
    ):
      with self.assertRaises(ValueError):
        await self.driver.run_worklist(make_worklist(commands))  # type: ignore[arg-type]
    self.assertEqual(self.fake.names(), [])

  async def test_devices_must_be_ready(self) -> None:
    """Busy, faulted or disconnected monitored devices and an unready detector refuse a run."""
    self.fake.states["Pump"] = "Running"
    with self.assertRaises(DeviceNotReadyError):
      await self.driver.run_worklist(acquiring_worklist())
    self.fake.states["Pump"] = "Faulted"
    with self.assertRaises(DeviceFaultedError):
      await self.driver.run_worklist(acquiring_worklist())
    self.fake.states["Pump"] = "Ready"
    self.fake.replies["cytometer_is_ready_to_run"] = False
    with self.assertRaises(DeviceNotReadyError):
      await self.driver.run_worklist(acquiring_worklist())
    self.assertNotIn("worklist_run", self.fake.names())

  async def test_only_monitored_devices_count(self) -> None:
    """A worklist that monitors some devices ignores the state of the others."""
    self.fake.states["Shaker"] = "Disconnected"
    self.plan(stream=(), file_events=None)
    ids = CommandIds()
    worklist = make_worklist([start_pump(ids), stop_pump(ids)], monitor=["Pump", "Sampler"])
    await self.driver.run_worklist(worklist)

  async def test_warnings(self) -> None:
    """Device warnings (system state Yellow) refuse a run unless allowed."""
    self.fake.replies["controller_get_system_state"] = "Yellow"
    self.fake.messages["Cytometer"] = [
      DeviceMessage(detail="", message="Sheath bottle is low.", message_level="Warning")
    ]
    with self.assertRaises(IQueError) as caught:
      await self.driver.run_worklist(acquiring_worklist())
    self.assertIn("Sheath bottle is low.", str(caught.exception))
    self.plan()
    await self.driver.run_worklist(acquiring_worklist(), allow_warnings=True)

  async def test_failure(self) -> None:
    """A run the controller stops raises with its stop reason, after finalising, with the data."""
    self.plan(stop_reason="DeviceFaulted", stop_before=3)
    with self.assertRaises(WorklistFailedError) as caught:
      await self.driver.run_worklist(acquiring_worklist())
    self.assertNotIsInstance(caught.exception, WorklistCancelledError)
    self.assertEqual(caught.exception.stop_reason, "DeviceFaulted")
    self.assertEqual(caught.exception.run.events, self.data)
    self.assertIn("worklist_finalize", self.fake.names())

  async def test_error_text_is_a_failure(self) -> None:
    """A run with an error text fails even when every command completed."""
    self.plan(stop_reason="WorklistError", error="Contact not found at 200")
    with self.assertRaises(WorklistFailedError) as caught:
      await self.driver.run_worklist(acquiring_worklist())
    self.assertEqual(caught.exception.error, "Contact not found at 200")

  async def test_cancel_worklist(self) -> None:
    """cancel_worklist makes the run raise WorklistCancelledError."""
    self.plan(wait_for_cancel=True)
    task = asyncio.ensure_future(self.driver.run_worklist(acquiring_worklist()))
    await self.fake.waiting.wait()
    await self.driver.cancel_worklist()
    with self.assertRaises(WorklistCancelledError) as caught:
      await task
    self.assertEqual(caught.exception.stop_reason, "Cancelled")
    self.assertIn("worklist_finalize", self.fake.names())

  async def test_cancel_worklist_without_a_run(self) -> None:
    """cancel_worklist sends nothing when nothing runs."""
    await self.driver.cancel_worklist()
    self.assertNotIn("worklist_cancel", self.fake.names())

  async def test_task_cancellation(self) -> None:
    """Cancelling the waiting task cancels the worklist, waits for it and finalises it."""
    self.plan(wait_for_cancel=True)
    task = asyncio.ensure_future(self.driver.run_worklist(acquiring_worklist()))
    await self.fake.waiting.wait()
    task.cancel()
    with self.assertRaises(asyncio.CancelledError):
      await task
    names = self.fake.names()
    self.assertIn("worklist_cancel", names)
    self.assertLess(names.index("worklist_cancel"), names.index("worklist_finalize"))
    self.assertEqual(self.fake.status, "NoActiveWorklist")

  async def test_progress_callback_error(self) -> None:
    """An error of the progress callback cancels the worklist and is raised."""
    self.plan(wait_for_cancel=True)

    def fail(progress: RunProgress) -> None:
      """Fail as a faulty callback would."""
      raise RuntimeError("callback failed")

    with self.assertRaises(RuntimeError):
      await self.driver.run_worklist(acquiring_worklist(), on_progress=fail)
    self.assertIn("worklist_cancel", self.fake.names())
    self.assertIn("worklist_finalize", self.fake.names())

  async def test_timeout(self) -> None:
    """A run past its timeout is cancelled, finalised and raised as WorklistTimeoutError."""
    self.plan(wait_for_cancel=True)
    with self.assertRaises(WorklistTimeoutError) as caught:
      await self.driver.run_worklist(acquiring_worklist(), timeout=0.05)
    self.assertIsInstance(caught.exception, TimeoutError)
    self.assertEqual(caught.exception.stop_reason, "Cancelled")
    self.assertIn("worklist_cancel", self.fake.names())
    self.assertIn("worklist_finalize", self.fake.names())

  async def test_lost_completion_callback(self) -> None:
    """Without its completion callback, the run is read with Worklist_GetCurrent."""
    self.plan(lose_completion=True, provisional_reads=2)
    with self.assertLogs("pylabrobot.sartorius.ique.driver.master", logging.WARNING) as logs:
      run = await self.driver.run_worklist(acquiring_worklist())
    self.assertIn("without its completion callback", "\n".join(logs.output))
    self.assertEqual(run.events, self.data)
    self.assertGreaterEqual(self.fake.names().count("worklist_get_current"), 3)
    self.assertGreater(run.worklist.end_time.year, 1900)
    self.assertEqual(self.driver.rail_position, "ejected")

  async def test_file_with_more_events(self) -> None:
    """A file with more events than were streamed is used, with a warning."""
    self.plan(stream=chunks(self.data, 10)[:1])
    with self.assertLogs("pylabrobot.sartorius.ique.driver.master", logging.WARNING) as logs:
      run = await self.driver.run_worklist(acquiring_worklist())
    self.assertIn("the file is used", "\n".join(logs.output))
    self.assertEqual(run.stream_event_count, 10)
    self.assertEqual(run.events, self.data)

  async def test_file_with_fewer_events(self) -> None:
    """A file with fewer events than were streamed is inconsistent; the run is still finalised."""
    self.plan(file_events=self.data.slice(0, 5))
    with self.assertRaises(EventDataError):
      await self.driver.run_worklist(acquiring_worklist())
    self.assertIn("worklist_finalize", self.fake.names())

  async def test_running_count_gap(self) -> None:
    """A gap in the running value count marks the stream inconsistent."""
    self.plan(index_gap=32)
    with self.assertLogs("pylabrobot.sartorius.ique.driver.master", logging.WARNING):
      run = await self.driver.run_worklist(acquiring_worklist())
    self.assertFalse(run.stream_consistent)
    self.assertEqual(run.events, self.data)

  async def test_checksum_mismatch(self) -> None:
    """A file whose checksum differs from the controller's is refused."""
    self.plan(crc_offset=1)
    with self.assertRaises(EventDataError):
      await self.driver.run_worklist(acquiring_worklist())
    self.assertIn("worklist_finalize", self.fake.names())

  async def test_no_file(self) -> None:
    """A completed run that acquired but has no file is refused."""
    self.plan(file_events=None)
    with self.assertRaises(EventDataError):
      await self.driver.run_worklist(acquiring_worklist())

  async def test_foreign_command(self) -> None:
    """Callbacks about another worklist's command stop the run."""
    ids = CommandIds()
    for _ in range(50):
      ids.fields("Info")
    self.plan(foreign_command=start_pump(ids), wait_for_cancel=True)
    with self.assertRaises(IQueError) as caught:
      await self.driver.run_worklist(acquiring_worklist())
    self.assertIn("not in this worklist", str(caught.exception))
    self.assertIn("worklist_cancel", self.fake.names())

  async def test_direct_operations_are_refused_during_a_run(self) -> None:
    """While a run waits, direct operations are refused without reaching the controller."""
    self.plan(wait_for_cancel=True)
    task = asyncio.ensure_future(self.driver.run_worklist(acquiring_worklist()))
    await self.fake.waiting.wait()
    with self.assertRaises(WorklistActiveError):
      await self.driver.pump.start(20.0)
    with self.assertRaises(WorklistActiveError):
      await self.driver.stop()
    await self.driver.cancel_worklist()
    with self.assertRaises(WorklistCancelledError):
      await task
    self.assertNotIn("pump_start", self.fake.names())


def _sample_fields() -> Dict[str, Any]:
  """The own fields of a SampleCommand, which has no executor."""
  return {
    "fast_move": True,
    "sip_time": 1000,
    "up_time": 500,
    "x": D(20),
    "y": D(100),
    "z_down": D(204),
    "z_up": D(250),
  }


class CalibrationTests(unittest.IsolatedAsyncioTestCase):
  """Downloading and uploading the calibration file."""

  async def asyncSetUp(self) -> None:
    """Set up a driver with a backup folder."""
    self.folder = tempfile.TemporaryDirectory()
    self.backups = pathlib.Path(self.folder.name) / "backups"
    self.driver, self.fake = await set_up(calibration_backup_dir=self.backups)
    self.new = write_calibration(simulated_calibration(deck_height=201.0))

  async def asyncTearDown(self) -> None:
    """Remove the backup folder."""
    self.folder.cleanup()

  async def test_upload_backs_up_first(self) -> None:
    """The instrument's file is written to the backup folder before the new one is uploaded."""
    backup = await self.driver.upload_calibration(self.new)
    assert backup is not None
    self.assertEqual(backup.parent, self.backups)
    self.assertRegex(backup.name, r"^calibration-00155D010203-\d{8}T\d{12}Z(-\d+)?\.cbr$")
    self.assertEqual(backup.read_bytes(), CALIBRATION)
    self.assertEqual(
      self.fake.names(),
      [
        "controller_download_calibration_settings",
        "controller_upload_calibration_settings",
        "controller_download_calibration_settings",
      ],
    )
    self.assertEqual(self.fake.calibration, self.new)
    assert self.driver.calibration is not None
    self.assertEqual(self.driver.calibration.instrument.deck_height, 201.0)

  async def test_two_uploads_keep_two_backups(self) -> None:
    """Every upload keeps its own backup."""
    await self.driver.upload_calibration(self.new)
    await self.driver.upload_calibration(CALIBRATION)
    self.assertEqual(len(list(self.backups.iterdir())), 2)

  async def test_upload_needs_a_backup_folder(self) -> None:
    """Without a backup folder an upload is refused before anything is sent."""
    self.driver.calibration_backup_dir = None
    with self.assertRaises(CalibrationRefusedError):
      await self.driver.upload_calibration(self.new)
    self.assertEqual(self.fake.names(), [])

  async def test_upload_refuses_bad_files(self) -> None:
    """Unreadable files, files for another instrument or family, and huge files are refused."""
    other = write_calibration(simulated_calibration(device_id="OTHER"))
    family = write_calibration(simulated_calibration(system_kind="iQuePlusBR"))
    for data in (b"junk", other, family, b"x" * (5000 * 1024 + 1)):
      with self.assertRaises(CalibrationRefusedError):
        await self.driver.upload_calibration(data)
    self.assertNotIn("controller_upload_calibration_settings", self.fake.names())

  async def test_upload_without_an_existing_file(self) -> None:
    """With no file to back up the upload is refused unless that is allowed."""
    self.fake.calibration = None
    with self.assertRaises(CalibrationRefusedError):
      await self.driver.upload_calibration(self.new)
    self.assertNotIn("controller_upload_calibration_settings", self.fake.names())
    with self.assertLogs("pylabrobot.sartorius.ique.driver.master", logging.WARNING):
      backup = await self.driver.upload_calibration(self.new, allow_no_existing_file=True)
    self.assertIsNone(backup)
    self.assertEqual(self.fake.calibration, self.new)

  async def test_read_back_mismatch(self) -> None:
    """A file that does not read back as uploaded is reported."""

    async def keep_old(data: bytes) -> None:
      """Accept the upload and store nothing."""

    self.fake.controller_upload_calibration_settings = keep_old  # type: ignore[method-assign]
    with self.assertRaises(CalibrationRefusedError):
      await self.driver.upload_calibration(self.new)

  async def test_download(self) -> None:
    """download_calibration returns the file and keeps it parsed."""
    self.fake.calibration = self.new
    self.assertEqual(await self.driver.download_calibration(), self.new)
    assert self.driver.calibration is not None
    self.assertEqual(self.driver.calibration.instrument.deck_height, 201.0)
    self.fake.calibration = b"junk"
    with self.assertRaises(CalibrationMissingError):
      await self.driver.download_calibration()
    self.assertIsNone(self.driver.calibration)


class StopTests(unittest.IsolatedAsyncioTestCase):
  """Stopping the driver."""

  async def test_stop(self) -> None:
    """stop stops the pump and the shaker, parks the probe and closes the session."""
    driver, fake = await set_up()
    fake.states["Pump"] = "Running"
    fake.states["Shaker"] = "Running"
    fake.position = [50.0, 50.0, 250.0]
    await driver.stop()
    names = fake.names()
    for name in ("pump_stop", "shaker_stop", "sampler_home"):
      self.assertIn(name, names)
    self.assertLess(names.index("pump_stop"), names.index("sampler_home"))
    self.assertEqual(names[-1], "close")
    self.assertFalse(driver.is_set_up)
    await driver.stop()
    self.assertEqual(fake.names()[-1], "close")

  async def test_stop_with_a_running_worklist(self) -> None:
    """stop refuses while a worklist runs, unless told to cancel it."""
    driver, fake = await set_up()
    fake.status = "RunningWorklist"
    with self.assertRaises(WorklistActiveError):
      await driver.stop()
    self.assertNotIn("close", fake.names())

    def cancel() -> None:
      """Cancel, and end the worklist."""
      fake.status = "CompletedUnfinalizedWorklist"

    fake.replies["worklist_cancel"] = cancel
    await driver.stop(cancel=True)
    names = fake.names()
    self.assertLess(names.index("worklist_cancel"), names.index("sampler_home"))
    self.assertEqual(names[-1], "close")

  async def test_stop_skips_faulted_devices(self) -> None:
    """A faulted device is skipped, the others are stopped, the session closed, then raised."""
    driver, fake = await set_up()
    fake.states["Pump"] = "Faulted"
    fake.states["Shaker"] = "Running"
    with self.assertRaises(DeviceFaultedError) as caught:
      await driver.stop()
    self.assertEqual(list(caught.exception.devices), ["Pump"])
    names = fake.names()
    self.assertNotIn("pump_stop", names)
    self.assertIn("shaker_stop", names)
    self.assertIn("sampler_home", names)
    self.assertEqual(names[-1], "close")

  async def test_stop_with_contact(self) -> None:
    """A probe in contact cannot be parked; the session is closed all the same."""
    driver, fake = await set_up()
    fake.contact = True
    with self.assertRaises(UnsafeMotionError):
      await driver.stop()
    self.assertNotIn("sampler_home", fake.names())
    self.assertFalse(fake.is_open)


class FaultTests(unittest.IsolatedAsyncioTestCase):
  """Faults of the controller reach the caller unchanged."""

  async def test_controller_fault_propagates(self) -> None:
    """A fault the controller answers with is raised as it is."""
    fake = FakeController()
    fake.replies["controller_determine_system_type"] = controller_fault(
      "Controller_DetermineSystemType", "boom"
    )
    with self.assertRaises(ControllerFault):
      await make_driver(fake).setup()
    self.assertFalse(fake.is_open)


if __name__ == "__main__":
  unittest.main()
