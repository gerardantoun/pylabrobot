"""Tests of the sampler feature on the simulated controller, and of its geometry helpers."""

import datetime
import decimal
import unittest
import uuid
from typing import List, Optional

from pylabrobot.sartorius.ique.driver.contract import ContactZResult
from pylabrobot.sartorius.ique.driver.errors import (
  CalibrationMissingError,
  DeviceFaultedError,
  DeviceOperationError,
  UnsafeMotionError,
)
from pylabrobot.sartorius.ique.driver.features.sampler import (
  PLATE_AREA_MARGIN,
  PlateArea,
  plate_area,
  segment_meets_area,
)
from pylabrobot.sartorius.ique.driver.master import IQueDriver
from pylabrobot.sartorius.ique.driver.simulator import (
  SIMULATED_DECK_HEIGHT,
  SimulatedController,
  simulated_calibration,
)
from pylabrobot.sartorius.ique.driver.simulator_tests import FAST, simulated_driver

AREA = PlateArea(10.0, 20.0, 10.0, 20.0)


class GeometryTests(unittest.TestCase):
  """The plate area and paths across it."""

  def test_plate_area(self) -> None:
    """The plate area spans every format's corner wells, widened by the margin."""
    area = plate_area(simulated_calibration())
    self.assertAlmostEqual(area.x_min, 16.7 - PLATE_AREA_MARGIN)
    self.assertAlmostEqual(area.x_max, 122.9 + PLATE_AREA_MARGIN)
    self.assertAlmostEqual(area.y_min, 42.7 - PLATE_AREA_MARGIN)
    self.assertAlmostEqual(area.y_max, 112.8 + PLATE_AREA_MARGIN)
    self.assertTrue(area.contains(50.0, 80.0))
    self.assertFalse(area.contains(131.0, 195.0))
    self.assertFalse(area.contains(3.0, 105.0))

  def test_paths(self) -> None:
    """A path touches the area when any point of it does."""
    self.assertTrue(segment_meets_area((0, 0), (30, 30), AREA))
    self.assertTrue(segment_meets_area((15, 0), (15, 30), AREA))
    self.assertTrue(segment_meets_area((15, 15), (15, 15), AREA))
    self.assertTrue(segment_meets_area((0, 10), (30, 10), AREA))
    self.assertFalse(segment_meets_area((0, 0), (30, 5), AREA))
    self.assertFalse(segment_meets_area((25, 0), (25, 30), AREA))
    self.assertFalse(segment_meets_area((0, 22), (22, 30), AREA))
    self.assertFalse(segment_meets_area((5, 5), (5, 5), AREA))


class SamplerTests(unittest.IsolatedAsyncioTestCase):
  """Moving the probe, and the safety rules around it."""

  async def asyncSetUp(self) -> None:
    """Set up a driver on the simulator."""
    self.driver, self.controller = await simulated_driver()
    self.sampler = self.driver.sampler

  async def asyncTearDown(self) -> None:
    """Stop the simulator's tasks."""
    await self.controller.shutdown()

  def names(self) -> List[str]:
    """The operations the controller received."""
    return [name for name, _ in self.controller.calls]

  async def test_position_and_travel_height(self) -> None:
    """The probe starts parked; the travel height is the calibration's."""
    self.assertEqual(tuple(await self.sampler.request_position()), (131.0, 195.0, 250.0))
    self.assertEqual(self.sampler.travel_height, 250.0)

  async def test_xy_needs_the_travel_height(self) -> None:
    """An XY move is refused with the probe below the travel height."""
    await self.sampler.move_z(240.0)
    with self.assertRaises(UnsafeMotionError):
      await self.sampler.move_xy(125.0, 150.0)
    self.assertNotIn("sampler_move_xy", self.names())
    await self.sampler.move_z(250.0)
    await self.sampler.move_xy(125.0, 150.0)
    self.assertEqual(self.controller.probe_position[:2], (125.0, 150.0))

  async def test_xy_over_the_plate_needs_the_rail_loaded(self) -> None:
    """A move to or across the plate area is refused unless the rail is loaded."""
    with self.assertRaises(UnsafeMotionError):
      await self.sampler.move_xy(50.0, 80.0)
    with self.assertRaises(UnsafeMotionError):
      await self.sampler.move_xy(5.0, 5.0)
    self.assertNotIn("sampler_move_xy", self.names())
    await self.sampler.move_xy(130.0, 5.0)
    await self.sampler.home()
    await self.driver.plate_rail.load()
    await self.sampler.move_xy(50.0, 80.0, fast=True)
    self.assertIn(
      ("sampler_move_xy", (decimal.Decimal("50.000"), decimal.Decimal("80.000"), True)),
      self.controller.calls,
    )

  async def test_lowering_over_the_plate_needs_the_rail_loaded(self) -> None:
    """Lowering over the plate area is refused when the rail is not known to be loaded."""
    await self.driver.plate_rail.load()
    await self.sampler.move_xy(50.0, 80.0)
    self.driver.rail_position = None
    with self.assertRaises(UnsafeMotionError):
      await self.sampler.move_z(220.0)
    self.assertNotIn("sampler_move_z", self.names())

  async def test_contact(self) -> None:
    """With contact, lowering, XY moves and parking are refused; raising is allowed."""
    await self.sampler.move_z(240.0)
    self.controller.set_contact(True)
    self.controller.calls.clear()
    with self.assertRaises(UnsafeMotionError):
      await self.sampler.move_z(230.0)
    with self.assertRaises(UnsafeMotionError):
      await self.sampler.home()
    with self.assertRaises(UnsafeMotionError):
      await self.sampler.find_z_contact(240.0, 200.0)
    self.assertFalse(
      {"sampler_move_z", "sampler_home", "sampler_find_z_contact"} & set(self.names())
    )
    await self.sampler.move_z(250.0)
    self.assertEqual(self.controller.probe_position[2], 250.0)

  async def test_faulted_device(self) -> None:
    """With another mechanical device faulted, the probe may rise and park, not go down."""
    await self.sampler.move_z(240.0)
    self.controller.fault_device("Pump", "Start Faulted")
    with self.assertRaises(DeviceFaultedError):
      await self.sampler.move_z(230.0)
    await self.sampler.move_z(250.0)
    await self.sampler.home()

  async def test_find_contact_on_the_deck(self) -> None:
    """A contact search with the rail loaded finds the deck and returns to its start."""
    await self.driver.plate_rail.load()
    await self.sampler.move_xy(3.0, 105.0)
    contact = await self.sampler.find_z_contact(250.0, 195.0)
    self.assertEqual(contact, SIMULATED_DECK_HEIGHT)
    self.assertEqual(self.controller.probe_position[2], 250.0)

  async def test_find_contact_without_a_surface(self) -> None:
    """A contact search that touches nothing returns None."""
    self.assertIsNone(await self.sampler.find_z_contact(250.0, 195.0))

  async def test_find_contact_arguments(self) -> None:
    """Heights out of the travel or out of order are refused."""
    for high, low in ((250.0, 260.0), (200.0, 210.0), (250.0, 180.0)):
      with self.assertRaises(ValueError):
        await self.sampler.find_z_contact(high, low)

  async def test_find_contact_error(self) -> None:
    """An error the controller reports for a search is raised."""

    async def failing(
      z_high: decimal.Decimal,
      z_low: decimal.Decimal,
      retract_after_find: bool,
      *,
      timeout: Optional[float] = None,
    ) -> Optional[ContactZResult]:
      """Answer as a sampler that is not ready."""
      return ContactZResult(
        contact_time=datetime.datetime(2026, 1, 1),
        contact_z_location=decimal.Decimal(0),
        error_message="Device not ready for contact z.",
        found_contact=False,
        has_error=True,
        id=uuid.UUID(int=1),
        is_simulated=False,
        z_low=z_low,
      )

    self.controller.sampler_find_z_contact = failing  # type: ignore[method-assign]
    with self.assertRaises(DeviceOperationError) as caught:
      await self.sampler.find_z_contact(250.0, 195.0)
    self.assertIn("not ready", str(caught.exception))

  async def test_out_of_travel(self) -> None:
    """Targets outside the travel are refused."""
    with self.assertRaises(ValueError):
      await self.sampler.move_xy(140.0, 10.0)
    with self.assertRaises(ValueError):
      await self.sampler.move_z(185.0)

  async def test_without_calibration(self) -> None:
    """Without a calibration XY moves are refused; parking and raising still work."""
    controller = SimulatedController(speed=FAST, calibration=None)
    driver = IQueDriver(client=controller, ready_poll_interval=0.001)
    with self.assertLogs("pylabrobot.sartorius.ique.driver.master"):
      await driver.setup(require_calibration=False)
    with self.assertRaises(CalibrationMissingError):
      await driver.sampler.move_xy(125.0, 150.0)
    await driver.sampler.move_z(250.0)
    await driver.sampler.home()
    await controller.shutdown()


if __name__ == "__main__":
  unittest.main()
