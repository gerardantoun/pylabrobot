"""Tests of the pump feature on the simulated controller."""

import unittest
from typing import List

from pylabrobot.sartorius.ique.driver.errors import CalibrationMissingError, DeviceFaultedError
from pylabrobot.sartorius.ique.driver.features.pump import wire_speed
from pylabrobot.sartorius.ique.driver.master import IQueDriver
from pylabrobot.sartorius.ique.driver.simulator import SimulatedController
from pylabrobot.sartorius.ique.driver.simulator_tests import FAST, simulated_driver


class PumpTests(unittest.IsolatedAsyncioTestCase):
  """Running and stopping the pump."""

  async def asyncSetUp(self) -> None:
    """Set up a driver on the simulator."""
    self.driver, self.controller = await simulated_driver()
    self.pump = self.driver.pump

  async def asyncTearDown(self) -> None:
    """Stop the simulator's tasks."""
    await self.controller.shutdown()

  def names(self) -> List[str]:
    """The operations the controller received."""
    return [name for name, _ in self.controller.calls]

  def test_wire_speed(self) -> None:
    """Speeds travel as hundredths of rpm."""
    self.assertEqual(wire_speed(29.0), 2900)
    self.assertEqual(wire_speed(28.6), 2860)
    self.assertEqual(wire_speed(1.605), 160)

  async def test_limits(self) -> None:
    """The pump's range and default come from the controller, in rpm; the highest speed is
    the instrument's own priming speed, 53 rpm, below the pump's 160 rpm."""
    self.assertEqual(self.pump.speed_range, (1.6, 53.0))
    self.assertEqual(self.pump.default_speed, 28.6)

  async def test_start(self) -> None:
    """Start sends hundredths of rpm and the instrument's direction (clockwise on the right)."""
    await self.pump.start(29.0)
    self.assertIn(("pump_start", (2900, "CW")), self.controller.calls)
    self.assertEqual(self.driver.device_state("Pump"), "Running")

  async def test_default_speed_and_direction(self) -> None:
    """Without arguments the pump runs at its default speed in the instrument's direction."""
    await self.pump.start()
    self.assertIn(("pump_start", (2860, "CW")), self.controller.calls)

  async def test_counterclockwise(self) -> None:
    """The direction can be given."""
    await self.pump.start(10.0, "counterclockwise")
    self.assertIn(("pump_start", (1000, "CCW")), self.controller.calls)

  async def test_change_while_running(self) -> None:
    """A running pump is updated, not started again."""
    await self.pump.start(10.0)
    await self.pump.start(20.0)
    self.assertIn(("pump_update", (2000, "CW")), self.controller.calls)
    self.assertEqual(self.names().count("pump_start"), 1)

  async def test_out_of_range(self) -> None:
    """A speed outside the pump's range is refused, not clamped."""
    for speed in (1.0, 53.5, 200.0):
      with self.assertRaises(ValueError):
        await self.pump.start(speed)
    self.assertNotIn("pump_start", self.names())

  async def test_stop(self) -> None:
    """Stop sends nothing to a still pump, and stops a running one."""
    await self.pump.stop()
    self.assertNotIn("pump_stop", self.names())
    await self.pump.start(10.0)
    await self.pump.stop()
    self.assertIn("pump_stop", self.names())
    self.assertEqual(self.driver.device_state("Pump"), "Ready")

  async def test_faults(self) -> None:
    """Starting is refused while another mechanical device is faulted; stopping is not."""
    await self.pump.start(10.0)
    self.controller.fault_device("Shaker", "Home Faulted")
    with self.assertRaises(DeviceFaultedError):
      await self.pump.start(20.0)
    await self.pump.stop()

  async def test_direction_needs_calibration(self) -> None:
    """Without a calibration the direction must be given."""
    controller = SimulatedController(speed=FAST, calibration=None)
    driver = IQueDriver(client=controller, ready_poll_interval=0.001)
    with self.assertLogs("pylabrobot.sartorius.ique.driver.master"):
      await driver.setup(require_calibration=False)
    with self.assertRaises(CalibrationMissingError):
      await driver.pump.start(10.0)
    await driver.pump.start(10.0, "clockwise")
    await controller.shutdown()


if __name__ == "__main__":
  unittest.main()
