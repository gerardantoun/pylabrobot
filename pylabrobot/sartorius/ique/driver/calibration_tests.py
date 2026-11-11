"""Tests of the calibration data model, file format and coordinate formulae."""

import dataclasses
import datetime
import io
import unittest
import zipfile
from typing import Optional
from unittest import mock

from pylabrobot.sartorius.ique.driver import calibration as cal
from pylabrobot.sartorius.ique.driver.calibration import (
  XY,
  Calibration,
  CalibrationError,
  CalibrationFormatError,
  ConsumablesOdometer,
  Corners,
  DepthMap,
  DetectorConfiguration,
  Instrument,
  PlateModel,
  PlateTypeCalibration,
  Station,
)

ROOT_OPEN = (
  '<CalibrationContainer4 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
  'xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
)

MAP_DOCUMENT = """<?xml version="1.0" encoding="utf-16"?>
<PlateMappingState xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" \
xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <Points>
    <int>0</int>
    <int>4</int>
    <int>8</int>
    <int>11</int>
    <int>48</int>
    <int>52</int>
    <int>56</int>
    <int>59</int>
    <int>84</int>
    <int>88</int>
    <int>92</int>
    <int>95</int>
  </Points>
  <Values>
    <decimal>3.95</decimal>
    <decimal>3.9</decimal>
    <decimal>3.9</decimal>
    <decimal>3.95</decimal>
    <decimal>3.85</decimal>
    <decimal>3.8</decimal>
    <decimal>3.85</decimal>
    <decimal>3.9</decimal>
    <decimal>3.9</decimal>
    <decimal>3.85</decimal>
    <decimal>3.9</decimal>
    <decimal>3.95</decimal>
  </Values>
  <PlateType>Well96</PlateType>
  <InterpolationPoints>12</InterpolationPoints>
  <LastCalibratedOn>2021-06-01T15:02:11.4170213+02:00</LastCalibratedOn>
</PlateMappingState>"""


def escaped(text: str) -> str:
  """Escape a document for use as the text of an element."""
  return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def plate_type_item(plate_type: str, corners: str, date: Optional[str]) -> str:
  """Return one plate type entry of the example document; corners are eight numbers."""
  names = "TopLeftX TopLeftY TopRightX TopRightY BottomLeftX BottomLeftY BottomRightX BottomRightY"
  lines = ["    <PlateTypeCalibrationContainer3>", f"      <PlateType>{plate_type}</PlateType>"]
  lines += [
    f"      <{name}>{value}</{name}>" for name, value in zip(names.split(), corners.split())
  ]
  if date is None:
    lines.append('      <LastCalibratedOn xsi:nil="true" />')
  else:
    lines.append(f"      <LastCalibratedOn>{date}</LastCalibratedOn>")
  lines.append("    </PlateTypeCalibrationContainer3>")
  return "\n".join(lines)


def station_item(name: str, x: str, y: str, depth: str) -> str:
  """Return one station entry of the example document."""
  return "\n".join(
    [
      "    <RinseStationCalibrationContainer2>",
      f"      <RinseStationName>{name}</RinseStationName>",
      f"      <XPos>{x}</XPos>",
      f"      <YPos>{y}</YPos>",
      f"      <ZLow>{depth}</ZLow>",
      "      <LastCalibratedOn>2021-06-01T14:31:40</LastCalibratedOn>",
      "    </RinseStationCalibrationContainer2>",
    ]
  )


ZEROS = "0.000 0.000 0.000 0.000 0.000 0.000 0.000 0.000"

EXAMPLE = "\n".join(
  [
    '<?xml version="1.0" encoding="utf-8"?>',
    ROOT_OPEN,
    "  <HyperCyt>",
    "    <HasShaker>true</HasShaker>",
    "    <PumpLocation>Right</PumpLocation>",
    "    <ZHigh>250.000</ZHigh>",
    '    <ZHighLastCalibratedOn xsi:nil="true" />',
    "    <ZLow>201.350</ZLow>",
    "    <ZLowLastCalibratedOn>2021-06-01T14:03:27</ZLowLastCalibratedOn>",
    "    <SystemKind>iQue3</SystemKind>",
    "    <HyperCytName>iQue Screener PLUS 1</HyperCytName>",
    "    <DeviceID>EXAMPLE-SYSTEM-ID</DeviceID>",
    "    <SerialNumber>EX-0001</SerialNumber>",
    "    <SDSEnabled>false</SDSEnabled>",
    "    <DeckHeightX>3.250</DeckHeightX>",
    "    <DeckHeightY>104.800</DeckHeightY>",
    "    <NotifyHoursProbeChange>20</NotifyHoursProbeChange>",
    "  </HyperCyt>",
    "  <PlateTypes>",
    plate_type_item(
      "Well96",
      "20.350 109.350 119.700 109.050 20.150 46.150 119.550 45.900",
      "2021-06-01T14:20:05.573",
    ),
    plate_type_item(
      "Well384",
      "18.150 111.550 121.900 111.250 17.950 43.950 121.750 43.700",
      "2021-06-01T14:20:05",
    ),
    plate_type_item("TestTube", ZEROS, None),
    plate_type_item("Vial", ZEROS, None),
    plate_type_item(
      "Well1536",
      "17.050 112.650 123.000 112.350 16.850 42.850 122.850 42.600",
      "2021-06-01T14:20:05",
    ),
    "  </PlateTypes>",
    "  <RinseStations>",
    station_item("Buffer", "27.300", "1.550", "4.350"),
    station_item("Decon", "75.800", "1.500", "-0.900"),
    station_item("Clean", "89.200", "1.500", "-0.950"),
    station_item("Water", "109.700", "1.550", "4.400"),
    station_item("Markers", "54.900", "1.550", "4.300"),
    station_item("Tube", "0.650", "13.450", "12.300"),
    "  </RinseStations>",
    "  <PlateModels>",
    "    <PlateModelCalibrationContainer3>",
    "      <ModelName>Generic 96 Well Plate</ModelName>",
    "      <WellDepthOffset>0.000</WellDepthOffset>",
    "      <XOffset>0.000</XOffset>",
    "      <YOffset>0.000</YOffset>",
    f"      <MapState>{escaped(MAP_DOCUMENT)}</MapState>",
    "    </PlateModelCalibrationContainer3>",
    "    <PlateModelCalibrationContainer3>",
    "      <ModelName>Generic 384 Well Plate</ModelName>",
    "      <WellDepthOffset>0.000</WellDepthOffset>",
    "      <XOffset>0.150</XOffset>",
    "      <YOffset>-0.100</YOffset>",
    "    </PlateModelCalibrationContainer3>",
    "  </PlateModels>",
    "  <SDSConfiguration>",
    "    <LaserConfiguration>Normal</LaserConfiguration>",
    "    <FL1Filter>Bandpass_533_30</FL1Filter>",
    "    <FL2Filter>Bandpass_585_40</FL2Filter>",
    "    <FL3Filter>Longpass_670</FL3Filter>",
    "    <FL4Filter>Bandpass_675_25</FL4Filter>",
    '    <LastCalibratedOn xsi:nil="true" />',
    "  </SDSConfiguration>",
    "  <ConsumablesOdometer>",
    "    <LastRunOn>2021-06-03T09:12:44</LastRunOn>",
    "    <RunTimeSinceNewProbe>312.50</RunTimeSinceNewProbe>",
    "    <RunTimeSinceNewFilter>1520.25</RunTimeSinceNewFilter>",
    "    <RunTimeSinceBottleClean>1520.25</RunTimeSinceBottleClean>",
    "    <RunTimeSinceNewPigTail>312.50</RunTimeSinceNewPigTail>",
    "    <RunTimeSinceNewCytTubing>0.00</RunTimeSinceNewCytTubing>",
    "    <CumulativeRunTimePump>2210.75</CumulativeRunTimePump>",
    "    <CumulativeRunTimeCyt>0</CumulativeRunTimeCyt>",
    "  </ConsumablesOdometer>",
    "</CalibrationContainer4>",
  ]
).replace("\n", "\r\n")

MAP_VALUES = (3.95, 3.9, 3.9, 3.95, 3.85, 3.8, 3.85, 3.9, 3.9, 3.85, 3.9, 3.95)
CEST = datetime.timezone(datetime.timedelta(hours=2))


def utc_date() -> datetime.datetime:
  """Return a time in UTC."""
  return datetime.datetime(2021, 6, 1, 15, 2, 11, tzinfo=datetime.timezone.utc)


def archive(document: str, name: str = "Calibration.xml", bom: bool = True) -> bytes:
  """Return a calibration file holding a document."""
  buffer = io.BytesIO()
  with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
    zipped.writestr(name, document.encode("utf-8-sig" if bom else "utf-8"))
  return buffer.getvalue()


def document_of(data: bytes) -> bytes:
  """Return the bytes of the XML entry of a calibration file."""
  with zipfile.ZipFile(io.BytesIO(data)) as zipped:
    return zipped.read("Calibration.xml")


def edited(old: str, new: str) -> bytes:
  """Return the example file with one piece of its document replaced."""
  assert EXAMPLE.count(old) == 1, old
  return archive(EXAMPLE.replace(old, new))


def example() -> Calibration:
  """Return the example calibration."""
  return cal.read_calibration(archive(EXAMPLE))


class ReadExampleTests(unittest.TestCase):
  """Reading a current-version document."""

  def setUp(self) -> None:
    """Read the example file."""
    self.calibration = example()

  def test_instrument(self) -> None:
    """Every field of the instrument record is read."""
    self.assertEqual(
      self.calibration.instrument,
      Instrument(
        travel_height=250.0,
        deck_height=201.35,
        pump_location="Right",
        system_kind="iQue3",
        has_shaker=True,
        selectable_detectors_enabled=False,
        travel_height_calibrated_on=None,
        deck_height_calibrated_on=datetime.datetime(2021, 6, 1, 14, 3, 27),
        deck_height_test_x=3.25,
        deck_height_test_y=104.8,
        name="iQue Screener PLUS 1",
        device_id="EXAMPLE-SYSTEM-ID",
        serial_number="EX-0001",
        probe_change_reminder_hours=20,
      ),
    )

  def test_plate_types(self) -> None:
    """All five plate types are read, taught or not."""
    plate_types = self.calibration.plate_types
    self.assertEqual(
      [entry.plate_type for entry in plate_types],
      ["Well96", "Well384", "TestTube", "Vial", "Well1536"],
    )
    self.assertEqual(
      plate_types[0],
      PlateTypeCalibration(
        "Well96",
        Corners(XY(20.35, 109.35), XY(119.7, 109.05), XY(20.15, 46.15), XY(119.55, 45.9)),
        datetime.datetime(2021, 6, 1, 14, 20, 5, 573000),
      ),
    )
    self.assertIsNone(plate_types[2].calibrated_on)
    self.assertFalse(plate_types[2].is_calibrated)
    self.assertEqual(plate_types[4].corners.bottom_right, XY(122.85, 42.6))

  def test_stations(self) -> None:
    """Stations keep their order, positions and relative depths."""
    self.assertEqual(
      [station.name for station in self.calibration.stations],
      ["Buffer", "Decon", "Clean", "Water", "Markers", "Tube"],
    )
    taught = datetime.datetime(2021, 6, 1, 14, 31, 40)
    self.assertEqual(
      self.calibration.station("Buffer"), Station("Buffer", 27.3, 1.55, 4.35, taught)
    )
    self.assertEqual(self.calibration.station("Decon").depth, -0.9)
    self.assertEqual(self.calibration.station("Tube"), Station("Tube", 0.65, 13.45, 12.3, taught))

  def test_plate_models(self) -> None:
    """A plate model carries its offsets and its depth map."""
    mapped, unmapped = self.calibration.plate_models
    self.assertEqual(
      mapped,
      PlateModel(
        name="Generic 96 Well Plate",
        depth_map=DepthMap(
          plate_format="Well96",
          values=MAP_VALUES,
          calibrated_on=datetime.datetime(2021, 6, 1, 15, 2, 11, 417021, CEST),
        ),
      ),
    )
    self.assertEqual(unmapped, PlateModel("Generic 384 Well Plate", 0.0, 0.15, -0.1, None))

  def test_detector_configuration_and_odometer(self) -> None:
    """The filter set is read, and the counters are converted from minutes to seconds."""
    self.assertEqual(self.calibration.detector_configuration, DetectorConfiguration())
    self.assertEqual(
      self.calibration.consumables_odometer,
      ConsumablesOdometer(
        last_run_on=datetime.datetime(2021, 6, 3, 9, 12, 44),
        run_time_since_new_probe=18750.0,
        run_time_since_new_filter=91215.0,
        run_time_since_bottle_clean=91215.0,
        run_time_since_new_pig_tail=18750.0,
        run_time_since_new_cytometer_tubing=0.0,
        cumulative_run_time_pump=132645.0,
        cumulative_run_time_cytometer=0.0,
      ),
    )

  def test_lookups_fail_for_what_is_not_there(self) -> None:
    """Looking up a missing or repeated entry is an error; names are matched exactly."""
    with self.assertRaisesRegex(CalibrationError, "no station 'Rinse'"):
      self.calibration.station("Rinse")
    with self.assertRaisesRegex(CalibrationError, "no plate model"):
      self.calibration.plate_model("generic 96 well plate")
    doubled = dataclasses.replace(
      self.calibration, stations=self.calibration.stations + self.calibration.stations[:1]
    )
    with self.assertRaisesRegex(CalibrationError, "2 entries for station 'Buffer'"):
      doubled.station("Buffer")

  def test_stations_under_short_names(self) -> None:
    """A station that a file names by its short name is found under the full name."""
    taught = utc_date()
    short_names = {"S1": "Buffer", "S2": "Decon", "S3": "Clean", "S4": "Water", "MB": "Markers"}
    stations = tuple(
      Station(short, float(index), 1.5, 4.0, taught) for index, short in enumerate(short_names)
    )
    calibration = dataclasses.replace(self.calibration, stations=stations)
    for index, (short, name) in enumerate(short_names.items()):
      self.assertEqual(calibration.station(name), Station(short, float(index), 1.5, 4.0, taught))
    self.assertEqual(cal.station_xy(calibration, "Markers"), XY(4.0, 1.5))
    rinse = dataclasses.replace(self.calibration, stations=(Station("Rinse", 9.0, 1.5, 4.0),))
    self.assertEqual(rinse.station("Water").x, 9.0)
    # An old name of the tube holder is not a short name of the station named Tube.
    vial = dataclasses.replace(self.calibration, stations=(Station("Vial", 9.0, 1.5, 4.0),))
    with self.assertRaisesRegex(CalibrationError, "no station 'Tube'"):
      vial.station("Tube")
    both = stations + (Station("Water", 9.0, 1.5, 4.0),)
    with self.assertRaisesRegex(CalibrationError, "2 entries for station 'Water'"):
      dataclasses.replace(calibration, stations=both).station("Water")


class ReadContainerTests(unittest.TestCase):
  """How the archive and the text of the document may vary."""

  def test_byte_order_mark_is_optional(self) -> None:
    """A document without the mark is UTF-8."""
    self.assertEqual(cal.read_calibration(archive(EXAMPLE, bom=False)), example())

  def test_utf16_document_with_mark(self) -> None:
    """A byte-order mark decides the encoding, whatever the declaration says."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
      zipped.writestr("Calibration.xml", EXAMPLE.encode("utf-16"))
    self.assertEqual(cal.read_calibration(buffer.getvalue()), example())

  def test_entry_name_case_and_other_entries(self) -> None:
    """The entry is found in any case, stored or deflated, among other entries."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zipped:
      zipped.writestr("readme.txt", b"not part of the format")
      zipped.writestr("CALIBRATION.XML", EXAMPLE.encode("utf-8"))
    self.assertEqual(cal.read_calibration(buffer.getvalue()), example())

  def test_elements_in_any_order(self) -> None:
    """Elements may come in any order, and a boolean may be 1 or 0."""
    swapped = EXAMPLE.replace(
      "    <HasShaker>true</HasShaker>\r\n    <PumpLocation>Right</PumpLocation>\r\n",
      "    <PumpLocation>Right</PumpLocation>\r\n    <HasShaker>1</HasShaker>\r\n",
    )
    self.assertNotEqual(swapped, EXAMPLE)
    self.assertEqual(cal.read_calibration(archive(swapped)), example())

  def test_missing_string_and_empty_string(self) -> None:
    """A missing string element is no string; an empty element is an empty string."""
    without = cal.read_calibration(edited("    <SerialNumber>EX-0001</SerialNumber>\r\n", ""))
    self.assertIsNone(without.instrument.serial_number)
    empty = cal.read_calibration(edited("<SerialNumber>EX-0001</SerialNumber>", "<SerialNumber />"))
    self.assertEqual(empty.instrument.serial_number, "")

  def test_missing_lists_and_records_are_empty(self) -> None:
    """A document may hold the instrument record alone."""
    document = "\r\n".join(
      EXAMPLE.split("\r\n")[: EXAMPLE.split("\r\n").index("  <PlateTypes>")]
      + ["</CalibrationContainer4>"]
    )
    calibration = cal.read_calibration(archive(document))
    self.assertEqual(calibration.instrument, example().instrument)
    self.assertEqual(calibration.plate_types, ())
    self.assertEqual(calibration.stations, ())
    self.assertEqual(calibration.plate_models, ())
    self.assertIsNone(calibration.detector_configuration)
    self.assertIsNone(calibration.consumables_odometer)

  def test_not_an_archive(self) -> None:
    """Data that is not a ZIP archive is refused."""
    with self.assertRaisesRegex(CalibrationFormatError, "not a calibration file"):
      cal.read_calibration(EXAMPLE.encode("utf-8"))

  def test_archive_without_the_entry(self) -> None:
    """An archive without the calibration entry is refused."""
    with self.assertRaisesRegex(CalibrationFormatError, "this archive has 0"):
      cal.read_calibration(archive(EXAMPLE, name="Settings.xml"))

  def test_malformed_xml(self) -> None:
    """A truncated document is refused."""
    with self.assertRaisesRegex(CalibrationFormatError, "not well-formed"):
      cal.read_calibration(archive(EXAMPLE[:-10]))

  def test_document_type_is_refused(self) -> None:
    """A document type declaration, which could define entities, is refused."""
    document = EXAMPLE.replace(ROOT_OPEN, "<!DOCTYPE x [<!ENTITY a 'b'>]>" + ROOT_OPEN)
    with self.assertRaisesRegex(CalibrationFormatError, "document type"):
      cal.read_calibration(archive(document))


class ReadErrorTests(unittest.TestCase):
  """Content that is unknown or malformed is an error, never a default."""

  def assert_refused(self, old: str, new: str, message: str) -> None:
    """Check that the example with one piece replaced is refused with a message."""
    with self.assertRaisesRegex(CalibrationFormatError, message):
      cal.read_calibration(edited(old, new))

  def test_unknown_version(self) -> None:
    """A root element of no known version is refused."""
    document = EXAMPLE.replace("CalibrationContainer4", "CalibrationContainer5")
    with self.assertRaisesRegex(CalibrationFormatError, "unknown calibration content version"):
      cal.read_calibration(archive(document))

  def test_missing_value(self) -> None:
    """A missing number is an error, not zero."""
    self.assert_refused("    <ZHigh>250.000</ZHigh>\r\n", "", "HyperCyt/ZHigh is missing")

  def test_missing_instrument(self) -> None:
    """A document without the instrument record is refused."""
    lines = EXAMPLE.split("\r\n")
    start, end = lines.index("  <HyperCyt>"), lines.index("  </HyperCyt>")
    document = "\r\n".join(lines[:start] + lines[end + 1 :])
    with self.assertRaisesRegex(CalibrationFormatError, "HyperCyt is missing"):
      cal.read_calibration(archive(document))

  def test_nil_where_a_value_is_needed(self) -> None:
    """A number that must exist cannot be nil."""
    self.assert_refused("<ZLow>201.350</ZLow>", '<ZLow xsi:nil="true" />', "ZLow is missing")

  def test_unknown_element(self) -> None:
    """An element the format does not have is refused."""
    self.assert_refused(
      "    <SDSEnabled>false</SDSEnabled>",
      "    <SDSEnabled>false</SDSEnabled><ProbeLength>1</ProbeLength>",
      "HyperCyt: unexpected <ProbeLength>",
    )

  def test_repeated_element(self) -> None:
    """An element that occurs twice is refused."""
    self.assert_refused(
      "<ZHigh>250.000</ZHigh>", "<ZHigh>250.000</ZHigh><ZHigh>1</ZHigh>", "more than once"
    )

  def test_unknown_item(self) -> None:
    """A list item of another kind is refused."""
    self.assert_refused(
      "  <RinseStations>", "  <RinseStations><Station />", "RinseStations: unexpected <Station>"
    )

  def test_bad_number(self) -> None:
    """Numbers have a decimal point and no exponent; whole numbers have no fraction."""
    self.assert_refused("<ZHigh>250.000</ZHigh>", "<ZHigh>2.5e2</ZHigh>", "not a decimal number")
    self.assert_refused("<ZHigh>250.000</ZHigh>", "<ZHigh>250,000</ZHigh>", "not a decimal number")
    self.assert_refused(
      "<NotifyHoursProbeChange>20<", "<NotifyHoursProbeChange>20.5<", "not a whole number"
    )

  def test_bad_boolean(self) -> None:
    """Booleans are lower case."""
    self.assert_refused("<HasShaker>true<", "<HasShaker>True<", "not true or false")

  def test_bad_names(self) -> None:
    """Names out of a fixed set must match one of them exactly."""
    self.assert_refused("<PumpLocation>Right<", "<PumpLocation>right<", "is not one of Right, Left")
    self.assert_refused("<SystemKind>iQue3<", "<SystemKind>iQue4<", "is not one of")
    self.assert_refused("<PlateType>Vial<", "<PlateType>TestTubeRack<", "is not one of")
    self.assert_refused("<FL3Filter>Longpass_670<", "<FL3Filter>Longpass<", "is not one of")

  def test_bad_date(self) -> None:
    """A date that is malformed or does not exist is refused."""
    self.assert_refused("2021-06-01T14:03:27", "2021-06-01 14:03:27", "not a date and time")
    self.assert_refused("2021-06-01T14:03:27", "2021-13-01T14:03:27", "not a date and time")

  def test_value_holding_elements(self) -> None:
    """A value element cannot hold elements."""
    self.assert_refused("<ZHigh>250.000</ZHigh>", "<ZHigh><a />250</ZHigh>", "holds elements")

  def test_map_with_unequal_lists(self) -> None:
    """A depth map needs one value per point."""
    self.assert_refused(
      "    &lt;decimal&gt;3.8&lt;/decimal&gt;\r\n",
      "",
      "12 points, 11 values and a stated count of 12",
    )

  def test_map_with_wrong_stated_count(self) -> None:
    """The number of points a depth map states must be the number it has."""
    self.assert_refused(
      "&lt;InterpolationPoints&gt;12&lt;", "&lt;InterpolationPoints&gt;9&lt;", "stated count of 9"
    )

  def test_map_with_unsupported_point_count(self) -> None:
    """A depth map with a number of points that fits no grid is refused."""
    document = EXAMPLE.replace("    &lt;int&gt;95&lt;/int&gt;\r\n", "")
    document = document.replace("    &lt;decimal&gt;3.8&lt;/decimal&gt;\r\n", "")
    document = document.replace("Points&gt;12&lt;", "Points&gt;11&lt;")
    with self.assertRaisesRegex(CalibrationFormatError, "cannot have 11 points"):
      cal.read_calibration(archive(document))

  def test_map_with_points_off_the_grid(self) -> None:
    """The points of a depth map must be the wells of its grid."""
    self.assert_refused("&lt;int&gt;52&lt;", "&lt;int&gt;53&lt;", "points are not the wells")

  def test_map_of_a_plate_type_without_wells(self) -> None:
    """A depth map is of a 96-, 384- or 1536-well plate."""
    self.assert_refused(
      "&lt;PlateType&gt;Well96&lt;", "&lt;PlateType&gt;Vial&lt;", "is not one of Well96"
    )

  def test_map_that_is_not_xml(self) -> None:
    """A depth map that is not well-formed is refused."""
    self.assert_refused("&lt;/PlateMappingState&gt;", "", "MapState is not well-formed")


VERSION_3 = "\r\n".join(
  [
    '<?xml version="1.0" encoding="utf-8"?>',
    ROOT_OPEN.replace("CalibrationContainer4", "CalibrationContainer3"),
    "  <HyperCyt>",
    "    <HasShaker>true</HasShaker>",
    "    <PumpLocation>Left</PumpLocation>",
    "    <ZHigh>248.5</ZHigh>",
    "    <ZHighLastCalibratedOn>2019-01-02T03:04:05</ZHighLastCalibratedOn>",
    "    <ZLow>199.75</ZLow>",
    "    <ZLowLastCalibratedOn>2019-01-02T03:04:06</ZLowLastCalibratedOn>",
    "    <SystemKind>iQue</SystemKind>",
    "    <HyperCytName>Old</HyperCytName>",
    "    <DeviceID>ID-3</DeviceID>",
    "    <SerialNumber>SN-3</SerialNumber>",
    "    <SDSEnabled>true</SDSEnabled>",
    "    <HasTestTubeRack>false</HasTestTubeRack>",
    "    <HasCirculatingRinseStation>false</HasCirculatingRinseStation>",
    "    <DeckHeightX>3.1</DeckHeightX>",
    '    <DeckHeightY xsi:nil="true" />',
    "  </HyperCyt>",
    "  <PlateTypes>",
    plate_type_item("Well384", "18.1 111.5 121.9 111.2 17.9 43.9 121.7 43.7", "2019-01-02T03:04:07")
    .replace("\n", "\r\n")
    .replace("Container3", "Container2"),
    plate_type_item("TestTubeRack", ZEROS, None)
    .replace("\n", "\r\n")
    .replace("Container3", "Container2"),
    "  </PlateTypes>",
    "  <RinseStations>",
    station_item("Buffer", "27.3", "1.55", "4.35").replace("\n", "\r\n"),
    "  </RinseStations>",
    "  <PlateModels>",
    "    <PlateModelCalibrationContainer3>",
    "      <ModelName>Model 3</ModelName>",
    "      <WellDepthOffset>3.5</WellDepthOffset>",
    "      <XOffset>0.1</XOffset>",
    "      <YOffset>0.2</YOffset>",
    "    </PlateModelCalibrationContainer3>",
    "  </PlateModels>",
    "</CalibrationContainer3>",
  ]
)

VERSION_2 = "\r\n".join(
  [
    '<?xml version="1.0" encoding="utf-8"?>',
    ROOT_OPEN.replace("CalibrationContainer4", "CalibrationContainer2"),
    "  <HyperCyt>",
    "    <HasShaker>false</HasShaker>",
    "    <PumpLocation>Left</PumpLocation>",
    "    <ZHigh>245.5</ZHigh>",
    "    <ZHighLastCalibratedOn>2015-01-02T03:04:05</ZHighLastCalibratedOn>",
    "    <ZLow>120.25</ZLow>",
    "    <ZLowLastCalibratedOn>2015-01-02T03:04:06</ZLowLastCalibratedOn>",
    "    <SystemKind>SYSTEM_KIND</SystemKind>",
    "    <HyperCytName>Older</HyperCytName>",
    "    <DeviceID>ID-2</DeviceID>",
    "    <SerialNumber>SN-2</SerialNumber>",
    "    <SDSEnabled>true</SDSEnabled>",
    "    <HasTestTubeRack>true</HasTestTubeRack>",
    "    <HasCirculatingRinseStation>false</HasCirculatingRinseStation>",
    "  </HyperCyt>",
    "  <PlateTypes>",
    "    <PlateTypeCalibrationContainer2>",
    "      <AnythingAtAll>these are replaced, so they are not interpreted</AnythingAtAll>",
    "    </PlateTypeCalibrationContainer2>",
    "  </PlateTypes>",
    "  <RinseStations />",
    "  <BeadStations><BeadStationCalibrationContainer /></BeadStations>",
    "  <TestTubeStations />",
    "  <PlateModels>",
    "    <PlateModelCalibrationContainer2>",
    "      <ModelName>Model 2</ModelName>",
    "      <WellDepthOffset>12.5</WellDepthOffset>",
    "      <XOffset>0.3</XOffset>",
    "      <YOffset>-0.4</YOffset>",
    "    </PlateModelCalibrationContainer2>",
    "  </PlateModels>",
    "  <SDSConfiguration>",
    "    <LaserConfiguration>AllBlue</LaserConfiguration>",
    "    <FL1Filter>Bandpass_510_15</FL1Filter>",
    "    <FL2Filter>Bandpass_540_20</FL2Filter>",
    "    <FL3Filter>Bandpass_610_20</FL3Filter>",
    "    <FL4Filter>Bandpass_630_30</FL4Filter>",
    "    <LastCalibratedOn>2015-02-03T04:05:06</LastCalibratedOn>",
    "  </SDSConfiguration>",
    "</CalibrationContainer2>",
  ]
)

VERSION_1 = "\r\n".join(
  [
    '<?xml version="1.0" encoding="utf-8"?>',
    ROOT_OPEN.replace("CalibrationContainer4", "CalibrationContainer"),
    "  <HyperCyt>",
    "    <HasShaker>true</HasShaker>",
    "    <PumpLocation>Right</PumpLocation>",
    "    <ZHigh>2455</ZHigh>",
    '    <ZHighLastCalibratedOn xsi:nil="true" />',
    "    <ZLow>1202</ZLow>",
    "    <ZLowLastCalibratedOn>2012-01-02T03:04:06</ZLowLastCalibratedOn>",
    "    <HasHTFC>HAS_HTFC</HasHTFC>",
    "    <HasTestTubeRack>false</HasTestTubeRack>",
    "    <HasCirculatingRinseStation>true</HasCirculatingRinseStation>",
    "  </HyperCyt>",
    "  <PlateTypes />",
    "  <RinseStations />",
    "  <BeadStations />",
    "  <TestTubeStations />",
    "  <PlateModels>",
    "    <PlateModelCalibrationContainer>",
    "      <ModelName>Model 1</ModelName>",
    "      <WellDepthOffset>123</WellDepthOffset>",
    "    </PlateModelCalibrationContainer>",
    "  </PlateModels>",
    "</CalibrationContainer>",
  ]
)

REPLACEMENT_PLATE_TYPES = (
  PlateTypeCalibration(
    "Well96", Corners(XY(20.0, 109.7), XY(119.8, 45.8), XY(19.8, 46.1), XY(119.65, 45.8))
  ),
  PlateTypeCalibration(
    "Well384", Corners(XY(18.0, 111.7), XY(121.8, 111.35), XY(17.8, 43.8), XY(121.65, 43.8))
  ),
  PlateTypeCalibration(
    "Well1536", Corners(XY(17.2, 112.5), XY(122.6, 112.15), XY(17.0, 43.3), XY(122.45, 43.0))
  ),
  PlateTypeCalibration("TestTube", Corners(XY(0.0, 0.0), XY(0.0, 0.0), XY(0.0, 0.0), XY(0.0, 0.0))),
)
REPLACEMENT_STATIONS = tuple(
  Station(name, 0.0, 0.0, 0.0) for name in ("Buffer", "Markers", "Decon", "Clean", "Buffer", "Tube")
)


class OlderVersionTests(unittest.TestCase):
  """Older content versions are read and brought to the current model."""

  def test_version_3(self) -> None:
    """Version 3 loses unknown plate types and its deck-height date, and gains a tube station."""
    with self.assertLogs(cal.logger, "WARNING"):
      calibration = cal.read_calibration(archive(VERSION_3))
    self.assertEqual(
      calibration,
      Calibration(
        instrument=Instrument(
          travel_height=248.5,
          deck_height=199.75,
          pump_location="Left",
          system_kind="iQue",
          has_shaker=True,
          selectable_detectors_enabled=True,
          travel_height_calibrated_on=datetime.datetime(2019, 1, 2, 3, 4, 5),
          deck_height_calibrated_on=None,
          deck_height_test_x=3.1,
          deck_height_test_y=None,
          name="Old",
          device_id="ID-3",
          serial_number="SN-3",
          probe_change_reminder_hours=0,
        ),
        plate_types=(
          PlateTypeCalibration(
            "Well384",
            Corners(XY(18.1, 111.5), XY(121.9, 111.2), XY(17.9, 43.9), XY(121.7, 43.7)),
            datetime.datetime(2019, 1, 2, 3, 4, 7),
          ),
        ),
        stations=(
          Station("Buffer", 27.3, 1.55, 4.35, datetime.datetime(2021, 6, 1, 14, 31, 40)),
          Station("Tube", 0.0, 0.0, 0.0, None),
        ),
        plate_models=(PlateModel("Model 3", 3.5, 0.1, 0.2, None),),
        detector_configuration=None,
        consumables_odometer=ConsumablesOdometer(),
      ),
    )

  def test_version_3_has_no_reminder_field(self) -> None:
    """A field of a later version is unknown in version 3."""
    document = VERSION_3.replace(
      "  </HyperCyt>", "    <NotifyHoursProbeChange>20</NotifyHoursProbeChange>\r\n  </HyperCyt>"
    )
    with self.assertRaisesRegex(CalibrationFormatError, "unexpected <NotifyHoursProbeChange>"):
      cal.read_calibration(archive(document))

  def test_version_2_keeps_no_taught_position(self) -> None:
    """Version 2 keeps identity, plate models and filters; positions become placeholders."""
    with self.assertLogs(cal.logger, "WARNING"):
      calibration = cal.read_calibration(archive(VERSION_2.replace("SYSTEM_KIND", "HyperCyt")))
    self.assertEqual(
      calibration,
      Calibration(
        instrument=Instrument(
          travel_height=250.0,
          deck_height=150.0,
          pump_location="Left",
          system_kind="HyperCyt",
          has_shaker=False,
          selectable_detectors_enabled=True,
          name="Older",
          device_id="ID-2",
          serial_number="SN-2",
          probe_change_reminder_hours=0,
        ),
        plate_types=REPLACEMENT_PLATE_TYPES,
        stations=REPLACEMENT_STATIONS,
        plate_models=(PlateModel("Model 2", 12.5, 0.3, -0.4, None),),
        detector_configuration=DetectorConfiguration(
          "AllBlue",
          "Bandpass_510_15",
          "Bandpass_540_20",
          "Bandpass_610_20",
          "Bandpass_630_30",
          datetime.datetime(2015, 2, 3, 4, 5, 6),
        ),
        consumables_odometer=ConsumablesOdometer(),
      ),
    )

  def test_version_2_of_the_htfc_family_keeps_its_travel_height(self) -> None:
    """Only the HTFC family keeps the travel height of a version 2 document."""
    calibration = cal.read_calibration(archive(VERSION_2.replace("SYSTEM_KIND", "HTFC")))
    self.assertEqual(calibration.instrument.travel_height, 245.5)
    self.assertEqual(calibration.instrument.deck_height, 150.0)
    self.assertIsNone(calibration.instrument.travel_height_calibrated_on)

  def test_version_1(self) -> None:
    """Version 1 heights and depths are tenths of a millimetre; the family comes from a flag."""
    with self.assertLogs(cal.logger, "WARNING"):
      calibration = cal.read_calibration(archive(VERSION_1.replace("HAS_HTFC", "true")))
    self.assertEqual(
      calibration,
      Calibration(
        instrument=Instrument(
          travel_height=245.5,
          deck_height=150.0,
          pump_location="Right",
          system_kind="HTFC",
          has_shaker=True,
          name="",
          device_id="",
          serial_number="",
        ),
        plate_types=REPLACEMENT_PLATE_TYPES,
        stations=REPLACEMENT_STATIONS,
        plate_models=(PlateModel("Model 1", 12.3, 0.0, 0.0, None),),
        detector_configuration=None,
        consumables_odometer=ConsumablesOdometer(),
      ),
    )

  def test_version_1_of_another_family(self) -> None:
    """Without the HTFC flag the family is unknown and the travel height a placeholder."""
    calibration = cal.read_calibration(archive(VERSION_1.replace("HAS_HTFC", "false")))
    self.assertEqual(calibration.instrument.system_kind, "Unknown")
    self.assertEqual(calibration.instrument.travel_height, 250.0)

  def test_version_1_positions_are_whole_tenths(self) -> None:
    """A version 1 position with a decimal point is refused."""
    document = VERSION_1.replace("HAS_HTFC", "true").replace("<ZHigh>2455<", "<ZHigh>245.5<")
    with self.assertRaisesRegex(CalibrationFormatError, "not a whole number"):
      cal.read_calibration(archive(document))

  def test_upgraded_content_is_written_as_the_current_version(self) -> None:
    """Content read from an older version is written in the current one."""
    upgraded = cal.read_calibration(archive(VERSION_3))
    written = cal.write_calibration(upgraded)
    self.assertIn(b"<CalibrationContainer4 ", document_of(written))
    self.assertEqual(cal.read_calibration(written), upgraded)


class WriteTests(unittest.TestCase):
  """The file that is written."""

  def test_document_layout_matches_the_example(self) -> None:
    """Writing the example gives its text back, line for line."""
    # Counters always get two decimals, and times are kept to the microsecond.
    expected = EXAMPLE.replace("<CumulativeRunTimeCyt>0<", "<CumulativeRunTimeCyt>0.00<")
    expected = expected.replace("11.4170213+02:00", "11.417021+02:00")
    self.assertEqual(cal.calibration_document(example()), expected)

  def test_archive(self) -> None:
    """The file is a ZIP with one deflated entry holding the document after a UTF-8 mark."""
    data = cal.write_calibration(example())
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
      (entry,) = zipped.infolist()
      content = zipped.read(entry)
    self.assertEqual(entry.filename, "Calibration.xml")
    self.assertEqual(entry.compress_type, zipfile.ZIP_DEFLATED)
    self.assertEqual(entry.flag_bits & 0x1, 0)
    self.assertEqual(content[:3], b"\xef\xbb\xbf")
    self.assertEqual(content[3:].decode("utf-8"), cal.calibration_document(example()))
    self.assertEqual(data, cal.write_calibration(example()))

  def test_minimal_document(self) -> None:
    """Missing values are nil, missing strings are left out and empty lists are empty elements."""
    calibration = Calibration(
      Instrument(travel_height=250, deck_height=190.05, pump_location="Left", system_kind="Unknown")
    )
    expected = "\r\n".join(
      [
        '<?xml version="1.0" encoding="utf-8"?>',
        ROOT_OPEN,
        "  <HyperCyt>",
        "    <HasShaker>true</HasShaker>",
        "    <PumpLocation>Left</PumpLocation>",
        "    <ZHigh>250.000</ZHigh>",
        '    <ZHighLastCalibratedOn xsi:nil="true" />',
        "    <ZLow>190.050</ZLow>",
        '    <ZLowLastCalibratedOn xsi:nil="true" />',
        "    <SystemKind>Unknown</SystemKind>",
        "    <SDSEnabled>false</SDSEnabled>",
        '    <DeckHeightX xsi:nil="true" />',
        '    <DeckHeightY xsi:nil="true" />',
        "    <NotifyHoursProbeChange>0</NotifyHoursProbeChange>",
        "  </HyperCyt>",
        "  <PlateTypes />",
        "  <RinseStations />",
        "  <PlateModels />",
        "</CalibrationContainer4>",
      ]
    )
    self.assertEqual(cal.calibration_document(calibration), expected)
    self.assertEqual(cal.read_calibration(cal.write_calibration(calibration)), calibration)

  def test_round_trips(self) -> None:
    """Reading a written file gives the same calibration."""
    utc = datetime.timezone.utc
    odd = Calibration(
      instrument=Instrument(
        travel_height=249.95,
        deck_height=201.1234,
        pump_location="Right",
        system_kind="iQuePlusVYB",
        has_shaker=False,
        selectable_detectors_enabled=True,
        travel_height_calibrated_on=datetime.datetime(2020, 2, 29, 23, 59, 59, 5, utc),
        deck_height_calibrated_on=datetime.datetime(
          1999, 12, 31, 0, 0, 0, 0, datetime.timezone(-datetime.timedelta(hours=9, minutes=30))
        ),
        deck_height_test_x=0.0,
        deck_height_test_y=None,
        name="A & B <C>",
        device_id="",
        serial_number=None,
        probe_change_reminder_hours=-3,
      ),
      plate_types=(PlateTypeCalibration("Vial", Corners(XY(1, 2), XY(3, 4), XY(5, 6), XY(7, 8))),),
      stations=(Station("Line one\nline two", -1.5, 1e-7, 1900.0),),
      plate_models=(
        PlateModel(
          "M", 250.0, -0.05, 0.05, DepthMap("Well384", (1.0, 2.5, -0.125, 4.0), utc_date())
        ),
      ),
      detector_configuration=DetectorConfiguration("HalfBlue", calibrated_on=utc_date()),
      consumables_odometer=ConsumablesOdometer(None, 0.6, 4.2, 100.0, 1e6, 0.0, 59.99, 3600.0),
    )
    for calibration in (example(), cal.nominal_calibration(), odd):
      self.assertEqual(cal.read_calibration(cal.write_calibration(calibration)), calibration)

  def test_number_text(self) -> None:
    """Numbers are padded, never rounded, and never written with an exponent."""
    document = cal.calibration_document(
      Calibration(
        Instrument(
          travel_height=1e-7, deck_height=-0.0, pump_location="Left", system_kind="Unknown"
        ),
        consumables_odometer=ConsumablesOdometer(run_time_since_new_probe=100.0),
      )
    )
    self.assertIn("<ZHigh>0.0000001</ZHigh>", document)
    self.assertIn("<ZLow>0.000</ZLow>", document)
    self.assertIn("<RunTimeSinceNewProbe>1.666666666666666666666666667<", document)
    self.assertIn("<RunTimeSinceNewFilter>0.00<", document)

  def test_timestamp_text(self) -> None:
    """Timestamps carry a fraction and a zone only when they have one."""

    def written(value: datetime.datetime) -> str:
      """Return a deck-height date as it is written."""
      instrument = Instrument(250.0, 190.0, "Left", "Unknown", deck_height_calibrated_on=value)
      document = cal.calibration_document(Calibration(instrument))
      return document.split("<ZLowLastCalibratedOn>")[1].split("<")[0]

    self.assertEqual(written(datetime.datetime(2021, 6, 1, 4, 3, 7)), "2021-06-01T04:03:07")
    self.assertEqual(
      written(datetime.datetime(2021, 6, 1, 4, 3, 7, 573000)), "2021-06-01T04:03:07.573"
    )
    self.assertEqual(written(utc_date()), "2021-06-01T15:02:11Z")
    self.assertEqual(
      written(datetime.datetime(2021, 6, 1, 4, 3, 7, 1, CEST)), "2021-06-01T04:03:07.000001+02:00"
    )

  def test_values_that_cannot_be_written(self) -> None:
    """Numbers that are not finite and control characters are refused."""
    base = Instrument(250.0, 190.0, "Left", "Unknown")
    with self.assertRaisesRegex(CalibrationError, "not a finite number"):
      cal.write_calibration(Calibration(dataclasses.replace(base, deck_height=float("nan"))))
    with self.assertRaisesRegex(CalibrationError, "cannot hold"):
      cal.write_calibration(Calibration(dataclasses.replace(base, name="a\x00b")))

  def test_file_larger_than_the_instrument_accepts(self) -> None:
    """A file beyond the size the instrument accepts is not produced."""
    self.assertEqual(cal.MAX_FILE_SIZE, 5_120_000)
    with mock.patch.object(cal, "MAX_FILE_SIZE", 500):
      with self.assertRaisesRegex(CalibrationError, "the instrument accepts 500"):
        cal.write_calibration(example())


class PatchDeckHeightTests(unittest.TestCase):
  """Changing the deck height of an instrument's file in place."""

  def test_only_the_deck_height_and_its_date_change(self) -> None:
    """Every other byte of the document stays; the stations' depths, also named ZLow, too."""
    original = EXAMPLE
    data = archive(original)
    measured_on = datetime.datetime(2026, 11, 11, 10, 30, tzinfo=CEST)
    patched = cal.patch_deck_height(data, 201.55, measured_on)
    expected = original.replace("<ZLow>201.350</ZLow>", "<ZLow>201.550</ZLow>").replace(
      "<ZLowLastCalibratedOn>2021-06-01T14:03:27</ZLowLastCalibratedOn>",
      "<ZLowLastCalibratedOn>2026-11-11T10:30:00+02:00</ZLowLastCalibratedOn>",
    )
    self.assertEqual(document_of(patched), expected.encode("utf-8-sig"))
    read = cal.read_calibration(patched)
    self.assertEqual(read.instrument.deck_height, 201.55)
    self.assertEqual(read.instrument.deck_height_calibrated_on, measured_on)
    self.assertEqual(read.stations, example().stations)
    self.assertEqual(read.plate_models, example().plate_models)

  def test_a_nil_date_and_other_entries(self) -> None:
    """A deck height without a date gets one; other entries of the archive keep their bytes;
    a document without a byte-order mark keeps none."""
    document = EXAMPLE.replace(
      "<ZLowLastCalibratedOn>2021-06-01T14:03:27</ZLowLastCalibratedOn>",
      '<ZLowLastCalibratedOn xsi:nil="true" />',
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
      zipped.writestr("Calibration.xml", document.encode("utf-8"))
      zipped.writestr("Notes.txt", b"kept as it is")
    measured_on = datetime.datetime(2026, 11, 11, 10, 30, tzinfo=datetime.timezone.utc)
    patched = cal.patch_deck_height(buffer.getvalue(), 200.0, measured_on)
    self.assertEqual(
      document_of(patched),
      document.replace("<ZLow>201.350</ZLow>", "<ZLow>200.000</ZLow>")
      .replace(
        '<ZLowLastCalibratedOn xsi:nil="true" />',
        "<ZLowLastCalibratedOn>2026-11-11T10:30:00Z</ZLowLastCalibratedOn>",
      )
      .encode("utf-8"),
    )
    with zipfile.ZipFile(io.BytesIO(patched)) as zipped:
      self.assertEqual(zipped.read("Notes.txt"), b"kept as it is")
    self.assertEqual(cal.read_calibration(patched).instrument.deck_height, 200.0)

  def test_refusals(self) -> None:
    """Only a current-version file with one deck height in its instrument record is patched."""
    now = datetime.datetime(2026, 11, 11, tzinfo=datetime.timezone.utc)
    with self.assertRaises(cal.CalibrationFormatError):
      cal.patch_deck_height(b"junk", 200.0, now)
    with self.assertLogs(cal.logger, "WARNING"):
      with self.assertRaisesRegex(cal.CalibrationFormatError, "current content version"):
        cal.patch_deck_height(archive(VERSION_3), 200.0, now)


class WellNameTests(unittest.TestCase):
  """Converting between well names and zero-based rows and columns."""

  def test_parse(self) -> None:
    """Columns may be zero padded; rows after Z have two letters."""
    self.assertEqual(cal.parse_well_name("A1"), (0, 0))
    self.assertEqual(cal.parse_well_name("A01"), (0, 0))
    self.assertEqual(cal.parse_well_name("H12"), (7, 11))
    self.assertEqual(cal.parse_well_name("P24", "Well384"), (15, 23))
    self.assertEqual(cal.parse_well_name("Z1"), (25, 0))
    self.assertEqual(cal.parse_well_name("AA1"), (26, 0))
    self.assertEqual(cal.parse_well_name("AF48", "Well1536"), (31, 47))

  def test_parse_refuses(self) -> None:
    """Text that is not a well name, or a well off the plate, is refused."""
    for name in ("", "A", "1", "a1", "A0", "A00", "A1 ", "A-1", "A001", "AAA1", "A1.0"):
      with self.assertRaises(ValueError, msg=name):
        cal.parse_well_name(name)
    with self.assertRaisesRegex(ValueError, "not on a Well96 plate"):
      cal.parse_well_name("I1", "Well96")
    with self.assertRaisesRegex(ValueError, "not on a Well96 plate"):
      cal.parse_well_name("A13", "Well96")

  def test_format(self) -> None:
    """Names have a two-digit column and parse back."""
    self.assertEqual(cal.format_well_name(0, 0), "A01")
    self.assertEqual(cal.format_well_name(7, 11), "H12")
    self.assertEqual(cal.format_well_name(25, 9), "Z10")
    self.assertEqual(cal.format_well_name(26, 0), "AA01")
    self.assertEqual(cal.format_well_name(31, 47), "AF48")
    for row in range(32):
      for column in (0, 8, 47):
        self.assertEqual(cal.parse_well_name(cal.format_well_name(row, column)), (row, column))
    with self.assertRaises(ValueError):
      cal.format_well_name(-1, 0)
    with self.assertRaises(ValueError):
      cal.format_well_name(0, 99)


CORNERS_96 = Corners(XY(20.35, 109.35), XY(119.7, 109.05), XY(20.15, 46.15), XY(119.55, 45.9))


class WellXYTests(unittest.TestCase):
  """Well centres from the four corner wells."""

  def test_interior_well(self) -> None:
    """An interior well follows the edge-then-interior formula."""
    # C3 of the 96-well example, rows 8, columns 12:
    #   left edge   20.35 + 2 * (20.15 - 20.35) / 7   = 20.292857... -> 20.293
    #   right edge  119.7 + 2 * (119.55 - 119.7) / 7  = 119.657142... -> 119.657
    #   X           20.293 + 2 * (119.657 - 20.293) / 11 = 38.359181... -> 38.359
    #   top edge    109.35 + 2 * (109.05 - 109.35) / 11 = 109.295454... -> 109.295
    #   bottom edge 46.15 + 2 * (45.9 - 46.15) / 11     = 46.104545... -> 46.105
    #   Y           109.295 + 2 * (46.105 - 109.295) / 7 = 91.240714... -> 91.241
    self.assertEqual(cal.interpolate_well_xy(CORNERS_96, 8, 12, 2, 2), XY(38.359, 91.241))

  def test_corner_wells(self) -> None:
    """The top-left well is the taught corner; the other corners are rounded."""
    corners = Corners(XY(20.3504, 109.3504), XY(119.7004, 109.0504), XY(20.1504, 46.1504), XY(1, 2))
    # The top-left well is the taught corner as it is; the others are rounded to 0.001.
    self.assertEqual(cal.interpolate_well_xy(corners, 8, 12, 0, 0), XY(20.3504, 109.3504))
    self.assertEqual(cal.interpolate_well_xy(corners, 8, 12, 0, 11), XY(119.7, 109.05))
    self.assertEqual(cal.interpolate_well_xy(corners, 8, 12, 7, 0), XY(20.15, 46.15))
    self.assertEqual(cal.interpolate_well_xy(CORNERS_96, 8, 12, 7, 11), XY(119.55, 45.9))

  def test_edges(self) -> None:
    """Row A takes its X from the top corners and column 1 its Y from the left corners."""
    # Row A, column 4: X = 20.35 + 3 * 99.35 / 11 = 47.445454... ; Y = 109.35 - 3 * 0.3 / 11.
    self.assertEqual(cal.interpolate_well_xy(CORNERS_96, 8, 12, 0, 3), XY(47.445, 109.268))
    # Row D, column 1: X = 20.35 - 3 * 0.2 / 7 = 20.264285... ; Y = 109.35 - 3 * 63.2 / 7.
    self.assertEqual(cal.interpolate_well_xy(CORNERS_96, 8, 12, 3, 0), XY(20.264, 82.264))

  def test_halves_round_to_the_even_digit(self) -> None:
    """A value halfway between two thousandths goes to the even one."""

    def left_edge_x(bottom_left_x: float) -> float:
      """Return the X of the bottom-left well for a bottom-left corner X."""
      corners = Corners(XY(0, 0), XY(110, 0), XY(bottom_left_x, -70), XY(110, -70))
      return cal.interpolate_well_xy(corners, 8, 12, 7, 0).x

    self.assertEqual(left_edge_x(0.0025), 0.002)
    self.assertEqual(left_edge_x(0.0035), 0.004)
    self.assertEqual(left_edge_x(0.0045), 0.004)
    # 0.0105 * 1 / 7 = 0.0015 exactly, which no binary float is.
    corners = Corners(XY(0, 0), XY(110, 0), XY(0.0105, -70), XY(110, -70))
    self.assertEqual(cal.interpolate_well_xy(corners, 8, 12, 1, 0).x, 0.002)

  def test_well_xy_uses_the_plate_type(self) -> None:
    """Wells are interpolated from the corners of their own plate type."""
    calibration = example()
    self.assertEqual(cal.well_xy(calibration, "Well96", 2, 2), XY(38.359, 91.241))
    # P24 of the 384-well example is its bottom-right corner.
    self.assertEqual(cal.well_xy(calibration, "Well384", 15, 23), XY(121.75, 43.7))
    self.assertEqual(cal.well_xy(calibration, "Well1536", 0, 0), XY(17.05, 112.65))

  def test_well_xy_refuses(self) -> None:
    """Wells off the plate and plate types that are untaught or absent are refused."""
    calibration = example()
    with self.assertRaisesRegex(CalibrationError, "row 8, column 0 is not on a Well96 plate"):
      cal.well_xy(calibration, "Well96", 8, 0)
    with self.assertRaisesRegex(CalibrationError, "not a plate format with wells"):
      cal.well_xy(calibration, "Vial", 0, 0)  # type: ignore[arg-type]
    untaught = dataclasses.replace(
      calibration,
      plate_types=tuple(
        dataclasses.replace(entry, calibrated_on=None) for entry in calibration.plate_types
      ),
    )
    with self.assertRaisesRegex(CalibrationError, "Well96 are not calibrated"):
      cal.well_xy(untaught, "Well96", 0, 0)
    with self.assertRaisesRegex(CalibrationError, "no plate type Well96"):
      cal.well_xy(dataclasses.replace(calibration, plate_types=()), "Well96", 0, 0)

  def test_plate_model_offset(self) -> None:
    """The plate model's offset is added to the well centre."""
    calibration = example()
    # 121.75 + 0.15 and 43.7 - 0.1.
    self.assertEqual(
      cal.well_sampling_xy(calibration, "Well384", "Generic 384 Well Plate", 15, 23),
      XY(121.9, 43.6),
    )
    self.assertEqual(
      cal.well_sampling_xy(calibration, "Well96", "Generic 96 Well Plate", 2, 2),
      XY(38.359, 91.241),
    )
    with self.assertRaisesRegex(CalibrationError, "no plate model 'Other'"):
      cal.well_sampling_xy(calibration, "Well96", "Other", 0, 0)

  def test_derived_corners(self) -> None:
    """96-well corners lie 2.2 mm inside the 384-well ones, 1536-well corners 1.1 mm outside."""
    corners_384 = Corners(XY(18, 111.7), XY(121.8, 111.35), XY(17.8, 44.1), XY(121.65, 43.8))
    self.assertEqual(
      cal.derive_corners(corners_384, "Well96"),
      Corners(XY(20.2, 109.5), XY(119.6, 109.15), XY(20.0, 46.3), XY(119.45, 46.0)),
    )
    self.assertEqual(
      cal.derive_corners(corners_384, "Well1536"),
      Corners(XY(16.9, 112.8), XY(122.9, 112.45), XY(16.7, 43.0), XY(122.75, 42.7)),
    )
    self.assertEqual(cal.derive_corners(corners_384, "Well384"), corners_384)

  def test_derived_corners_with_y_growing_down_the_plate(self) -> None:
    """On a sampler whose Y grows down the plate the Y moves are mirrored."""
    corners_384 = Corners(XY(18, 10), XY(121.5, 10), XY(18, 77.5), XY(121.5, 77.5))
    self.assertEqual(
      cal.derive_corners(corners_384, "Well96", y_axis_inverted=False),
      Corners(XY(20.2, 12.2), XY(119.3, 12.2), XY(20.2, 75.3), XY(119.3, 75.3)),
    )
    self.assertEqual(
      cal.derive_corners(corners_384, "Well1536", y_axis_inverted=False),
      Corners(XY(16.9, 8.9), XY(122.6, 8.9), XY(16.9, 78.6), XY(122.6, 78.6)),
    )


class DepthMapTests(unittest.TestCase):
  """The grid of a depth map and the interpolation between its points."""

  def setUp(self) -> None:
    """Build the 12-point map of the example."""
    self.depth_map = DepthMap("Well96", MAP_VALUES, utc_date())

  def test_nodes_of_the_usual_maps(self) -> None:
    """The grids of the maps made in practice; their last interval is shorter."""
    self.assertEqual(cal.depth_map_nodes("Well96", 12), ((0, 4, 8, 11), (0, 4, 7)))
    self.assertEqual(cal.depth_map_nodes("Well384", 12), ((0, 8, 16, 23), (0, 8, 15)))
    self.assertEqual(
      cal.depth_map_nodes("Well1536", 35), ((0, 8, 16, 24, 32, 40, 47), (0, 8, 16, 24, 31))
    )

  def test_nodes_of_other_maps(self) -> None:
    """Grids of other sizes that span their plate."""
    self.assertEqual(cal.depth_map_nodes("Well96", 4), ((0, 11), (0, 7)))
    self.assertEqual(cal.depth_map_nodes("Well96", 25), ((0, 3, 6, 9, 11), (0, 2, 4, 6, 7)))
    self.assertEqual(cal.depth_map_nodes("Well96", 96), (tuple(range(12)), tuple(range(8))))
    self.assertEqual(cal.depth_map_nodes("Well384", 6), ((0, 12, 23), (0, 15)))

  def test_points(self) -> None:
    """Measured wells are listed row by row as row-major indices."""
    self.assertEqual(
      cal.depth_map_points("Well96", 12), (0, 4, 8, 11, 48, 52, 56, 59, 84, 88, 92, 95)
    )
    self.assertEqual(
      cal.depth_map_points("Well384", 12), (0, 8, 16, 23, 192, 200, 208, 215, 360, 368, 376, 383)
    )

  def test_maps_that_cannot_exist(self) -> None:
    """Counts without a grid, and grids that do not span the plate, are refused."""
    with self.assertRaisesRegex(CalibrationError, "cannot have 13 points"):
      cal.depth_map_nodes("Well96", 13)
    # 384 nodes on 96 wells would all sit on the first well.
    with self.assertRaisesRegex(CalibrationError, "does not span a Well96 plate"):
      cal.depth_map_nodes("Well96", 384)
    # 12 node columns, 2 wells apart, end on column 23 of 24: the last column is outside.
    with self.assertRaisesRegex(CalibrationError, "does not span a Well384 plate"):
      DepthMap("Well384", tuple([1.0] * 96), utc_date())

  def test_value_between_four_points(self) -> None:
    """A well between four measured wells gets their weighted mean."""
    # C3 lies halfway between columns 1 and 5 and rows A and E:
    # (3.95 + 3.9 + 3.85 + 3.8) / 4 = 3.875.
    self.assertAlmostEqual(cal.depth_map_value(self.depth_map, 2, 2), 3.875, places=12)

  def test_value_at_and_between_points(self) -> None:
    """Measured wells give their value; the short last intervals are interpolated too."""
    self.assertEqual(cal.depth_map_value(self.depth_map, 0, 0), 3.95)
    self.assertEqual(cal.depth_map_value(self.depth_map, 4, 8), 3.85)
    self.assertEqual(cal.depth_map_value(self.depth_map, 7, 11), 3.95)
    # Row A, column 2: a quarter of the way from 3.95 to 3.9.
    self.assertAlmostEqual(cal.depth_map_value(self.depth_map, 0, 1), 3.9375, places=12)
    # Row A, column 10: the last interval is 3 wells long; a third of the way from 3.9 to 3.95.
    self.assertAlmostEqual(cal.depth_map_value(self.depth_map, 0, 9), 3.9 + 0.05 / 3, places=12)
    # Row G, column 12: the last row interval is 3 wells; two thirds from 3.9 to 3.95.
    self.assertAlmostEqual(cal.depth_map_value(self.depth_map, 6, 11), 3.9 + 0.1 / 3, places=12)
    # H11: two thirds along the last column interval, in the last row: 3.9 + 2/3 * 0.05.
    self.assertAlmostEqual(cal.depth_map_value(self.depth_map, 7, 10), 3.9 + 0.1 / 3, places=12)

  def test_value_off_the_plate(self) -> None:
    """A well off the mapped plate is refused."""
    with self.assertRaisesRegex(CalibrationError, "not on a Well96 plate"):
      cal.depth_map_value(self.depth_map, 0, 12)


class HeightTests(unittest.TestCase):
  """Travel height and the depths derived from the deck height."""

  def setUp(self) -> None:
    """Read the example file."""
    self.calibration = example()

  def test_travel_height(self) -> None:
    """The travel height is the stored one; a stored 0 means there is none."""
    self.assertEqual(cal.travel_height(self.calibration), 250.0)
    unset = dataclasses.replace(self.calibration.instrument, travel_height=0.0)
    with self.assertRaisesRegex(CalibrationError, "travel height is not set"):
      cal.travel_height(dataclasses.replace(self.calibration, instrument=unset))

  def test_mapped_well(self) -> None:
    """A mapped well is sampled at deck height + mapped height + sampling height."""
    # Deck 201.35 + mapped 3.875 + sampling height 0.5.
    z = cal.well_sampling_z(self.calibration, "Well96", "Generic 96 Well Plate", 2, 2, 0.5)
    self.assertEqual(z, 205.725)
    # A1: 201.35 + 3.95 + 0.2.
    z = cal.well_sampling_z(self.calibration, "Well96", "Generic 96 Well Plate", 0, 0, 0.2)
    self.assertEqual(z, 205.5)

  def test_default_sampling_heights(self) -> None:
    """The protocol defaults per plate format."""
    self.assertEqual(cal.DEFAULT_SAMPLING_HEIGHTS, {"Well96": 0.5, "Well384": 0.2, "Well1536": 0.5})

  def test_well_of_a_model_with_a_taught_depth(self) -> None:
    """Without a map, every well is sampled at deck height + the model's depth offset."""
    models = (PlateModel("Flat", well_depth_offset=4.2),)
    calibration = dataclasses.replace(self.calibration, plate_models=models)
    # Deck 201.35 + 4.2; the sampling height plays no part.
    self.assertEqual(cal.well_sampling_z(calibration, "Well384", "Flat", 15, 23, 0.2), 205.55)

  def test_well_refusals(self) -> None:
    """A sampling depth is refused when anything it needs is missing."""
    name = "Generic 96 Well Plate"
    with self.assertRaisesRegex(CalibrationError, "no depth map and no taught depth"):
      cal.well_sampling_z(self.calibration, "Well384", "Generic 384 Well Plate", 0, 0, 0.2)
    never_taught = dataclasses.replace(
      self.calibration, plate_models=(PlateModel("New", well_depth_offset=250.0),)
    )
    with self.assertRaisesRegex(CalibrationError, "no depth map and no taught depth"):
      cal.well_sampling_z(never_taught, "Well384", "New", 0, 0, 0.2)
    with self.assertRaisesRegex(CalibrationError, "is for Well96"):
      cal.well_sampling_z(self.calibration, "Well384", name, 0, 0, 0.2)
    with self.assertRaisesRegex(CalibrationError, "below the well bottom"):
      cal.well_sampling_z(self.calibration, "Well96", name, 0, 0, -0.1)
    with self.assertRaisesRegex(CalibrationError, "not on a Well96 plate"):
      cal.well_sampling_z(self.calibration, "Well96", name, 0, 12, 0.5)
    with self.assertRaisesRegex(CalibrationError, "no plate model"):
      cal.well_sampling_z(self.calibration, "Well96", "Other", 0, 0, 0.5)
    undated = dataclasses.replace(
      self.calibration,
      instrument=dataclasses.replace(self.calibration.instrument, deck_height_calibrated_on=None),
    )
    with self.assertRaisesRegex(CalibrationError, "deck height is not calibrated"):
      cal.well_sampling_z(undated, "Well96", name, 0, 0, 0.5)

  def test_stations(self) -> None:
    """A station is dipped into at deck height + its depth."""
    self.assertEqual(cal.station_xy(self.calibration, "Buffer"), XY(27.3, 1.55))
    # Deck 201.35 + 4.35; a negative depth dips below the deck height: 201.35 - 0.9.
    self.assertEqual(cal.station_dip_z(self.calibration, "Buffer"), 205.7)
    self.assertEqual(cal.station_dip_z(self.calibration, "Decon"), 200.45)
    # Continuous draw is 2 mm higher: 201.35 + 4.4 + 2.
    self.assertEqual(cal.station_draw_z(self.calibration, "Water"), 207.75)

  def test_tube(self) -> None:
    """The tube holder is the station named Tube; sampling is 2 mm higher."""
    self.assertEqual(cal.tube_xy(self.calibration), XY(0.65, 13.45))
    # Deck 201.35 + 12.3 for the empty holder, 2 mm more for sampling.
    self.assertEqual(cal.tube_contact_z(self.calibration), 213.65)
    self.assertEqual(cal.tube_sampling_z(self.calibration), 215.65)

  def test_station_refusals(self) -> None:
    """Stations that are unknown, undated or at the origin are refused, as is an undated deck."""
    with self.assertRaisesRegex(CalibrationError, "no station 'Rinse'"):
      cal.station_xy(self.calibration, "Rinse")
    undated = Station("Buffer", 27.3, 1.55, 4.35)
    at_origin = Station("Water", 0.0, 0.0, 1900.0, utc_date())
    calibration = dataclasses.replace(self.calibration, stations=(undated, at_origin))
    for name in ("Buffer", "Water"):
      with self.assertRaisesRegex(CalibrationError, f"station '{name}' is not calibrated"):
        cal.station_xy(calibration, name)
      with self.assertRaisesRegex(CalibrationError, f"station '{name}' is not calibrated"):
        cal.station_dip_z(calibration, name)
    undated_deck = dataclasses.replace(
      self.calibration,
      instrument=dataclasses.replace(self.calibration.instrument, deck_height_calibrated_on=None),
    )
    self.assertEqual(cal.station_xy(undated_deck, "Buffer"), XY(27.3, 1.55))
    for function in (cal.station_dip_z, cal.station_draw_z):
      with self.assertRaisesRegex(CalibrationError, "deck height is not calibrated"):
        function(undated_deck, "Buffer")
    with self.assertRaisesRegex(CalibrationError, "deck height is not calibrated"):
      cal.tube_sampling_z(undated_deck)

  def test_deck_height_test_point(self) -> None:
    """The test point needs both coordinates and a calibrated deck height."""
    self.assertEqual(cal.deck_height_test_point(self.calibration), XY(3.25, 104.8))
    instrument = self.calibration.instrument
    for changed in (
      dataclasses.replace(instrument, deck_height_test_y=None),
      dataclasses.replace(instrument, deck_height_calibrated_on=None),
    ):
      with self.assertRaisesRegex(CalibrationError, "test point are not calibrated"):
        cal.deck_height_test_point(dataclasses.replace(self.calibration, instrument=changed))

  def test_a_new_deck_height_moves_every_depth_alike(self) -> None:
    """Only the deck height and its date change; every derived depth follows."""
    measured_on = datetime.datetime(2022, 3, 4, 5, 6, 7)
    moved = cal.with_deck_height(self.calibration, 201.6, measured_on)
    self.assertEqual(moved.instrument.deck_height, 201.6)
    self.assertEqual(moved.instrument.deck_height_calibrated_on, measured_on)
    # Nothing else is rewritten: the stored offsets and the test point stay.
    self.assertEqual(
      dataclasses.replace(moved, instrument=self.calibration.instrument), self.calibration
    )
    self.assertEqual(cal.deck_height_test_point(moved), XY(3.25, 104.8))
    # Every depth is 0.25 mm higher than before.
    name = "Generic 96 Well Plate"
    self.assertEqual(cal.well_sampling_z(moved, "Well96", name, 2, 2, 0.5), 205.975)
    self.assertEqual(cal.station_dip_z(moved, "Buffer"), 205.95)
    self.assertEqual(cal.station_dip_z(moved, "Decon"), 200.7)
    self.assertEqual(cal.tube_sampling_z(moved), 215.9)
    self.assertEqual(cal.travel_height(moved), 250.0)
    self.assertEqual(self.calibration.instrument.deck_height, 201.35)

  def test_a_first_deck_height_sets_the_test_point(self) -> None:
    """The test point can be taught together with the deck height."""
    nominal = cal.nominal_calibration()
    taught = cal.with_deck_height(nominal, 200.4, utc_date(), XY(3.4, 104.6))
    self.assertEqual(cal.deck_height_test_point(taught), XY(3.4, 104.6))
    self.assertEqual(taught.instrument.deck_height, 200.4)


class NominalCalibrationTests(unittest.TestCase):
  """The nominal starting values."""

  def setUp(self) -> None:
    """Build the nominal calibration."""
    self.nominal = cal.nominal_calibration(name="N", device_id="D", serial_number="S")

  def test_instrument(self) -> None:
    """Heights at the ends of the Z range, nothing dated."""
    self.assertEqual(
      self.nominal.instrument,
      Instrument(
        travel_height=250.0,
        deck_height=190.0,
        pump_location="Right",
        system_kind="iQue3",
        deck_height_test_x=3.0,
        deck_height_test_y=105.0,
        name="N",
        device_id="D",
        serial_number="S",
        probe_change_reminder_hours=20,
      ),
    )
    self.assertEqual(cal.nominal_calibration("iQuePlusBR").instrument.system_kind, "iQuePlusBR")

  def test_plate_types(self) -> None:
    """Nominal 384-well corners with the other formats derived from them."""
    by_type = {entry.plate_type: entry for entry in self.nominal.plate_types}
    self.assertEqual(list(by_type), ["Well96", "Well384", "TestTube", "Vial", "Well1536"])
    self.assertEqual(
      by_type["Well384"].corners,
      Corners(XY(18.0, 111.7), XY(121.8, 111.35), XY(17.8, 44.1), XY(121.65, 43.8)),
    )
    self.assertEqual(
      by_type["Well96"].corners,
      Corners(XY(20.2, 109.5), XY(119.6, 109.15), XY(20.0, 46.3), XY(119.45, 46.0)),
    )
    self.assertEqual(
      by_type["Well1536"].corners,
      Corners(XY(16.9, 112.8), XY(122.9, 112.45), XY(16.7, 43.0), XY(122.75, 42.7)),
    )
    self.assertEqual(by_type["Vial"].corners.top_left, XY(0.0, 0.0))

  def test_stations_and_plate_models(self) -> None:
    """Station starting positions with marker depths, and the generic plate models."""
    self.assertEqual(
      [(station.name, station.x, station.y, station.depth) for station in self.nominal.stations],
      [
        ("Buffer", 27.25, 1.5, 190.0),
        ("Decon", 75.75, 1.5, 190.0),
        ("Clean", 89.15, 1.5, 190.0),
        ("Water", 109.65, 1.5, 190.0),
        ("Markers", 54.85, 1.5, 190.0),
        ("Tube", 0.0, 13.5, 190.0),
      ],
    )
    self.assertEqual(
      self.nominal.plate_models,
      (
        PlateModel("Generic 96 Well Plate"),
        PlateModel("Generic 384 Well Plate"),
        PlateModel("Generic 1536 Well Plate"),
      ),
    )
    self.assertEqual(self.nominal.detector_configuration, DetectorConfiguration())
    self.assertEqual(self.nominal.consumables_odometer, ConsumablesOdometer())

  def test_nothing_is_calibrated(self) -> None:
    """No coordinate can be derived from nominal values."""
    self.assertFalse(self.nominal.instrument.deck_height_is_calibrated)
    self.assertFalse(any(entry.is_calibrated for entry in self.nominal.plate_types))
    self.assertFalse(any(station.is_calibrated for station in self.nominal.stations))
    self.assertFalse(any(model.is_calibrated for model in self.nominal.plate_models))
    with self.assertRaises(CalibrationError):
      cal.well_xy(self.nominal, "Well384", 0, 0)
    with self.assertRaises(CalibrationError):
      cal.well_sampling_z(self.nominal, "Well96", "Generic 96 Well Plate", 0, 0, 0.5)
    with self.assertRaises(CalibrationError):
      cal.station_xy(self.nominal, "Buffer")
    with self.assertRaises(CalibrationError):
      cal.tube_sampling_z(self.nominal)
    with self.assertRaises(CalibrationError):
      cal.deck_height_test_point(self.nominal)


if __name__ == "__main__":
  unittest.main()
