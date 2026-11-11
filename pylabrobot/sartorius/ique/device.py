"""The IntelliCyt / Sartorius iQue Screener PLUS as a PyLabRobot resource, and its operations.

:class:`IQueDevice` is the instrument: its plate holder, which takes a plate only while the
plate rail is out; its features (``plate_rail``, ``shaker``, ``pump``, ``sampler``,
``detector``); and the operations the controller runs as worklists: plate and tube reads,
priming, the probe and tubing clean, start-up and shut-down, the fluidics check, and the check
and calibration of the deck height. Build one with :func:`IQue`.

Every operation checks what the controller does not: that the calibration holds what it needs,
that no worklist runs, that the detector is ready, that the probe reports no contact. Whatever
happens, the controller ends every worklist with the pump and the shaker stopped, the probe
parked and the plate rail ejected.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import pathlib
from dataclasses import dataclass
from typing import Callable, List, Literal, Optional, Sequence, Tuple, Union

from pylabrobot.resources import Coordinate, Plate, PlateHolder, Resource, Well
from pylabrobot.sartorius.ique.driver.calibration import (
  PLATE_DIMENSIONS,
  PLATE_FORMATS,
  Calibration,
  CalibrationError,
  PlateFormat,
  deck_height_test_point,
  format_well_name,
  parse_well_name,
  with_deck_height,
  write_calibration,
)
from pylabrobot.sartorius.ique.driver.contract import (
  CombinedShakeRinseCommand,
  ContactToleranceType,
  FlowCytometerAcquireCommand,
  FlushCommand,
  StartShakerCommand,
  Worklist,
)
from pylabrobot.sartorius.ique.driver.errors import (
  CalibrationMissingError,
  CalibrationRefusedError,
  DeviceFaultedError,
  DeviceNotReadyError,
  IQueError,
  PlateAccessError,
  UnsafeMotionError,
  WorklistActiveError,
  WorklistFailedError,
)
from pylabrobot.sartorius.ique.driver.features.detector import Detector
from pylabrobot.sartorius.ique.driver.features.plate_rail import PlateRail
from pylabrobot.sartorius.ique.driver.features.pump import Pump
from pylabrobot.sartorius.ique.driver.features.sampler import Sampler
from pylabrobot.sartorius.ique.driver.features.shaker import Shaker
from pylabrobot.sartorius.ique.driver.master import CompletedRun, IQueDriver, RunProgress
from pylabrobot.sartorius.ique.driver.results import (
  PlateRead,
  TubeRead,
  plate_read_from_run,
  tube_read_from_run,
)
from pylabrobot.sartorius.ique.driver.simulator import SimulatedController
from pylabrobot.sartorius.ique.driver.worklist import (
  PROBE_CLEAN_DURATION,
  PlateReadSettings,
  TubeReadSettings,
  WellOrder,
  deck_height_validation_worklist,
  fluidics_check_worklist,
  flush_target,
  plate_read_worklist,
  prime_worklist,
  probe_and_tubing_clean_worklist,
  shutdown_worklist,
  startup_worklist,
  tube_read_worklist,
  well_order,
)

logger = logging.getLogger(__name__)

# Outer size of the instrument in mm, from its hardware manual: 130 cm wide, 63 cm deep with the
# bottle station, 66 cm high. The computer and the UPS stand beside it and are not included.
SIZE_X = 1300.0
SIZE_Y = 630.0
SIZE_Z = 660.0

# The plate holder takes an SBS-format plate.
PLATE_HOLDER_SIZE_X = 127.76
PLATE_HOLDER_SIZE_Y = 85.48

PRIME_DURATION = 60.0
"""Seconds of the default prime: enough to clear sample a stopped read left in the line."""
CONTACT_CLEAR_TIMEOUT = 2.0
"""Seconds the contact sensor may take to clear once the probe is raised."""
CONTACT_POLL_INTERVAL = 0.1
"""Seconds between contact sensor reads while waiting for it to clear."""

ReadKind = Literal["plate", "tube"]


class IQuePlateHolder(PlateHolder):
  """The plate holder on the plate rail: a plate goes on or comes off only while the rail is
  out (ejected)."""

  def __init__(self, name: str, plate_rail: PlateRail) -> None:
    """Create the holder.

    Args:
      name: The resource name.
      plate_rail: The rail that carries it.
    """
    super().__init__(
      name=name,
      size_x=PLATE_HOLDER_SIZE_X,
      size_y=PLATE_HOLDER_SIZE_Y,
      size_z=0,
      pedestal_size_z=0,
      child_location=Coordinate.zero(),
      model="ique_plate_holder",
    )
    self._plate_rail = plate_rail

  def _require_rail_out(self, action: str) -> None:
    """Raise unless the rail is known to be out.

    Raises:
      PlateAccessError: The rail is in, or where it is is not known.
    """
    position = self._plate_rail.position
    if position is None:
      raise PlateAccessError(
        f"cannot {action}: the plate rail position is not known; eject the rail first"
      )
    if position != "ejected":
      raise PlateAccessError(f"cannot {action}: the plate rail is in; eject it first")

  def check_can_drop_resource_here(self, resource: Resource, *, reassign: bool = True) -> None:
    """Raise unless the rail is out and the holder can take `resource`.

    Args:
      resource: The plate.
      reassign: Whether a resource already there may be replaced.

    Raises:
      PlateAccessError: The rail is not out.
    """
    self._require_rail_out(f"place {resource.name} on {self.name}")
    super().check_can_drop_resource_here(resource, reassign=reassign)

  def assign_child_resource(
    self,
    resource: Resource,
    location: Optional[Coordinate] = None,
    reassign: bool = True,
  ) -> None:
    """Place `resource`, checked as a drop is.

    Args:
      resource: The plate.
      location: Where on the holder; the holder's own child location when None.
      reassign: Whether a resource already there may be replaced.

    Raises:
      PlateAccessError: The rail is not out.
    """
    self.check_can_drop_resource_here(resource, reassign=reassign)
    super().assign_child_resource(resource, location, reassign)

  def unassign_child_resource(self, resource: Resource) -> None:
    """Take `resource` off; only while the rail is out.

    Args:
      resource: The plate.

    Raises:
      PlateAccessError: The rail is not out.
    """
    self._require_rail_out(f"remove {resource.name} from {self.name}")
    super().unassign_child_resource(resource)


@dataclass
class _ActiveRead:
  """A read this device runs, as :meth:`IQueDevice.stop_read_early` needs to know it.

  Attributes:
    kind: A plate read or a tube read.
    stop_requested: Whether a plate read was asked to stop early.
  """

  kind: ReadKind
  stop_requested: bool = False


def _plate_format_of(plate: Plate) -> PlateFormat:
  """Return the plate format a plate resource has by its grid of wells.

  Raises:
    ValueError: The plate is not a full grid of 8 x 12, 16 x 24 or 32 x 48 wells.
  """
  grid = (plate.num_items_y, plate.num_items_x)
  for plate_format, dimensions in PLATE_DIMENSIONS.items():
    if dimensions == grid:
      return plate_format
  raise ValueError(
    f"{plate.name} has {grid[0]} rows and {grid[1]} columns; the instrument reads plates of "
    "96 (8 x 12), 384 (16 x 24) and 1536 (32 x 48) wells"
  )


def _plate_models_for(calibration: Calibration, plate_format: PlateFormat) -> List[str]:
  """Return the names of the plate models a plate of a format can be read with.

  That is a model mapped for the format, or one with a taught depth and no map, which holds for
  every format.
  """
  names = []
  for model in calibration.plate_models:
    if model.depth_map is not None:
      if model.depth_map.plate_format == plate_format:
        names.append(model.name)
    elif model.is_calibrated:
      names.append(model.name)
  return names


def _resolve_plate_model(
  calibration: Calibration, plate_format: PlateFormat, plate_model: Optional[str]
) -> str:
  """Return the plate model to read with: the one given, or the only one calibrated.

  Raises:
    ValueError: No model is given and several are calibrated for the format.
    CalibrationMissingError: None is calibrated for the format, or the one given is unknown or
      not calibrated for it.
  """
  candidates = _plate_models_for(calibration, plate_format)
  if plate_model is None:
    if len(candidates) == 1:
      return candidates[0]
    if not candidates:
      raise CalibrationMissingError(
        f"no plate model of the instrument's calibration is mapped for {plate_format}"
      )
    raise ValueError(
      f"several plate models are calibrated for {plate_format}: {', '.join(candidates)}; "
      "choose one with plate_model"
    )
  if plate_model not in candidates:
    known = [model.name for model in calibration.plate_models]
    if plate_model not in known:
      raise CalibrationMissingError(
        f"the instrument's calibration has no plate model {plate_model!r}; it has "
        f"{', '.join(known) or 'none'}"
      )
    raise CalibrationMissingError(
      f"plate model {plate_model!r} is not calibrated for {plate_format}; calibrated for it: "
      f"{', '.join(candidates) or 'none'}"
    )
  return plate_model


def _from_calibration(build: Callable[[], Worklist]) -> Worklist:
  """Build a worklist from the calibration.

  Raises:
    CalibrationMissingError: The calibration lacks something the worklist needs.
  """
  try:
    return build()
  except CalibrationError as error:
    raise CalibrationMissingError(f"the instrument's calibration is incomplete: {error}") from error


def _sample_left_in_line(worklist: Worklist) -> bool:
  """Return whether a plate read ended after recording started and before its flush ended."""
  recording = False
  for command in worklist.commands or []:
    if isinstance(command, FlowCytometerAcquireCommand) and command.completed:
      recording = True
    if isinstance(command, FlushCommand):
      return recording and not command.completed
  return recording


class IQueDevice(Resource):
  """The iQue Screener PLUS as a PyLabRobot resource: its plate holder, its features
  (``plate_rail``, ``shaker``, ``pump``, ``sampler``, ``detector``), its operations and the
  driver that runs it.

  The resource is the instrument without its computer and UPS. Where the plate holder sits
  within it is not known; the holder is placed at the instrument's origin.

  Build one with the :func:`IQue` factory.
  """

  def __init__(
    self,
    driver: IQueDriver,
    name: str = "iQue",
    *,
    size_x: float = SIZE_X,
    size_y: float = SIZE_Y,
    size_z: float = SIZE_Z,
  ) -> None:
    """Create the resource.

    Args:
      driver: The driver.
      name: The resource name.
      size_x: Width in mm.
      size_y: Depth in mm.
      size_z: Height in mm.
    """
    super().__init__(
      name=name,
      size_x=size_x,
      size_y=size_y,
      size_z=size_z,
      category="device",
      model="iQue Screener PLUS",
    )
    self.driver = driver
    self.plate_holder = IQuePlateHolder(f"{name}_plate_holder", driver.plate_rail)
    self.assign_child_resource(self.plate_holder, location=Coordinate.zero())
    self._read: Optional[_ActiveRead] = None
    self._flush_needed = False

  def check_can_drop_resource_here(self, resource: Resource, *, reassign: bool = True) -> None:
    """Raise: a plate goes on the plate holder, not on the instrument.

    Raises:
      PlateAccessError: Always.
    """
    raise PlateAccessError(
      f"cannot place {resource.name} on {self.name}: place it on {self.plate_holder.name}"
    )

  def assign_child_resource(
    self,
    resource: Resource,
    location: Optional[Coordinate],
    reassign: bool = True,
  ) -> None:
    """Assign the instrument's own plate holder; anything else is refused.

    Raises:
      PlateAccessError: `resource` is not the plate holder.
    """
    if resource is not self.plate_holder:
      self.check_can_drop_resource_here(resource, reassign=reassign)
    super().assign_child_resource(resource, location, reassign)

  @property
  def plate_rail(self) -> PlateRail:
    """The plate rail."""
    return self.driver.plate_rail

  @property
  def shaker(self) -> Shaker:
    """The shaker."""
    return self.driver.shaker

  @property
  def pump(self) -> Pump:
    """The sample pump."""
    return self.driver.pump

  @property
  def sampler(self) -> Sampler:
    """The probe's sampler."""
    return self.driver.sampler

  @property
  def detector(self) -> Detector:
    """The detector."""
    return self.driver.detector

  @property
  def flush_needed(self) -> bool:
    """Whether the last plate read stopped with sample in the line, after recording started and
    before its flush ended. A prime, a probe and tubing clean, start-up or a completed plate
    read clears it."""
    return self._flush_needed

  async def setup(self, *, require_calibration: bool = True) -> None:
    """Connect and bring the instrument up (``IQueDriver.setup``).

    Args:
      require_calibration: Fail when the instrument's calibration file is missing or
        unreadable.
    """
    await self.driver.setup(require_calibration=require_calibration)

  async def stop(self, *, cancel: bool = False) -> None:
    """Stop the pump and the shaker, park the probe and disconnect (``IQueDriver.stop``).

    Args:
      cancel: Cancel a running worklist first instead of refusing.
    """
    await self.driver.stop(cancel=cancel)

  # -------------------------------------------------------------------------------------------
  # Checks before a worklist
  # -------------------------------------------------------------------------------------------

  def _calibration(self) -> Calibration:
    """Return the calibration, which must be for the connected instrument's system kind.

    Raises:
      NotSetUpError: The driver is not set up.
      CalibrationMissingError: No calibration is loaded, or it is for another system kind.
    """
    self.driver.require_setup()
    calibration = self.driver.require_calibration()
    kind = calibration.instrument.system_kind
    if kind != self.driver.system_type:
      raise CalibrationMissingError(
        f"the instrument's calibration is for system kind {kind}; the instrument is "
        f"{self.driver.system_type}"
      )
    return calibration

  async def _contact_tolerance(self, activity: ContactToleranceType) -> float:
    """Return how far in mm the controller lets a contact lie from its expected height."""
    driver = self.driver
    return float(await driver.call(driver.client.sampler_get_z_contact_tolerance, activity))

  def _default_pump_speed(self, pump_speed: Optional[float]) -> float:
    """Return a pump speed, or the pump's default speed as it reported it."""
    if pump_speed is not None:
      return pump_speed
    return self.driver.require_limits().pump_default_speed

  async def _require_no_contact(self, action: str) -> None:
    """Raise if the probe reports contact: the controller parks the probe first, and parking
    in contact faults the sampler.

    Raises:
      UnsafeMotionError: It does.
    """
    if await self.sampler.has_contact():
      raise UnsafeMotionError(
        f"cannot {action}: the probe reports contact; raise it with sampler.move_z first"
      )

  async def _require_detector_ready(self, action: str) -> None:
    """Raise unless the detector is ready to run; device states are read afresh.

    Raises:
      DeviceFaultedError: The detector is faulted.
      DeviceNotReadyError: It is off, busy, or not ready to run.
    """
    status = await self.driver.request_status()
    info = status.devices.get("Cytometer")
    if info is not None and info.state == "Faulted":
      raise DeviceFaultedError(action, {"Cytometer": info})
    if info is None or info.state != "Ready" or not await self.detector.is_ready_to_run():
      raise DeviceNotReadyError(f"{action} (detector.wait_until_ready() waits)", "Cytometer", info)

  def _require_rail_known(self, action: str) -> None:
    """Raise unless the plate rail is ready and where it is is known.

    Raises:
      DeviceNotReadyError: The rail is not ready.
      PlateAccessError: Where it is is not known.
    """
    rail = self.driver.devices.get("PlateRail")
    if rail is None or rail.state != "Ready":
      raise DeviceNotReadyError(action, "PlateRail", rail)
    if self.plate_rail.position is None:
      raise PlateAccessError(
        f"cannot {action}: the plate rail position is not known; eject it with "
        "plate_rail.eject() first"
      )

  async def _require_plate_read_possible(self, worklist: Worklist, plate_format: str) -> None:
    """Check the instrument before a plate read: no worklist runs, the instrument reads the
    format, the shaker reaches the read's speeds, the detector is ready, the plate rail is
    ready and its position known, and the probe reports no contact.

    Raises:
      WorklistActiveError: A worklist runs.
      IQueError: The instrument cannot read 1536-well plates.
      ValueError: The read shakes faster than the shaker can.
      DeviceFaultedError: The detector is faulted.
      DeviceNotReadyError: The detector or the plate rail is not ready.
      PlateAccessError: The plate rail's position is not known.
      UnsafeMotionError: The probe reports contact.
    """
    driver = self.driver
    action = "read the plate"
    await driver.require_idle(action)
    if plate_format == "Well1536":
      if not await driver.call(driver.client.controller_is1536_capable):
        raise IQueError("cannot read the plate: the instrument cannot read 1536-well plates")
    speeds = [
      command.rpm
      for command in worklist.commands or []
      if isinstance(command, (CombinedShakeRinseCommand, StartShakerCommand))
    ]
    if speeds:
      maximum = await driver.call(driver.client.shaker_max_rpm)
      if max(speeds) > maximum:
        raise ValueError(
          f"the read shakes at {max(speeds)} rpm; the instrument's shaker runs at most "
          f"{maximum} rpm"
        )
    await self._require_detector_ready(action)
    self._require_rail_known(action)
    await self._require_no_contact(action)

  # -------------------------------------------------------------------------------------------
  # Reads
  # -------------------------------------------------------------------------------------------

  def _plate_format(
    self, plate: Optional[Plate], plate_format: Optional[PlateFormat]
  ) -> PlateFormat:
    """Return the format of the plate to read, from the resource or as given.

    Raises:
      ValueError: Neither is given, the format is unknown, or the two disagree; or without a
        resource, the plate on the holder has another format.
      PlateAccessError: The plate is not on the plate holder.
    """
    if plate_format is not None and plate_format not in PLATE_FORMATS:
      raise ValueError(
        f"plate_format must be one of {', '.join(PLATE_FORMATS)}, not {plate_format}"
      )
    if plate is None:
      if plate_format is None:
        raise ValueError("give the plate, or its plate_format")
      held = self.plate_holder.resource
      if isinstance(held, Plate) and _plate_format_of(held) != plate_format:
        raise ValueError(
          f"{held.name} on {self.plate_holder.name} is a {_plate_format_of(held)} plate, not "
          f"{plate_format}"
        )
      return plate_format
    if plate.parent is not self.plate_holder:
      raise PlateAccessError(
        f"{plate.name} is not on {self.plate_holder.name}; eject the plate rail and place it there"
      )
    found = _plate_format_of(plate)
    if plate_format is not None and plate_format != found:
      raise ValueError(f"{plate.name} is a {found} plate, not {plate_format}")
    return found

  @staticmethod
  def _well_names(
    plate: Optional[Plate],
    plate_format: PlateFormat,
    wells: Optional[Sequence[Union[str, Well]]],
    order: Optional[WellOrder],
  ) -> List[str]:
    """Return the names of the wells to read, in sampling order.

    Raises:
      ValueError: A well is not on the plate or given twice, the order is unknown, or no order
        and no wells are given.
    """
    names: Optional[List[str]] = None
    if wells is not None:
      if isinstance(wells, str):
        raise ValueError(f"wells must be a sequence of wells or well names, not {wells!r}")
      names = []
      for well in wells:
        if isinstance(well, str):
          names.append(well)
        elif plate is not None and well.parent is plate:
          names.append(plate.get_child_identifier(well))
        else:
          raise ValueError(f"{well.name} is not a well of the plate that is read")
    if order is None:
      if names is None:
        raise ValueError("order=None samples the wells in the order given; give the wells")
      return [format_well_name(*parse_well_name(name, plate_format)) for name in names]
    return well_order(plate_format, order, names)

  async def read_plate(
    self,
    plate: Optional[Plate] = None,
    *,
    plate_format: Optional[PlateFormat] = None,
    wells: Optional[Sequence[Union[str, Well]]] = None,
    order: Optional[WellOrder] = "row",
    settings: PlateReadSettings = PlateReadSettings(),
    plate_model: Optional[str] = None,
    metadata: Sequence[Tuple[str, str]] = (),
    on_progress: Optional[Callable[[RunProgress], None]] = None,
    timeout: Optional[float] = None,
    allow_warnings: bool = False,
  ) -> PlateRead:
    """Read a plate: sample its wells one after another into one acquisition, then assign the
    events to the wells.

    The instrument loads the plate, checks the deck height at its test point (within the
    tolerance the controller states), samples the wells as `settings` say, flushes the line
    while it ejects the plate, cleans the probe and stops. Afterwards the wells are found in the
    event stream by the times of their sampling commands; a result whose verdict is
    ``needs_review`` is logged as a warning.

    Before anything is sent the read is checked: the plate is on the plate holder; the
    calibration is for this instrument's system kind, maps the plate model, and holds the plate
    format's corners, the stations and the deck height; the instrument reads the plate format;
    the shaker reaches the read's speeds; no worklist runs; the detector is ready to run; the
    plate rail is ready and its position known; the probe reports no contact. When the last
    plate read left sample in the line and `settings` do not prime first, a warning suggests
    :meth:`prime`.

    Args:
      plate: The plate, which must be on :attr:`plate_holder`; its format follows from its grid
        of wells. None to read by `plate_format` alone.
      plate_format: ``Well96``, ``Well384`` or ``Well1536``; needed without `plate`, and must
        agree with it otherwise.
      wells: The wells to sample, as names (``A1`` or ``A01``) or wells of `plate`; None for
        every well.
      order: The order to sample them in (``row``, ``serpentine_row``, ``column``,
        ``serpentine_column``); None to sample `wells` in the order given.
      settings: How to read.
      plate_model: The calibration's plate model of the plate; None for the only model
        calibrated for the format.
      metadata: (key, value) pairs the instrument writes into its FCS file as ``IC$<key>``.
      on_progress: Called for every command of the worklist that starts or ends.
      timeout: Seconds after which the read is cancelled; None waits as long as it takes.
      allow_warnings: Read although devices report warnings.

    Returns:
      The read. After :meth:`stop_read_early` it holds the wells sampled until then.

    Raises:
      ValueError: An argument is invalid or does not suit the plate or the instrument, or
        several plate models are calibrated for the format and none is chosen.
      PlateAccessError: The plate is not on the plate holder, or the rail's position is not
        known.
      CalibrationMissingError: The calibration lacks something the read needs.
      WorklistActiveError: A worklist runs.
      DeviceFaultedError: A device is faulted.
      DeviceNotReadyError: The detector or another device is not ready.
      UnsafeMotionError: The probe reports contact.
      WorklistFailedError: The read did not complete; the error holds the partial run
        (``WorklistCancelledError`` when it was cancelled, ``WorklistTimeoutError`` after
        `timeout`).
      EventDataError: The event data is missing or inconsistent.
      WellIdentificationFailedError: The events cannot be assigned to wells; the error holds the
        run.
    """
    driver = self.driver
    driver.require_setup()
    if self._read is not None:
      raise WorklistActiveError(f"a {self._read.kind} read of {self.name} runs", "RunningWorklist")
    resolved = self._plate_format(plate, plate_format)
    names = self._well_names(plate, resolved, wells, order)
    calibration = self._calibration()
    model = _resolve_plate_model(calibration, resolved, plate_model)
    tolerance = await self._contact_tolerance("DeckHeightValidation")
    worklist = _from_calibration(
      lambda: plate_read_worklist(
        calibration,
        resolved,
        names,
        settings,
        plate_model=model,
        metadata=metadata,
        deck_height_tolerance=tolerance,
      )
    )
    await self._require_plate_read_possible(worklist, resolved)
    if self._flush_needed and not settings.pre_plate_prime:
      logger.warning(
        "the last plate read stopped with sample in the line; prime() before reading, or set "
        "pre_plate_prime in the settings, to keep it out of this plate's first wells"
      )
    what = plate.name if plate is not None else f"a {resolved} plate"
    logger.info("reading %s: %d wells with plate model %r", what, len(names), model)
    read = _ActiveRead("plate")
    self._read = read
    try:
      run = await driver.run_worklist(
        worklist, on_progress=on_progress, timeout=timeout, allow_warnings=allow_warnings
      )
    except WorklistFailedError as error:
      if _sample_left_in_line(error.run.worklist):
        self._flush_needed = True
        logger.warning("the plate read stopped with sample in the line; prime() flushes it")
      raise
    finally:
      self._read = None
    self._flush_needed = False
    result = plate_read_from_run(
      run,
      plate_format=resolved,
      plate_model=model,
      settings=settings,
      plate_name=None if plate is None else plate.name,
      stopped_early=read.stop_requested,
    )
    if result.verdict == "needs_review":
      logger.warning("plate read of %s needs review: %s", what, result.message)
    else:
      logger.info("plate read of %s: %s", what, result.message)
    return result

  async def stop_read_early(self) -> None:
    """Stop the read that runs, keeping what it has read.

    A plate read that records events skips to its flush: the well being sampled is finished,
    the wells after it are skipped, and the line is flushed and the probe cleaned as at the end
    of every read; :meth:`read_plate` then returns the wells sampled until then. A plate read
    that has not started recording, and a tube read, are cancelled instead, and their read
    raises ``WorklistCancelledError``. Nothing is done when no read runs, or when the plate
    read already flushes. The method returns once the controller has the request; the read
    returns when the instrument has finished.

    Raises:
      NotSetUpError: The driver is not set up.
      IQueError: The controller runs a worklist but does not report it.
    """
    driver = self.driver
    driver.require_setup()
    read = self._read
    if read is None:
      logger.info("no read runs; nothing to stop")
      return
    if read.kind == "tube":
      await driver.cancel_worklist()
      return
    if await driver.call(driver.client.worklist_get_status) != "RunningWorklist":
      logger.info("the plate read does not run on the instrument yet, or any more; nothing to stop")
      return
    current = await driver.call(driver.client.worklist_get_current)
    if current is None:
      raise IQueError("the controller runs a worklist but reports none")
    recording = any(
      isinstance(command, FlowCytometerAcquireCommand) and command.completed
      for command in current.commands or []
    )
    if not recording:
      logger.info("the plate read has not started recording; cancelling it")
      read.stop_requested = True
      await driver.cancel_worklist()
      return
    running = driver.status.current_command
    target = flush_target(current, running=None if running is None else running.id)
    if target is None:
      logger.info("the plate read already flushes the line; nothing to skip")
      return
    read.stop_requested = True
    if await driver.skip_worklist_to(target.id):
      logger.info("stopping the plate read early: skipping to %r", target.description)

  async def read_tube(
    self,
    settings: TubeReadSettings = TubeReadSettings(),
    *,
    metadata: Sequence[Tuple[str, str]] = (),
    on_progress: Optional[Callable[[RunProgress], None]] = None,
    timeout: Optional[float] = None,
    allow_warnings: bool = False,
  ) -> TubeRead:
    """Read the 1.5 mL microcentrifuge tube in the tube holder.

    The instrument feels for the tube (within the tolerance the controller states), primes the
    line, draws from the tube for the sip time, flushes the line and, as `settings` say, cleans
    the probe. Every event belongs to the tube.

    Args:
      settings: How to read.
      metadata: (key, value) pairs the instrument writes into its FCS file as ``IC$<key>``.
      on_progress: Called for every command of the worklist that starts or ends.
      timeout: Seconds after which the read is cancelled; None waits as long as it takes.
      allow_warnings: Read although devices report warnings.

    Returns:
      The read.

    Raises:
      ValueError: A setting does not suit the instrument.
      CalibrationMissingError: The calibration lacks the tube holder, a station or a height.
      WorklistActiveError: A worklist runs.
      DeviceFaultedError: A device is faulted.
      DeviceNotReadyError: The detector or another device is not ready.
      UnsafeMotionError: The probe reports contact.
      WorklistFailedError: The read did not complete, for example because no tube is in the
        holder; the error holds the partial run.
      EventDataError: The event data is missing or inconsistent.
    """
    driver = self.driver
    driver.require_setup()
    if self._read is not None:
      raise WorklistActiveError(f"a {self._read.kind} read of {self.name} runs", "RunningWorklist")
    calibration = self._calibration()
    tolerance = await self._contact_tolerance("MicrofugeTubeValidation")
    worklist = _from_calibration(
      lambda: tube_read_worklist(calibration, settings, metadata=metadata, tube_tolerance=tolerance)
    )
    action = "read the tube"
    await driver.require_idle(action)
    await self._require_detector_ready(action)
    await self._require_no_contact(action)
    self._read = _ActiveRead("tube")
    try:
      run = await driver.run_worklist(
        worklist, on_progress=on_progress, timeout=timeout, allow_warnings=allow_warnings
      )
    finally:
      self._read = None
    result = tube_read_from_run(run, settings)
    logger.info("tube read: %d events", result.event_count)
    return result

  # -------------------------------------------------------------------------------------------
  # Maintenance
  # -------------------------------------------------------------------------------------------

  async def _run_maintenance(
    self,
    action: str,
    build: Callable[[Calibration], Worklist],
    *,
    on_progress: Optional[Callable[[RunProgress], None]],
    timeout: Optional[float],
    allow_warnings: bool = False,
    flushes_line: bool = False,
  ) -> CompletedRun:
    """Build a maintenance worklist from the calibration, check the instrument and run it.

    Args:
      action: What is done, for messages.
      build: Builds the worklist.
      on_progress: Called for every command that starts or ends.
      timeout: Seconds after which the run is cancelled.
      allow_warnings: Run although devices report warnings.
      flushes_line: Whether the worklist clears sample a stopped read left in the line.
    """
    calibration = self._calibration()
    worklist = _from_calibration(lambda: build(calibration))
    await self.driver.require_idle(action)
    await self._require_no_contact(action)
    logger.info("%s: %s", action, worklist.name)
    run = await self.driver.run_worklist(
      worklist, on_progress=on_progress, timeout=timeout, allow_warnings=allow_warnings
    )
    if flushes_line:
      self._flush_needed = False
    logger.info("%s: done", action)
    return run

  async def prime(
    self,
    duration: float = PRIME_DURATION,
    *,
    pump_speed: Optional[float] = None,
    sip_time: float = 1.0,
    up_time: float = 1.0,
    on_progress: Optional[Callable[[RunProgress], None]] = None,
    timeout: Optional[float] = None,
  ) -> CompletedRun:
    """Prime the line from Buffer, the probe dipping repeatedly, with the detector's fluidics
    on.

    A minute clears sample a stopped read left in the line. The instrument's manual prime runs
    an hour (``duration=3600``) until it is cancelled; the controller also stops a prime when
    the session ends. Buffer must be filled.

    Args:
      duration: How long, 1 to 3600 s in whole seconds.
      pump_speed: Pump speed in rpm, 10 to 53; None for the pump's default speed. Before a
        plate read, prime at the read's pump speed.
      sip_time: Time down per dip, 0.5 to 59.9 s.
      up_time: Time up per dip, 0.5 to 59.9 s.
      on_progress: Called for every command that starts or ends.
      timeout: Seconds after which the prime is cancelled.

    Returns:
      The completed run.

    Raises:
      ValueError: A value is out of range.
      CalibrationMissingError: Buffer, the deck height or the travel height is not calibrated.
      WorklistActiveError: A worklist runs.
      DeviceNotReadyError: The detector or another device is not ready.
      UnsafeMotionError: The probe reports contact.
      WorklistFailedError: The prime did not complete.
    """
    speed = self._default_pump_speed(pump_speed)
    return await self._run_maintenance(
      "prime",
      lambda calibration: prime_worklist(
        calibration, duration=duration, pump_speed=speed, sip_time=sip_time, up_time=up_time
      ),
      on_progress=on_progress,
      timeout=timeout,
      flushes_line=True,
    )

  async def clean_probe_and_tubing(
    self,
    duration: float = PROBE_CLEAN_DURATION,
    *,
    pump_speed: Optional[float] = None,
    on_progress: Optional[Callable[[RunProgress], None]] = None,
    timeout: Optional[float] = None,
  ) -> CompletedRun:
    """Clean the probe and the sample tubing: dip in Decon for a quarter of the duration, in
    Clean for a quarter and in Water for half. The troughs must be filled.

    Args:
      duration: How long, 4 to 32 minutes in steps of 4, in seconds (240 to 1920).
      pump_speed: Pump speed in rpm, 10 to 53; None for the pump's default speed.
      on_progress: Called for every command that starts or ends.
      timeout: Seconds after which the clean is cancelled.

    Returns:
      The completed run.

    Raises:
      ValueError: A value is out of range.
      CalibrationMissingError: A station, the deck height or the travel height is not
        calibrated.
      WorklistActiveError: A worklist runs.
      DeviceNotReadyError: The detector or another device is not ready.
      UnsafeMotionError: The probe reports contact.
      WorklistFailedError: The clean did not complete.
    """
    speed = self._default_pump_speed(pump_speed)
    return await self._run_maintenance(
      "clean the probe and tubing",
      lambda calibration: probe_and_tubing_clean_worklist(calibration, duration, pump_speed=speed),
      on_progress=on_progress,
      timeout=timeout,
      flushes_line=True,
    )

  async def start_up(
    self,
    *,
    clean_duration: float = PROBE_CLEAN_DURATION,
    pump_speed: Optional[float] = None,
    ready_timeout: Optional[float] = None,
    on_progress: Optional[Callable[[RunProgress], None]] = None,
    timeout: Optional[float] = None,
  ) -> CompletedRun:
    """Start the instrument up for the day: switch the detector on if it is off, wait until
    it can start maintenance, and run the start-up probe and tubing clean.

    The Waste bottle must be empty, the Sheath, PLUS One and PLUS Two bottles and the Decon,
    Clean and Water troughs filled. Warnings of the devices do not stop the clean.

    Args:
      clean_duration: How long the clean lasts, as for :meth:`clean_probe_and_tubing`.
      pump_speed: Pump speed in rpm, 10 to 53; None for the pump's default speed.
      ready_timeout: Seconds to wait for the detector; by default its remaining operation time
        plus a minute, or 25 minutes when it gives none.
      on_progress: Called for every command of the clean that starts or ends.
      timeout: Seconds after which the clean is cancelled.

    Returns:
      The completed clean.

    Raises:
      IQueError: Installation priming or the decommissioning purge is under way; finish it
        first.
      IQueTimeoutError: The detector did not come on, or was not ready, in time.
      DeviceFaultedError: The detector faulted while it was waited for.
      DeviceNotReadyError: The detector left the ready state while it was waited for.
      WorklistActiveError: A worklist runs.
      WorklistFailedError: The clean did not complete.
    """
    detector = self.detector
    await detector.power_on()
    priming, _ = await detector.installation_priming_step()
    if priming != "None":
      raise IQueError(
        f"installation priming is at step {priming}; finish it with "
        "detector.run_next_installation_priming_step() before the start-up clean"
      )
    purge, _ = await detector.purge_step()
    if purge != "None":
      raise IQueError(f"the decommissioning purge is at step {purge}; no start-up clean")
    await detector.wait_until_ready_to_clean(ready_timeout)
    speed = self._default_pump_speed(pump_speed)
    return await self._run_maintenance(
      "start up",
      lambda calibration: startup_worklist(
        calibration, clean_duration=clean_duration, pump_speed=speed
      ),
      on_progress=on_progress,
      timeout=timeout,
      allow_warnings=True,
      flushes_line=True,
    )

  async def shut_down(
    self,
    *,
    clean_duration: float = PROBE_CLEAN_DURATION,
    pump_speed: Optional[float] = None,
    on_progress: Optional[Callable[[RunProgress], None]] = None,
    timeout: Optional[float] = None,
  ) -> Optional[CompletedRun]:
    """Shut the instrument down for the day: the probe and tubing clean, then the detector's
    shutdown. Nothing is run when the detector is off already.

    The bottles and troughs must be prepared as for :meth:`start_up`. A leak (low pressure)
    makes the detector refuse to shut down and fails the run. ``detector.power_off()`` shuts
    the detector down without the clean. Warnings of the devices do not stop the run.

    Args:
      clean_duration: How long the clean lasts, as for :meth:`clean_probe_and_tubing`.
      pump_speed: Pump speed in rpm, 10 to 53; None for the pump's default speed.
      on_progress: Called for every command that starts or ends.
      timeout: Seconds after which the run is cancelled.

    Returns:
      The completed run, or None when the detector was off.

    Raises:
      WorklistActiveError: A worklist runs.
      DeviceNotReadyError: The detector is not ready to run.
      WorklistFailedError: The clean or the shutdown did not complete.
    """
    if not await self.detector.has_power():
      logger.info("the detector is off already; nothing to shut down")
      return None
    speed = self._default_pump_speed(pump_speed)
    return await self._run_maintenance(
      "shut down",
      lambda calibration: shutdown_worklist(
        calibration, clean_duration=clean_duration, pump_speed=speed
      ),
      on_progress=on_progress,
      timeout=timeout,
      allow_warnings=True,
    )

  async def fluidics_check(
    self,
    *,
    pump_speed: Optional[float] = None,
    on_progress: Optional[Callable[[RunProgress], None]] = None,
    timeout: Optional[float] = None,
  ) -> CompletedRun:
    """Run the one-minute fluidics (leak) check, after changing filters, the FluidLink or the
    probe: the probe stays in Water while the pump runs. Meanwhile look for leaks at the pump
    clamp, the tubing port and both ends of the FluidLink. Water must be filled.

    Args:
      pump_speed: Pump speed in rpm, 10 to 53; None for the pump's default speed.
      on_progress: Called for every command that starts or ends.
      timeout: Seconds after which the check is cancelled.

    Returns:
      The completed run.

    Raises:
      CalibrationMissingError: Water, the deck height or the travel height is not calibrated.
      WorklistActiveError: A worklist runs.
      DeviceNotReadyError: The detector or another device is not ready.
      UnsafeMotionError: The probe reports contact.
      WorklistFailedError: The check did not complete.
    """
    speed = self._default_pump_speed(pump_speed)
    return await self._run_maintenance(
      "check the fluidics",
      lambda calibration: fluidics_check_worklist(calibration, pump_speed=speed),
      on_progress=on_progress,
      timeout=timeout,
    )

  async def validate_deck_height(
    self,
    *,
    on_progress: Optional[Callable[[RunProgress], None]] = None,
    timeout: Optional[float] = None,
  ) -> CompletedRun:
    """Check the deck height as every plate read does first: the instrument loads the plate
    rail and touches the test point, and the run fails unless the contact is within the
    tolerance the controller states of the calibrated deck height.

    Args:
      on_progress: Called for the command when it starts and ends.
      timeout: Seconds after which the check is cancelled.

    Returns:
      The completed run; the command's notes hold what the controller measured.

    Raises:
      CalibrationMissingError: The deck height, its test point or the travel height is not
        calibrated.
      WorklistActiveError: A worklist runs.
      DeviceNotReadyError: A device is not ready.
      UnsafeMotionError: The probe reports contact.
      WorklistFailedError: The deck is not at its calibrated height, or the check did not
        complete.
    """
    tolerance = await self._contact_tolerance("DeckHeightValidation")
    return await self._run_maintenance(
      "check the deck height",
      lambda calibration: deck_height_validation_worklist(calibration, tolerance=tolerance),
      on_progress=on_progress,
      timeout=timeout,
    )

  async def _raise_park_and_eject(self) -> None:
    """Raise the probe, wait for the contact sensor to clear, park the probe and eject the
    plate rail.

    Raises:
      UnsafeMotionError: The contact sensor still reports contact with the probe raised.
    """
    await self.sampler.move_z(self.driver.require_limits().z_range.max)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CONTACT_CLEAR_TIMEOUT
    while await self.sampler.has_contact():
      if loop.time() >= deadline:
        raise UnsafeMotionError(
          "the contact sensor reports contact with the probe raised; it appears to be stuck"
        )
      await asyncio.sleep(CONTACT_POLL_INTERVAL)
    await self.sampler.home()
    await self.plate_rail.eject()

  async def calibrate_deck_height(self, *, confirm: bool = False) -> float:
    """Measure the deck height again at its test point and store it in the instrument's
    calibration, as after a probe change.

    The probe parks, the plate rail loads, and the probe moves slowly to the test point and
    searches down for contact from the top of its travel to the stored deck height less the
    tolerance the controller states for a new probe (4 mm), then returns up. A contact within
    that tolerance of the stored deck height becomes the new deck height, with today's date;
    the test point stays. The probe then parks and the rail ejects, also when the measurement
    fails. The new calibration file is uploaded after the instrument's current file is backed
    up to the driver's ``calibration_backup_dir``.

    Every well, station and tube depth is stored relative to the deck height, so all of them
    follow the new value.

    Args:
      confirm: Must be True: the stored calibration changes.

    Returns:
      The new deck height in mm.

    Raises:
      ValueError: `confirm` is not True.
      CalibrationRefusedError: The driver has no backup folder, the probe did not touch within
        the tolerance, or the upload failed its checks.
      CalibrationMissingError: The deck height and its test point are not calibrated.
      WorklistActiveError: A worklist runs.
      UnsafeMotionError: The probe reports contact, or the plate rail cannot move.
      DeviceOperationError: The controller reported an error for the contact search.
    """
    if confirm is not True:
      raise ValueError(
        "calibrating the deck height changes the instrument's stored calibration, which every "
        "well, station and tube depth follows; pass confirm=True"
      )
    driver = self.driver
    driver.require_setup()
    if driver.calibration_backup_dir is None:
      raise CalibrationRefusedError(
        "calibrating the deck height needs calibration_backup_dir, where the instrument's "
        "current calibration file is kept before the new one is stored"
      )
    calibration = self._calibration()
    try:
      point = deck_height_test_point(calibration)
    except CalibrationError as error:
      raise CalibrationMissingError(f"cannot calibrate the deck height: {error}") from error
    previous = calibration.instrument.deck_height
    tolerance = await self._contact_tolerance("NewProbeAndTubing")
    z_range = driver.require_limits().z_range
    z_low = max(previous - tolerance, z_range.min)
    await self.sampler.home()
    await self.plate_rail.load()
    try:
      await self.sampler.move_xy(point.x, point.y)
      contact = await self.sampler.find_z_contact(z_range.max, z_low, retract_after_find=True)
    finally:
      await self._raise_park_and_eject()
    expected = f"within {tolerance} mm of the stored deck height {previous} mm"
    if contact is None or contact > previous + tolerance:
      found = "nothing" if contact is None else f"something at {contact} mm"
      raise CalibrationRefusedError(
        f"the probe touched {found} at X {point.x}, Y {point.y}; the deck was expected {expected}"
      )
    if contact < previous - tolerance:
      raise CalibrationRefusedError(
        f"the probe touched at {contact} mm at X {point.x}, Y {point.y}; the deck was expected "
        f"{expected}"
      )
    measured_on = datetime.datetime.now().astimezone().replace(microsecond=0)
    updated = with_deck_height(calibration, contact, measured_on)
    backup = await driver.upload_calibration(write_calibration(updated))
    logger.warning(
      "stored a new deck height of %s mm (was %s mm); the previous calibration file is at %s",
      contact,
      previous,
      backup,
    )
    return contact


def IQue(
  name: str = "iQue",
  *,
  host: Optional[str] = None,
  simulation: bool = False,
  simulation_speed: float = 1.0,
  simulation_seed: int = 0,
  calibration_backup_dir: Optional[pathlib.Path] = None,
  size_x: float = SIZE_X,
  size_y: float = SIZE_Y,
  size_z: float = SIZE_Z,
) -> IQueDevice:
  """Build an iQue Screener PLUS resource.

  Args:
    name: The resource name.
    host: Host name or address of the instrument's internal computer.
    simulation: Run against an in-process simulated controller instead of an instrument.
    simulation_speed: How many simulated seconds pass per real second during a simulated
      worklist.
    simulation_seed: Seed of the simulator's random choices.
    calibration_backup_dir: Where uploads keep the instrument's previous calibration file.
    size_x: Width in mm.
    size_y: Depth in mm.
    size_z: Height in mm.

  Raises:
    ValueError: A host is given with `simulation`, or neither is given.
  """
  if simulation:
    if host is not None:
      raise ValueError("a simulated instrument has no host")
    controller = SimulatedController(speed=simulation_speed, seed=simulation_seed)
    driver = IQueDriver(client=controller, calibration_backup_dir=calibration_backup_dir)
  else:
    if host is None:
      raise ValueError("give the instrument's host, or simulation=True")
    driver = IQueDriver(host, calibration_backup_dir=calibration_backup_dir)
  return IQueDevice(driver, name, size_x=size_x, size_y=size_y, size_z=size_z)
