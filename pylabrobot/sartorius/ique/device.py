"""The IntelliCyt / Sartorius iQue Screener PLUS as a PyLabRobot resource."""

from __future__ import annotations

import pathlib
from typing import Optional

from pylabrobot.resources import Coordinate, PlateHolder, Resource
from pylabrobot.sartorius.ique.driver.errors import PlateAccessError
from pylabrobot.sartorius.ique.driver.features.detector import Detector
from pylabrobot.sartorius.ique.driver.features.plate_rail import PlateRail
from pylabrobot.sartorius.ique.driver.features.pump import Pump
from pylabrobot.sartorius.ique.driver.features.sampler import Sampler
from pylabrobot.sartorius.ique.driver.features.shaker import Shaker
from pylabrobot.sartorius.ique.driver.master import IQueDriver
from pylabrobot.sartorius.ique.driver.simulator import SimulatedController

# Outer size of the instrument in mm. Placeholders: the hardware manual's figures are not in
# hand, so these are a rough bench-top envelope, not measured.
SIZE_X = 740.0
SIZE_Y = 640.0
SIZE_Z = 620.0

# The plate holder takes an SBS-format plate. Where it sits within the instrument's envelope is
# not known; the holder is placed at the instrument's origin.
PLATE_HOLDER_SIZE_X = 127.76
PLATE_HOLDER_SIZE_Y = 85.48


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


class IQueDevice(Resource):
  """The iQue Screener PLUS as a PyLabRobot resource: its plate holder, its features
  (``plate_rail``, ``shaker``, ``pump``, ``sampler``, ``detector``) and the driver that runs it.

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
