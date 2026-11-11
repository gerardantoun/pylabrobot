"""Tests of the iQue resource: the factory, the plate holder that follows the plate rail, and
the operations end to end on the simulated controller."""

import asyncio
import dataclasses
import io
import logging
import pathlib
import tempfile
import unittest
import uuid
import zipfile
from typing import List, Optional, Union

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
  DeviceFaultedError,
  DeviceResetError,
  PlateAccessError,
  WorklistActiveError,
  WorklistCancelledError,
  WorklistFailedError,
)
from pylabrobot.sartorius.ique.driver.fcs import read_fcs
from pylabrobot.sartorius.ique.driver.master import IQueDriver, RunProgress
from pylabrobot.sartorius.ique.driver.simulator import SimulatedController, simulated_calibration
from pylabrobot.sartorius.ique.driver.worklist import (
  PlateReadSettings,
  TubeReadSettings,
  plate_read_worklist,
  well_order,
)

FAST = 1000.0  # simulated seconds per real second during reads
FASTER = 20000.0  # simulated seconds per real second during maintenance
MODEL_96 = "Generic 96 Well Plate"
MODEL_384 = "Generic 384 Well Plate"


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
    read = await self.ique.read_plate(self.plate, plate_model=MODEL_96)
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
    read = await self.ique.read_plate(
      self.plate, wells=wells, order="serpentine_row", plate_model=MODEL_96
    )
    expected = ["A01", "A02", "B02", "B01", "C01"]
    self.assertEqual(read.wells, expected)
    self.assertEqual(
      [f"{command.row}{command.column:02d}" for command in sampled_commands(read)], expected
    )
    self.assertEqual(read.verdict, "identified", read.message)
    as_given = await self.ique.read_plate(
      plate_format="Well96", wells=["C3", "A1"], order=None, plate_model=MODEL_96
    )
    self.assertEqual(as_given.wells, ["C03", "A01"])
    self.assertIsNone(as_given.plate_name)

  async def test_384_well_plate(self) -> None:
    """Some wells of a 384-well plate are read with the 384-well plate model."""
    self.ique.plate_holder.unassign_child_resource(self.plate)
    plate = biorad_384_wellplate_50uL_Vb(name="plate_384")
    self.ique.plate_holder.assign_child_resource(plate)
    read = await self.ique.read_plate(
      plate, wells=["P24", "A1", "A2", "B1"], order="column", plate_model=MODEL_384
    )
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

    task = asyncio.ensure_future(
      self.ique.read_plate(self.plate, plate_model=MODEL_96, on_progress=on_progress)
    )
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
      await self.ique.read_plate(self.plate, plate_model=MODEL_96, on_progress=fault)
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
      await self.ique.read_plate(self.plate, plate_model=MODEL_384)
    self.assertNotIn("worklist_run", self.names())

  async def test_plate_model_must_be_named(self) -> None:
    """A read names its plate model: as plate_model, or as the plate resource's model."""
    with self.assertRaises(ValueError) as caught:
      await self.ique.read_plate(self.plate)
    self.assertIn("plate_model", str(caught.exception))
    self.assertIn(MODEL_96, str(caught.exception))
    self.assertNotIn("worklist_run", self.names())
    self.plate.model = MODEL_96
    read = await self.ique.read_plate(self.plate, wells=["A1", "A2"])
    self.assertEqual(read.plate_model, MODEL_96)

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


def on_well(column: int, phase: str, event: asyncio.Event):
  """Return a progress callback that sets `event` when well A`column` reaches `phase`."""

  def note(progress: RunProgress) -> None:
    """Set the event for the well."""
    command = progress.command
    if progress.phase == phase and type(command) is wire.SampleWellCommand:
      if (command.row, command.column) == ("A", column):
        event.set()

  return note


class SampleLineTests(unittest.IsolatedAsyncioTestCase):
  """Whatever way a read ends, flush_needed says whether it left sample in the line."""

  async def asyncSetUp(self) -> None:
    """Set up a simulated instrument with a plate on the holder."""
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

  def read(
    self, on_progress=None, wells: Optional[List[str]] = None
  ) -> "asyncio.Future[PlateRead]":
    """Start a read of row A, or of the given wells."""
    return asyncio.ensure_future(
      self.ique.read_plate(
        self.plate,
        wells=wells or [f"A{column}" for column in range(1, 13)],
        plate_model=MODEL_96,
        on_progress=on_progress,
      )
    )

  async def test_cancelled_task(self) -> None:
    """A read whose task is cancelled after recording started leaves sample in the line."""
    sampled = asyncio.Event()
    task = self.read(on_well(3, "completed", sampled))
    await sampled.wait()
    task.cancel()
    with self.assertRaises(asyncio.CancelledError):
      await task
    self.assertTrue(self.ique.flush_needed)
    await self.ique.prime(5.0)
    self.assertFalse(self.ique.flush_needed)

  async def test_failing_progress_callback(self) -> None:
    """A read whose progress callback fails after recording started leaves sample."""

    def fail(progress: RunProgress) -> None:
      """Fail once the third well is sampled."""
      command = progress.command
      if progress.phase == "completed" and type(command) is wire.SampleWellCommand:
        if command.column == 3:
          raise RuntimeError("the callback failed")

    with self.assertRaises(RuntimeError):
      await self.read(fail)
    self.assertTrue(self.ique.flush_needed)

  async def test_refused_read_leaves_the_line_as_it_was(self) -> None:
    """A read the instrument never ran changes nothing."""
    self.controller.fault_device("Shaker", "Home Faulted")
    for previous in (False, True):
      self.ique._flush_needed = previous
      with self.assertRaises(DeviceFaultedError):
        await self.read()
      self.assertEqual(self.ique.flush_needed, previous)
    await self.ique.driver.call(self.controller.controller_reset_devices, None)

  async def test_stopped_tube_read(self) -> None:
    """A tube read cancelled while it draws leaves sample in the line."""
    drawing = asyncio.Event()

    def note(progress: RunProgress) -> None:
      """Set the event when the draw from the tube starts."""
      if progress.phase == "started" and isinstance(progress.command, wire.SampleVialCommand):
        drawing.set()

    task = asyncio.ensure_future(
      self.ique.read_tube(TubeReadSettings(sip_time=60.0), on_progress=note)
    )
    await drawing.wait()
    await self.ique.driver.cancel_worklist()
    with self.assertRaises(WorklistCancelledError):
      await task
    self.assertTrue(self.ique.flush_needed)

  async def test_stop_early_while_the_tube_draws(self) -> None:
    """Stopping a tube read while it draws sends nothing: its flush comes next, and the read
    ends with the line flushed."""
    drawing = asyncio.Event()

    def note(progress: RunProgress) -> None:
      """Set the event when the draw from the tube starts."""
      if progress.phase == "started" and isinstance(progress.command, wire.SampleVialCommand):
        drawing.set()

    task = asyncio.ensure_future(
      self.ique.read_tube(TubeReadSettings(sip_time=10.0), on_progress=note)
    )
    await drawing.wait()
    await self.ique.stop_read_early()
    tube = await task
    names = [name for name, _ in self.controller.calls]
    self.assertNotIn("worklist_skip_to", names)
    self.assertNotIn("worklist_cancel", names)
    self.assertGreater(tube.event_count, 0)
    self.assertFalse(self.ique.flush_needed)

  async def test_no_skip_while_the_last_well_runs(self) -> None:
    """Stopping while the last well is sampled sends no skip: the flush is next."""
    last = asyncio.Event()
    task = self.read(on_well(12, "started", last))
    await last.wait()
    await self.ique.stop_read_early()
    read = await task
    self.assertNotIn("worklist_skip_to", [name for name, _ in self.controller.calls])
    self.assertFalse(read.stopped_early)
    self.assertEqual(len(read.wells), 12)
    self.assertFalse(self.ique.flush_needed)

  async def test_skip_arriving_after_the_flush_started(self) -> None:
    """A skip that reaches the controller once the flush has started skips the cleans after it:
    the read still returns its wells, and asks for a probe clean."""
    third = asyncio.Event()

    note = on_well(3, "started", third)

    original = self.controller.worklist_skip_to

    async def late_skip(id: uuid.UUID, *, timeout: Optional[float] = None) -> None:
      """Deliver the skip once the flush has started, which moves the rail out."""
      while self.controller.rail_position != "ejected" and self.controller._rail_motion is None:
        await asyncio.sleep(0.0005)
      await original(id, timeout=timeout)

    self.controller.worklist_skip_to = late_skip  # type: ignore[method-assign]
    task = self.read(note, wells=["A1", "A2", "A3", "A4", "A5"])
    await third.wait()
    with self.assertLogs("pylabrobot.sartorius.ique.device", logging.WARNING) as logs:
      await self.ique.stop_read_early()
      read = await task
    self.assertIn("clean_probe_and_tubing()", "\n".join(logs.output))
    self.assertTrue(read.stopped_early)
    skipped = [
      type(command).__name__
      for command in read.worklist.commands or []
      if command is not None and command.execution_notes == "Skipped"
    ]
    self.assertIn("StopFlowCytometerFluidicsCommand", skipped)
    self.assertTrue(self.ique.flush_needed)
    self.assertEqual(self.controller.rail_position, "ejected")


class UnfinalisedWorklistTests(unittest.IsolatedAsyncioTestCase):
  """A finished worklist the controller holds unfinalised, as after it restarted."""

  async def asyncSetUp(self) -> None:
    """Set up a simulated instrument."""
    self.controller = SimulatedController(speed=FASTER)
    self.ique = IQueDevice(IQueDriver(client=self.controller, ready_poll_interval=0.001))
    prepare(self.ique)
    await self.ique.setup()

  async def asyncTearDown(self) -> None:
    """Disconnect and stop the simulator's tasks."""
    if self.ique.driver.is_set_up:
      await self.ique.stop()
    await self.controller.shutdown()

  async def test_every_operation_can_finalise_it(self) -> None:
    """Without consent every operation refuses; finalize_previous or finalize_worklist finalise."""
    await self.ique.prime(5.0)
    self.controller._worklist_status = "CompletedUnfinalizedWorklist"
    for operation in (
      lambda **kw: self.ique.prime(5.0, **kw),
      lambda **kw: self.ique.validate_deck_height(**kw),
      lambda **kw: self.ique.detector.quick_clean(**kw),
    ):
      with self.assertRaises(WorklistActiveError):
        await operation()
      await operation(finalize_previous=True)
      self.controller._worklist_status = "CompletedUnfinalizedWorklist"
    self.assertTrue(await self.ique.finalize_worklist())
    self.assertFalse(await self.ique.finalize_worklist())
    await self.ique.prime(5.0)

  async def test_recover_a_plate_read(self) -> None:
    """A plate read the session lost is recovered with its events and rebuilt as a read."""
    calibration = self.ique.driver.calibration
    assert calibration is not None
    settings = PlateReadSettings(sip_time=1.0, up_time=0.5)
    worklist = plate_read_worklist(
      calibration, "Well96", ["A01", "A02", "A03"], settings, plate_model=MODEL_96
    )
    self.controller.speed = FAST
    await self.controller.worklist_run(worklist)
    await self.controller.wait_for_worklist()
    recovered = await self.ique.recover_unfinalized_run()
    assert recovered is not None
    self.assertIsNone(recovered.plate_read)
    assert recovered.run.events is not None
    self.assertGreater(recovered.run.events.event_count, 0)
    self.assertEqual(await self.controller.worklist_get_status(), "NoActiveWorklist")
    self.assertIsNone(await self.ique.recover_unfinalized_run())
    await self.controller.worklist_run(
      plate_read_worklist(calibration, "Well96", ["A01", "A02"], settings, plate_model=MODEL_96)
    )
    await self.controller.wait_for_worklist()
    with self.assertRaises(ValueError):
      await self.ique.recover_unfinalized_run(PlateReadSettings(sip_time=2.0))
    recovered = await self.ique.recover_unfinalized_run(settings)
    assert recovered is not None and recovered.plate_read is not None
    self.assertEqual(recovered.plate_read.wells, ["A01", "A02"])
    self.assertEqual(recovered.plate_read.plate_model, MODEL_96)
    self.assertFalse(self.ique.flush_needed)


class SetupTests(unittest.IsolatedAsyncioTestCase):
  """Bringing the instrument up after a session left the probe down in a loaded plate."""

  async def asyncSetUp(self) -> None:
    """Leave the probe resting on the deck over the loaded plate, and end the session as a
    crashed program would."""
    self.controller = SimulatedController(speed=FASTER)
    ique = IQueDevice(IQueDriver(client=self.controller, ready_poll_interval=0.001))
    prepare(ique)
    await ique.setup()
    await ique.plate_rail.load()
    await ique.sampler.move_xy(50.0, 80.0)
    await ique.sampler.find_z_contact(250.0, 200.0, retract_after_find=False)
    self.assertTrue(await ique.sampler.has_contact())
    await self.controller.close()
    self.again = IQueDevice(IQueDriver(client=self.controller, ready_poll_interval=0.001))
    prepare(self.again)

  async def asyncTearDown(self) -> None:
    """Stop the simulator's tasks."""
    await self.controller.shutdown()

  async def test_the_probe_rises_before_the_rail_moves(self) -> None:
    """Setup raises the probe, and only then homes and ejects the rail."""
    await self.again.setup()
    self.assertEqual(self.controller.collisions, [])
    self.assertEqual(self.controller.rail_position, "ejected")
    self.assertEqual(self.controller.probe_position, (131.0, 195.0, 250.0))
    await self.again.stop()

  async def test_a_stuck_contact_sensor_stops_setup(self) -> None:
    """A sensor that still reports contact with the probe raised stops setup; the rail stays."""
    self.controller.set_contact(True)
    with self.assertRaises(DeviceFaultedError):
      await self.again.setup()
    self.assertEqual(self.controller.collisions, [])
    self.assertEqual(self.controller.rail_position, "loaded")

  async def test_a_stuck_probe_stops_setup(self) -> None:
    """A probe that is not at the top after the sampler started up stops setup."""

    async def no_park() -> None:
      """Start the sampler, raising the probe only partway."""
      self.controller._z = 240.0

    self.controller._initialize_sampler = no_park  # type: ignore[method-assign]
    with self.assertRaises(DeviceResetError):
      await self.again.setup()
    self.assertEqual(self.controller.collisions, [])
    self.assertEqual(self.controller.rail_position, "loaded")


class PlateModelTests(unittest.IsolatedAsyncioTestCase):
  """The plate model a read uses when none is chosen."""

  async def test_no_plate_model_is_chosen_for_the_plate(self) -> None:
    """Without a plate model named, the read refuses even when only one is mapped; a model with
    only a taught depth, whose format the calibration does not record, is used only when named,
    with a warning."""
    calibration = simulated_calibration()
    extra = PlateModel("Other 96 Well Plate", well_depth_offset=4.0)
    calibration = dataclasses.replace(calibration, plate_models=calibration.plate_models + (extra,))
    controller = SimulatedController(speed=FAST, calibration=write_calibration(calibration))
    ique = IQueDevice(IQueDriver(client=controller, ready_poll_interval=0.001))
    prepare(ique)
    await ique.setup()
    plate = cor_96_wellplate_360uL_Fb(name="plate")
    ique.plate_holder.assign_child_resource(plate)
    with self.assertRaises(ValueError) as caught:
      await ique.read_plate(plate)
    self.assertIn("Generic 96 Well Plate", str(caught.exception))
    self.assertNotIn("Other 96 Well Plate", str(caught.exception))
    plate.model = "Other 96 Well Plate"
    with self.assertRaises(ValueError):
      await ique.read_plate(plate)
    self.assertNotIn("worklist_run", [name for name, _ in controller.calls])
    with self.assertLogs("pylabrobot.sartorius.ique.device", "WARNING") as logs:
      read = await ique.read_plate(plate, plate_model="Other 96 Well Plate", wells=["A1"])
    self.assertIn("taught depth", "\n".join(logs.output))
    self.assertEqual(read.plate_model, "Other 96 Well Plate")
    await ique.stop()
    await controller.shutdown()

  async def test_wells_below_the_deck_are_refused(self) -> None:
    """A plate model whose wells would be sampled below the deck height is refused."""
    calibration = simulated_calibration()
    low = PlateModel("Low 96 Well Plate", well_depth_offset=-2.0)
    calibration = dataclasses.replace(calibration, plate_models=calibration.plate_models + (low,))
    controller = SimulatedController(speed=FAST, calibration=write_calibration(calibration))
    ique = IQueDevice(IQueDriver(client=controller, ready_poll_interval=0.001))
    prepare(ique)
    await ique.setup()
    plate = cor_96_wellplate_360uL_Fb(name="plate")
    ique.plate_holder.assign_child_resource(plate)
    with self.assertRaises(ValueError) as caught:
      await ique.read_plate(plate, plate_model="Low 96 Well Plate", wells=["A1"])
    self.assertIn("below the deck height", str(caught.exception))
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
    # Only the deck height and its date changed in the instrument's document.
    assert old is not None
    before = zipfile.ZipFile(io.BytesIO(old)).read("Calibration.xml").decode("utf-8-sig")
    after = zipfile.ZipFile(io.BytesIO(self.controller.calibration_file)).read("Calibration.xml")
    changed = [
      (a, b) for a, b in zip(before.splitlines(), after.decode("utf-8-sig").splitlines()) if a != b
    ]
    self.assertEqual(
      [a.strip()[: a.strip().index(">")] for a, _ in changed], ["<ZLow", "<ZLowLastCalibratedOn"]
    )
    self.assertEqual(self.controller.probe_position, (131.0, 195.0, 250.0))
    self.assertEqual(self.controller.rail_position, "ejected")
    await self.ique.validate_deck_height()

  async def test_what_the_calibration_does_not_model_is_kept(self) -> None:
    """Parts of the instrument's file that the calibration does not read survive a new deck
    height: the file is patched, not written again."""
    old = self.controller.calibration_file
    assert old is not None
    with zipfile.ZipFile(io.BytesIO(old)) as source:
      document = source.read("Calibration.xml").decode("utf-8-sig")
    closing = "</CalibrationContainer4>"
    self.assertEqual(document.count(closing), 1)
    kept = "<!-- a part the calibration does not model -->"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
      target.writestr(
        "Calibration.xml", document.replace(closing, kept + closing).encode("utf-8-sig")
      )
    self.controller.calibration_file = buffer.getvalue()
    self.controller.deck_height = 201.0
    self.assertEqual(await self.ique.calibrate_deck_height(confirm=True), 201.0)
    assert self.controller.calibration_file is not None
    with zipfile.ZipFile(io.BytesIO(self.controller.calibration_file)) as stored:
      self.assertIn(kept, stored.read("Calibration.xml").decode("utf-8-sig"))
    self.assertEqual(
      read_calibration(self.controller.calibration_file).instrument.deck_height, 201.0
    )

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
