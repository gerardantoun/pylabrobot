"""The sampler: the probe's XYZ arm, and the probe's contact sensor.

Positions are millimetres in the sampler's own frame: X 0 to 131, Y 0 to 195, Z 190 to 250,
larger Z higher; the park position is the top of Z at the largest X and Y. The controller moves
the probe wherever it is told. The driver adds the checks it lacks:

- an XY move needs the probe at or above the travel height of the calibration, and the probe
  must not report contact; the move is refused otherwise (the driver does not raise the probe
  on its own);
- an XY move that would pass over the plate area needs the plate rail loaded, and so does
  lowering the probe or a contact search there; the plate area is the rectangle of every plate
  format's corner wells in the calibration, widened by :data:`PLATE_AREA_MARGIN`;
- lowering the probe or a contact search is refused while the probe reports contact;
- nothing moves while a worklist runs, or while a mechanical device is faulted, except parking
  and raising the probe, which are always allowed.

A Z move returns once the reported Z has reached the target: the controller answers as soon as
the move is accepted, before the probe arrives.
"""

from __future__ import annotations

import asyncio
import decimal
import logging
from typing import TYPE_CHECKING, NamedTuple, Optional, Tuple

from pylabrobot.sartorius.ique.driver.calibration import (
  PLATE_FORMATS,
  Calibration,
  CalibrationError,
  travel_height,
)
from pylabrobot.sartorius.ique.driver.errors import (
  CalibrationMissingError,
  DeviceOperationError,
  IQueTimeoutError,
  UnsafeMotionError,
)

if TYPE_CHECKING:
  from pylabrobot.sartorius.ique.driver.master import AxisRange, IQueDriver

logger = logging.getLogger(__name__)

POSITION_TOLERANCE = 0.1
"""How far, in mm, a reported position may lie from a target and still count as there."""
PARK_TOLERANCE = 0.5
"""How far, in mm, the probe may lie from the park position and still count as parked."""
PLATE_AREA_MARGIN = 5.0
"""How far, in mm, the plate area reaches beyond the centres of the corner wells: more than half
the 96-well pitch."""
Z_SETTLE_TIMEOUT = 15.0
"""Seconds a Z move may take to arrive."""
Z_POLL_INTERVAL = 0.05
"""Seconds between Z reads while waiting for a Z move to arrive."""


class SamplerPosition(NamedTuple):
  """Where the probe is, in mm."""

  x: float
  y: float
  z: float


class PlateArea(NamedTuple):
  """A rectangle of the sampler's horizontal plane, in mm."""

  x_min: float
  x_max: float
  y_min: float
  y_max: float

  def contains(self, x: float, y: float) -> bool:
    """Return whether a point lies in the rectangle, its edges included."""
    return self.x_min <= x <= self.x_max and self.y_min <= y <= self.y_max


def plate_area(calibration: Calibration) -> PlateArea:
  """Return the plate area: the rectangle of every plate format's corner wells, widened.

  Raises:
    CalibrationMissingError: The calibration has no plate corners.
  """
  points = []
  for entry in calibration.plate_types:
    if entry.plate_type in PLATE_FORMATS:
      corners = entry.corners
      points += [corners.top_left, corners.top_right, corners.bottom_left, corners.bottom_right]
  if not points:
    raise CalibrationMissingError("the calibration holds no plate corners")
  return PlateArea(
    min(p.x for p in points) - PLATE_AREA_MARGIN,
    max(p.x for p in points) + PLATE_AREA_MARGIN,
    min(p.y for p in points) - PLATE_AREA_MARGIN,
    max(p.y for p in points) + PLATE_AREA_MARGIN,
  )


def segment_meets_area(
  start: Tuple[float, float], end: Tuple[float, float], area: PlateArea
) -> bool:
  """Return whether the straight path between two points touches a rectangle.

  The sampler moves X and Y together, so the probe follows that line.
  """
  low, high = 0.0, 1.0
  (x0, y0), (x1, y1) = start, end
  for delta, begin, minimum, maximum in (
    (x1 - x0, x0, area.x_min, area.x_max),
    (y1 - y0, y0, area.y_min, area.y_max),
  ):
    if delta == 0:
      if not minimum <= begin <= maximum:
        return False
      continue
    first, second = (minimum - begin) / delta, (maximum - begin) / delta
    low, high = max(low, min(first, second)), min(high, max(first, second))
    if low > high:
      return False
  return True


def _millimetres(value: float) -> decimal.Decimal:
  """Return a length in mm as the decimal the controller takes, to a thousandth of a mm."""
  return decimal.Decimal(value).quantize(decimal.Decimal("0.001"))


def _check_axis(name: str, value: float, axis: AxisRange) -> None:
  """Raise unless a coordinate lies within an axis's travel.

  Raises:
    ValueError: It does not.
  """
  if not axis.min <= value <= axis.max:
    raise ValueError(f"{name} {value} mm is outside the sampler's travel {axis.min}..{axis.max} mm")


class Sampler:
  """The sampler and the probe's contact sensor.

  Reached as ``driver.sampler``.
  """

  def __init__(self, driver: IQueDriver) -> None:
    """Create the feature.

    Args:
      driver: The driver whose session it uses.
    """
    self._driver = driver

  @property
  def travel_height(self) -> float:
    """The Z the probe must be at or above before any horizontal move, from the calibration.

    Raises:
      CalibrationMissingError: No calibration is loaded or it holds no travel height.
    """
    try:
      return travel_height(self._driver.require_calibration())
    except CalibrationError as error:
      raise CalibrationMissingError(str(error)) from error

  @property
  def plate_area(self) -> PlateArea:
    """The area above the plate holder where the probe may go only with the rail loaded.

    Raises:
      CalibrationMissingError: No calibration is loaded or it holds no plate corners.
    """
    return plate_area(self._driver.require_calibration())

  async def request_position(self) -> SamplerPosition:
    """Read the probe's position.

    Raises:
      DeviceFaultedError: The read failed; the controller faulted the sampler.
    """
    driver = self._driver
    driver.require_setup()
    client = driver.client
    position = SamplerPosition(
      float(await driver.call(client.sampler_get_x)),
      float(await driver.call(client.sampler_get_y)),
      float(await driver.call(client.sampler_get_z)),
    )
    if -1.0 in position:
      await driver.confirm_operation("read the probe position", "Sampler")
    return position

  async def has_contact(self) -> bool:
    """Read whether the probe's contact sensor reports contact."""
    driver = self._driver
    driver.require_setup()
    return await driver.call(driver.client.contact_sensor_has_contact)

  async def _refuse_contact(self, action: str) -> None:
    """Raise if the probe reports contact.

    Raises:
      UnsafeMotionError: It does.
    """
    if await self.has_contact():
      raise UnsafeMotionError(
        f"cannot {action}: the probe reports contact; raise it with move_z first"
      )

  def _require_rail_loaded_over(self, action: str, x: float, y: float) -> None:
    """Raise if a point over the plate area is to be used while the rail is not loaded.

    Raises:
      UnsafeMotionError: It is over the plate area and the rail is not known to be loaded.
    """
    if self.plate_area.contains(x, y) and self._driver.rail_position != "loaded":
      raise UnsafeMotionError(
        f"cannot {action}: X {x}, Y {y} is over the plate area and the plate rail is not loaded"
      )

  async def _wait_for_z(self, action: str, target: float) -> None:
    """Wait until the reported Z is at the target.

    Raises:
      IQueTimeoutError: It is not there within :data:`Z_SETTLE_TIMEOUT`.
    """
    driver = self._driver
    loop = asyncio.get_running_loop()
    deadline = loop.time() + Z_SETTLE_TIMEOUT
    while True:
      z = float(await driver.call(driver.client.sampler_get_z))
      if abs(z - target) <= POSITION_TOLERANCE:
        return
      if loop.time() >= deadline:
        await driver.confirm_operation(action, "Sampler")
        raise IQueTimeoutError(f"cannot {action}: the probe is at Z {z} mm, not {target} mm")
      await asyncio.sleep(Z_POLL_INTERVAL)

  async def home(self) -> None:
    """Park the probe slowly: Z to the top, then X and Y to their largest values.

    Allowed while another mechanical device is faulted.

    Raises:
      UnsafeMotionError: The probe reports contact; the controller would refuse and fault the
        sampler.
      DeviceFaultedError: The sampler faulted.
    """
    driver = self._driver
    await driver.require_direct_operation("park the probe", "Sampler", check_other_devices=False)
    await self._refuse_contact("park the probe")
    await driver.call(driver.client.sampler_home)
    await self._wait_for_z("park the probe", driver.require_limits().z_range.max)
    await driver.confirm_operation("park the probe", "Sampler")
    logger.info("probe parked")

  async def move_z(self, z: float) -> None:
    """Move the probe to a height and wait until it is there.

    Raising the probe is always allowed. Lowering it is refused while the probe reports contact,
    over the plate area while the plate rail is not loaded, and while a mechanical device is
    faulted.

    Args:
      z: The height in mm; larger is higher.

    Raises:
      ValueError: `z` is outside the travel.
      UnsafeMotionError: A safety rule forbids the move.
      CalibrationMissingError: Lowering needs the calibration's plate area.
      DeviceFaultedError: The sampler faulted.
    """
    driver = self._driver
    _check_axis("Z", z, driver.require_limits().z_range)
    driver.require_setup()
    position = await self.request_position()
    lowering = z < position.z - POSITION_TOLERANCE
    action = f"move the probe to Z {z} mm"
    await driver.require_direct_operation(action, "Sampler", check_other_devices=lowering)
    if lowering:
      await self._refuse_contact(action)
      self._require_rail_loaded_over(action, position.x, position.y)
    await driver.call(driver.client.sampler_move_z, _millimetres(z))
    await self._wait_for_z(action, z)
    await driver.confirm_operation(action, "Sampler")

  async def move_xy(self, x: float, y: float, *, fast: bool = False) -> None:
    """Move the probe horizontally; the controller returns once X and Y have stopped.

    Refused unless the probe is at or above the travel height and reports no contact, and, for
    a path over the plate area, unless the plate rail is loaded.

    Args:
      x: Target X in mm.
      y: Target Y in mm.
      fast: The fast XY speed instead of the slow one.

    Raises:
      ValueError: The target is outside the travel.
      UnsafeMotionError: A safety rule forbids the move.
      CalibrationMissingError: No calibration gives the travel height and plate area.
      DeviceFaultedError: The sampler faulted.
    """
    driver = self._driver
    limits = driver.require_limits()
    _check_axis("X", x, limits.x_range)
    _check_axis("Y", y, limits.y_range)
    action = f"move the probe to X {x}, Y {y}"
    await driver.require_direct_operation(action, "Sampler")
    travel = self.travel_height
    area = self.plate_area
    await self._refuse_contact(action)
    position = await self.request_position()
    if position.z < travel - POSITION_TOLERANCE:
      raise UnsafeMotionError(
        f"cannot {action}: the probe is at Z {position.z} mm, below the travel height "
        f"{travel} mm; raise it with move_z first"
      )
    if segment_meets_area((position.x, position.y), (x, y), area):
      if driver.rail_position != "loaded":
        raise UnsafeMotionError(
          f"cannot {action}: the path passes over the plate area and the plate rail is not loaded"
        )
    await driver.call(driver.client.sampler_move_xy, _millimetres(x), _millimetres(y), fast)
    await driver.confirm_operation(action, "Sampler")

  async def find_z_contact(
    self, z_high: float, z_low: float, *, retract_after_find: bool = True
  ) -> Optional[float]:
    """Lower the probe at its current XY until its contact sensor reports contact.

    The controller descends to 3 mm below `z_low` (but not below the travel), then reports the
    height at which contact ended on the way back up. With `retract_after_find` the probe
    returns to `z_high`; otherwise it stays at the contact height.

    Args:
      z_high: The height to search from and return to, in mm.
      z_low: The lowest expected contact height, in mm.
      retract_after_find: Return to `z_high` afterwards.

    Returns:
      The contact height in mm, or None when the probe touched nothing.

    Raises:
      ValueError: The heights are outside the travel or not in order.
      UnsafeMotionError: The probe reports contact already, or it is over the plate area with
        the rail not loaded.
      DeviceOperationError: The controller reported an error for the search.
      DeviceFaultedError: The sampler faulted.
    """
    driver = self._driver
    z_range = driver.require_limits().z_range
    _check_axis("z_high", z_high, z_range)
    _check_axis("z_low", z_low, z_range)
    if z_low >= z_high:
      raise ValueError(f"z_low {z_low} mm must be below z_high {z_high} mm")
    action = "search for contact"
    await driver.require_direct_operation(action, "Sampler")
    await self._refuse_contact(action)
    position = await self.request_position()
    self._require_rail_loaded_over(action, position.x, position.y)
    result = await driver.call(
      driver.client.sampler_find_z_contact,
      _millimetres(z_high),
      _millimetres(z_low),
      retract_after_find,
    )
    await driver.confirm_operation(action, "Sampler")
    if result is None:
      raise DeviceOperationError("Sampler_FindZContact", "the controller returned no result")
    if result.has_error:
      raise DeviceOperationError("Sampler_FindZContact", result.error_message or "no message")
    if retract_after_find:
      await self._wait_for_z(action, z_high)
    if not result.found_contact:
      return None
    return float(result.contact_z_location)

  async def require_parked(self, action: str) -> None:
    """Raise unless the probe is parked and reports no contact.

    Raises:
      UnsafeMotionError: It is not parked, or reports contact.
    """
    await self._refuse_contact(action)
    limits = self._driver.require_limits()
    position = await self.request_position()
    parked = (
      position.z >= limits.z_range.max - PARK_TOLERANCE
      and position.x >= limits.x_range.max - PARK_TOLERANCE
      and position.y >= limits.y_range.max - PARK_TOLERANCE
    )
    if not parked:
      raise UnsafeMotionError(
        f"cannot {action}: the probe is not parked (it is at X {position.x}, Y {position.y}, "
        f"Z {position.z}); call sampler.home() first"
      )

  async def require_clear_of_plate(self, action: str) -> None:
    """Raise unless the probe is at or above the travel height or outside the plate area.

    Raises:
      UnsafeMotionError: The probe is low over the plate area.
      CalibrationMissingError: No calibration gives the travel height and plate area.
    """
    travel = self.travel_height
    area = self.plate_area
    position = await self.request_position()
    if position.z >= travel - POSITION_TOLERANCE or not area.contains(position.x, position.y):
      return
    raise UnsafeMotionError(
      f"cannot {action}: the probe is at Z {position.z} mm over the plate area; raise it to the "
      f"travel height {travel} mm first"
    )
