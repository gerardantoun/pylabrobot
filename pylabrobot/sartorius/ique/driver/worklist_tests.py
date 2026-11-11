"""Tests of the worklist builders and of reading the worklist of a finished run."""

import dataclasses
import datetime
import decimal
import unittest
import uuid
from typing import Dict, List, Optional, Sequence, Tuple

from pylabrobot.sartorius.ique.driver import calibration as cal
from pylabrobot.sartorius.ique.driver import contract as c
from pylabrobot.sartorius.ique.driver import worklist as w
from pylabrobot.sartorius.ique.driver.calibration import CalibrationError
from pylabrobot.sartorius.ique.driver.wcf import nbfx
from pylabrobot.sartorius.ique.driver.well_identification import SampledWell

D = decimal.Decimal
TAUGHT = datetime.datetime(2026, 10, 2, 9, 15)
UTC = datetime.timezone.utc

# A 96-well plate with a 9 mm pitch and a 384-well plate with a 4.5 mm pitch, so every well
# centre is a round number: 96 wells at X = 20.2 + 9 c, Y = 109.5 - 9 r; 384 wells at
# X = 18 + 4.5 c, Y = 111.7 - 4.5 r.
CORNERS_96 = cal.Corners(
  top_left=cal.XY(20.2, 109.5),
  top_right=cal.XY(119.2, 109.5),
  bottom_left=cal.XY(20.2, 46.5),
  bottom_right=cal.XY(119.2, 46.5),
)
CORNERS_384 = cal.Corners(
  top_left=cal.XY(18.0, 111.7),
  top_right=cal.XY(121.5, 111.7),
  bottom_left=cal.XY(18.0, 44.2),
  bottom_right=cal.XY(121.5, 44.2),
)
# A 96-well depth map measured at columns 0, 4, 8, 11 of rows 0, 4, 7, rising by 0.01 mm per
# column and 0.02 mm per row from 4.0 mm, so every well's bottom is 4.0 + 0.01 c + 0.02 r.
MAP_96 = (4.0, 4.04, 4.08, 4.11, 4.08, 4.12, 4.16, 4.19, 4.14, 4.18, 4.22, 4.25)
STATIONS = (
  cal.Station("Buffer", 27.25, 1.5, 5.0, TAUGHT),
  cal.Station("Decon", 75.75, 1.5, 5.2, TAUGHT),
  cal.Station("Clean", 89.15, 1.5, 5.1, TAUGHT),
  cal.Station("Water", 109.65, 1.5, 4.9, TAUGHT),
  cal.Station("Tube", 0.5, 13.5, 2.0, TAUGHT),
)


def make_calibration(
  system_kind: cal.SystemKind = "iQue3", pump_location: cal.PumpLocation = "Right"
) -> cal.Calibration:
  """Return a taught calibration: deck at 201.35 mm, travel at 250 mm, both plate formats."""
  instrument = cal.Instrument(
    travel_height=250.0,
    deck_height=201.35,
    pump_location=pump_location,
    system_kind=system_kind,
    deck_height_calibrated_on=TAUGHT,
    deck_height_test_x=3.2,
    deck_height_test_y=104.85,
    device_id="IQ3-0042",
  )
  return cal.Calibration(
    instrument=instrument,
    plate_types=(
      cal.PlateTypeCalibration("Well96", CORNERS_96, TAUGHT),
      cal.PlateTypeCalibration("Well384", CORNERS_384, TAUGHT),
    ),
    stations=STATIONS,
    plate_models=(
      cal.PlateModel(
        "Test 96", x_offset=0.1, y_offset=-0.05, depth_map=cal.DepthMap("Well96", MAP_96, TAUGHT)
      ),
      cal.PlateModel("Test 384", depth_map=cal.DepthMap("Well384", (3.5,) * 12, TAUGHT)),
    ),
  )


def with_stations(calibration: cal.Calibration, stations: Sequence[cal.Station]) -> cal.Calibration:
  """Return the calibration with other stations."""
  return dataclasses.replace(calibration, stations=tuple(stations))


def commands_of(worklist: c.Worklist) -> List[c.WorklistCommand]:
  """Return the commands of a worklist, checking none is missing."""
  assert worklist.commands is not None
  commands: List[c.WorklistCommand] = []
  for command in worklist.commands:
    assert command is not None
    commands.append(command)
  return commands


def kinds(worklist: c.Worklist) -> List[str]:
  """Return the class names of a worklist's commands, in order."""
  return [type(command).__name__ for command in commands_of(worklist)]


def seconds(value: float) -> datetime.timedelta:
  """Return a duration in seconds as a timedelta, to the microsecond."""
  return datetime.timedelta(microseconds=round(value * 1_000_000))


def round_trip(worklist: c.Worklist) -> c.Worklist:
  """Write a worklist as the worklist-run request does and read it back."""
  _, body, _ = c.WORKLIST_RUN.request((c.to_wire(c.WORKLIST, worklist),))
  (value,) = c.WORKLIST_RUN.read_request([nbfx.decode(nbfx.encode(body))], {})
  result: c.Worklist = c.from_wire(c.WORKLIST, value)
  return result


def well_xyz_96(row: int, column: int, sampling_height: str = "0.5") -> Tuple[D, D, D]:
  """Return the sampling position of a well of the 96-well test plate, model offsets included."""
  x = D("20.3") + 9 * column
  y = D("109.45") - 9 * row
  z = D("201.35") + D("4.0") + D("0.01") * column + D("0.02") * row + D(sampling_height)
  return x, y, z


class _WorklistTestCase(unittest.TestCase):
  """Assertions shared by the worklist tests."""

  def assert_spot(self, x: D, y: D, z: D, expected: Tuple[str, str, str]) -> None:
    """Assert an XYZ position against decimal texts, scale included."""
    self.assertEqual((str(x), str(y), str(z)), expected)

  def assert_fresh(self, worklist: c.Worklist) -> None:
    """Assert that a worklist and its commands carry nothing a run fills in."""
    commands = commands_of(worklist)
    self.assertEqual(len({command.id for command in commands}), len(commands))
    for command in commands:
      self.assertEqual(command.id.version, 4)
      self.assertFalse(command.completed)
      self.assertEqual((command.start_time, command.end_time), (w.NOT_RUN, w.NOT_RUN))
      self.assertIsNone(command.execution_notes)
      self.assertIsNone(command.name)
      self.assertTrue(command.description)
    self.assertFalse(worklist.all_commands_completed)
    self.assertEqual(worklist.stop_reason, "Unspecified")
    self.assertEqual((worklist.start_time, worklist.end_time), (w.NOT_RUN, w.NOT_RUN))
    self.assertEqual(worklist.client_time_offset, datetime.timedelta(0))
    self.assertIsNone(worklist.fcs_file_name)


class PlateRead96DefaultsTests(_WorklistTestCase):
  """A full 96-well plate read in row order with every setting at its default."""

  def setUp(self) -> None:
    """Build the worklist."""
    self.calibration = make_calibration()
    wells = w.well_order("Well96", "row")
    self.worklist = w.plate_read_worklist(
      self.calibration, "Well96", wells, w.PlateReadSettings(), plate_model="Test 96"
    )
    self.commands = commands_of(self.worklist)

  def test_command_sequence(self) -> None:
    """Fluidics, deck check, pump, acquisition, wells with a shake every 12, flush, cleans."""
    expected = [
      "StartMatryoshkaFluidicsCommand",
      "ValidateDeckHeightCommand",
      "StartPumpCommand",
      "FlowCytometerAcquireCommand",
    ]
    expected += (["SampleWellCommand"] * 12 + ["CombinedShakeRinseAtLocationCommand"]) * 7
    expected += ["SampleWellCommand"] * 12
    expected += ["FlushMatryoshkaCommand"] + ["TimedCyclingRinseCommand"] * 3
    expected += ["StopPumpCommand", "StopFlowCytometerFluidicsCommand"]
    self.assertEqual(kinds(self.worklist), expected)
    self.assertEqual(len(self.commands), 113)

  def test_header(self) -> None:
    """The worklist is archived, homes the shaker, and carries no results."""
    worklist = self.worklist
    self.assertEqual(worklist.name, "Plate read")
    self.assertEqual(worklist.device_id, "IQ3-0042")
    self.assertIsNotNone(worklist.experiment_id)
    self.assertIsNotNone(worklist.plate_id)
    self.assertNotEqual(worklist.plate_id, uuid.UUID(int=0))
    self.assertFalse(worklist.leave_fluidics_running_on_complete)
    self.assertEqual(worklist.devices_to_monitor, [])
    self.assert_fresh(worklist)

  def test_start_fluidics(self) -> None:
    """All lasers, the FSC-H trigger at 100,000, no metadata, the unknown user."""
    start = self.commands[0]
    assert isinstance(start, c.StartMatryoshkaFluidicsCommand)
    self.assertEqual(start.enabled_lasers, "VioletBlueRed")
    self.assertEqual(start.thresholds, [c.Threshold(id=0, maximum=0, minimum=100_000)])
    self.assertEqual(start.fcs_metadata, [])
    self.assertEqual(start.user_name, "UNK")
    self.assertEqual((start.speed, start.laser_configuration), ("Medium", "Normal"))
    self.assertEqual(start.category, "FlowCytometer")
    self.assertEqual(start.estimated_duration, seconds(8))

  def test_deck_check(self) -> None:
    """The test point, travel height, deck height and the 0.25 mm tolerance."""
    check = self.commands[1]
    assert isinstance(check, c.ValidateDeckHeightCommand)
    self.assertFalse(check.is_microfuge_tube_check)
    self.assertEqual(
      [str(value) for value in (check.x, check.y, check.z_high, check.z_low, check.z_tolerance)],
      ["3.200", "104.850", "250.000", "201.350", "0.250"],
    )
    self.assertEqual(check.category, "Info")
    self.assertEqual(check.estimated_duration, seconds(6.35))

  def test_pump(self) -> None:
    """The pump starts at 29 rpm (2900 on the wire), clockwise, and stops before the fluidics."""
    pump = self.commands[2]
    assert isinstance(pump, c.StartPumpCommand)
    self.assertEqual((pump.speed, pump.direction, pump.category), (2900, "CW", "Pump"))
    self.assertIsInstance(self.commands[-2], c.StopPumpCommand)
    self.assertEqual(self.commands[-2].estimated_duration, seconds(1))
    self.assertEqual(self.commands[-1].estimated_duration, seconds(5))

  def test_acquire(self) -> None:
    """No event limit, no delay, the all-laser maximum and the expected acquisition time."""
    acquire = self.commands[3]
    assert isinstance(acquire, c.FlowCytometerAcquireCommand)
    self.assertEqual(acquire.event_threshold, 2_147_483_647)
    self.assertEqual(acquire.acquistion_delay, 0)
    self.assertEqual(acquire.event_maximum, 17_500_000)
    # 1 s acquire + 96 samples (206.70 s) + 7 shakes of 7.8 s, up to the flush.
    self.assertEqual(acquire.estimated_acquisition_time, seconds(262.3))

  def test_wells(self) -> None:
    """Every well in row order at its interpolated, offset, mapped position."""
    samples = [command for command in self.commands if isinstance(command, c.SampleWellCommand)]
    self.assertEqual(len(samples), 96)
    for index, sample in enumerate(samples):
      row, column = divmod(index, 12)
      with self.subTest(well=index):
        self.assertEqual((sample.row, sample.column), ("ABCDEFGH"[row], column + 1))
        self.assertEqual((sample.x, sample.y, sample.z_down), well_xyz_96(row, column))
        self.assertEqual(str(sample.z_up), "250.000")
        self.assertEqual((sample.sip_time, sample.up_time), (1000, 500))
        self.assertTrue(sample.fast_move)
        self.assertEqual((sample.well_type, sample.category), ("Sample", "Sample"))
    self.assert_spot(
      samples[0].x, samples[0].y, samples[0].z_down, ("20.300", "109.450", "205.850")
    )
    last = samples[-1]
    self.assert_spot(last.x, last.y, last.z_down, ("119.300", "46.450", "206.100"))

  def test_shakes(self) -> None:
    """After wells 12, 24, ... 84: 4 s at 2400 rpm with the probe dipping in Buffer."""
    positions = [
      index
      for index, command in enumerate(self.commands)
      if isinstance(command, c.CombinedShakeRinseAtLocationCommand)
    ]
    self.assertEqual(positions, [4 + 13 * k + 12 for k in range(7)])
    for index in positions:
      shake = self.commands[index]
      assert isinstance(shake, c.CombinedShakeRinseAtLocationCommand)
      self.assertEqual((shake.shake_duration, shake.rpm), (4, 2400))
      self.assert_spot(
        shake.rinse_x, shake.rinse_y, shake.rinse_z_down, ("27.250", "1.500", "206.350")
      )
      self.assertEqual(str(shake.rinse_z_up), "250.000")
      self.assertEqual((shake.sip_time, shake.up_time), (1000, 500))
      self.assertEqual((shake.location_name, shake.rinse_station_pump), ("Buffer", "S1"))
      self.assertEqual((shake.category, shake.countdown_duration), ("Shake", 4))
      self.assertEqual(shake.estimated_duration, seconds(7.8))

  def test_flush_and_cleans(self) -> None:
    """A 30 s flush in Buffer, then Decon 30 s, Clean 30 s, Water 60 s."""
    flush = self.commands[-6]
    assert isinstance(flush, c.FlushMatryoshkaCommand)
    self.assertEqual((flush.rinse_duration, flush.sip_time, flush.up_time), (30, 1000, 500))
    self.assert_spot(flush.x, flush.y, flush.z_down, ("27.250", "1.500", "206.350"))
    self.assertEqual((flush.rinse_station_name, flush.rinse_station_pump), ("Buffer", "S1"))
    self.assertEqual((flush.countdown_duration, flush.category), (30, "Rinse"))
    self.assertEqual(flush.estimated_duration, seconds(31.8))
    expected = [
      ("Decon", "S2", 30, ("75.750", "1.500", "206.550")),
      ("Clean", "S3", 30, ("89.150", "1.500", "206.450")),
      ("Water", "S4", 60, ("109.650", "1.500", "206.250")),
    ]
    for clean, (station, pump, duration, spot) in zip(self.commands[-5:-2], expected):
      assert isinstance(clean, c.TimedCyclingRinseCommand)
      self.assertIs(type(clean), c.TimedCyclingRinseCommand)
      self.assertEqual((clean.rinse_station_name, clean.rinse_station_pump), (station, pump))
      self.assertEqual((clean.rinse_duration, clean.sip_time, clean.up_time), (duration, 1000, 500))
      self.assert_spot(clean.x, clean.y, clean.z_down, spot)
      self.assertEqual(str(clean.z_up), "250.000")
      self.assertTrue(clean.fast_move)
      self.assertEqual(clean.estimated_duration, seconds(duration + 1.8))

  def test_sample_estimates(self) -> None:
    """Short moves take 0.63 s, long ones 1 s; a move is long from 50 mm in X or Y."""
    samples = [command for command in self.commands if isinstance(command, c.SampleWellCommand)]
    # A01 from the test point (17.1 mm), within a row (9 mm), B01 to G01 from Buffer after a
    # shake (more than 50 mm in Y), H01 from Buffer (44.95 mm in Y).
    self.assertEqual(samples[0].estimated_duration, seconds(2.13))
    self.assertEqual(samples[1].estimated_duration, seconds(2.13))
    for row in range(1, 7):
      self.assertEqual(samples[12 * row].estimated_duration, seconds(2.5))
    self.assertEqual(samples[84].estimated_duration, seconds(2.13))

  def test_estimated_duration(self) -> None:
    """16.35 s to start, 206.70 s of wells, 54.6 s of shakes, 31.8 s flush, 125.4 s clean, 6 s."""
    self.assertEqual(w.estimated_duration(self.worklist), seconds(440.85))

  def test_wire_round_trip(self) -> None:
    """The worklist survives being written to and read from the wire unchanged."""
    self.assertEqual(round_trip(self.worklist), self.worklist)


class PlateRead384DefaultsTests(_WorklistTestCase):
  """A full 384-well plate read in row order with every setting at its default."""

  def setUp(self) -> None:
    """Build the worklist."""
    self.worklist = w.plate_read_worklist(
      make_calibration(),
      "Well384",
      w.well_order("Well384", "row"),
      w.PlateReadSettings(),
      plate_model="Test 384",
    )
    self.commands = commands_of(self.worklist)

  def test_command_sequence(self) -> None:
    """384 wells with a shake after every 12th but the last."""
    expected = [
      "StartMatryoshkaFluidicsCommand",
      "ValidateDeckHeightCommand",
      "StartPumpCommand",
      "FlowCytometerAcquireCommand",
    ]
    expected += (["SampleWellCommand"] * 12 + ["CombinedShakeRinseAtLocationCommand"]) * 31
    expected += ["SampleWellCommand"] * 12
    expected += ["FlushMatryoshkaCommand"] + ["TimedCyclingRinseCommand"] * 3
    expected += ["StopPumpCommand", "StopFlowCytometerFluidicsCommand"]
    self.assertEqual(kinds(self.worklist), expected)
    self.assertEqual(len(self.commands), 425)
    self.assert_fresh(self.worklist)

  def test_wells(self) -> None:
    """Every well at 18 + 4.5 c, 111.7 - 4.5 r, sampled 0.2 mm above the 3.5 mm map."""
    samples = [command for command in self.commands if isinstance(command, c.SampleWellCommand)]
    self.assertEqual(len(samples), 384)
    for index, sample in enumerate(samples):
      row, column = divmod(index, 24)
      with self.subTest(well=index):
        self.assertEqual((sample.row, sample.column), ("ABCDEFGHIJKLMNOP"[row], column + 1))
        self.assertEqual(sample.x, D("18") + D("4.5") * column)
        self.assertEqual(sample.y, D("111.7") - D("4.5") * row)
        self.assertEqual(str(sample.z_down), "205.050")
    self.assert_spot(
      samples[-1].x, samples[-1].y, samples[-1].z_down, ("121.500", "44.200", "205.050")
    )

  def test_shakes(self) -> None:
    """Shakes run at the 3000 rpm default of 384-well plates."""
    shakes = [
      command
      for command in self.commands
      if isinstance(command, c.CombinedShakeRinseAtLocationCommand)
    ]
    self.assertEqual(len(shakes), 31)
    self.assertEqual({(shake.rpm, shake.shake_duration) for shake in shakes}, {(3000, 4)})

  def test_estimates(self) -> None:
    """27 of the wells after a shake are long moves; those in rows O and P are short."""
    # 384 x 2.13 s + 27 x 0.37 s = 827.91 s of wells and 31 x 7.8 s = 241.8 s of shakes.
    acquire = self.commands[3]
    assert isinstance(acquire, c.FlowCytometerAcquireCommand)
    self.assertEqual(acquire.estimated_acquisition_time, seconds(1070.71))
    self.assertEqual(w.estimated_duration(self.worklist), seconds(1249.26))


def all_options_settings() -> w.PlateReadSettings:
  """Return plate-read settings with every option on and nothing at its default."""
  return w.PlateReadSettings(
    thresholds=(w.TriggerThreshold("SSC-H", 50_000), w.TriggerThreshold("BL1-H", 20_000)),
    lasers=("blue", "red"),
    event_limit=250_000,
    sip_time=0.8,
    up_time=0.3,
    pump_speed=19.5,
    sampling_height=0.7,
    pre_plate_prime=True,
    pre_plate_prime_duration=45,
    pre_plate_shake=True,
    pre_plate_shake_duration=10,
    pre_plate_shake_speed=1800,
    shake_mode="after_wells",
    shake_every=2,
    shake_duration=3,
    shake_speed=2200,
    shake_station="Water",
    probe_rinse=True,
    rinse_every=3,
    rinses=(w.RinseStep("Decon", 1.5), w.RinseStep("Water", 2.0)),
    flush_duration=45,
    post_plate_clean=True,
    cleans=(w.RinseStep("Clean", 20), w.RinseStep("Water", 25)),
  )


ALL_OPTION_WELLS = ["A01", "A02", "A03", "B01", "B02", "B03", "B04"]


class PlateReadAllOptionsTests(_WorklistTestCase):
  """A 96-well read with every option on, on an instrument whose pump sits on the left."""

  def setUp(self) -> None:
    """Build the worklist."""
    self.worklist = w.plate_read_worklist(
      make_calibration(pump_location="Left"),
      "Well96",
      ALL_OPTION_WELLS,
      all_options_settings(),
      plate_model="Test 96",
      name="Assay plate 3",
      metadata=[("Plate", "P1"), ("Project", "test")],
      user_name="alice",
      deck_height_tolerance=0.3,
    )
    self.commands = commands_of(self.worklist)

  def test_command_sequence(self) -> None:
    """Prime and shake first; shakes after wells 2, 4, 6; rinses after 3 and 6, after the shake."""
    self.assertEqual(
      kinds(self.worklist),
      [
        "StartMatryoshkaFluidicsCommand",
        "ValidateDeckHeightCommand",
        "StartPumpCommand",
        "TimedCyclingRinseCommand",
        "CombinedShakeRinseAtLocationCommand",
        "FlowCytometerAcquireCommand",
        "SampleWellCommand",
        "SampleWellCommand",
        "CombinedShakeRinseAtLocationCommand",
        "SampleWellCommand",
        "CyclingRinseCommand",
        "CyclingRinseCommand",
        "SampleWellCommand",
        "CombinedShakeRinseAtLocationCommand",
        "SampleWellCommand",
        "SampleWellCommand",
        "CombinedShakeRinseAtLocationCommand",
        "CyclingRinseCommand",
        "CyclingRinseCommand",
        "SampleWellCommand",
        "FlushMatryoshkaCommand",
        "TimedCyclingRinseCommand",
        "TimedCyclingRinseCommand",
        "StopPumpCommand",
        "StopFlowCytometerFluidicsCommand",
      ],
    )
    self.assertEqual(self.worklist.name, "Assay plate 3")
    self.assert_fresh(self.worklist)

  def test_acquisition_settings(self) -> None:
    """Blue and red lasers, SSC-H and BL1-H triggers, metadata, user, limits."""
    start = self.commands[0]
    assert isinstance(start, c.StartMatryoshkaFluidicsCommand)
    self.assertEqual(start.enabled_lasers, "BlueRed")
    self.assertEqual(
      start.thresholds,
      [c.Threshold(id=1, maximum=0, minimum=50_000), c.Threshold(id=2, maximum=0, minimum=20_000)],
    )
    self.assertEqual(start.fcs_metadata, [("Plate", "P1"), ("Project", "test")])
    self.assertEqual(start.user_name, "alice")
    acquire = self.commands[5]
    assert isinstance(acquire, c.FlowCytometerAcquireCommand)
    self.assertEqual((acquire.event_threshold, acquire.event_maximum), (250_000, 28_000_000))
    check = self.commands[1]
    assert isinstance(check, c.ValidateDeckHeightCommand)
    self.assertEqual(str(check.z_tolerance), "0.300")

  def test_pump(self) -> None:
    """19.5 rpm is 1950 on the wire; a pump on the left turns counter-clockwise."""
    pump = self.commands[2]
    assert isinstance(pump, c.StartPumpCommand)
    self.assertEqual((pump.speed, pump.direction), (1950, "CCW"))

  def test_prime_and_pre_plate_shake(self) -> None:
    """Both in Buffer, dipping 0.8 s down and at least 0.5 s up."""
    prime = self.commands[3]
    assert isinstance(prime, c.TimedCyclingRinseCommand)
    self.assertEqual((prime.rinse_station_name, prime.rinse_duration), ("Buffer", 45))
    self.assertEqual((prime.sip_time, prime.up_time, prime.countdown_duration), (800, 500, 45))
    self.assert_spot(prime.x, prime.y, prime.z_down, ("27.250", "1.500", "206.350"))
    self.assertEqual(prime.estimated_duration, seconds(46.8))
    shake = self.commands[4]
    assert isinstance(shake, c.CombinedShakeRinseAtLocationCommand)
    self.assertEqual((shake.location_name, shake.shake_duration, shake.rpm), ("Buffer", 10, 1800))
    self.assertEqual((shake.sip_time, shake.up_time), (800, 500))
    self.assertEqual(shake.estimated_duration, seconds(13.8))

  def test_wells(self) -> None:
    """Sampled 0.7 mm above the mapped bottom, 0.8 s down and 0.3 s up."""
    samples = [command for command in self.commands if isinstance(command, c.SampleWellCommand)]
    positions = [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (1, 3)]
    self.assertEqual(
      [format(sample.z_down) for sample in samples],
      ["206.050", "206.060", "206.070", "206.070", "206.080", "206.090", "206.100"],
    )
    for sample, (row, column) in zip(samples, positions):
      self.assertEqual((sample.x, sample.y, sample.z_down), well_xyz_96(row, column, "0.7"))
      self.assertEqual((sample.sip_time, sample.up_time), (800, 300))
    # Long moves from a station, short ones from the well before.
    self.assertEqual(
      [sample.estimated_duration for sample in samples],
      [seconds(value) for value in (2.1, 1.73, 2.1, 2.1, 2.1, 1.73, 2.1)],
    )

  def test_shakes_between_wells(self) -> None:
    """3 s at 2200 rpm with the probe dipping in Water."""
    for index in (8, 13, 16):
      shake = self.commands[index]
      assert isinstance(shake, c.CombinedShakeRinseAtLocationCommand)
      self.assertEqual((shake.shake_duration, shake.rpm, shake.location_name), (3, 2200, "Water"))
      self.assert_spot(
        shake.rinse_x, shake.rinse_y, shake.rinse_z_down, ("109.650", "1.500", "206.250")
      )
      self.assertEqual((shake.rinse_station_pump, shake.countdown_duration), ("S4", 3))
      self.assertEqual(shake.estimated_duration, seconds(6.8))

  def test_rinses(self) -> None:
    """Decon 1.5 s and Water 2 s in ms, with the read's own up time."""
    for decon_index in (10, 17):
      decon, water = self.commands[decon_index], self.commands[decon_index + 1]
      assert isinstance(decon, c.CyclingRinseCommand)
      assert isinstance(water, c.CyclingRinseCommand)
      self.assertEqual((decon.rinse_station_name, decon.rinse_duration), ("Decon", 1500))
      self.assertEqual((water.rinse_station_name, water.rinse_duration), ("Water", 2000))
      self.assertEqual((decon.sip_time, decon.up_time, decon.operation), (800, 300, "Rinse"))
      self.assert_spot(decon.x, decon.y, decon.z_down, ("75.750", "1.500", "206.550"))
      self.assertEqual((decon.rinse_station_pump, water.rinse_station_pump), ("S2", "S4"))
      self.assertEqual((decon.countdown_duration, water.countdown_duration), (1, 2))
      self.assertEqual(decon.estimated_duration, seconds(2.8))
      self.assertEqual(water.estimated_duration, seconds(3.3))

  def test_flush_and_cleans(self) -> None:
    """A 45 s flush, then Clean 20 s and Water 25 s."""
    flush, clean, water = self.commands[20:23]
    assert isinstance(flush, c.FlushMatryoshkaCommand)
    assert isinstance(clean, c.TimedCyclingRinseCommand)
    assert isinstance(water, c.TimedCyclingRinseCommand)
    self.assertEqual((flush.rinse_duration, flush.sip_time, flush.up_time), (45, 800, 500))
    self.assertEqual((clean.rinse_station_name, clean.rinse_duration), ("Clean", 20))
    self.assertEqual((water.rinse_station_name, water.rinse_duration), ("Water", 25))

  def test_estimates(self) -> None:
    """The acquisition runs 47.56 s and the whole worklist 224.91 s."""
    acquire = self.commands[5]
    assert isinstance(acquire, c.FlowCytometerAcquireCommand)
    self.assertEqual(acquire.estimated_acquisition_time, seconds(47.56))
    self.assertEqual(w.estimated_duration(self.worklist), seconds(224.91))

  def test_wire_round_trip(self) -> None:
    """The worklist survives being written to and read from the wire unchanged."""
    self.assertEqual(round_trip(self.worklist), self.worklist)


class ContinuousShakeTests(_WorklistTestCase):
  """Shaking throughout a 96-well read."""

  def setUp(self) -> None:
    """Build a three-well read with continuous shaking."""
    self.worklist = w.plate_read_worklist(
      make_calibration(),
      "Well96",
      ["A01", "A02", "A03"],
      w.PlateReadSettings(shake_mode="continuous"),
      plate_model="Test 96",
    )
    self.commands = commands_of(self.worklist)

  def test_command_sequence(self) -> None:
    """The shaker starts after the acquisition starts and stops before the flush."""
    self.assertEqual(
      kinds(self.worklist),
      [
        "StartMatryoshkaFluidicsCommand",
        "ValidateDeckHeightCommand",
        "StartPumpCommand",
        "FlowCytometerAcquireCommand",
        "StartShakerCommand",
        "SampleWellCommand",
        "SampleWellCommand",
        "SampleWellCommand",
        "StopShakerCommand",
        "FlushMatryoshkaCommand",
        "TimedCyclingRinseCommand",
        "TimedCyclingRinseCommand",
        "TimedCyclingRinseCommand",
        "StopPumpCommand",
        "StopFlowCytometerFluidicsCommand",
      ],
    )

  def test_shaker(self) -> None:
    """At the 2400 rpm default; starting takes 1 s, stopping 2 s, both in the acquisition."""
    start, stop = self.commands[4], self.commands[8]
    assert isinstance(start, c.StartShakerCommand)
    self.assertEqual((start.rpm, start.category), (2400, "Shake"))
    self.assertEqual((start.estimated_duration, stop.estimated_duration), (seconds(1), seconds(2)))
    acquire = self.commands[3]
    assert isinstance(acquire, c.FlowCytometerAcquireCommand)
    self.assertEqual(acquire.estimated_acquisition_time, seconds(1 + 1 + 3 * 2.13 + 2))

  def test_only_for_96_wells(self) -> None:
    """Continuous shaking is refused for a 384-well plate."""
    with self.assertRaisesRegex(ValueError, "96-well"):
      w.plate_read_worklist(
        make_calibration(),
        "Well384",
        ["A01"],
        w.PlateReadSettings(shake_mode="continuous"),
        plate_model="Test 384",
      )


class EmptyWellTests(_WorklistTestCase):
  """Reads of part of a plate: wells left out are skipped and not counted."""

  def test_skipped_wells_do_not_count_towards_the_shake(self) -> None:
    """With A05 left out, the shake after the 12th sampled well comes after B01."""
    wells = w.well_order(
      "Well96",
      "row",
      ["B03", "A01", "A2", "A03", "A04", "A06", "A07", "A08", "A09", "A10", "A11", "A12", "B1"]
      + ["B02"],
    )
    self.assertEqual(
      wells,
      ["A01", "A02", "A03", "A04", "A06", "A07", "A08", "A09", "A10", "A11", "A12", "B01"]
      + ["B02", "B03"],
    )
    worklist = w.plate_read_worklist(
      make_calibration(), "Well96", wells, w.PlateReadSettings(), plate_model="Test 96"
    )
    commands = commands_of(worklist)
    samples = [command for command in commands if isinstance(command, c.SampleWellCommand)]
    self.assertEqual(
      [f"{sample.row}{sample.column:02d}" for sample in samples],
      wells,
    )
    shakes = [
      index
      for index, command in enumerate(commands)
      if isinstance(command, c.CombinedShakeRinseAtLocationCommand)
    ]
    self.assertEqual(shakes, [16])
    self.assertEqual(commands[15], samples[11])
    # A06 follows A04 18 mm away: still a short move.
    self.assertEqual(samples[4].estimated_duration, seconds(2.13))
    self.assertEqual(samples[4].x, D("65.300"))

  def test_given_order_is_kept(self) -> None:
    """Wells are sampled in the order given."""
    worklist = w.plate_read_worklist(
      make_calibration(), "Well96", ["C05", "A01"], w.PlateReadSettings(), plate_model="Test 96"
    )
    samples = [cmd for cmd in commands_of(worklist) if isinstance(cmd, c.SampleWellCommand)]
    self.assertEqual([(sample.row, sample.column) for sample in samples], [("C", 5), ("A", 1)])


class PlateReadRefusalTests(unittest.TestCase):
  """Plate reads that cannot be built."""

  def setUp(self) -> None:
    """Make the calibration."""
    self.calibration = make_calibration()

  def build(
    self,
    settings: Optional[w.PlateReadSettings] = None,
    wells: Sequence[str] = ("A01", "A02"),
    calibration: Optional[cal.Calibration] = None,
    plate_format: cal.PlateFormat = "Well96",
    plate_model: str = "Test 96",
    name: str = "Plate read",
    metadata: Sequence[Tuple[str, str]] = (),
  ) -> c.Worklist:
    """Build a plate read with test defaults."""
    return w.plate_read_worklist(
      self.calibration if calibration is None else calibration,
      plate_format,
      wells,
      w.PlateReadSettings() if settings is None else settings,
      plate_model=plate_model,
      name=name,
      metadata=metadata,
    )

  def test_wells(self) -> None:
    """Duplicate wells, wells off the plate, no wells and a bare text are refused."""
    with self.assertRaisesRegex(ValueError, "A01 appears twice"):
      self.build(wells=["A1", "A01"])
    with self.assertRaisesRegex(ValueError, "not on a Well96 plate"):
      self.build(wells=["I01"])
    with self.assertRaisesRegex(ValueError, "at least one well"):
      self.build(wells=[])
    with self.assertRaisesRegex(ValueError, "sequence of well names"):
      self.build(wells="A01")

  def test_names(self) -> None:
    """Names the controller treats specially are refused."""
    for name in ("Prime", " prime ", "Vial plate", "my test tube"):
      with self.subTest(name=name), self.assertRaises(ValueError):
        self.build(name=name)

  def test_metadata(self) -> None:
    """Metadata keys must be unique and not empty."""
    with self.assertRaisesRegex(ValueError, "appears twice"):
      self.build(metadata=[("Plate", "1"), ("Plate", "2")])
    with self.assertRaisesRegex(ValueError, "key and a value"):
      self.build(metadata=[("", "1")])

  def test_other_instrument(self) -> None:
    """A calibration of an instrument outside the PLUS family is refused."""
    with self.assertRaisesRegex(ValueError, "PLUS family"):
      self.build(calibration=make_calibration(system_kind="iQue"))

  def test_calibration_errors_propagate(self) -> None:
    """What the calibration lacks raises its own error, naming what is missing."""
    with self.assertRaisesRegex(CalibrationError, "plate model 'Other'"):
      self.build(plate_model="Other")
    only_96 = dataclasses.replace(self.calibration, plate_types=self.calibration.plate_types[:1])
    with self.assertRaisesRegex(CalibrationError, "Well384"):
      self.build(calibration=only_96, plate_format="Well384", plate_model="Test 384")
    no_buffer = with_stations(self.calibration, STATIONS[1:])
    with self.assertRaisesRegex(CalibrationError, "station 'Buffer'"):
      self.build(calibration=no_buffer)
    no_deck = dataclasses.replace(
      self.calibration,
      instrument=dataclasses.replace(self.calibration.instrument, deck_height_calibrated_on=None),
    )
    with self.assertRaisesRegex(CalibrationError, "deck height"):
      self.build(calibration=no_deck)

  def test_stations_are_read_when_used(self) -> None:
    """An untaught shake station matters only when a shake happens."""
    untaught = [
      dataclasses.replace(station, calibrated_on=None) if station.name == "Water" else station
      for station in STATIONS
    ]
    calibration = with_stations(self.calibration, untaught)
    cleans = (w.RinseStep("Decon", 30), w.RinseStep("Clean", 30))
    settings = w.PlateReadSettings(shake_every=1, shake_station="Water", cleans=cleans)
    self.assertEqual(len(commands_of(self.build(settings, ["A01"], calibration))), 10)
    with self.assertRaisesRegex(CalibrationError, "station 'Water' is not calibrated"):
      self.build(settings, ["A01", "A02"], calibration)

  def test_positions_outside_the_sampler(self) -> None:
    """A station beyond the sampler's travel is refused."""
    moved = [
      dataclasses.replace(station, x=140.0) if station.name == "Buffer" else station
      for station in STATIONS
    ]
    with self.assertRaisesRegex(ValueError, "outside the sampler"):
      self.build(calibration=with_stations(self.calibration, moved))

  def test_thresholds_and_lasers(self) -> None:
    """Parameters the instrument lacks, or whose laser is off, and laser sets it lacks."""
    with self.assertRaisesRegex(ValueError, "no parameter YL1-H"):
      self.build(
        w.PlateReadSettings(
          thresholds=(w.TriggerThreshold("FSC-H", 100_000), w.TriggerThreshold("YL1-H", 5000))
        )
      )
    with self.assertRaisesRegex(ValueError, "needs the red laser"):
      self.build(
        w.PlateReadSettings(
          thresholds=(w.TriggerThreshold("FSC-H", 100_000), w.TriggerThreshold("RL1-H", 5000)),
          lasers=("blue",),
        )
      )
    with self.assertRaisesRegex(ValueError, "cannot acquire with lasers violet"):
      self.build(w.PlateReadSettings(lasers=("violet",)))

  def test_laser_sets_per_family(self) -> None:
    """Each family names its laser sets and limits its events by them."""
    cases: List[Tuple[cal.SystemKind, Optional[Tuple[w.Laser, ...]], str, int]] = [
      ("iQue3HD", None, "VioletBlueRed", 17_500_000),
      ("iQue3", ("violet", "blue"), "VioletBlue", 20_000_000),
      ("iQue3", ("blue",), "Blue", 35_000_000),
      ("iQuePlusVYB", None, "VioletYellowBlue", 17_500_000),
      ("iQuePlusVYB", ("yellow", "blue"), "YellowBlue", 28_000_000),
      ("iQuePlusVYB", ("violet", "yellow"), "VioletYellow", 21_000_000),
      ("iQuePlusVYB", ("yellow",), "Yellow", 40_000_000),
      ("iQuePlusBR", None, "BlueRed", 30_000_000),
      ("iQuePlusBR", ("blue",), "Blue", 40_000_000),
    ]
    for kind, lasers, enabled, maximum in cases:
      with self.subTest(kind=kind, lasers=lasers):
        commands = commands_of(
          self.build(w.PlateReadSettings(lasers=lasers), calibration=make_calibration(kind))
        )
        start, acquire = commands[0], commands[3]
        assert isinstance(start, c.StartMatryoshkaFluidicsCommand)
        assert isinstance(acquire, c.FlowCytometerAcquireCommand)
        self.assertEqual((start.enabled_lasers, acquire.event_maximum), (enabled, maximum))
    with self.assertRaisesRegex(ValueError, "cannot acquire"):
      self.build(w.PlateReadSettings(lasers=("blue",)), calibration=make_calibration("iQuePlusVYB"))

  def test_vyb_threshold_indices(self) -> None:
    """Threshold channels count among the pulse heights of the instrument's own family."""
    settings = w.PlateReadSettings(
      thresholds=(w.TriggerThreshold("FSC-H", 100_000), w.TriggerThreshold("YL1-H", 5000))
    )
    start = commands_of(self.build(settings, calibration=make_calibration("iQuePlusVYB")))[0]
    assert isinstance(start, c.StartMatryoshkaFluidicsCommand)
    self.assertEqual(
      start.thresholds,
      [c.Threshold(id=0, maximum=0, minimum=100_000)]
      + [c.Threshold(id=11, maximum=0, minimum=5000)],
    )

  def test_event_limits(self) -> None:
    """The event limit and maximum cannot exceed what the instrument records."""
    with self.assertRaisesRegex(ValueError, "exceeds the event maximum"):
      self.build(w.PlateReadSettings(event_limit=20_000_000))
    with self.assertRaisesRegex(ValueError, "exceeds the 17500000 events"):
      self.build(w.PlateReadSettings(event_maximum=18_000_000))
    acquire = commands_of(self.build(w.PlateReadSettings(event_maximum=1_000_000)))[3]
    assert isinstance(acquire, c.FlowCytometerAcquireCommand)
    self.assertEqual(acquire.event_maximum, 1_000_000)
    with self.assertRaisesRegex(ValueError, "exceeds the event maximum 1000000"):
      self.build(w.PlateReadSettings(event_maximum=1_000_000, event_limit=1_000_001))

  def test_shaker_speeds(self) -> None:
    """3500 rpm is beyond the BioShake 3000 but within the HD model's BioShake 5000."""
    settings = w.PlateReadSettings(shake_speed=3500, shake_every=1)
    with self.assertRaisesRegex(ValueError, "200 to 3000 rpm"):
      self.build(settings)
    hd = commands_of(self.build(settings, calibration=make_calibration("iQue3HD")))
    shake = hd[5]
    assert isinstance(shake, c.CombinedShakeRinseAtLocationCommand)
    self.assertEqual(shake.rpm, 3500)
    with self.assertRaisesRegex(ValueError, "pre_plate|200 to 3000 rpm"):
      self.build(w.PlateReadSettings(pre_plate_shake=True, pre_plate_shake_speed=4000))
    hd_default = commands_of(
      self.build(w.PlateReadSettings(shake_every=1), calibration=make_calibration("iQue3HD"))
    )[5]
    assert isinstance(hd_default, c.CombinedShakeRinseAtLocationCommand)
    self.assertEqual(hd_default.rpm, 2000)

  def test_intervals_beyond_the_plate(self) -> None:
    """Shaking or rinsing after more wells than the plate has is refused."""
    with self.assertRaisesRegex(ValueError, "shake_every 97 exceeds the 96 wells"):
      self.build(w.PlateReadSettings(shake_every=97))
    with self.assertRaisesRegex(ValueError, "rinse_every 97 exceeds"):
      self.build(w.PlateReadSettings(rinse_every=97))


class WellOrderTests(unittest.TestCase):
  """The standard sampling orders."""

  def test_row(self) -> None:
    """Row by row, left to right."""
    order = w.well_order("Well96", "row")
    self.assertEqual(len(order), 96)
    self.assertEqual(order[:13], [f"A{n:02d}" for n in range(1, 13)] + ["B01"])
    self.assertEqual(order[-1], "H12")

  def test_serpentine_row(self) -> None:
    """Row B runs right to left."""
    order = w.well_order("Well96", "serpentine_row")
    self.assertEqual(order[10:15], ["A11", "A12", "B12", "B11", "B10"])
    self.assertEqual(order[23:25], ["B01", "C01"])
    self.assertEqual(order[-1], "H01")

  def test_column(self) -> None:
    """Column by column, top to bottom."""
    order = w.well_order("Well96", "column")
    self.assertEqual(order[:9], ["A01", "B01", "C01", "D01", "E01", "F01", "G01", "H01", "A02"])

  def test_serpentine_column(self) -> None:
    """Column 2 runs bottom to top."""
    order = w.well_order("Well96", "serpentine_column")
    self.assertEqual(order[6:11], ["G01", "H01", "H02", "G02", "F02"])
    self.assertEqual(order[-1], "A12")

  def test_subset(self) -> None:
    """A subset keeps the plate's directions; spellings are normalised."""
    order = w.well_order("Well96", "serpentine_row", ["B1", "A01", "B12", "A12"])
    self.assertEqual(order, ["A01", "A12", "B12", "B01"])

  def test_larger_plates(self) -> None:
    """384 and 1536 wells, with two-letter rows on 1536-well plates."""
    self.assertEqual(len(w.well_order("Well384", "column")), 384)
    order = w.well_order("Well1536", "row")
    self.assertEqual(len(order), 1536)
    self.assertEqual(order[26 * 48], "AA01")
    self.assertEqual(order[-1], "AF48")

  def test_refusals(self) -> None:
    """Unknown orders, duplicate or foreign wells are refused."""
    with self.assertRaisesRegex(ValueError, "order must be one of"):
      w.well_order("Well96", "random")  # type: ignore[arg-type]
    with self.assertRaisesRegex(ValueError, "appears twice"):
      w.well_order("Well96", "row", ["A01", "A1"])
    with self.assertRaisesRegex(ValueError, "not on a Well96 plate"):
      w.well_order("Well96", "row", ["A13"])
    with self.assertRaisesRegex(ValueError, "not a plate format"):
      w.well_order("Well48", "row")  # type: ignore[arg-type]


class PlateReadSettingsTests(unittest.TestCase):
  """Validation of plate-read settings."""

  def test_defaults(self) -> None:
    """The instrument's defaults."""
    settings = w.PlateReadSettings()
    self.assertEqual((settings.sip_time, settings.up_time, settings.pump_speed), (1.0, 0.5, 29.0))
    self.assertEqual(
      (settings.shake_mode, settings.shake_every, settings.shake_duration),
      (
        "after_wells",
        12,
        4.0,
      ),
    )
    self.assertEqual((settings.shake_station, settings.flush_duration), ("Buffer", 30.0))
    self.assertEqual(
      settings.cleans,
      (w.RinseStep("Decon", 30.0), w.RinseStep("Clean", 30.0), w.RinseStep("Water", 60.0)),
    )
    self.assertEqual(settings.thresholds, (w.TriggerThreshold("FSC-H", 100_000),))
    self.assertFalse(settings.pre_plate_prime or settings.pre_plate_shake or settings.probe_rinse)
    self.assertTrue(settings.post_plate_clean)
    self.assertIsNone(settings.lasers)
    self.assertIsNone(settings.event_limit)

  def test_out_of_range(self) -> None:
    """Values outside the instrument's ranges, or not in its units, are refused."""
    cases: List[Dict[str, object]] = [
      {"sip_time": 0.4},
      {"sip_time": 60.0},
      {"sip_time": 1.0005},
      {"sip_time": "1"},
      {"up_time": -0.1},
      {"pump_speed": 9.9},
      {"pump_speed": 32.5},
      {"pump_speed": 29.005},
      {"sampling_height": -0.1},
      {"pre_plate_prime_duration": 4},
      {"pre_plate_prime_duration": 60.5},
      {"pre_plate_shake_duration": 181},
      {"pre_plate_shake_speed": 199},
      {"shake_mode": "sometimes"},
      {"shake_every": 0},
      {"shake_every": 2.0},
      {"shake_duration": 0},
      {"shake_duration": 4.5},
      {"shake_speed": 5001},
      {"shake_station": "Markers"},
      {"rinse_every": 0},
      {"probe_rinse": True},
      {"rinses": (w.RinseStep("Water", 0.4),)},
      {"rinses": (w.RinseStep("Water", 60.5),)},
      {"rinses": [w.RinseStep("Water", 1.0)]},
      {"flush_duration": 29},
      {"flush_duration": 601},
      {"flush_duration": 30.5},
      {"cleans": ()},
      {"cleans": (w.RinseStep("Water", 4),)},
      {"cleans": (w.RinseStep("Water", 30.5),)},
      {"thresholds": ()},
      {"thresholds": [w.TriggerThreshold("FSC-H", 100_000)]},
      {"thresholds": (w.TriggerThreshold("BL1-H", 100_000),)},
      {"thresholds": (w.TriggerThreshold("FSC-H", 1000), w.TriggerThreshold("FSC-H", 2000))},
      {"thresholds": (w.TriggerThreshold("FSC-H", 1000),) * 3},
      {"lasers": ()},
      {"lasers": ("green",)},
      {"lasers": ("blue", "blue")},
      {"event_limit": 0},
      {"event_limit": 40_000_001},
      {"event_maximum": 0},
    ]
    for case in cases:
      with self.subTest(case=case), self.assertRaises(ValueError):
        w.PlateReadSettings(**case)  # type: ignore[arg-type]

  def test_steps(self) -> None:
    """Rinse steps need a wash station, triggers a pulse height in range."""
    with self.assertRaisesRegex(ValueError, "station"):
      w.RinseStep("Tube", 1.0)  # type: ignore[arg-type]
    for parameter, value in (("FSC-H", 999), ("FSC-H", 16_700_001), ("FSC", 1000)):
      with self.subTest(parameter=parameter, value=value), self.assertRaises(ValueError):
        w.TriggerThreshold(parameter, value)

  def test_flush_must_clear_the_line(self) -> None:
    """Flush and clean together clear the line: 60 s at 29 rpm, 90 s at 19.5 rpm."""
    with self.assertRaisesRegex(ValueError, "must last 60 s"):
      w.PlateReadSettings(post_plate_clean=False)
    w.PlateReadSettings(post_plate_clean=False, flush_duration=60)
    cleans = (w.RinseStep("Water", 45),)
    with self.assertRaisesRegex(ValueError, "must last 90 s"):
      w.PlateReadSettings(pump_speed=19.5, cleans=cleans, flush_duration=44)
    w.PlateReadSettings(pump_speed=19.5, cleans=cleans, flush_duration=45)
    with self.assertRaisesRegex(ValueError, "must last 150 s"):
      w.PlateReadSettings(pump_speed=10.0, post_plate_clean=False, flush_duration=145)
    w.PlateReadSettings(pump_speed=10.0, flush_duration=30)

  def test_positional_arguments_refused(self) -> None:
    """Settings are given by name."""
    with self.assertRaises(TypeError):
      w.PlateReadSettings((w.TriggerThreshold("FSC-H", 1000),))  # type: ignore[misc]


class TubeReadTests(_WorklistTestCase):
  """Reads of the microcentrifuge tube."""

  def setUp(self) -> None:
    """Build a tube read with defaults."""
    self.worklist = w.tube_read_worklist(make_calibration(), w.TubeReadSettings())
    self.commands = commands_of(self.worklist)

  def test_command_sequence(self) -> None:
    """Tube check, fluidics, prime, pump, acquire, sample, flush, clean, stops."""
    self.assertEqual(
      kinds(self.worklist),
      [
        "ValidateDeckHeightCommand",
        "StartFlowCytometerFluidicsCommand",
        "SampleContinuouslyCommand",
        "StartPumpCommand",
        "FlowCytometerAcquireCommand",
        "SampleVialCommand",
        "TimedContinuousRinseCommand",
        "FlowCytometerStopAcquireCommand",
        "StartPumpCommand",
        "TimedCyclingRinseCommand",
        "TimedCyclingRinseCommand",
        "TimedCyclingRinseCommand",
        "StopPumpCommand",
        "StopFlowCytometerFluidicsCommand",
      ],
    )
    self.assertEqual(self.worklist.name, "Tube read")
    self.assertIsNotNone(self.worklist.experiment_id)
    self.assertIsNone(self.worklist.plate_id)
    self.assert_fresh(self.worklist)

  def test_tube_check_and_sample(self) -> None:
    """The tube is felt for 0.2 to 10 mm above the holder and sampled 2 mm above it."""
    check = self.commands[0]
    assert isinstance(check, c.ValidateDeckHeightCommand)
    self.assertTrue(check.is_microfuge_tube_check)
    self.assertEqual(
      [str(value) for value in (check.x, check.y, check.z_high, check.z_low, check.z_tolerance)],
      ["0.500", "13.500", "250.000", "203.350", "10.000"],
    )
    sample = self.commands[5]
    assert isinstance(sample, c.SampleVialCommand)
    self.assert_spot(sample.x, sample.y, sample.z_down, ("0.500", "13.500", "205.350"))
    self.assertEqual((sample.sip_time, sample.up_time, sample.fast_move), (10_000, 0, False))
    self.assertEqual((sample.row, sample.column, sample.well_type), ("A", 1, "Sample"))
    self.assertEqual((sample.event_threshold, str(sample.z_tolerance)), (2_147_483_647, "10.000"))
    self.assertEqual((sample.countdown_duration, sample.estimated_duration), (10, seconds(11.4)))

  def test_fluidics_prime_and_acquire(self) -> None:
    """Fluidics ready for acquisition; a 25 s prime at 53 rpm; recording after 39 s."""
    start = self.commands[1]
    assert isinstance(start, c.StartFlowCytometerFluidicsCommand)
    self.assertTrue(start.prepare_for_acquisition)
    self.assertEqual(start.thresholds, [c.Threshold(id=0, maximum=0, minimum=100_000)])
    self.assertEqual(start.enabled_lasers, "VioletBlueRed")
    prime = self.commands[2]
    assert isinstance(prime, c.SampleContinuouslyCommand)
    self.assertEqual((prime.pump_speed, prime.pump_direction, prime.sip_time), (5300, "CW", 25_000))
    self.assert_spot(prime.x, prime.y, prime.z_low, ("27.250", "1.500", "208.350"))
    self.assertEqual((str(prime.z_high), prime.fast_move), ("250.000", False))
    self.assertEqual(prime.estimated_duration, seconds(31))
    pump = self.commands[3]
    assert isinstance(pump, c.StartPumpCommand)
    self.assertEqual(pump.speed, 2900)
    acquire = self.commands[4]
    assert isinstance(acquire, c.FlowCytometerAcquireCommand)
    self.assertEqual((acquire.acquistion_delay, acquire.event_maximum), (39, 17_500_000))
    # No flush command of the plate kind: the acquisition time runs to the end.
    self.assertEqual(acquire.estimated_acquisition_time, seconds(191.6))

  def test_flush_and_clean(self) -> None:
    """A 44 s flush with the probe down, then cleans at 53 rpm with slow moves."""
    flush = self.commands[6]
    assert isinstance(flush, c.TimedContinuousRinseCommand)
    self.assertEqual((flush.rinse_duration, flush.sample_speed, flush.up_time), (44, "Fast", 0))
    self.assert_spot(flush.x, flush.y, flush.z_down, ("27.250", "1.500", "206.350"))
    self.assertEqual((flush.rinse_station_pump, flush.estimated_duration), ("S1", seconds(45.8)))
    raise_pump = self.commands[8]
    assert isinstance(raise_pump, c.StartPumpCommand)
    self.assertEqual(raise_pump.speed, 5300)
    for clean, station in zip(self.commands[9:12], ("Decon", "Clean", "Water")):
      assert isinstance(clean, c.TimedCyclingRinseCommand)
      self.assertEqual(clean.rinse_station_name, station)
      self.assertEqual((clean.sip_time, clean.up_time, clean.fast_move), (1000, 500, False))

  def test_estimated_duration(self) -> None:
    """6.35 + 8 + 31 + 1 + 1 + 11.4 + 45.8 + 1 + 1 + 125.4 + 1 + 5 s."""
    self.assertEqual(w.estimated_duration(self.worklist), seconds(237.95))

  def test_without_clean_and_slow(self) -> None:
    """Without the clean; at 19.5 rpm the flush takes 66 s and the delay 60 s."""
    settings = w.TubeReadSettings(pump_speed=19.5, post_tube_clean=False, sip_time=30)
    worklist = w.tube_read_worklist(make_calibration(), settings, name="Sample 7")
    self.assertEqual(
      kinds(worklist),
      [
        "ValidateDeckHeightCommand",
        "StartFlowCytometerFluidicsCommand",
        "SampleContinuouslyCommand",
        "StartPumpCommand",
        "FlowCytometerAcquireCommand",
        "SampleVialCommand",
        "TimedContinuousRinseCommand",
        "StopPumpCommand",
        "StopFlowCytometerFluidicsCommand",
      ],
    )
    commands = commands_of(worklist)
    acquire, flush = commands[4], commands[6]
    assert isinstance(acquire, c.FlowCytometerAcquireCommand)
    assert isinstance(flush, c.TimedContinuousRinseCommand)
    self.assertEqual((acquire.acquistion_delay, flush.rinse_duration), (60, 66))

  def test_settings(self) -> None:
    """Sip times from 5 s to the time to draw 320 uL: 180 s at 29 rpm, 510 s at 10, 160 s at 32."""
    w.TubeReadSettings(sip_time=180)
    w.TubeReadSettings(sip_time=510, pump_speed=10)
    w.TubeReadSettings(sip_time=160, pump_speed=32)
    for case in (
      {"sip_time": 4.9},
      {"sip_time": 180.001},
      {"sip_time": 511, "pump_speed": 10},
      {"sip_time": 161, "pump_speed": 32},
      {"station": "Tube"},
      {"cleans": ()},
      {"pump_speed": 33},
    ):
      with self.subTest(case=case), self.assertRaises(ValueError):
        w.TubeReadSettings(**case)  # type: ignore[arg-type]

  def test_needs_the_tube_holder(self) -> None:
    """A calibration without the tube holder is refused."""
    with self.assertRaisesRegex(CalibrationError, "station 'Tube'"):
      w.tube_read_worklist(with_stations(make_calibration(), STATIONS[:4]), w.TubeReadSettings())
    with self.assertRaisesRegex(ValueError, "prime"):
      w.tube_read_worklist(make_calibration(), w.TubeReadSettings(), name="Prime")

  def test_wire_round_trip(self) -> None:
    """The worklist survives being written to and read from the wire unchanged."""
    self.assertEqual(round_trip(self.worklist), self.worklist)


class CleanTestTests(_WorklistTestCase):
  """The cleanliness QC test."""

  def test_commands(self) -> None:
    """Record while drawing water for 60 s at 29 rpm, then flush."""
    worklist = w.qc_clean_test_worklist(make_calibration(), w.TubeReadSettings())
    self.assertEqual(
      kinds(worklist),
      [
        "StartFlowCytometerFluidicsCommand",
        "StartPumpCommand",
        "FlowCytometerAcquireCommand",
        "SampleContinuouslyCommand",
        "StartPumpCommand",
        "TimedContinuousRinseCommand",
        "StopPumpCommand",
        "StopFlowCytometerFluidicsCommand",
      ],
    )
    commands = commands_of(worklist)
    acquire, draw, flush = commands[2], commands[3], commands[5]
    assert isinstance(acquire, c.FlowCytometerAcquireCommand)
    assert isinstance(draw, c.SampleContinuouslyCommand)
    assert isinstance(flush, c.TimedContinuousRinseCommand)
    self.assertEqual(acquire.acquistion_delay, 39)
    self.assertEqual((draw.pump_speed, draw.sip_time), (2900, 60_000))
    self.assert_spot(draw.x, draw.y, draw.z_low, ("109.650", "1.500", "208.250"))
    self.assertEqual((flush.rinse_station_name, flush.rinse_duration), ("Buffer", 44))
    self.assertEqual(acquire.estimated_acquisition_time, seconds(119.8))
    self.assertEqual(w.estimated_duration(worklist), seconds(128.8))
    self.assert_fresh(worklist)
    self.assertEqual(round_trip(worklist), worklist)


class MaintenanceTests(_WorklistTestCase):
  """The maintenance routines."""

  def setUp(self) -> None:
    """Make the calibration."""
    self.calibration = make_calibration()

  def assert_maintenance_fluidics(self, command: c.WorklistCommand) -> None:
    """Assert the start of fluidics without acquisition."""
    assert isinstance(command, c.StartFlowCytometerFluidicsCommand)
    self.assertFalse(command.prepare_for_acquisition)
    self.assertEqual((command.thresholds, command.fcs_metadata), ([], []))
    self.assertEqual((command.user_name, command.enabled_lasers), ("UNK", "VioletBlueRed"))

  def test_prime(self) -> None:
    """An hour of dipping in Buffer at the controller's default pump speed, named Prime."""
    worklist = w.prime_worklist(self.calibration)
    self.assertEqual(worklist.name, "Prime")
    self.assertEqual(
      kinds(worklist),
      ["StartFlowCytometerFluidicsCommand", "StartPumpCommand", "TimedCyclingRinseCommand"],
    )
    commands = commands_of(worklist)
    self.assert_maintenance_fluidics(commands[0])
    pump, prime = commands[1], commands[2]
    assert isinstance(pump, c.StartPumpCommand)
    assert isinstance(prime, c.TimedCyclingRinseCommand)
    self.assertEqual((pump.speed, pump.direction), (2860, "CW"))
    self.assertEqual((prime.rinse_duration, prime.sip_time, prime.up_time), (3600, 1000, 1000))
    self.assert_spot(prime.x, prime.y, prime.z_down, ("27.250", "1.500", "206.350"))
    self.assertEqual((worklist.devices_to_monitor, worklist.experiment_id), ([], None))
    self.assertEqual(w.estimated_duration(worklist), seconds(3610.8))
    short = w.prime_worklist(
      self.calibration, duration=60, pump_speed=29, sip_time=1.5, up_time=0.5
    )
    short_prime = commands_of(short)[2]
    assert isinstance(short_prime, c.TimedCyclingRinseCommand)
    self.assertEqual((short_prime.rinse_duration, short_prime.sip_time), (60, 1500))
    for kwargs in ({"duration": 0}, {"duration": 3601}, {"pump_speed": 54}, {"up_time": 0.4}):
      with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
        w.prime_worklist(self.calibration, **kwargs)  # type: ignore[arg-type]

  def test_probe_and_tubing_clean(self) -> None:
    """5 min in Decon, 5 in Clean and 10 in Water of a 20 minute clean."""
    worklist = w.probe_and_tubing_clean_worklist(self.calibration)
    self.assertEqual(
      kinds(worklist),
      [
        "StartFlowCytometerFluidicsCommand",
        "StartPumpCommand",
        "TimedCyclingRinseCommand",
        "TimedCyclingRinseCommand",
        "TimedCyclingRinseCommand",
        "StopPumpCommand",
        "StopFlowCytometerFluidicsCommand",
      ],
    )
    commands = commands_of(worklist)
    self.assert_maintenance_fluidics(commands[0])
    rinses = commands[2:5]
    expected = [("Decon", 300, "206.550"), ("Clean", 300, "206.450"), ("Water", 600, "206.250")]
    for rinse, (station, duration, depth) in zip(rinses, expected):
      assert isinstance(rinse, c.TimedCyclingRinseCommand)
      self.assertEqual((rinse.rinse_station_name, rinse.rinse_duration), (station, duration))
      self.assertEqual((rinse.sip_time, rinse.up_time, rinse.fast_move), (1000, 500, True))
      self.assertEqual(str(rinse.z_down), depth)
    self.assertEqual(worklist.devices_to_monitor, list(w.MAINTENANCE_DEVICES))
    self.assertNotIn("Shaker", worklist.devices_to_monitor or [])
    self.assertEqual(w.estimated_duration(worklist), seconds(1220.4))
    eight = commands_of(w.probe_and_tubing_clean_worklist(self.calibration, 480))
    self.assertEqual(
      [
        command.rinse_duration
        for command in eight[2:5]
        if isinstance(command, c.TimedCyclingRinseCommand)
      ],
      [120, 120, 240],
    )
    for bad in (500.0, 120.0, 2160.0):
      with self.subTest(duration=bad), self.assertRaises(ValueError):
        w.probe_and_tubing_clean_worklist(self.calibration, bad)
    no_decon = with_stations(self.calibration, [s for s in STATIONS if s.name != "Decon"])
    with self.assertRaisesRegex(CalibrationError, "station 'Decon'"):
      w.probe_and_tubing_clean_worklist(no_decon)

  def test_startup(self) -> None:
    """The start-up worklist is the 20 minute probe and tubing clean."""
    worklist = w.startup_worklist(self.calibration)
    self.assertEqual(kinds(worklist), kinds(w.probe_and_tubing_clean_worklist(self.calibration)))
    self.assertEqual(worklist.devices_to_monitor, list(w.MAINTENANCE_DEVICES))
    self.assertEqual(w.estimated_duration(worklist), seconds(1220.4))

  def test_shutdown(self) -> None:
    """The clean, then the cytometer shut down; the cytometer is not monitored."""
    worklist = w.shutdown_worklist(self.calibration, clean_duration=240)
    self.assertEqual(kinds(worklist)[-1], "ShutdownFlowCytometerCommand")
    self.assertEqual(
      kinds(worklist)[:-1], kinds(w.probe_and_tubing_clean_worklist(self.calibration))
    )
    self.assertEqual(worklist.devices_to_monitor, ["Sampler", "Pump", "PlateRail", "ContactSensor"])
    self.assertEqual(commands_of(worklist)[-1].category, "FlowCytometer")
    self.assertEqual(
      w.estimated_duration(worklist), seconds(8 + 1 + 61.8 + 61.8 + 121.8 + 1 + 5 + 1)
    )

  def test_detector_cleans(self) -> None:
    """One cytometer routine each; the fluidics are left running."""
    cases = [
      (w.detector_quick_clean_worklist(), "QuickClean", 660),
      (w.detector_long_clean_worklist(), "LongClean", 1800),
      (w.detector_unclog_worklist(), "Unclog", 630),
      (w.detector_debubble_worklist(), "Priming", 780),
    ]
    for worklist, method, duration in cases:
      with self.subTest(method=method):
        (command,) = commands_of(worklist)
        assert isinstance(command, c.CleanFlowCytometerCommand)
        self.assertEqual(command.clean_method, method)
        self.assertTrue(worklist.leave_fluidics_running_on_complete)
        self.assertEqual(worklist.devices_to_monitor, list(w.MAINTENANCE_DEVICES))
        self.assertEqual(w.estimated_duration(worklist), seconds(duration))
        self.assert_fresh(worklist)

  def test_fluidics_check(self) -> None:
    """A minute in Water, 59 s down and 2 s up."""
    worklist = w.fluidics_check_worklist(self.calibration)
    self.assertEqual(
      kinds(worklist),
      [
        "StartFlowCytometerFluidicsCommand",
        "StartPumpCommand",
        "TimedCyclingRinseCommand",
        "StopPumpCommand",
        "StopFlowCytometerFluidicsCommand",
      ],
    )
    rinse = commands_of(worklist)[2]
    assert isinstance(rinse, c.TimedCyclingRinseCommand)
    self.assertEqual((rinse.rinse_station_name, rinse.rinse_duration), ("Water", 60))
    self.assertEqual((rinse.sip_time, rinse.up_time), (59_000, 2000))
    self.assertEqual(w.estimated_duration(worklist), seconds(76.8))

  def test_deck_height_validation(self) -> None:
    """A single deck check at the test point."""
    worklist = w.deck_height_validation_worklist(self.calibration, tolerance=0.2)
    (check,) = commands_of(worklist)
    assert isinstance(check, c.ValidateDeckHeightCommand)
    self.assertEqual(
      [str(value) for value in (check.x, check.y, check.z_high, check.z_low, check.z_tolerance)],
      ["3.200", "104.850", "250.000", "201.350", "0.200"],
    )
    self.assertEqual(w.estimated_duration(worklist), seconds(6.35))
    with self.assertRaisesRegex(ValueError, "positive"):
      w.deck_height_validation_worklist(self.calibration, tolerance=0)

  def test_wire_round_trip(self) -> None:
    """Every maintenance worklist survives the wire unchanged."""
    worklists = [
      w.prime_worklist(self.calibration),
      w.probe_and_tubing_clean_worklist(self.calibration),
      w.shutdown_worklist(self.calibration),
      w.detector_quick_clean_worklist(),
      w.fluidics_check_worklist(self.calibration),
      w.deck_height_validation_worklist(self.calibration),
    ]
    for worklist in worklists:
      with self.subTest(name=worklist.name):
        self.assert_fresh(worklist)
        self.assertEqual(round_trip(worklist), worklist)


ORIGIN = datetime.datetime(2026, 11, 11, 9, 30, tzinfo=UTC)


def finish(
  worklist: c.Worklist,
  skipped: Sequence[int] = (),
  stopped_at: Optional[int] = None,
) -> c.Worklist:
  """Return a worklist as the controller hands it back: command i starts 10 i s after the first.

  Every command takes 2.5 s. Commands in ``skipped`` were passed over by a skip. A command at
  ``stopped_at`` was cancelled while running, and nothing after it ran.
  """
  finished: List[Optional[c.WorklistCommand]] = []
  for index, command in enumerate(commands_of(worklist)):
    start = ORIGIN + datetime.timedelta(seconds=10 * index)
    end = start + datetime.timedelta(seconds=2.5)
    if index in skipped:
      command = dataclasses.replace(command, execution_notes="Skipped")
    elif stopped_at is not None and index == stopped_at:
      command = dataclasses.replace(command, start_time=start)
    elif stopped_at is None or index < stopped_at:
      command = dataclasses.replace(command, start_time=start, end_time=end, completed=True)
    finished.append(command)
  return dataclasses.replace(
    worklist,
    commands=finished,
    start_time=ORIGIN,
    end_time=ORIGIN + datetime.timedelta(seconds=10 * len(finished)),
    all_commands_completed=stopped_at is None,
    stop_reason="Unspecified" if stopped_at is None else "Cancelled",
  )


def sample_at(commands: Sequence[c.WorklistCommand], index: int) -> c.SampleWellCommand:
  """Return the command at an index, which must sample a well."""
  command = commands[index]
  assert isinstance(command, c.SampleWellCommand)
  return command


class SampledWellsTests(unittest.TestCase):
  """Reading the sampled wells back from a finished plate read."""

  def setUp(self) -> None:
    """Build a six-well read with a shake after the third well.

    Its commands: 0 fluidics, 1 deck check, 2 pump, 3 acquire, 4-6 A01-A03, 7 shake, 8-10
    A04-A06, 11 flush, 12-14 cleans, 15 pump off, 16 fluidics off.
    """
    self.worklist = w.plate_read_worklist(
      make_calibration(),
      "Well96",
      ["A01", "A02", "A03", "A04", "A05", "A06"],
      w.PlateReadSettings(shake_every=3),
      plate_model="Test 96",
    )
    self.assertEqual(kinds(self.worklist)[7], "CombinedShakeRinseAtLocationCommand")

  def test_complete_run(self) -> None:
    """Every well, timed from the first command's start; the well after the shake is marked."""
    self.assertEqual(
      w.sampled_wells(finish(self.worklist)),
      [
        SampledWell("A01", 40.0, 42.5, False),
        SampledWell("A02", 50.0, 52.5, False),
        SampledWell("A03", 60.0, 62.5, False),
        SampledWell("A04", 80.0, 82.5, True),
        SampledWell("A05", 90.0, 92.5, False),
        SampledWell("A06", 100.0, 102.5, False),
      ],
    )

  def test_wells_named_by_row_and_column(self) -> None:
    """Descriptions are not read; the row and column fields name the well."""
    commands = commands_of(finish(self.worklist))
    commands[5] = dataclasses.replace(sample_at(commands, 5), description="other", row="H")
    commands[6] = dataclasses.replace(sample_at(commands, 6), description=None, column=12)
    wells = w.sampled_wells(dataclasses.replace(self.worklist, commands=list(commands)))
    self.assertEqual([well.well for well in wells], ["A01", "H02", "A12", "A04", "A05", "A06"])

  def test_empty_and_rinse_wells(self) -> None:
    """Wells of type Empty or Rinse are not sampled wells and break the run."""
    for well_type in ("Empty", "Rinse"):
      with self.subTest(well_type=well_type):
        commands = commands_of(finish(self.worklist))
        commands[9] = dataclasses.replace(sample_at(commands, 9), well_type=well_type)
        wells = w.sampled_wells(dataclasses.replace(self.worklist, commands=list(commands)))
        self.assertEqual([well.well for well in wells], ["A01", "A02", "A03", "A04", "A06"])
        self.assertEqual([well.after_pause for well in wells], [False, False, False, True, True])

  def test_skipped_well(self) -> None:
    """A skipped well makes the run incomplete; allowed, the next well is marked."""
    finished = finish(self.worklist, skipped=[9])
    with self.assertRaises(w.IncompleteWorklistError) as raised:
      w.sampled_wells(finished)
    message = str(raised.exception)
    self.assertIn("command 10 of 17", message)
    self.assertIn("SampleWellCommand", message)
    self.assertIn("was skipped", message)
    wells = w.sampled_wells(finished, allow_incomplete=True)
    self.assertEqual(
      wells,
      [
        SampledWell("A01", 40.0, 42.5, False),
        SampledWell("A02", 50.0, 52.5, False),
        SampledWell("A03", 60.0, 62.5, False),
        SampledWell("A04", 80.0, 82.5, True),
        SampledWell("A06", 100.0, 102.5, True),
      ],
    )

  def test_cancelled_run(self) -> None:
    """A run cancelled while sampling A05 names that command; allowed, A01 to A04 remain."""
    finished = finish(self.worklist, stopped_at=9)
    with self.assertRaisesRegex(
      w.IncompleteWorklistError, "command 10 of 17.*started but did not finish.*Cancelled"
    ):
      w.sampled_wells(finished)
    wells = w.sampled_wells(finished, allow_incomplete=True)
    self.assertEqual([well.well for well in wells], ["A01", "A02", "A03", "A04"])

  def test_error_is_reported(self) -> None:
    """The controller's error text is part of the message."""
    finished = dataclasses.replace(
      finish(self.worklist, stopped_at=1), stop_reason="WorklistError", error="Contact not found"
    )
    with self.assertRaisesRegex(w.IncompleteWorklistError, "Contact not found"):
      w.sampled_wells(finished)
    self.assertEqual(w.sampled_wells(finished, allow_incomplete=True), [])

  def test_nothing_ran(self) -> None:
    """A run that never started has no wells."""
    finished = finish(self.worklist, stopped_at=0)
    no_start = commands_of(finished)
    no_start[0] = dataclasses.replace(no_start[0], start_time=w.NOT_RUN)
    run = dataclasses.replace(finished, commands=list(no_start))
    self.assertEqual(w.sampled_wells(run, allow_incomplete=True), [])
    with self.assertRaisesRegex(w.IncompleteWorklistError, "command 1 of 17.*did not run"):
      w.sampled_wells(run)

  def test_tube_read_has_no_plate_wells(self) -> None:
    """The tube sample is not a plate well."""
    tube = finish(w.tube_read_worklist(make_calibration(), w.TubeReadSettings()))
    self.assertEqual(w.sampled_wells(tube), [])

  def test_malformed(self) -> None:
    """Missing commands are refused."""
    with self.assertRaisesRegex(ValueError, "no commands"):
      w.sampled_wells(dataclasses.replace(self.worklist, commands=None))
    with self.assertRaisesRegex(ValueError, "command 2 .* is missing"):
      w.sampled_wells(
        dataclasses.replace(self.worklist, commands=[commands_of(self.worklist)[0], None])
      )


class FlushTargetTests(unittest.TestCase):
  """The command to skip to when a read is stopped early."""

  def test_plate_read(self) -> None:
    """The flush, until it has run or while it runs."""
    worklist = w.plate_read_worklist(
      make_calibration(), "Well96", ["A01"], w.PlateReadSettings(), plate_model="Test 96"
    )
    commands = commands_of(worklist)
    flush = commands[5]
    self.assertIsInstance(flush, c.FlushMatryoshkaCommand)
    self.assertIs(w.flush_target(worklist), flush)
    self.assertIsNone(w.flush_target(worklist, running=flush.id))
    commands[5] = dataclasses.replace(flush, completed=True)
    self.assertIsNone(w.flush_target(dataclasses.replace(worklist, commands=list(commands))))

  def test_continuous_shake(self) -> None:
    """Stopping the shaker first, then the flush once the shaker has stopped."""
    worklist = w.plate_read_worklist(
      make_calibration(),
      "Well96",
      ["A01"],
      w.PlateReadSettings(shake_mode="continuous"),
      plate_model="Test 96",
    )
    commands = commands_of(worklist)
    stop = w.flush_target(worklist)
    self.assertIsInstance(stop, c.StopShakerCommand)
    commands[6] = dataclasses.replace(commands[6], completed=True)
    flush = w.flush_target(dataclasses.replace(worklist, commands=list(commands)))
    self.assertIsInstance(flush, c.FlushMatryoshkaCommand)

  def test_maintenance(self) -> None:
    """A clean has nothing to skip to."""
    self.assertIsNone(w.flush_target(w.probe_and_tubing_clean_worklist(make_calibration())))


if __name__ == "__main__":
  unittest.main()
