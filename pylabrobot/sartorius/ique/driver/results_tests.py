"""Tests of the plate and tube read results, built from reads on the simulated controller."""

import dataclasses
import pathlib
import statistics
import tempfile
import unittest

from pylabrobot.sartorius.ique.driver.errors import (
  EventDataError,
  WellIdentificationFailedError,
)
from pylabrobot.sartorius.ique.driver.fcs import WELL_KEYWORD, read_fcs
from pylabrobot.sartorius.ique.driver.master import CompletedRun, IQueDriver
from pylabrobot.sartorius.ique.driver.results import (
  PlateRead,
  plate_read_from_run,
  tube_read_from_run,
)
from pylabrobot.sartorius.ique.driver.simulator import SimulatedController
from pylabrobot.sartorius.ique.driver.simulator_tests import simulated_driver
from pylabrobot.sartorius.ique.driver.worklist import (
  PlateReadSettings,
  TubeReadSettings,
  plate_read_worklist,
  sampled_wells,
  tube_read_worklist,
)

WELLS = ["A01", "A02", "A03", "B03", "B02"]
MODEL = "Generic 96 Well Plate"


class ResultTests(unittest.IsolatedAsyncioTestCase):
  """A plate read and a tube read on the simulator, turned into results."""

  async def asyncSetUp(self) -> None:
    """Set up a driver on a fast simulator."""
    self.driver: IQueDriver
    self.controller: SimulatedController
    self.driver, self.controller = await simulated_driver(seed=4)
    self.settings = PlateReadSettings()

  async def asyncTearDown(self) -> None:
    """Stop the simulator's tasks."""
    await self.controller.shutdown()

  async def plate_run(self) -> CompletedRun:
    """Run a read of five wells of a 96-well plate and return the completed run."""
    calibration = self.driver.require_calibration()
    worklist = plate_read_worklist(
      calibration, "Well96", WELLS, self.settings, plate_model=MODEL, metadata=[("Run", "7")]
    )
    return await self.driver.run_worklist(worklist)

  def from_run(self, run: CompletedRun) -> PlateRead:
    """Return the read of a run of the five wells."""
    return plate_read_from_run(
      run, plate_format="Well96", plate_model=MODEL, settings=self.settings, plate_name="p"
    )

  async def plate_read(self) -> PlateRead:
    """Return the read of five wells."""
    return self.from_run(await self.plate_run())

  async def test_wells_and_their_events(self) -> None:
    """The wells come in sampling order, each with a contiguous slice of the plate's events."""
    read = await self.plate_read()
    self.assertEqual(read.wells, WELLS)
    self.assertEqual(read.verdict, "identified", read.message)
    self.assertEqual(read.message, read.identification.message)
    counts = read.event_counts()
    self.assertEqual(list(counts), WELLS)
    self.assertTrue(all(count > 50 for count in counts.values()), counts)
    self.assertLessEqual(sum(counts.values()), read.events.event_count)
    first = read.identification.wells[0]
    assert first.first_event is not None and first.last_event is not None
    self.assertEqual(
      read.well_events("A1"), read.events.slice(first.first_event, first.last_event + 1)
    )
    self.assertEqual(read.well_events("A01").event_count, counts["A01"])
    with self.assertRaises(KeyError):
      read.well_events("H12")
    with self.assertRaises(ValueError):
      read.well_events("not a well")
    self.assertFalse(read.stopped_early)
    self.assertEqual(read.plate_name, "p")
    self.assertTrue(read.worklist.all_commands_completed)

  async def test_write_fcs(self) -> None:
    """The plate's file is the controller's; each well's file holds its events and its name."""
    read = await self.plate_read()
    with tempfile.TemporaryDirectory() as folder:
      paths = read.write_fcs(folder)
      self.assertEqual(len(paths), 1 + len(WELLS))
      self.assertEqual(paths[0].read_bytes(), read.fcs_file)
      self.assertEqual([path.name for path in paths[1:]], [f"Well_{well}.fcs" for well in WELLS])
      events, keywords = read_fcs(paths[2].read_bytes())
      self.assertEqual(events, read.well_events("A02"))
      self.assertEqual(keywords[WELL_KEYWORD], "A02")
      self.assertEqual(keywords["$CYT"], read.fcs_keywords["$CYT"])
      self.assertEqual(keywords["$TIMESTEP"], read.fcs_keywords["$TIMESTEP"])
      self.assertEqual(keywords["IC$Run"], "7")
      self.assertNotIn("$SMNO", keywords)
      with self.assertRaises(FileExistsError):
        read.write_fcs(folder)
      self.assertEqual(len(read.write_fcs(folder, overwrite=True)), 1 + len(WELLS))
      only_plate = read.write_fcs(pathlib.Path(folder) / "plate", per_well=False)
      self.assertEqual([path.read_bytes() for path in only_plate], [read.fcs_file])

  async def test_unusable_runs(self) -> None:
    """A run without events, or whose events cannot be assigned, is reported with the run."""
    run = await self.plate_run()
    with self.assertRaises(EventDataError):
      self.from_run(dataclasses.replace(run, events=None))
    assert run.events is not None
    empty = dataclasses.replace(run, events=run.events.slice(0, 0))
    with self.assertRaises(WellIdentificationFailedError) as caught:
      self.from_run(empty)
    self.assertIs(caught.exception.run, empty)

  async def test_measured_transit_delay(self) -> None:
    """A clean read gives its transit delay; with it, a read whose first well is blank and
    whose line held sample keeps every other well in place and names nothing else."""
    read = await self.plate_read()
    start = read.acquisition_start
    assert start is not None
    self.assertGreater(start, 0.0)
    self.assertLessEqual(start, sampled_wells(read.worklist)[0].start)
    delays = read.transit_delays()
    self.assertEqual(list(delays), WELLS)
    transit = round(statistics.median(delays.values()), 1)
    self.assertLess(max(delays.values()) - min(delays.values()), 0.25)
    await self.controller.shutdown()
    self.driver, self.controller = await simulated_driver(
      seed=4, blank_wells=["A01"], carry_over=[24.0, 26.5]
    )
    run = await self.plate_run()
    without = self.from_run(run)
    self.assertEqual(without.verdict, "needs_review")
    self.assertNotEqual(without.identification.wells[1].event_range, None)
    self.settings = PlateReadSettings(transit_delay=transit)
    placed = self.from_run(run)
    self.assertEqual(placed.identification.flagged_wells, ("A01",))
    self.assertEqual(placed.identification.inconsistent_wells, ())
    self.assertEqual(placed.event_counts()["A01"], 0)
    self.assertTrue(all(placed.event_counts()[well] > 50 for well in WELLS[1:]))

  async def test_tube_read(self) -> None:
    """Every event of a tube read belongs to the tube, and its file is the controller's."""
    settings = TubeReadSettings()
    calibration = self.driver.require_calibration()
    run = await self.driver.run_worklist(tube_read_worklist(calibration, settings))
    read = tube_read_from_run(run, settings)
    self.assertGreater(read.event_count, 0)
    self.assertEqual(read.verdict, "identified")
    self.assertEqual(read.identification.wells[0].event_count, read.event_count)
    with tempfile.TemporaryDirectory() as folder:
      path = read.write_fcs(folder)
      self.assertEqual(path.read_bytes(), read.fcs_file)
      with self.assertRaises(FileExistsError):
        read.write_fcs(folder)


if __name__ == "__main__":
  unittest.main()
