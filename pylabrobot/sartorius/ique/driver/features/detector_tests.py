"""Tests of the detector feature on the simulated controller and on the scripted fake."""

import asyncio
import unittest
from typing import List

from pylabrobot.sartorius.ique.driver.client import ControllerFault
from pylabrobot.sartorius.ique.driver.errors import (
  DeviceFaultedError,
  DeviceNotReadyError,
  DeviceOperationError,
  IQueError,
  IQueTimeoutError,
  WorklistActiveError,
  WorklistCancelledError,
)
from pylabrobot.sartorius.ique.driver.master_tests import set_up
from pylabrobot.sartorius.ique.driver.simulator_tests import (
  plate_worklist,
  simulated_driver,
  until_running,
)


class DetectorTests(unittest.IsolatedAsyncioTestCase):
  """Power, readiness, status and maintenance steps of the detector."""

  async def asyncSetUp(self) -> None:
    """Set up a driver on the simulator."""
    self.driver, self.controller = await simulated_driver()
    self.detector = self.driver.detector

  async def asyncTearDown(self) -> None:
    """Stop the simulator's tasks."""
    await self.controller.shutdown()

  def names(self) -> List[str]:
    """The operations the controller received."""
    return [name for name, _ in self.controller.calls]

  async def test_status(self) -> None:
    """The status gathers state, power, readiness, steps and the controller's summary."""
    status = await self.detector.request_status()
    self.assertEqual(status.state, "Ready")
    self.assertTrue(status.has_power and status.ready_to_run and status.ready_to_clean)
    self.assertFalse(status.powering_on)
    self.assertEqual(status.purge_step, ("None", True))
    self.assertEqual(status.installation_priming_step, ("None", True))
    self.assertIn("laser", status.summary or "")

  async def test_cleans_run_while_measurements_are_blocked(self) -> None:
    """A status that blocks measurements but not maintenance lets the detector's own routines
    run, such as the unclog meant to clear it; a read needs it ready to run."""
    self.controller.speed = 20000.0
    self.controller.block_runs("Sample line pressure high")
    run = await self.detector.unclog()
    self.assertTrue(run.worklist.all_commands_completed)
    self.assertIn("cytometer_is_ready_to_clean", self.names())
    with self.assertRaises(DeviceNotReadyError):
      await self.driver.run_worklist(plate_worklist(columns=1))

  async def test_power_off_and_on(self) -> None:
    """Power off shuts the detector down; power on switches it on and waits for it; both are
    idempotent."""
    await self.detector.power_off()
    self.assertFalse(await self.detector.has_power())
    self.assertEqual(self.driver.device_state("Cytometer"), "Disconnected")
    await self.detector.power_off()
    self.assertEqual(self.names().count("cytometer_shutdown"), 1)
    await self.detector.power_on()
    self.assertTrue(await self.detector.has_power())
    self.assertEqual(self.driver.device_state("Cytometer"), "Ready")
    await self.detector.power_on()
    self.assertEqual(self.names().count("controller_power_on_cytometer"), 1)

  async def test_power_on_timeout(self) -> None:
    """A detector the controller does not find in time is reported."""
    await self.detector.power_off()
    self.controller.speed = 0.01
    with self.assertRaises(IQueTimeoutError):
      await self.detector.power_on(timeout=0.05)

  async def test_shutdown_refused_on_a_leak(self) -> None:
    """The detector refuses to shut down while it reports a leak."""
    self.controller.set_leak(True)
    with self.assertRaises(ControllerFault):
      await self.detector.power_off()

  async def test_wait_until_ready(self) -> None:
    """Waiting returns for a ready detector and fails for one that is off or faulted."""
    await self.detector.wait_until_ready()
    self.controller.fault_device("Cytometer", "Start Fluidics Faulted")
    with self.assertRaises(DeviceFaultedError):
      await self.detector.wait_until_ready(timeout=1.0)
    await self.driver.call(self.controller.controller_reset_devices, None)
    await self.detector.power_off()
    with self.assertRaises(DeviceNotReadyError):
      await self.detector.wait_until_ready(timeout=1.0)

  async def test_odometer_and_bottle_station(self) -> None:
    """The odometer reset and the bottle-station calibration wait for their result."""
    await self.detector.reset_consumables_odometer()
    await self.detector.calibrate_fluid_stations()
    self.assertIn("cytometer_reset_consumables_odometer", self.names())
    self.assertIn("cytometer_calibrate_fluid_stations", self.names())
    self.assertEqual(self.driver.status.last_long_running_result, "")

  async def test_maintenance_needs_the_detector(self) -> None:
    """Maintenance is refused while the detector is off."""
    await self.detector.power_off()
    with self.assertRaises(DeviceNotReadyError):
      await self.detector.reset_consumables_odometer()

  async def test_purge_steps(self) -> None:
    """The purge starts only with consent, and each step only after the previous one."""
    with self.assertRaises(ValueError):
      await self.detector.run_next_purge_step()
    self.assertNotIn("ique3_run_next_purge_step", self.names())
    await self.detector.run_next_purge_step(confirm_decommission=True)
    self.assertEqual(await self.detector.purge_step(), ("FlushSystem", False))
    self.assertEqual(self.driver.status.alerts["Cytometer"], "CYT_PurgingFluidics")
    with self.assertRaises(IQueError):
      await self.detector.run_next_purge_step()
    while not (await self.detector.purge_step())[1]:
      await asyncio.sleep(0.01)
    await self.detector.run_next_purge_step()
    self.assertEqual(await self.detector.purge_step(), ("PurgeSystem", False))

  async def test_installation_priming_steps(self) -> None:
    """Installation priming advances one completed step at a time."""
    await self.detector.run_next_installation_priming_step()
    self.assertEqual(await self.detector.installation_priming_step(), ("Start", False))
    with self.assertRaises(IQueError):
      await self.detector.run_next_installation_priming_step()

  async def test_refused_during_a_worklist(self) -> None:
    """Power and maintenance are refused while a worklist runs."""
    task = asyncio.ensure_future(self.driver.run_worklist(plate_worklist(flush_seconds=600)))
    await until_running(self.controller)
    with self.assertRaises(WorklistActiveError):
      await self.detector.power_off()
    with self.assertRaises(WorklistActiveError):
      await self.detector.reset_consumables_odometer()
    await self.driver.cancel_worklist()
    with self.assertRaises(WorklistCancelledError):
      await task


class LongRunningResultTests(unittest.IsolatedAsyncioTestCase):
  """The result of a long-running operation, which may arrive before the call's reply."""

  async def test_error_result(self) -> None:
    """An error text in the result callback is raised."""
    driver, fake = await set_up()

    def fail() -> None:
      """Send the result callback before the reply, as the controller may."""
      fake.fire("OnCytometerLongRunningOperationCompleted", "Odometer reset failed")

    fake.replies["cytometer_reset_consumables_odometer"] = fail
    with self.assertRaises(DeviceOperationError) as caught:
      await driver.detector.reset_consumables_odometer()
    self.assertEqual(caught.exception.message, "Odometer reset failed")

  async def test_no_result(self) -> None:
    """An operation whose result never comes times out."""
    driver, _ = await set_up()
    with self.assertRaises(IQueTimeoutError):
      await driver.detector.calibrate_fluid_stations(timeout=0.02)


if __name__ == "__main__":
  unittest.main()
