"""Tests of the iQue resource: the factory, the plate holder that follows the plate rail, and
the operations end to end on the simulated controller."""

import asyncio
import dataclasses
import pathlib
import tempfile
import unittest
from typing import List, Union

from pylabrobot.resources import (
  Resource,
  Well,
  biorad_384_wellplate_50uL_Vb,
  cor_96_wellplate_360uL_Fb,
)
from pylabrobot.sartorius import IQue as SartoriusIQue
from pylabrobot.sartorius.ique import PlateRead
from pylabrobot.sartorius.ique.device import IQue, IQueDevice
from pylabrobot.sartorius.ique.driver import contract as wire
from pylabrobot.sartorius.ique.driver.calibration import (
  PlateModel,
  read_calibration,
  write_calibration,
)
from pylabrobot.sartorius.ique.driver.client import ControllerClient
from pylabrobot.sartorius.ique.driver.errors import (
  CalibrationMissingError,
  CalibrationRefusedError,
  PlateAccessError,
  WorklistCancelledError,
  WorklistFailedError,
)
from pylabrobot.sartorius.ique.driver.fcs import read_fcs
from pylabrobot.sartorius.ique.driver.master import IQueDriver, RunProgress
from pylabrobot.sartorius.ique.driver.simulator import SimulatedController, simulated_calibration
from pylabrobot.sartorius.ique.driver.worklist import TubeReadSettings, well_order

FAST = 1000.0  # simulated seconds per real second during reads
FASTER = 20000.0  # simulated seconds per real second during maintenance


def prepare(ique: IQueDevice) -> SimulatedController:
  """Shorten the polling of a simulated instrument's driver and return its controller."""
  ique.driver.ready_poll_interval = 0.001
  ique.driver.status_poll_interval = 0.05
  ique.detector.poll_interval = 0.001
  controller = ique.driver.client
  assert isinstance(controller, SimulatedController)
  return controller


def sampled_commands(read: PlateRead) -> List[wire.SampleWellCommand]:
  """Return the well-sampling commands of a read's worklist, in order."""
  return [
    command for command in read.worklist.commands or [] if type(command) is wire.SampleWellCommand
  ]


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
    self.assertIs(SartoriusIQue, IQue)

  def test_instrument(self) -> None:
    """An instrument needs its host; building connects to nothing."""
    ique = IQue(host="192.0.2.1")
    self.assertIsInstance(ique.driver.client, ControllerClient)
    with self.assertRaises(ValueError):
      IQue()
    with self.assertRaises(ValueError):
      IQue(host="192.0.2.1", simulation=True)

  def test_size(self) -> None:
    """The resource has the instrument's outer size: 130 cm wide, 63 cm deep, 66 cm high."""
    ique = IQue(simulation=True)
    self.assertEqual(
      (ique.get_size_x(), ique.get_size_y(), ique.get_size_z()), (1300.0, 630.0, 660.0)
    )

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


class ReadTests(unittest.IsolatedAsyncioTestCase):
  """Plate and tube reads on a simulated instrument, with a 96-well plate on the holder."""

  async def asyncSetUp(self) -> None:
    """Set up a simulated instrument and put a plate on its holder."""
    self.ique = IQue(simulation=True, simulation_speed=FAST, simulation_seed=1)
    self.controller = prepare(self.ique)
    await self.ique.setup()
    self.plate = cor_96_wellplate_360uL_Fb(name="plate")
    self.ique.plate_holder.assign_child_resource(self.plate)

  async def asyncTearDown(self) -> None:
    """Disconnect and stop the simulator's tasks."""
    if self.ique.driver.is_set_up:
      await self.ique.stop()
    await self.controller.shutdown()

  def names(self) -> List[str]:
    """The operations the controller received."""
    return [name for name, _ in self.controller.calls]

  async def test_whole_plate(self) -> None:
    """Every well of a 96-well plate is read and found, and its FCS files are readable."""
    read = await self.ique.read_plate(self.plate)
    self.assertEqual(read.plate_format, "Well96")
    self.assertEqual(read.plate_model, "Generic 96 Well Plate")
    self.assertEqual(read.plate_name, "plate")
    self.assertEqual(len(read.wells), 96)
    self.assertEqual(read.wells[:13], [f"A{c:02d}" for c in range(1, 13)] + ["B01"])
    self.assertEqual(read.verdict, "identified", read.message)
    counts = read.event_counts()
    self.assertTrue(all(100 < count < 2000 for count in counts.values()), counts)
    self.assertTrue(read.worklist.all_commands_completed)
    self.assertEqual(self.ique.plate_rail.position, "ejected")
    self.assertFalse(self.ique.flush_needed)
    with tempfile.TemporaryDirectory() as folder:
      paths = read.write_fcs(folder)
      self.assertEqual(len(paths), 97)
      plate_events, _ = read_fcs(paths[0].read_bytes())
      self.assertEqual(plate_events, read.events)
      well_events, keywords = read_fcs(paths[-1].read_bytes())
      self.assertEqual(well_events.event_count, counts["H12"])
      self.assertEqual(keywords["IC$WellID"], "H12")

  async def test_wells_in_serpentine_order(self) -> None:
    """A subset of wells, given as names and as wells, is sampled in serpentine order."""
    wells: List[Union[str, Well]] = ["B1", self.plate.get_well("A2"), "A1", "B02", "C1"]
    read = await self.ique.read_plate(self.plate, wells=wells, order="serpentine_row")
    expected = ["A01", "A02", "B02", "B01", "C01"]
    self.assertEqual(read.wells, expected)
    self.assertEqual(
      [f"{command.row}{command.column:02d}" for command in sampled_commands(read)], expected
    )
    self.assertEqual(read.verdict, "identified", read.message)
    as_given = await self.ique.read_plate(plate_format="Well96", wells=["C3", "A1"], order=None)
    self.assertEqual(as_given.wells, ["C03", "A01"])
    self.assertIsNone(as_given.plate_name)

  async def test_384_well_plate(self) -> None:
    """Some wells of a 384-well plate are read with the 384-well plate model."""
    self.ique.plate_holder.unassign_child_resource(self.plate)
    plate = biorad_384_wellplate_50uL_Vb(name="plate_384")
    self.ique.plate_holder.assign_child_resource(plate)
    read = await self.ique.read_plate(plate, wells=["P24", "A1", "A2", "B1"], order="column")
    self.assertEqual(read.plate_format, "Well384")
    self.assertEqual(read.plate_model, "Generic 384 Well Plate")
    self.assertEqual(read.wells, ["A01", "B01", "A02", "P24"])
    self.assertEqual(read.verdict, "identified", read.message)
    with self.assertRaises(ValueError):
      await self.ique.read_plate(plate, plate_format="Well96")

  async def test_stop_read_early(self) -> None:
    """A read stopped early flushes the line and returns the wells sampled until then."""
    sampled = asyncio.Event()
    count = 0

    def on_progress(progress: RunProgress) -> None:
      """Note when ten wells are sampled."""
      nonlocal count
      command = progress.command
      if progress.phase == "completed" and type(command) is wire.SampleWellCommand:
        count += 1
        if count == 10:
          sampled.set()

    task = asyncio.ensure_future(self.ique.read_plate(self.plate, on_progress=on_progress))
    await sampled.wait()
    await self.ique.stop_read_early()
    read = await task
    self.assertTrue(read.stopped_early)
    self.assertIn("worklist_skip_to", self.names())
    self.assertGreaterEqual(len(read.wells), 10)
    self.assertLess(len(read.wells), 96)
    self.assertEqual(read.wells, well_order("Well96", "row")[: len(read.wells)])
    self.assertEqual(read.verdict, "identified", read.message)
    skipped = [c for c in read.worklist.commands or [] if c and c.execution_notes == "Skipped"]
    self.assertTrue(skipped)
    flush = next(c for c in read.worklist.commands or [] if isinstance(c, wire.FlushCommand))
    self.assertTrue(flush.completed)
    self.assertFalse(self.ique.flush_needed)
    await self.ique.stop_read_early()

  async def test_failed_read(self) -> None:
    """A read stopped by a device fault raises with its partial run, and a prime flushes the
    sample it left in the line."""

    def fault(progress: RunProgress) -> None:
      """Fault the detector once the fifth well is sampled."""
      command = progress.command
      if progress.phase == "completed" and type(command) is wire.SampleWellCommand:
        if command.row == "A" and command.column == 5:
          self.controller.fault_device("Cytometer", "Laser Faulted")

    with self.assertRaises(WorklistFailedError) as caught:
      await self.ique.read_plate(self.plate, on_progress=fault)
    run = caught.exception.run
    self.assertEqual(run.stop_reason, "DeviceFaulted")
    assert run.events is not None
    self.assertGreater(run.events.event_count, 0)
    self.assertTrue(self.ique.flush_needed)
    await self.ique.driver.call(self.controller.controller_reset_devices, None)
    await self.ique.prime()
    self.assertFalse(self.ique.flush_needed)

  async def test_refusals(self) -> None:
    """A plate off the holder, a missing or contradicting format and an unknown plate model
    are refused before anything is sent."""
    other = cor_96_wellplate_360uL_Fb(name="other")
    with self.assertRaises(PlateAccessError):
      await self.ique.read_plate(other)
    with self.assertRaises(ValueError):
      await self.ique.read_plate()
    with self.assertRaises(ValueError):
      await self.ique.read_plate(plate_format="Well384")
    with self.assertRaises(ValueError):
      await self.ique.read_plate(self.plate, wells=[other.get_well("A1")])
    with self.assertRaises(CalibrationMissingError):
      await self.ique.read_plate(self.plate, plate_model="Unknown plate")
    with self.assertRaises(CalibrationMissingError):
      await self.ique.read_plate(self.plate, plate_model="Generic 384 Well Plate")
    self.assertNotIn("worklist_run", self.names())

  async def test_read_tube(self) -> None:
    """A tube read records the tube's events; without a tube the read fails."""
    read = await self.ique.read_tube(TubeReadSettings(sip_time=5.0))
    self.assertGreater(read.event_count, 100)
    self.assertEqual(read.verdict, "identified")
    self.controller.tube_in_holder = False
    with self.assertRaises(WorklistFailedError) as caught:
      await self.ique.read_tube()
    self.assertIn("No tube", caught.exception.error)

  async def test_stop_tube_read(self) -> None:
    """Stopping a tube read cancels it."""
    task = asyncio.ensure_future(self.ique.read_tube())
    while "worklist_run" not in self.names():
      await asyncio.sleep(0.001)
    await self.ique.stop_read_early()
    with self.assertRaises(WorklistCancelledError):
      await task


class PlateModelTests(unittest.IsolatedAsyncioTestCase):
  """The plate model a read uses when none is chosen."""

  async def test_ambiguous_plate_model(self) -> None:
    """With two plate models calibrated for the format, the read asks for one of them."""
    calibration = simulated_calibration()
    extra = PlateModel("Other 96 Well Plate", well_depth_offset=4.0)
    calibration = dataclasses.replace(calibration, plate_models=calibration.plate_models + (extra,))
    controller = SimulatedController(speed=FAST, calibration=write_calibration(calibration))
    ique = IQueDevice(IQueDriver(client=controller, ready_poll_interval=0.001))
    await ique.setup()
    plate = cor_96_wellplate_360uL_Fb(name="plate")
    ique.plate_holder.assign_child_resource(plate)
    with self.assertRaises(ValueError) as caught:
      await ique.read_plate(plate)
    self.assertIn("Generic 96 Well Plate", str(caught.exception))
    self.assertIn("Other 96 Well Plate", str(caught.exception))
    self.assertNotIn("worklist_run", [name for name, _ in controller.calls])
    await ique.stop()
    await controller.shutdown()


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
  """Every maintenance operation runs on the simulated instrument."""

  async def asyncSetUp(self) -> None:
    """Set up a fast simulated instrument."""
    self.ique = IQue(simulation=True, simulation_speed=FASTER)
    self.controller = prepare(self.ique)
    await self.ique.setup()

  async def asyncTearDown(self) -> None:
    """Disconnect and stop the simulator's tasks."""
    await self.ique.stop()
    await self.controller.shutdown()

  async def test_detector_cleans(self) -> None:
    """The detector's own routines each run their clean method and leave the fluidics on."""
    detector = self.ique.detector
    for clean, method in (
      (detector.quick_clean, "QuickClean"),
      (detector.long_clean, "LongClean"),
      (detector.unclog, "Unclog"),
      (detector.debubble, "Priming"),
    ):
      run = await clean()
      (command,) = run.commands
      assert isinstance(command, wire.CleanFlowCytometerCommand)
      self.assertEqual(command.clean_method, method)
      self.assertTrue(run.worklist.all_commands_completed)

  async def test_probe_maintenance(self) -> None:
    """Prime, probe and tubing clean, fluidics check and deck-height check complete."""
    runs = [
      await self.ique.prime(5.0),
      await self.ique.clean_probe_and_tubing(240.0),
      await self.ique.fluidics_check(),
      await self.ique.validate_deck_height(),
    ]
    self.assertEqual(
      [run.worklist.name for run in runs],
      ["Prime", "Probe and Tubing Clean", "Fluidics Check", "Validate Deck Height"],
    )
    self.assertTrue(all(run.worklist.all_commands_completed for run in runs))
    pump = next(c for c in runs[0].commands if isinstance(c, wire.StartPumpCommand))
    self.assertEqual(pump.speed, 2860)
    self.assertEqual(self.controller.rail_position, "ejected")

  async def test_shut_down_and_start_up(self) -> None:
    """Shut-down cleans and switches the detector off; start-up switches it on and cleans."""
    shut_down = await self.ique.shut_down(clean_duration=240.0)
    assert shut_down is not None
    self.assertTrue(shut_down.worklist.all_commands_completed)
    self.assertFalse(await self.ique.detector.has_power())
    self.assertIsNone(await self.ique.shut_down())
    run = await self.ique.start_up(clean_duration=240.0)
    self.assertEqual(run.worklist.name, "Start-up Clean")
    self.assertTrue(await self.ique.detector.has_power())


class DeckHeightCalibrationTests(unittest.IsolatedAsyncioTestCase):
  """Measuring the deck height again and storing it."""

  async def asyncSetUp(self) -> None:
    """Set up a simulated instrument that keeps calibration backups in a temporary folder."""
    self.folder = tempfile.TemporaryDirectory()
    self.backups = pathlib.Path(self.folder.name)
    self.ique = IQue(simulation=True, simulation_speed=FASTER, calibration_backup_dir=self.backups)
    self.controller = prepare(self.ique)
    await self.ique.setup()

  async def asyncTearDown(self) -> None:
    """Disconnect, stop the simulator's tasks and remove the folder."""
    await self.ique.stop()
    await self.controller.shutdown()
    self.folder.cleanup()

  async def test_refused_without_consent_or_backup(self) -> None:
    """Without confirm=True, or without a backup folder, nothing moves."""
    before = len(self.controller.calls)
    with self.assertRaises(ValueError):
      await self.ique.calibrate_deck_height()
    self.ique.driver.calibration_backup_dir = None
    with self.assertRaises(CalibrationRefusedError):
      await self.ique.calibrate_deck_height(confirm=True)
    self.assertEqual(len(self.controller.calls), before)

  async def test_new_deck_height(self) -> None:
    """A deck now 1 mm higher is measured and stored, after the old file is backed up."""
    old = self.controller.calibration_file
    self.controller.deck_height = 201.0
    height = await self.ique.calibrate_deck_height(confirm=True)
    self.assertEqual(height, 201.0)
    assert self.controller.calibration_file is not None
    stored = read_calibration(self.controller.calibration_file)
    self.assertEqual(stored.instrument.deck_height, 201.0)
    self.assertEqual(stored.instrument.deck_height_test_x, 3.0)
    assert self.ique.driver.calibration is not None
    self.assertEqual(self.ique.driver.calibration.instrument.deck_height, 201.0)
    (backup,) = self.backups.iterdir()
    self.assertEqual(backup.read_bytes(), old)
    self.assertEqual(self.controller.probe_position, (131.0, 195.0, 250.0))
    self.assertEqual(self.controller.rail_position, "ejected")
    await self.ique.validate_deck_height()

  async def test_deck_out_of_range(self) -> None:
    """A deck further than the tolerance from the stored height is refused; nothing is stored
    and the instrument is left parked and ejected."""
    old = self.controller.calibration_file
    self.controller.deck_height = 210.0
    with self.assertRaises(CalibrationRefusedError):
      await self.ique.calibrate_deck_height(confirm=True)
    self.assertEqual(self.controller.calibration_file, old)
    self.assertEqual(list(self.backups.iterdir()), [])
    self.assertEqual(self.controller.rail_position, "ejected")
    self.assertEqual(self.controller.probe_position, (131.0, 195.0, 250.0))


if __name__ == "__main__":
  unittest.main()
