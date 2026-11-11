"""The peristaltic pump that draws sample from the probe to the detector.

Speeds are rpm; the controller takes hundredths of rpm and would clamp a speed outside the
pump's range, which the driver refuses instead. The pump turns clockwise when it sits on the
right of the instrument, which the calibration records.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, Literal, Optional, Tuple

from pylabrobot.sartorius.ique.driver.contract import PumpDirection

if TYPE_CHECKING:
  from pylabrobot.sartorius.ique.driver.master import IQueDriver

logger = logging.getLogger(__name__)

PumpRotation = Literal["clockwise", "counterclockwise"]

_DIRECTIONS: Dict[PumpRotation, PumpDirection] = {"clockwise": "CW", "counterclockwise": "CCW"}


def wire_speed(rpm: float) -> int:
  """Return a pump speed in rpm as the controller's hundredths of rpm."""
  return round(rpm * 100)


class Pump:
  """The pump.

  Reached as ``driver.pump``.
  """

  def __init__(self, driver: IQueDriver) -> None:
    """Create the feature.

    Args:
      driver: The driver whose session it uses.
    """
    self._driver = driver

  @property
  def speed_range(self) -> Tuple[float, float]:
    """The pump's lowest and highest speed in rpm, as it reported them at setup."""
    limits = self._driver.require_limits()
    return limits.pump_min_speed, limits.pump_max_speed

  @property
  def default_speed(self) -> float:
    """The pump's default speed in rpm, as it reported it at setup."""
    return self._driver.require_limits().pump_default_speed

  def default_direction(self) -> PumpRotation:
    """Return the direction the pump turns on this instrument: clockwise when it is on the
    right, from the calibration.

    Raises:
      CalibrationMissingError: No calibration is loaded.
    """
    location = self._driver.require_calibration().instrument.pump_location
    return "clockwise" if location == "Right" else "counterclockwise"

  async def start(
    self, speed: Optional[float] = None, direction: Optional[PumpRotation] = None
  ) -> None:
    """Run the pump, or change its speed and direction while it runs.

    Args:
      speed: Speed in rpm; the pump's default speed when None.
      direction: Direction; the instrument's direction (:meth:`default_direction`) when None.

    Raises:
      ValueError: The speed is outside the pump's range.
      CalibrationMissingError: No direction is given and no calibration is loaded.
      WorklistActiveError: A worklist runs.
      DeviceFaultedError: A mechanical device is faulted, or the pump faulted.
      DeviceNotReadyError: The pump is neither ready nor running.
    """
    driver = self._driver
    rpm = self.default_speed if speed is None else speed
    low, high = self.speed_range
    if not low <= rpm <= high:
      raise ValueError(f"pump speed {rpm} rpm is outside {low}..{high} rpm")
    rotation = self.default_direction() if direction is None else direction
    action = f"run the pump at {rpm} rpm {rotation}"
    info = await driver.require_direct_operation(action, "Pump", ("Ready", "Running"))
    client = driver.client
    if info.state == "Running":
      await driver.call(client.pump_update, wire_speed(rpm), _DIRECTIONS[rotation])
    else:
      await driver.call(client.pump_start, wire_speed(rpm), _DIRECTIONS[rotation])
    await driver.confirm_operation(action, "Pump")
    logger.info("pump at %s rpm %s", rpm, rotation)

  async def stop(self) -> None:
    """Stop the pump; nothing is sent when it stands still. Allowed while another mechanical
    device is faulted.

    Raises:
      WorklistActiveError: A worklist runs.
      DeviceFaultedError: The pump is faulted.
      DeviceNotReadyError: The pump is not connected.
    """
    driver = self._driver
    info = await driver.require_direct_operation(
      "stop the pump", "Pump", ("Ready", "Running"), check_other_devices=False
    )
    if info.state == "Ready":
      return
    await driver.call(driver.client.pump_stop)
    await driver.confirm_operation("stop the pump", "Pump")
    logger.info("pump stopped")
