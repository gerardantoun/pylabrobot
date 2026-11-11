"""The shaker under the plate holder (a BioShake), and its plate lock.

Speeds are rpm; the controller would clamp a speed outside the shaker's range, the driver
refuses it. Shaking needs the probe out of the plate: at or above the travel height, or away
from the plate area. So do homing the shaker and moving the plate lock, which move the plate,
unless the plate rail is out. The plate lock is closed before shaking starts. The lock may only
change while the shaker stands still, and an open lock closes itself after five minutes and
faults the shaker.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
  from pylabrobot.sartorius.ique.driver.master import IQueDriver

logger = logging.getLogger(__name__)


class Shaker:
  """The shaker.

  Reached as ``driver.shaker``.

  Attributes:
    lock_closed: Whether the plate lock was last closed by this driver (True) or opened (False);
      None until this driver set it. Initialisation and homing close it.
  """

  def __init__(self, driver: IQueDriver) -> None:
    """Create the feature.

    Args:
      driver: The driver whose session it uses.
    """
    self._driver = driver
    self._speed_range: Optional[Tuple[int, int]] = None
    self.lock_closed: Optional[bool] = None

  async def speed_range(self) -> Tuple[int, int]:
    """Return the shaker's lowest and highest speed in rpm, as it reports them."""
    driver = self._driver
    driver.require_setup()
    if self._speed_range is None:
      client = driver.client
      self._speed_range = (
        await driver.call(client.shaker_min_rpm),
        await driver.call(client.shaker_max_rpm),
      )
    return self._speed_range

  async def start(self, speed: int) -> None:
    """Shake at a speed, or change the speed while shaking; returns once at speed.

    Args:
      speed: Speed in rpm.

    Raises:
      ValueError: The speed is outside the shaker's range.
      WorklistActiveError: A worklist runs.
      DeviceFaultedError: A mechanical device is faulted, or the shaker faulted.
      DeviceNotReadyError: The shaker is neither ready nor shaking.
      UnsafeMotionError: The probe is low over the plate area.
    """
    driver = self._driver
    low, high = await self.speed_range()
    if not low <= speed <= high:
      raise ValueError(f"shaker speed {speed} rpm is outside {low}..{high} rpm")
    action = f"shake at {speed} rpm"
    info = await driver.require_direct_operation(action, "Shaker", ("Ready", "Running"))
    await driver.sampler.require_clear_of_plate(action)
    if info.state == "Ready":
      await driver.call(driver.client.shaker_close_plate_lock)
      await driver.confirm_operation("close the plate lock", "Shaker")
      self.lock_closed = True
    await driver.call(driver.client.shaker_start, speed)
    await driver.confirm_operation(action, "Shaker")
    logger.info("shaking at %d rpm", speed)

  async def stop(self) -> None:
    """Stop shaking; returns once the shaker is back at its home position. Nothing is sent
    when it stands still. Allowed while another mechanical device is faulted.

    Raises:
      WorklistActiveError: A worklist runs.
      DeviceFaultedError: The shaker is faulted.
      DeviceNotReadyError: The shaker is not connected.
    """
    driver = self._driver
    info = await driver.require_direct_operation(
      "stop the shaker", "Shaker", ("Ready", "Running"), check_other_devices=False
    )
    if info.state == "Ready":
      return
    await driver.call(driver.client.shaker_stop)
    await driver.confirm_operation("stop the shaker", "Shaker")
    logger.info("shaker stopped")

  async def _require_probe_clear(self, action: str) -> None:
    """Raise if the plate may move under the probe: the rail is not known to be out and the
    probe is low over the plate area.

    Raises:
      UnsafeMotionError: The probe is low over the plate area.
      CalibrationMissingError: The rail is not out and no calibration gives the travel height
        and plate area.
    """
    driver = self._driver
    if driver.rail_position != "ejected":
      await driver.sampler.require_clear_of_plate(action)

  async def home(self) -> None:
    """Close the plate lock and move the shaker to its home position.

    Raises:
      WorklistActiveError: A worklist runs.
      DeviceFaultedError: A mechanical device is faulted, or the shaker faulted.
      DeviceNotReadyError: The shaker is not ready (it is when it stands still).
      UnsafeMotionError: The probe is low over the plate area.
    """
    driver = self._driver
    await driver.require_direct_operation("home the shaker", "Shaker")
    await self._require_probe_clear("home the shaker")
    await driver.call(driver.client.shaker_home)
    await driver.confirm_operation("home the shaker", "Shaker")
    self.lock_closed = True

  async def lock_plate(self) -> None:
    """Close the plate lock.

    Raises:
      WorklistActiveError: A worklist runs.
      DeviceFaultedError: A mechanical device is faulted, or the lock did not close.
      DeviceNotReadyError: The shaker is not ready.
      UnsafeMotionError: The probe is low over the plate area.
    """
    driver = self._driver
    await driver.require_direct_operation("close the plate lock", "Shaker")
    await self._require_probe_clear("close the plate lock")
    await driver.call(driver.client.shaker_close_plate_lock)
    await driver.confirm_operation("close the plate lock", "Shaker")
    self.lock_closed = True

  async def unlock_plate(self) -> None:
    """Open the plate lock. It closes itself after five minutes and faults the shaker.

    Raises:
      WorklistActiveError: A worklist runs.
      DeviceFaultedError: A mechanical device is faulted, or the lock did not open.
      DeviceNotReadyError: The shaker is not ready.
      UnsafeMotionError: The probe is low over the plate area.
    """
    driver = self._driver
    await driver.require_direct_operation("open the plate lock", "Shaker")
    await self._require_probe_clear("open the plate lock")
    await driver.call(driver.client.shaker_open_plate_lock)
    await driver.confirm_operation("open the plate lock", "Shaker")
    self.lock_closed = False
    logger.warning("plate lock open; it closes itself and faults the shaker after 5 minutes")
