"""Tests of the plate rail feature on the simulated controller."""

import asyncio
import unittest

from pylabrobot.sartorius.ique.driver.errors import (
  DeviceFaultedError,
  DeviceNotReadyError,
  UnsafeMotionError,
  WorklistActiveError,
  WorklistCancelledError,
)
from pylabrobot.sartorius.ique.driver.simulator_tests import (
  plate_worklist,
  simulated_driver,
  until_running,
)


class PlateRailTests(unittest.IsolatedAsyncioTestCase):
  """Loading and ejecting, and the checks around them."""

  async def asyncSetUp(self) -> None:
    """Set up a driver on the simulator."""
    self.driver, self.controller = await simulated_driver()
    self.rail = self.driver.plate_rail

  async def asyncTearDown(self) -> None:
    """Stop the simulator's tasks."""
    await self.controller.shutdown()

  def names(self) -> list:
    """The operations the controller received."""
    return [name for name, _ in self.controller.calls]

  async def test_load_and_eject(self) -> None:
    """The rail is ejected after setup, loads with the plate lock closed first, and ejects."""
    self.assertEqual(self.rail.position, "ejected")
    self.assertTrue(self.rail.is_ejected)
    await self.driver.shaker.unlock_plate()
    await self.rail.load()
    self.assertEqual((self.rail.position, self.controller.rail_position), ("loaded", "loaded"))
    self.assertTrue(self.controller.plate_lock_closed)
    names = self.names()
    self.assertLess(
      len(names) - 1 - names[::-1].index("shaker_close_plate_lock"), names.index("plate_rail_load")
    )
    await self.rail.eject()
    self.assertEqual((self.rail.position, self.controller.rail_position), ("ejected", "ejected"))

  async def test_probe_must_be_parked(self) -> None:
    """The rail does not move unless the probe is parked."""
    await self.driver.sampler.move_z(240.0)
    with self.assertRaises(UnsafeMotionError):
      await self.rail.load()
    await self.driver.sampler.move_z(250.0)
    await self.driver.sampler.move_xy(100.0, 150.0)
    with self.assertRaises(UnsafeMotionError):
      await self.rail.eject()
    self.assertNotIn("plate_rail_load", self.names())
    self.assertNotIn("plate_rail_eject", self.names())
    await self.driver.sampler.home()
    await self.rail.load()

  async def test_contact_refuses(self) -> None:
    """The rail does not move while the probe reports contact."""
    self.controller.set_contact(True)
    with self.assertRaises(UnsafeMotionError):
      await self.rail.load()
    self.assertNotIn("plate_rail_load", self.names())

  async def test_faulted_device_refuses_a_load(self) -> None:
    """The rail does not move in while a mechanical device is faulted; it still moves out, as a
    faulted pump or shaker must not keep the plate inside."""
    await self.rail.load()
    self.controller.fault_device("Pump", "Start Faulted")
    self.controller.fault_device("Shaker", "Plate lock closed itself")
    await self.rail.eject()
    self.assertEqual(self.controller.rail_position, "ejected")
    with self.assertRaises(DeviceFaultedError):
      await self.rail.load()

  async def test_faulted_sampler_refuses_an_eject(self) -> None:
    """With the sampler faulted the probe's position is not known, so the rail stays."""
    await self.rail.load()
    self.controller.fault_device("Sampler", "Move Z Faulted")
    with self.assertRaises(DeviceNotReadyError):
      await self.rail.eject()
    self.assertNotIn("plate_rail_eject", self.names())

  async def test_not_while_shaking(self) -> None:
    """The rail does not move while the shaker shakes."""
    await self.rail.load()
    await self.driver.shaker.start(1000)
    with self.assertRaises(UnsafeMotionError) as caught:
      await self.rail.eject()
    self.assertIn("shaker", str(caught.exception))
    self.assertNotIn("plate_rail_eject", self.names())
    await self.driver.shaker.stop()
    await self.rail.eject()

  async def test_rail_fault_forgets_the_position(self) -> None:
    """A faulted rail has no known position and does not move."""
    self.controller.fault_device("PlateRail", "Load Faulted")
    self.assertIsNone(self.rail.position)
    with self.assertRaises(DeviceFaultedError):
      await self.rail.eject()

  async def test_refused_during_a_worklist(self) -> None:
    """The rail does not move while a worklist runs."""
    task = asyncio.ensure_future(self.driver.run_worklist(plate_worklist(flush_seconds=600)))
    await until_running(self.controller)
    with self.assertRaises(WorklistActiveError):
      await self.rail.eject()
    await self.driver.cancel_worklist()
    with self.assertRaises(WorklistCancelledError):
      await task
    self.assertEqual(self.rail.position, "ejected")


if __name__ == "__main__":
  unittest.main()
