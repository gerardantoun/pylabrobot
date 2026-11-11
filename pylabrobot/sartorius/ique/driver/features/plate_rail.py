"""The plate rail: carries the plate holder, with the shaker on it, in and out of the instrument.

The rail has two positions: loaded (in, under the probe) and ejected (out, where a plate is
put on or taken off). The controller reports no position, so the driver keeps it: the rail is
ejected after initialisation and after every worklist, and wherever the last move of this
feature took it; while it moves, or after it faulted, the position is not known.

The controller does not check where the probe is before it moves the rail. The driver refuses a
rail move unless the probe is parked and reports no contact and the shaker stands still, and
closes the shaker's plate lock before loading, as the controller itself does before it loads the
rail during a worklist. Ejecting needs only the rail, the sampler and the shaker in order, so
that a fault elsewhere (a shaker whose open lock closed itself, say) does not keep a plate
inside.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, Optional

from pylabrobot.sartorius.ique.driver.errors import DeviceNotReadyError, UnsafeMotionError

if TYPE_CHECKING:
  from pylabrobot.sartorius.ique.driver.master import IQueDriver

logger = logging.getLogger(__name__)

PlatePosition = Literal["loaded", "ejected"]


class PlateRail:
  """The plate rail.

  Reached as ``driver.plate_rail``.
  """

  def __init__(self, driver: IQueDriver) -> None:
    """Create the feature.

    Args:
      driver: The driver whose session it uses.
    """
    self._driver = driver

  @property
  def position(self) -> Optional[PlatePosition]:
    """Where the rail is: ``loaded``, ``ejected``, or None when not known."""
    return self._driver.rail_position

  @property
  def is_ejected(self) -> bool:
    """Whether the rail is known to be out, so a plate can be put on or taken off."""
    return self._driver.rail_position == "ejected"

  async def _move(self, target: PlatePosition) -> None:
    """Move the rail after the checks, and record where it is.

    Raises:
      WorklistActiveError: A worklist runs.
      DeviceFaultedError: For a load a mechanical device is faulted, for an eject the rail; or
        the rail faulted while moving.
      DeviceNotReadyError: The rail or the sampler, or for a load the shaker, is not ready.
      UnsafeMotionError: The probe is not parked or reports contact, or the shaker shakes.
    """
    driver = self._driver
    loading = target == "loaded"
    action = "load the plate rail" if loading else "eject the plate rail"
    await driver.require_direct_operation(action, "PlateRail", check_other_devices=loading)
    devices = driver.devices
    sampler = devices.get("Sampler")
    if sampler is None or sampler.state != "Ready":
      raise DeviceNotReadyError(action, "Sampler", sampler)
    shaker = devices.get("Shaker")
    if shaker is not None and shaker.state == "Running":
      raise UnsafeMotionError(f"cannot {action}: the shaker shakes; stop it with shaker.stop()")
    await driver.sampler.require_parked(action)
    if target == "loaded":
      await driver.shaker.lock_plate()
    driver.rail_position = None
    client = driver.client
    await driver.call(client.plate_rail_load if target == "loaded" else client.plate_rail_eject)
    await driver.confirm_operation(action, "PlateRail")
    driver.rail_position = target
    logger.info("plate rail %s", target)

  async def load(self) -> None:
    """Close the plate lock and move the rail in (at most 15 s).

    Raises:
      WorklistActiveError: A worklist runs.
      DeviceFaultedError: A mechanical device is faulted, or the rail faulted while moving.
      DeviceNotReadyError: The rail, the sampler or the shaker is not ready.
      UnsafeMotionError: The probe is not parked or reports contact, or the shaker shakes.
    """
    await self._move("loaded")

  async def eject(self) -> None:
    """Move the rail out (at most 15 s).

    Only the rail, the sampler and the shaker are checked: a fault of another device, or of
    the shaker, does not keep the plate inside.

    Raises:
      WorklistActiveError: A worklist runs.
      DeviceFaultedError: The rail is faulted, or it faulted while moving.
      DeviceNotReadyError: The rail or the sampler is not ready.
      UnsafeMotionError: The probe is not parked or reports contact, or the shaker shakes.
    """
    await self._move("ejected")
