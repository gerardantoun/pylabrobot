"""Tests of the simulated controller, end to end through the driver; and helpers that build
worklists by hand for the other tests."""

import asyncio
import dataclasses
import datetime
import decimal
import unittest
import uuid
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from pylabrobot.sartorius.ique.driver import contract as wire
from pylabrobot.sartorius.ique.driver.client import ControllerFault
from pylabrobot.sartorius.ique.driver.contract import (
  CommandCategory,
  DeviceKind,
  Worklist,
  WorklistCommand,
)
from pylabrobot.sartorius.ique.driver.errors import (
  DeviceNotReadyError,
  WorklistCancelledError,
  WorklistFailedError,
)
from pylabrobot.sartorius.ique.driver.fcs import event_times, file_crc32, read_fcs
from pylabrobot.sartorius.ique.driver.master import CompletedRun, ControllerApi, IQueDriver
from pylabrobot.sartorius.ique.driver.simulator import (
  SIMULATED_DECK_HEIGHT,
  SimulatedController,
  controller_fault,
)
from pylabrobot.sartorius.ique.driver.well_identification import SampledWell, identify_wells

UNSET = datetime.datetime(1, 1, 1)
D = decimal.Decimal
FAST = 1000.0  # simulated seconds per real second in tests

# Row A of a 96-well plate at the simulated calibration's nominal corners.
ROW_A_X = [D("20.2") + D("9.036") * column for column in range(12)]
ROW_A_Y = D("109.5")
WELL_Z = D(str(SIMULATED_DECK_HEIGHT + 4))
TRAVEL_Z = D("250")
BUFFER_XY = (D("27.25"), D("1.5"))
BUFFER_Z = D(str(SIMULATED_DECK_HEIGHT + 8))


class CommandIds:
  """Hands out command IDs 1, 2, 3, ... so that worklists are repeatable."""

  def __init__(self) -> None:
    """Start at 1."""
    self._counter: Iterator[int] = iter(range(1, 1_000_000))

  def fields(self, category: CommandCategory) -> Dict[str, Any]:
    """Return the fields every command has, as a client fills them in."""
    return {
      "category": category,
      "completed": False,
      "countdown_duration": 0,
      "description": None,
      "end_time": UNSET,
      "estimated_duration": datetime.timedelta(0),
      "execution_notes": None,
      "id": uuid.UUID(int=next(self._counter)),
      "name": None,
      "start_time": UNSET,
    }


def make_worklist(
  commands: Sequence[WorklistCommand],
  *,
  name: str = "Cytometer Worklist",
  monitor: Sequence[DeviceKind] = (),
) -> Worklist:
  """Return a worklist of the given commands as a client sends it."""
  return Worklist(
    all_commands_completed=False,
    client_time_offset=datetime.timedelta(0),
    commands=list(commands),
    device_id=None,
    devices_to_monitor=list(monitor),
    end_time=UNSET,
    error=None,
    execution_notes=None,
    experiment_id=None,
    experiment_name="test",
    fcs_data_written=0,
    fcs_file_crc=0,
    fcs_file_name=None,
    fluidics_run_time_minutes=D(0),
    id=uuid.UUID(int=0),
    leave_fluidics_running_on_complete=False,
    name=name,
    plate_id=uuid.UUID(int=99),
    plate_number="P1",
    pump_run_time_minutes=D(0),
    start_time=UNSET,
    stop_reason="Unspecified",
  )


def start_fluidics(ids: CommandIds) -> wire.StartMatryoshkaFluidicsCommand:
  """Load the plate and start the fluidics for acquisition with every laser."""
  return wire.StartMatryoshkaFluidicsCommand(
    **ids.fields("FlowCytometer"),
    enabled_lasers="VioletBlueRed",
    fcs_metadata=[("Protocol", "test")],
    laser_configuration="Normal",
    speed="Medium",
    thresholds=[],
    user_name="tester",
  )


def start_pump(ids: CommandIds) -> wire.StartPumpCommand:
  """Start the pump at 29 rpm clockwise."""
  return wire.StartPumpCommand(**ids.fields("Pump"), direction="CW", speed=2900)


def acquire(ids: CommandIds, event_maximum: int = 17_500_000) -> wire.FlowCytometerAcquireCommand:
  """Start recording events."""
  return wire.FlowCytometerAcquireCommand(
    **ids.fields("FlowCytometer"),
    acquistion_delay=0,
    estimated_acquisition_time=datetime.timedelta(minutes=2),
    event_maximum=event_maximum,
    event_threshold=2_147_483_647,
  )


def sample_well(ids: CommandIds, column: int) -> wire.SampleWellCommand:
  """Sample one well of row A: 1 s down, 0.5 s up."""
  return wire.SampleWellCommand(
    **ids.fields("Sample"),
    fast_move=True,
    sip_time=1000,
    up_time=500,
    x=ROW_A_X[column - 1],
    y=ROW_A_Y,
    z_down=WELL_Z,
    z_up=TRAVEL_Z,
    column=column,
    row="A",
    well_type="Sample",
  )


def flush(ids: CommandIds, seconds: int = 60) -> wire.FlushMatryoshkaCommand:
  """Dip in the buffer station for a while, ejecting the plate."""
  return wire.FlushMatryoshkaCommand(
    **ids.fields("Rinse"),
    fast_move=True,
    rinse_description="flush",
    rinse_duration=seconds,
    rinse_station_name="Buffer",
    rinse_station_pump="S1",
    sip_time=1000,
    up_time=500,
    x=BUFFER_XY[0],
    y=BUFFER_XY[1],
    z_down=BUFFER_Z,
    z_up=TRAVEL_Z,
  )


def stop_pump(ids: CommandIds) -> wire.StopPumpCommand:
  """Stop the pump."""
  return wire.StopPumpCommand(**ids.fields("Pump"))


def stop_fluidics(ids: CommandIds) -> wire.StopFlowCytometerFluidicsCommand:
  """Stop the fluidics."""
  return wire.StopFlowCytometerFluidicsCommand(**ids.fields("FlowCytometer"))


def plate_worklist(
  columns: int = 12, flush_seconds: int = 60, event_maximum: int = 17_500_000
) -> Worklist:
  """Return a plate read of row A: fluidics, pump, acquisition, the wells, flush, stop.

  The flush lasts long enough by default for the last well's sample to reach the detector at
  29 rpm before the fluidics stop.
  """
  ids = CommandIds()
  commands: List[WorklistCommand] = [
    start_fluidics(ids),
    start_pump(ids),
    acquire(ids, event_maximum),
  ]
  commands += [sample_well(ids, column) for column in range(1, columns + 1)]
  commands += [flush(ids, flush_seconds), stop_pump(ids), stop_fluidics(ids)]
  return make_worklist(commands)


def sampled_wells(run: CompletedRun) -> List[SampledWell]:
  """Return the sampled wells of a completed run, timed from the acquisition command."""
  commands = run.commands
  start = next(c for c in commands if isinstance(c, wire.FlowCytometerAcquireCommand)).start_time
  return [
    SampledWell(
      f"{command.row}{command.column:02d}",
      (command.start_time - start).total_seconds(),
      (command.end_time - start).total_seconds(),
    )
    for command in commands
    if isinstance(command, wire.SampleWellCommand) and command.completed
  ]


async def until_running(controller: SimulatedController) -> None:
  """Wait until the simulator runs a worklist."""
  while await controller.worklist_get_status() != "RunningWorklist":
    await asyncio.sleep(0.001)


async def simulated_driver(**options: Any) -> Tuple[IQueDriver, SimulatedController]:
  """Return a set-up driver on a fast simulator built with `options`."""
  options.setdefault("speed", FAST)
  controller = SimulatedController(**options)
  driver = IQueDriver(client=controller, status_poll_interval=0.05, ready_poll_interval=0.001)
  driver.detector.poll_interval = 0.001
  await driver.setup()
  return driver, controller


class SimulatedRunTests(unittest.IsolatedAsyncioTestCase):
  """Worklists on the simulator, through the driver."""

  async def asyncSetUp(self) -> None:
    """Make a driver on a fast simulator."""
    self.driver, self.controller = await simulated_driver(seed=3)

  async def asyncTearDown(self) -> None:
    """Stop the simulator's tasks."""
    await self.controller.shutdown()

  async def test_twelve_wells_are_identified(self) -> None:
    """A 12-well read comes back with events, and well identification finds every well."""
    run = await self.driver.run_worklist(plate_worklist())
    self.assertEqual(run.stop_reason, "Unspecified")
    self.assertTrue(run.worklist.all_commands_completed)
    assert run.events is not None and run.fcs_file is not None
    self.assertEqual(run.events.event_count, run.stream_event_count)
    self.assertTrue(run.stream_consistent)
    self.assertEqual(file_crc32(run.fcs_file), run.worklist.fcs_file_crc)
    times = event_times(run.events)
    wells = sampled_wells(run)
    self.assertEqual(len(wells), 12)
    result = identify_wells(times, wells, sip_time=1.0, up_time=0.5)
    self.assertEqual(result.verdict, "identified", result.message)
    self.assertGreater(result.wells[0].start_time, 20.0)
    self.assertEqual([well.well for well in result.wells], [f"A{c:02d}" for c in range(1, 13)])
    self.assertTrue(all(well.event_count > 50 for well in result.wells))

  async def test_commands_are_timed_in_order(self) -> None:
    """Every command carries start and end times on the controller's clock, in order."""
    run = await self.driver.run_worklist(plate_worklist(columns=3))
    previous = run.worklist.start_time
    for command in run.commands:
      self.assertTrue(command.completed)
      self.assertGreaterEqual(command.start_time, previous)
      self.assertGreaterEqual(command.end_time, command.start_time)
      previous = command.end_time
    self.assertGreaterEqual(run.worklist.end_time, previous)

  async def test_the_file_states_the_run(self) -> None:
    """The controller's FCS file names the instrument, the operator and the metadata."""
    run = await self.driver.run_worklist(plate_worklist(columns=2))
    assert run.fcs_keywords is not None
    self.assertEqual(run.fcs_keywords["$CYT"], "IntelliCyt iQue3")
    self.assertEqual(run.fcs_keywords["$OP"], "tester")
    self.assertEqual(run.fcs_keywords["IC$Protocol"], "test")
    self.assertTrue((run.worklist.fcs_file_name or "").endswith("iQue Screener PLUS - VBR.fcs"))

  async def test_the_run_ends_parked_and_ejected(self) -> None:
    """After a run the probe is parked, the rail ejected and the pump stopped."""
    await self.driver.run_worklist(plate_worklist(columns=1))
    self.assertEqual(self.controller.probe_position, (131.0, 195.0, 250.0))
    self.assertEqual(self.controller.rail_position, "ejected")
    self.assertEqual(self.driver.rail_position, "ejected")
    self.assertEqual(self.driver.device_state("Pump"), "Ready")

  async def test_cancel(self) -> None:
    """A cancelled run stops early with stop reason Cancelled, and keeps its partial data."""
    task = asyncio.ensure_future(self.driver.run_worklist(plate_worklist(flush_seconds=600)))
    await until_running(self.controller)
    await self.driver.cancel_worklist()
    with self.assertRaises(WorklistCancelledError) as caught:
      await task
    run = caught.exception.run
    self.assertEqual(run.stop_reason, "Cancelled")
    self.assertFalse(run.worklist.all_commands_completed)
    self.assertIn(("worklist_finalize", ()), self.controller.calls)
    self.assertEqual(self.controller.rail_position, "ejected")

  async def test_a_faulted_device_stops_the_run(self) -> None:
    """A device that faults during a run stops it with stop reason DeviceFaulted."""
    task = asyncio.ensure_future(self.driver.run_worklist(plate_worklist(flush_seconds=600)))
    await until_running(self.controller)
    self.controller.fault_device("Shaker", "Start Faulted")
    with self.assertRaises(WorklistFailedError) as caught:
      await task
    self.assertEqual(caught.exception.stop_reason, "DeviceFaulted")

  async def test_event_limit(self) -> None:
    """Reaching the event maximum stops the run with stop reason EventLimit."""
    with self.assertRaises(WorklistFailedError) as caught:
      await self.driver.run_worklist(plate_worklist(columns=4, event_maximum=500))
    self.assertEqual(caught.exception.stop_reason, "EventLimit")
    run = caught.exception.run
    assert run.events is not None
    self.assertEqual(run.events.event_count, 500)

  async def test_lost_completion_callback(self) -> None:
    """A run whose completion callback is lost is still collected, by polling the status."""
    await self.controller.shutdown()
    self.driver, self.controller = await simulated_driver(seed=3, drop_worklist_completed=True)
    run = await self.driver.run_worklist(plate_worklist(columns=2))
    self.assertTrue(run.worklist.all_commands_completed)
    assert run.events is not None
    self.assertEqual(run.events.event_count, run.stream_event_count)

  async def test_lost_data_chunk(self) -> None:
    """A lost event chunk shows as a gap in the running count; the file still has everything."""
    await self.controller.shutdown()
    self.driver, self.controller = await simulated_driver(seed=3, drop_data_chunks=[1])
    run = await self.driver.run_worklist(plate_worklist(columns=6))
    self.assertFalse(run.stream_consistent)
    assert run.events is not None
    self.assertGreater(run.events.event_count, run.stream_event_count)


class SimulatorBehaviourTests(unittest.IsolatedAsyncioTestCase):
  """The controller's own behaviour as the simulator reproduces it."""

  async def test_calls_need_connect(self) -> None:
    """The controller refuses every call until the client has connected."""
    controller = SimulatedController()
    await controller.open()
    with self.assertRaises(ControllerFault) as caught:
      await controller.pump_stop()
    assert caught.exception.detail is not None
    self.assertIn("connect", caught.exception.detail.message or "")

  async def test_another_client(self) -> None:
    """With another client registered, connect says so and registers nothing."""
    controller = SimulatedController(other_client_connected=True)
    await controller.open()
    info = await controller.controller_connect(datetime.datetime.now().astimezone())
    assert info is not None
    self.assertTrue(info.is_client_already_connected)
    with self.assertRaises(ControllerFault):
      await controller.worklist_get_status()

  async def test_refusals_while_running(self) -> None:
    """A second run, a reset and a reconfiguration are refused while a worklist runs."""
    driver, controller = await simulated_driver()
    task = asyncio.ensure_future(driver.run_worklist(plate_worklist(flush_seconds=600)))
    await until_running(controller)
    with self.assertRaises(ControllerFault):
      await controller.worklist_run(plate_worklist(columns=1))
    with self.assertRaises(ControllerFault):
      await controller.controller_reset_devices(None)
    with self.assertRaises(ControllerFault):
      await controller.controller_configure_devices({})
    await driver.cancel_worklist()
    with self.assertRaises(WorklistCancelledError):
      await task
    await controller.shutdown()

  async def test_unsupported_command_stops_before_anything_runs(self) -> None:
    """A command without an executor stops the worklist with WorklistError at once."""
    driver, controller = await simulated_driver()
    ids = CommandIds()
    worklist = make_worklist([start_pump(ids), wire.StopFCAutomaterCommand(**ids.fields("Info"))])
    await controller.worklist_run(worklist)
    await controller.wait_for_worklist()
    current = await controller.worklist_get_current()
    assert current is not None
    self.assertEqual(current.stop_reason, "WorklistError")
    self.assertEqual(current.error, "Dev Error: Unsupported worklist command")
    self.assertFalse(any(c is not None and c.completed for c in current.commands or []))
    await controller.shutdown()

  async def test_runs_are_repeatable(self) -> None:
    """The same seed gives the same file; another seed another one."""
    files: List[Optional[bytes]] = []
    for seed in (5, 5, 6):
      driver, controller = await simulated_driver(seed=seed)
      files.append((await driver.run_worklist(plate_worklist(columns=3))).fcs_file)
      await controller.shutdown()
    self.assertEqual(files[0], files[1])
    self.assertNotEqual(files[0], files[2])

  async def test_events_are_whole_and_in_time_order(self) -> None:
    """Streamed chunks hold whole events of 32 values, and event times never go back."""
    driver, controller = await simulated_driver(seed=2)
    run = await driver.run_worklist(plate_worklist(columns=4))
    assert run.fcs_file is not None
    events, _ = read_fcs(run.fcs_file)
    times = list(events.parameter("Time"))
    self.assertEqual(times, sorted(times))
    self.assertEqual(events.parameter_count, 32)
    await controller.shutdown()

  async def test_detector_off_cannot_run(self) -> None:
    """With the detector off the system is Red and a plate run is refused by the driver."""
    driver, controller = await simulated_driver(detector_powered=False)
    self.assertEqual(driver.system_state, "Red")
    self.assertEqual(driver.device_state("Cytometer"), "Disconnected")
    with self.assertRaises(DeviceNotReadyError) as caught:
      await driver.run_worklist(plate_worklist(columns=1))
    self.assertEqual(caught.exception.device, "Cytometer")
    self.assertNotIn("worklist_run", [name for name, _ in controller.calls])
    await controller.shutdown()

  async def test_fault_helper_matches_the_client(self) -> None:
    """The simulator's refusals are ControllerFaults carrying the controller's message."""
    fault = controller_fault("Worklist_Run", "message")
    self.assertEqual(fault.operation, "Worklist_Run")
    self.assertEqual(fault.reason, "message")
    assert fault.detail is not None
    self.assertEqual(fault.detail.message, "message")

  async def test_motion_takes_time(self) -> None:
    """A Z move returns before the probe arrives; a rail move and parking return once done."""
    driver, controller = await simulated_driver()
    await controller.sampler_move_z(D("200"))
    self.assertGreater(controller.probe_position[2], 200.0)
    while controller.probe_position[2] > 200.0:
      await asyncio.sleep(0.001)
    await controller.sampler_home()
    self.assertEqual(controller.probe_position, (131.0, 195.0, 250.0))
    await controller.plate_rail_load()
    self.assertEqual(controller.rail_position, "loaded")
    self.assertEqual(controller.collisions, [])
    await controller.shutdown()

  async def test_initialisation_is_concurrent(self) -> None:
    """Initialising the sampler and the rail in one call moves the rail while the probe still
    rises: a probe left down over the plate collides."""
    controller = SimulatedController(speed=FAST)
    await controller.open()
    await controller.controller_connect(datetime.datetime.now().astimezone())
    optional: Tuple[DeviceKind, ...] = ("Shaker", "Cytometer", "PlateRail", "ContactSensor")
    await controller.controller_configure_devices(
      {
        kind: wire.DeviceConfiguration(configuration=None, include_this_device=True)
        for kind in optional
      }
    )
    await controller.controller_reset_devices(None)
    controller._rail = "loaded"
    controller._x, controller._y, controller._z = 50.0, 80.0, 204.0
    await controller.controller_initialize_devices([])
    self.assertTrue(controller.collisions)
    self.assertIn("the plate rail started moving", controller.collisions[0])
    await controller.shutdown()

  async def test_the_flush_ejects_while_the_probe_rises(self) -> None:
    """The flush moves the rail out at once: after a well with no up time the probe is still
    in the plate."""
    driver, controller = await simulated_driver(seed=3)
    worklist = plate_worklist(columns=1)
    commands = list(worklist.commands or [])
    well = commands[3]
    assert isinstance(well, wire.SampleWellCommand)
    commands[3] = dataclasses.replace(well, up_time=0)
    await driver.run_worklist(dataclasses.replace(worklist, commands=commands))
    self.assertTrue(controller.collisions)
    controller.collisions.clear()
    await driver.run_worklist(plate_worklist(columns=1))
    self.assertEqual(controller.collisions, [])
    await controller.shutdown()

  async def test_blank_wells_carry_over_and_station_events(self) -> None:
    """Blank wells give no events, carry-over arrives after recording starts, and station dips
    give events at their own rate, also before recording starts."""
    driver, controller = await simulated_driver(seed=3, blank_wells=["A01"], carry_over=[25.0])
    run = await driver.run_worklist(plate_worklist(columns=3))
    assert run.events is not None
    times = list(event_times(run.events))
    burst = [time for time in times if 25.0 <= time < 26.0]
    self.assertGreater(len(burst), 200)
    wells = sampled_wells(run)
    first = wells[0].start + 39.0
    self.assertLess(len([time for time in times if first <= time < first + 1.0]), 5)
    await controller.shutdown()
    driver, controller = await simulated_driver(
      seed=3, station_events_per_second=200.0, transit_delay=20.0, blank_wells=["A01"]
    )
    ids = CommandIds()
    rinse = wire.TimedCyclingRinseCommand(**dataclasses.asdict(flush(ids, seconds=10)))
    wait = dataclasses.replace(sample_well(ids, 1), sip_time=40_000)
    worklist = make_worklist(
      [start_fluidics(ids), start_pump(ids), rinse, acquire(ids), wait]
      + [stop_pump(ids), stop_fluidics(ids)]
    )
    run = await driver.run_worklist(worklist)
    assert run.events is not None
    # The Buffer dips before recording reach the detector 20 s later, while A01, blank, is
    # sampled.
    self.assertGreater(run.events.event_count, 100)
    await controller.shutdown()

  async def test_runs_blocked_but_not_maintenance(self) -> None:
    """A status that blocks runs makes the detector not ready to run, but ready to clean."""
    driver, controller = await simulated_driver()
    controller.block_runs("Laser warming up")
    self.assertFalse(await controller.cytometer_is_ready_to_run())
    self.assertTrue(await controller.cytometer_is_ready_to_clean())
    controller.block_runs(None)
    self.assertTrue(await controller.cytometer_is_ready_to_run())
    await controller.shutdown()

  def test_simulator_satisfies_the_client_protocol(self) -> None:
    """The simulator has the client's interface (checked by the type checker, too)."""
    client: ControllerApi = SimulatedController()
    self.assertIsInstance(client, ControllerApi)


if __name__ == "__main__":
  unittest.main()
