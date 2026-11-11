"""Tests of the shaker feature on the simulated controller."""

import logging
import unittest
from typing import List

from pylabrobot.sartorius.ique.driver.errors import UnsafeMotionError
from pylabrobot.sartorius.ique.driver.simulator_tests import simulated_driver


class ShakerTests(unittest.IsolatedAsyncioTestCase):
  """Shaking and the plate lock."""

  async def asyncSetUp(self) -> None:
    """Set up a driver on the simulator."""
    self.driver, self.controller = await simulated_driver()
    self.shaker = self.driver.shaker

  async def asyncTearDown(self) -> None:
    """Stop the simulator's tasks."""
    await self.controller.shutdown()

  def names(self) -> List[str]:
    """The operations the controller received."""
    return [name for name, _ in self.controller.calls]

  async def test_speed_range(self) -> None:
    """The range is the shaker's own."""
    self.assertEqual(await self.shaker.speed_range(), (100, 3000))

  async def test_start_closes_the_lock_first(self) -> None:
    """Shaking closes the plate lock first, then starts."""
    await self.shaker.unlock_plate()
    self.controller.calls.clear()
    await self.shaker.start(1000)
    self.assertEqual(
      [name for name in self.names() if name.startswith("shaker_") and "rpm" not in name],
      ["shaker_close_plate_lock", "shaker_start"],
    )
    self.assertTrue(self.controller.plate_lock_closed)
    self.assertTrue(self.shaker.lock_closed)
    self.assertEqual(self.driver.device_state("Shaker"), "Running")

  async def test_change_speed_while_shaking(self) -> None:
    """A second start changes the speed without touching the lock."""
    await self.shaker.start(1000)
    self.controller.calls.clear()
    await self.shaker.start(2000)
    self.assertIn(("shaker_start", (2000,)), self.controller.calls)
    self.assertNotIn("shaker_close_plate_lock", self.names())

  async def test_out_of_range(self) -> None:
    """A speed outside the shaker's range is refused, not clamped."""
    for speed in (50, 4000):
      with self.assertRaises(ValueError):
        await self.shaker.start(speed)
    self.assertNotIn("shaker_start", self.names())

  async def test_probe_must_be_clear_of_the_plate(self) -> None:
    """Shaking is refused while the probe is low over the plate."""
    await self.driver.plate_rail.load()
    await self.driver.sampler.move_xy(50.0, 80.0)
    await self.driver.sampler.move_z(220.0)
    with self.assertRaises(UnsafeMotionError):
      await self.shaker.start(1000)
    self.assertNotIn("shaker_start", self.names())
    await self.driver.sampler.move_z(250.0)
    await self.shaker.start(1000)

  async def test_plate_does_not_move_under_a_low_probe(self) -> None:
    """Homing and the plate lock, which move the plate, wait for the probe to be raised while
    the rail is in; with the rail out they need no probe check."""
    await self.driver.plate_rail.load()
    await self.driver.sampler.move_xy(50.0, 80.0)
    await self.driver.sampler.move_z(220.0)
    self.controller.calls.clear()
    for operation in (self.shaker.home, self.shaker.lock_plate, self.shaker.unlock_plate):
      with self.assertRaises(UnsafeMotionError):
        await operation()
    self.assertFalse(
      {"shaker_home", "shaker_close_plate_lock", "shaker_open_plate_lock"} & set(self.names())
    )
    await self.driver.sampler.move_z(250.0)
    await self.shaker.home()
    await self.shaker.unlock_plate()
    await self.shaker.lock_plate()

  async def test_stop(self) -> None:
    """Stop sends nothing to a still shaker, and stops a shaking one."""
    await self.shaker.stop()
    self.assertNotIn("shaker_stop", self.names())
    await self.shaker.start(1000)
    await self.shaker.stop()
    self.assertIn("shaker_stop", self.names())
    self.assertEqual(self.driver.device_state("Shaker"), "Ready")

  async def test_stop_with_another_device_faulted(self) -> None:
    """Stopping is allowed while another mechanical device is faulted."""
    await self.shaker.start(1000)
    self.controller.fault_device("Pump", "Start Faulted")
    await self.shaker.stop()
    self.assertEqual(self.driver.device_state("Shaker"), "Ready")

  async def test_lock_and_home(self) -> None:
    """The lock opens with a warning and closes; homing closes it."""
    with self.assertLogs("pylabrobot.sartorius.ique.driver.features.shaker", logging.WARNING):
      await self.shaker.unlock_plate()
    self.assertFalse(self.controller.plate_lock_closed)
    self.assertFalse(self.shaker.lock_closed)
    await self.shaker.home()
    self.assertTrue(self.controller.plate_lock_closed)
    await self.shaker.unlock_plate()
    await self.shaker.lock_plate()
    self.assertTrue(self.controller.plate_lock_closed)


if __name__ == "__main__":
  unittest.main()
