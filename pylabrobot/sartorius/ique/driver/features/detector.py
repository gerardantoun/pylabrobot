"""The detector: the cytometer that measures the sample.

The controller powers the detector through a relay on the plate rail's drive and then looks for
it for about 30 s. Switching it off runs the detector's own shutdown routine; the controller's
relay pulse alone would cut its power without shutting the fluidics down. Readiness, laser,
fluid and consumable states reach the driver only as the controller's status texts and device
summary, which :meth:`Detector.request_status` gathers.

Maintenance that the controller runs as worklists (cleans, priming, shutdown with a clean) is
not here; the step functions of installation priming and of the decommissioning purge are. The
purge leaves the instrument unusable until installation priming has been done, so its first step
needs an explicit confirmation. Firmware updates are not offered.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Tuple

from pylabrobot.sartorius.ique.driver.contract import (
  DeviceAlert,
  DeviceMessage,
  DeviceState,
  IQue3InstallationPrimeStep,
  IQue3PurgeStep,
)
from pylabrobot.sartorius.ique.driver.errors import (
  DeviceFaultedError,
  DeviceNotReadyError,
  IQueError,
  IQueTimeoutError,
)

if TYPE_CHECKING:
  from pylabrobot.sartorius.ique.driver.master import IQueDriver

logger = logging.getLogger(__name__)

POWER_ON_TIMEOUT = 60.0
"""Seconds to wait for the detector after switching it on; the controller looks for it for
about 30 s."""
READY_TIMEOUT_WITHOUT_ESTIMATE = 25 * 60.0
"""Seconds to wait for readiness when the detector gives no remaining operation time."""
READY_TIMEOUT_MARGIN = 60.0
"""Seconds added to the detector's remaining operation time when waiting for readiness."""
LONG_RUNNING_TIMEOUT = 60.0
"""Seconds to wait for the result of a fluid-station calibration or an odometer reset."""


@dataclass(frozen=True)
class DetectorStatus:
  """What the controller reports about the detector.

  Attributes:
    state: The device state, or None when the controller does not report the detector.
    model: The model text.
    messages: The status messages: fluid levels, laser and pressure states, warnings and errors,
      as the instrument words them.
    has_power: Whether the controller is connected to the detector.
    powering_on: Whether the controller is still looking for it after switching it on.
    ready_to_run: Whether a measurement can start.
    ready_to_clean: Whether fluidics maintenance can start.
    operation_remaining: Remaining time of the detector's current operation.
    alert: The detector's last alert, or None.
    purge_step: The current step of the decommissioning purge, and whether it is completed.
    installation_priming_step: The current step of installation priming, and whether it is
      completed.
    summary: The controller's description of the detector: model, serial number, firmware,
      fluidics run time, lasers and detectors.
  """

  state: Optional[DeviceState]
  model: Optional[str]
  messages: Tuple[DeviceMessage, ...]
  has_power: bool
  powering_on: bool
  ready_to_run: bool
  ready_to_clean: bool
  operation_remaining: datetime.timedelta
  alert: Optional[DeviceAlert]
  purge_step: Tuple[IQue3PurgeStep, bool]
  installation_priming_step: Tuple[IQue3InstallationPrimeStep, bool]
  summary: Optional[str]


class Detector:
  """The detector (cytometer).

  Reached as ``driver.detector``.

  Attributes:
    poll_interval: Seconds between status reads while waiting.
  """

  def __init__(self, driver: IQueDriver) -> None:
    """Create the feature.

    Args:
      driver: The driver whose session it uses.
    """
    self._driver = driver
    self.poll_interval = 1.0

  async def has_power(self) -> bool:
    """Read whether the controller is connected to the detector."""
    driver = self._driver
    driver.require_setup()
    return await driver.call(driver.client.controller_cytometer_has_power)

  async def is_ready_to_run(self) -> bool:
    """Read whether the detector can start a measurement."""
    driver = self._driver
    driver.require_setup()
    return await driver.call(driver.client.cytometer_is_ready_to_run)

  async def is_ready_to_clean(self) -> bool:
    """Read whether the detector can start fluidics maintenance."""
    driver = self._driver
    driver.require_setup()
    return await driver.call(driver.client.cytometer_is_ready_to_clean)

  async def request_status(self) -> DetectorStatus:
    """Read everything the controller reports about the detector."""
    driver = self._driver
    driver.require_setup()
    client = driver.client
    devices = await driver.call(client.controller_get_device_statuses)
    info = next((d for d in devices or [] if d is not None and d.kind == "Cytometer"), None)
    return DetectorStatus(
      state=None if info is None else info.state,
      model=None if info is None else info.model,
      messages=() if info is None else tuple(m for m in info.messages or [] if m is not None),
      has_power=await driver.call(client.controller_cytometer_has_power),
      powering_on=await driver.call(client.cytometer_is_powering_on),
      ready_to_run=await driver.call(client.cytometer_is_ready_to_run),
      ready_to_clean=await driver.call(client.cytometer_is_ready_to_clean),
      operation_remaining=await driver.call(client.cytometer_get_operation_duration),
      alert=driver.status.alerts.get("Cytometer"),
      purge_step=await driver.call(client.ique3_get_current_purge_step),
      installation_priming_step=await driver.call(
        client.ique3_get_current_installation_priming_step
      ),
      summary=await driver.call(client.controller_get_device_summary, "Cytometer"),
    )

  async def power_on(self, timeout: float = POWER_ON_TIMEOUT) -> None:
    """Switch the detector on and wait until the controller has found it. Nothing is sent
    when it is on already. It then warms up; :meth:`wait_until_ready` waits for that.

    Args:
      timeout: Seconds to wait for the controller to find the detector.

    Raises:
      WorklistActiveError: A worklist runs.
      IQueTimeoutError: The controller did not find the detector in time.
    """
    driver = self._driver
    await driver.require_idle("switch the detector on")
    client = driver.client
    if await driver.call(client.controller_cytometer_has_power):
      logger.info("the detector is on already")
      return
    if not await driver.call(client.cytometer_is_powering_on):
      await driver.call(client.controller_power_on_cytometer)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not await driver.call(client.controller_cytometer_has_power):
      if loop.time() >= deadline:
        raise IQueTimeoutError(f"the detector did not come on within {timeout} s")
      await asyncio.sleep(self.poll_interval)
    await driver.request_status()
    logger.info("detector on")

  async def power_off(self) -> None:
    """Shut the detector down with its own shutdown routine. Nothing is sent when it is off.

    Raises:
      WorklistActiveError: A worklist runs.
      ControllerFault: The detector refused, for example because it reports a leak.
    """
    driver = self._driver
    await driver.require_idle("shut the detector down")
    client = driver.client
    if not await driver.call(client.controller_cytometer_has_power):
      logger.info("the detector is off already")
      return
    await driver.call(client.cytometer_shutdown)
    logger.info("detector shut down")

  async def wait_until_ready(self, timeout: Optional[float] = None) -> None:
    """Wait until the detector is ready and can start a measurement.

    Args:
      timeout: Seconds to wait; by default the detector's remaining operation time plus a
        minute, or 25 minutes when it gives none.

    Raises:
      DeviceFaultedError: The detector faulted.
      DeviceNotReadyError: The detector is switched off.
      IQueTimeoutError: It was not ready in time.
    """
    driver = self._driver
    driver.require_setup()
    client = driver.client
    if timeout is None:
      remaining = await driver.call(client.cytometer_get_operation_duration)
      seconds = remaining.total_seconds()
      timeout = seconds + READY_TIMEOUT_MARGIN if seconds > 0 else READY_TIMEOUT_WITHOUT_ESTIMATE
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
      await driver.request_status()
      info = driver.devices.get("Cytometer")
      if info is not None and info.state == "Faulted":
        raise DeviceFaultedError("wait for the detector", {"Cytometer": info})
      if info is None or info.state == "Disconnected":
        raise DeviceNotReadyError("wait for the detector", "Cytometer", info)
      if info.state == "Ready" and await driver.call(client.cytometer_is_ready_to_run):
        return
      if loop.time() >= deadline:
        raise IQueTimeoutError(f"the detector was not ready within {timeout} s")
      await asyncio.sleep(self.poll_interval)

  async def reset_consumables_odometer(self, timeout: float = LONG_RUNNING_TIMEOUT) -> None:
    """Clear the detector's record of fluidics consumable run time, as after replacing the
    sheath filter, the FluidLink and the bottle filters.

    Args:
      timeout: Seconds to wait for the detector's result.

    Raises:
      WorklistActiveError: A worklist runs.
      DeviceNotReadyError: The detector is not ready.
      DeviceOperationError: The detector reported an error.
    """
    driver = self._driver
    await driver.require_direct_operation("reset the consumables odometer", "Cytometer")
    await driver.run_long_running_operation(
      "Cytometer_ResetConsumablesOdometer",
      driver.client.cytometer_reset_consumables_odometer,
      timeout,
    )
    logger.info("consumables odometer reset")

  async def calibrate_fluid_stations(self, timeout: float = LONG_RUNNING_TIMEOUT) -> None:
    """Zero the weight sensors of the bottle station. Every bottle must be off the station.

    Args:
      timeout: Seconds to wait for the detector's result.

    Raises:
      WorklistActiveError: A worklist runs.
      DeviceNotReadyError: The detector is not ready.
      DeviceOperationError: The detector reported an error.
    """
    driver = self._driver
    await driver.require_direct_operation("calibrate the bottle station", "Cytometer")
    await driver.run_long_running_operation(
      "Cytometer_CalibrateFluidStations", driver.client.cytometer_calibrate_fluid_stations, timeout
    )
    logger.info("bottle station calibrated")

  async def purge_step(self) -> Tuple[IQue3PurgeStep, bool]:
    """Read the current step of the decommissioning purge and whether it is completed."""
    driver = self._driver
    driver.require_setup()
    return await driver.call(driver.client.ique3_get_current_purge_step)

  async def run_next_purge_step(self, *, confirm_decommission: bool = False) -> None:
    """Start the next step of the decommissioning purge: flush, purge the system, purge the
    pressure sensor, then shut down. Each step needs the operator's preparation (empty and
    refill beakers, remove the probe and FluidLink, fit the inlet plug; see the instrument's
    manual) and starts only when the previous one is completed.

    Args:
      confirm_decommission: Must be True to start the first step: the purge drains the
        fluidics, and the instrument is unusable until installation priming is done.

    Raises:
      ValueError: The first step without `confirm_decommission`.
      WorklistActiveError: A worklist runs.
      IQueError: The current step has not completed.
    """
    driver = self._driver
    await driver.require_idle("run the next purge step")
    step, completed = await self.purge_step()
    if step == "None" and not confirm_decommission:
      raise ValueError(
        "the purge drains the detector's fluidics and leaves the instrument unusable until "
        "installation priming; pass confirm_decommission=True to start it"
      )
    if step != "None" and not completed:
      raise IQueError(f"the purge step {step} has not completed")
    await driver.call(driver.client.ique3_run_next_purge_step)
    logger.warning("purge step after %s started", step)

  async def installation_priming_step(self) -> Tuple[IQue3InstallationPrimeStep, bool]:
    """Read the current step of installation priming and whether it is completed."""
    driver = self._driver
    driver.require_setup()
    return await driver.call(driver.client.ique3_get_current_installation_priming_step)

  async def run_next_installation_priming_step(self) -> None:
    """Start the next step of installation priming: start, prime the system, prime the
    pressure sensor, prime, end. Each step needs the operator's preparation (see the
    instrument's manual) and starts only when the previous one is completed.

    Raises:
      WorklistActiveError: A worklist runs.
      IQueError: The current step has not completed.
    """
    driver = self._driver
    await driver.require_idle("run the next installation priming step")
    step, completed = await self.installation_priming_step()
    if step != "None" and not completed:
      raise IQueError(f"the installation priming step {step} has not completed")
    await driver.call(driver.client.ique3_run_next_installation_priming_step)
    logger.info("installation priming step after %s started", step)
