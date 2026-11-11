"""Tests of the iQue resource: the factory, and the plate holder that follows the plate rail."""

import unittest

from pylabrobot.resources import cor_96_wellplate_360uL_Fb, Resource
from pylabrobot.sartorius.ique.device import IQue, IQueDevice
from pylabrobot.sartorius.ique.driver.client import ControllerClient
from pylabrobot.sartorius.ique.driver.errors import PlateAccessError
from pylabrobot.sartorius.ique.driver.simulator import SimulatedController


class FactoryTests(unittest.TestCase):
  """Building the resource."""

  def test_simulation(self) -> None:
    """A simulated instrument runs on the in-process controller."""
    ique = IQue(simulation=True, simulation_speed=100.0)
    self.assertIsInstance(ique, IQueDevice)
    self.assertIsInstance(ique.driver.client, SimulatedController)
    self.assertIs(ique.plate_holder.parent, ique)
    self.assertIs(ique.sampler, ique.driver.sampler)
    self.assertIs(ique.plate_rail, ique.driver.plate_rail)

  def test_instrument(self) -> None:
    """An instrument needs its host; building connects to nothing."""
    ique = IQue(host="192.0.2.1")
    self.assertIsInstance(ique.driver.client, ControllerClient)
    with self.assertRaises(ValueError):
      IQue()
    with self.assertRaises(ValueError):
      IQue(host="192.0.2.1", simulation=True)

  def test_plates_go_on_the_holder(self) -> None:
    """The instrument itself takes no plate."""
    ique = IQue(simulation=True)
    with self.assertRaises(PlateAccessError):
      ique.assign_child_resource(Resource("thing", 1, 1, 1), location=None)


class PlateHolderTests(unittest.IsolatedAsyncioTestCase):
  """The plate holder follows the plate rail."""

  async def test_holder_follows_the_rail(self) -> None:
    """A plate goes on and comes off only while the rail is known to be out."""
    ique = IQue(simulation=True, simulation_speed=1000.0)
    ique.driver.ready_poll_interval = 0.001
    plate = cor_96_wellplate_360uL_Fb(name="plate")
    with self.assertRaises(PlateAccessError):
      ique.plate_holder.assign_child_resource(plate)
    await ique.setup()
    self.assertEqual(ique.plate_rail.position, "ejected")
    ique.plate_holder.assign_child_resource(plate)
    self.assertIs(ique.plate_holder.resource, plate)
    await ique.plate_rail.load()
    with self.assertRaises(PlateAccessError):
      ique.plate_holder.unassign_child_resource(plate)
    await ique.plate_rail.eject()
    ique.plate_holder.unassign_child_resource(plate)
    self.assertIsNone(ique.plate_holder.resource)
    await ique.stop()
    self.assertFalse(ique.driver.is_set_up)
    controller = ique.driver.client
    assert isinstance(controller, SimulatedController)
    await controller.shutdown()


if __name__ == "__main__":
  unittest.main()
