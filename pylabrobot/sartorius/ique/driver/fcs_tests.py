"""Tests of the event container, the parameter tables and the FCS 3.0 writer and reader."""

import array
import datetime
import importlib.util
import io
import pathlib
import struct
import sys
import tempfile
import unittest
from typing import Dict, List, Optional, Sequence, Tuple
from unittest import mock

from pylabrobot.sartorius.ique.driver import fcs
from pylabrobot.sartorius.ique.driver.fcs import EventData, FcsError

VBR_NAMES = [
  "FSC-H",
  "SSC-H",
  "BL1-H",
  "BL2-H",
  "BL3-H",
  "BL4-H",
  "BL5-H",
  "RL1-H",
  "RL2-H",
  "VL1-H",
  "VL2-H",
  "VL3-H",
  "VL4-H",
  "VL5-H",
  "VL6-H",
  "FSC-A",
  "SSC-A",
  "BL1-A",
  "BL2-A",
  "BL3-A",
  "BL4-A",
  "BL5-A",
  "RL1-A",
  "RL2-A",
  "VL1-A",
  "VL2-A",
  "VL3-A",
  "VL4-A",
  "VL5-A",
  "VL6-A",
  "Width",
  "Time",
]
VYB_NAMES = [
  "FSC-H",
  "SSC-H",
  "BL1-H",
  "BL3-H",
  "BL4-H",
  "VL1-H",
  "VL2-H",
  "VL3-H",
  "VL4-H",
  "VL5-H",
  "VL6-H",
  "YL1-H",
  "YL2-H",
  "YL3-H",
  "YL4-H",
  "FSC-A",
  "SSC-A",
  "BL1-A",
  "BL3-A",
  "BL4-A",
  "VL1-A",
  "VL2-A",
  "VL3-A",
  "VL4-A",
  "VL5-A",
  "VL6-A",
  "YL1-A",
  "YL2-A",
  "YL3-A",
  "YL4-A",
  "Width",
  "Time",
]
BR_NAMES = [
  "FSC-H",
  "SSC-H",
  "BL1-H",
  "BL2-H",
  "BL4-H",
  "BL5-H",
  "RL1-H",
  "RL2-H",
  "FSC-A",
  "SSC-A",
  "BL1-A",
  "BL2-A",
  "BL4-A",
  "BL5-A",
  "RL1-A",
  "RL2-A",
  "Width",
  "Time",
]

# Two events of two parameters; every value is exact in single precision.
SMALL_VALUES = (1.5, 200.0, -3.25, 400.0)


def small_events() -> EventData:
  """Return two events of the parameters ``A`` and ``Time``."""
  return EventData(["A", "Time"], SMALL_VALUES, ["Alpha", "Time"])


def text_of(pairs: Sequence[Tuple[str, str]], delimiter: str = "/") -> bytes:
  """Return a TEXT segment for keys and values that hold no delimiter."""
  return (
    delimiter + delimiter.join(token for pair in pairs for token in pair) + delimiter
  ).encode()


def header_of(text_length: int, data_begin: int, data_end: int, version: str = "FCS3.0") -> bytes:
  """Return a HEADER for a TEXT segment that starts at byte 58."""
  offsets = (58, 58 + text_length - 1, data_begin, data_end, 0, 0)
  return (version + "    " + "".join(f"{offset:>8d}" for offset in offsets)).encode()


def minimal_pairs(byte_order: str = "4,3,2,1", events: int = 2) -> List[Tuple[str, str]]:
  """Return the keywords of a two-parameter float file, without data offsets."""
  return [
    ("$BYTEORD", byte_order),
    ("$DATATYPE", "F"),
    ("$MODE", "L"),
    ("$NEXTDATA", "0"),
    ("$PAR", "2"),
    ("$TOT", str(events)),
    ("$P1B", "32"),
    ("$P1E", "0,0"),
    ("$P1N", "A"),
    ("$P1R", "1024"),
    ("$P2B", "32"),
    ("$P2E", "0,0"),
    ("$P2N", "Time"),
    ("$P2R", "1024"),
  ]


def file_of(
  pairs: Sequence[Tuple[str, str]],
  data: bytes,
  delimiter: str = "/",
  version: str = "FCS3.0",
) -> bytes:
  """Return a file whose HEADER alone states where DATA is."""
  text = text_of(pairs, delimiter)
  begin = 58 + len(text)
  return header_of(len(text), begin, begin + len(data) - 1, version) + text + data


def changed(
  pairs: Sequence[Tuple[str, str]], key: str, value: Optional[str]
) -> List[Tuple[str, str]]:
  """Return keywords with one value replaced, or the keyword removed."""
  kept = [(k, v) for k, v in pairs if k != key]
  return kept if value is None else kept + [(key, value)]


BIG_ENDIAN_DATA = struct.pack(">4f", *SMALL_VALUES)


class EventDataTests(unittest.TestCase):
  """The event container."""

  def test_shape_and_names(self) -> None:
    """Counts and names follow from the buffer and the names."""
    events = small_events()
    self.assertEqual(events.parameter_names, ("A", "Time"))
    self.assertEqual(events.long_names, ("Alpha", "Time"))
    self.assertEqual(events.parameter_count, 2)
    self.assertEqual(events.event_count, 2)
    self.assertEqual(len(events), 2)
    self.assertEqual(events.values.typecode, "f")
    self.assertEqual(list(events.values), [1.5, 200.0, -3.25, 400.0])
    self.assertEqual(EventData(["A", "B"]).long_names, ("A", "B"))
    self.assertEqual(EventData(["A", "B"]).event_count, 0)

  def test_an_array_is_taken_as_the_buffer(self) -> None:
    """A single-precision array is used as it is; anything else is copied."""
    buffer = array.array("f", SMALL_VALUES)
    self.assertIs(EventData(["A", "Time"], buffer).values, buffer)
    self.assertIsNot(EventData(["A", "Time"], array.array("d", SMALL_VALUES)).values, buffer)

  def test_event(self) -> None:
    """One event is its values in parameter order."""
    events = small_events()
    self.assertEqual(events.event(0), (1.5, 200.0))
    self.assertEqual(events.event(1), (-3.25, 400.0))
    for index in (-1, 2):
      with self.assertRaises(IndexError):
        events.event(index)

  def test_parameter(self) -> None:
    """One parameter is selected by name or by position."""
    events = small_events()
    self.assertEqual(events.parameter("A"), array.array("f", [1.5, -3.25]))
    self.assertEqual(events.parameter(1), array.array("f", [200.0, 400.0]))
    self.assertEqual(events.parameter("Time").typecode, "f")
    self.assertEqual(events.parameter_index("Time"), 1)
    with self.assertRaises(KeyError):
      events.parameter("Alpha")
    with self.assertRaises(IndexError):
      events.parameter(2)

  def test_slice(self) -> None:
    """A slice is a copy of a contiguous range of events."""
    events = EventData(["A", "B"], range(10))
    part = events.slice(1, 3)
    self.assertEqual(part, EventData(["A", "B"], [2, 3, 4, 5]))
    part.values[0] = 99.0
    self.assertEqual(events.event(1), (2.0, 3.0))
    self.assertEqual(events.slice(5, 5).event_count, 0)
    self.assertEqual(events.slice(0, 5), events)
    for start, stop in ((-1, 2), (3, 2), (0, 6)):
      with self.assertRaises(IndexError):
        events.slice(start, stop)

  def test_extend(self) -> None:
    """Whole events can be appended; a partial event is refused."""
    events = EventData(["A", "B"])
    events.extend([1, 2, 3, 4])
    events.extend(array.array("f", [5, 6]))
    events.extend(array.array("d", [7, 8]))
    self.assertEqual(list(events.values), [1, 2, 3, 4, 5, 6, 7, 8])
    with self.assertRaisesRegex(ValueError, "3 values are not a whole number of events"):
      events.extend([9, 10, 11])
    self.assertEqual(events.event_count, 4)

  def test_refused_containers(self) -> None:
    """Names and values must make whole, named events."""
    with self.assertRaisesRegex(ValueError, "at least one parameter"):
      EventData([])
    with self.assertRaisesRegex(ValueError, "unique"):
      EventData(["A", "A"])
    with self.assertRaisesRegex(ValueError, "one long name per parameter"):
      EventData(["A", "B"], long_names=["Alpha"])
    with self.assertRaisesRegex(ValueError, "whole number of events"):
      EventData(["A", "B"], [1, 2, 3])

  def test_equality(self) -> None:
    """Containers are equal when names and values are."""
    self.assertEqual(small_events(), small_events())
    self.assertNotEqual(small_events(), EventData(["A", "Time"], SMALL_VALUES))
    self.assertNotEqual(small_events(), EventData(["A", "Time"], [1.5, 200.0], ["Alpha", "Time"]))
    self.assertNotEqual(small_events(), "events")

  def test_to_numpy_names_the_missing_dependency(self) -> None:
    """Without numpy the conversion says which optional dependency is missing."""
    with mock.patch.dict(sys.modules, {"numpy": None}):
      with self.assertRaisesRegex(ImportError, "optional dependency numpy"):
        small_events().to_numpy()

  @unittest.skipIf(importlib.util.find_spec("numpy") is None, "numpy is not installed")
  def test_to_numpy(self) -> None:
    """The conversion gives one row per event."""
    table = small_events().to_numpy()
    self.assertEqual(table.shape, (2, 2))
    self.assertEqual(str(table.dtype), "float32")
    self.assertEqual(table.tolist(), [[1.5, 200.0], [-3.25, 400.0]])


class ParameterTableTests(unittest.TestCase):
  """The parameters of the three optical configurations."""

  def test_names_in_stream_order(self) -> None:
    """Parameter names of each configuration, in the order of the stream."""
    self.assertEqual(list(fcs.PARAMETER_TABLES["VBR"].names), VBR_NAMES)
    self.assertEqual(list(fcs.PARAMETER_TABLES["VYB"].names), VYB_NAMES)
    self.assertEqual(list(fcs.PARAMETER_TABLES["BR"].names), BR_NAMES)
    for table in fcs.PARAMETER_TABLES.values():
      self.assertEqual(table.long_names, table.names)

  def test_identity(self) -> None:
    """Cytometer, project and file names, parameter counts and lasers of each configuration."""
    identity = {
      name: (table.cytometer, table.project, table.file_name, len(table.parameters), table.lasers)
      for name, table in fcs.PARAMETER_TABLES.items()
    }
    self.assertEqual(
      identity,
      {
        "VBR": ("IntelliCyt iQue3", "iQue3", "IntelliCyt_iQue3.fcs", 32, ("violet", "blue", "red")),
        "VYB": (
          "IntelliCyt iQuePlus VYB",
          "iQuePlusVYB",
          "IntelliCyt_iQuePlusVYB.fcs",
          32,
          ("violet", "yellow", "blue"),
        ),
        "BR": (
          "IntelliCyt iQuePlus BR",
          "iQuePlusBR",
          "IntelliCyt_iQuePlusBR.fcs",
          18,
          ("blue", "red"),
        ),
      },
    )

  def test_width_and_time_come_last(self) -> None:
    """Width is the second to last parameter and time the last."""
    for name, time_index in (("VBR", 31), ("VYB", 31), ("BR", 17)):
      table = fcs.PARAMETER_TABLES[name]  # type: ignore[index]
      self.assertEqual(table.time_index, time_index)
      self.assertEqual(table.width_index, time_index - 1)
      self.assertEqual(table.parameters[time_index], fcs.Parameter("Time", "Time", "time"))
      self.assertEqual(table.parameters[time_index - 1], fcs.Parameter("Width", "Width", "width"))
      self.assertEqual(fcs.time_parameter_index(table.names), time_index)

  def test_lasers_and_filters_of_the_fluorescence_channels(self) -> None:
    """Laser, filter and position of every fluorescence height."""

    def channels(name: str) -> Dict[str, Tuple[Optional[str], Optional[str], int]]:
      """Return laser, filter and position of each fluorescence height by channel."""
      table = fcs.PARAMETER_TABLES[name]  # type: ignore[index]
      return {
        parameter.name[:-2]: (parameter.laser, parameter.emission_filter, index)
        for index, parameter in enumerate(table.parameters)
        if parameter.measurement == "height" and parameter.laser is not None
      }

    self.assertEqual(
      channels("VBR"),
      {
        "BL1": ("blue", "530/30", 2),
        "BL2": ("blue", "572/28", 3),
        "BL3": ("blue", "615/24", 4),
        "BL4": ("blue", "675/30", 5),
        "BL5": ("blue", "780/60", 6),
        "RL1": ("red", "675/30", 7),
        "RL2": ("red", "780/60", 8),
        "VL1": ("violet", "445/45", 9),
        "VL2": ("violet", "530/30", 10),
        "VL3": ("violet", "572/28", 11),
        "VL4": ("violet", "615/24", 12),
        "VL5": ("violet", "675/30", 13),
        "VL6": ("violet", "780/60", 14),
      },
    )
    self.assertEqual(
      channels("VYB"),
      {
        "BL1": ("blue", "530/30", 2),
        "BL3": ("blue", "615/20", 3),
        "BL4": ("blue", "660/20", 4),
        "VL1": ("violet", "445/45", 5),
        "VL2": ("violet", "530/30", 6),
        "VL3": ("violet", "586/20", 7),
        "VL4": ("violet", "615/20", 8),
        "VL5": ("violet", "660/20", 9),
        "VL6": ("violet", "780/60", 10),
        "YL1": ("yellow", "586/20", 11),
        "YL2": ("yellow", "615/20", 12),
        "YL3": ("yellow", "660/20", 13),
        "YL4": ("yellow", "780/60", 14),
      },
    )
    self.assertEqual(
      channels("BR"),
      {
        "BL1": ("blue", "530/30", 2),
        "BL2": ("blue", "572/28", 3),
        "BL4": ("blue", "675/30", 4),
        "BL5": ("blue", "780/60", 5),
        "RL1": ("red", "675/30", 6),
        "RL2": ("red", "780/60", 7),
      },
    )

  def test_areas_follow_the_heights_in_the_same_order(self) -> None:
    """Each area lies a fixed number of parameters after its height."""
    for name, area_offset in (("VBR", 15), ("VYB", 15), ("BR", 8)):
      parameters = fcs.PARAMETER_TABLES[name].parameters  # type: ignore[index]
      for index in range(area_offset):
        height, area = parameters[index], parameters[index + area_offset]
        self.assertEqual(height.measurement, "height")
        self.assertEqual(area.measurement, "area")
        self.assertEqual(area.name, height.name[:-2] + "-A")
        self.assertEqual((area.laser, area.emission_filter), (height.laser, height.emission_filter))

  def test_scatter_has_no_laser_assigned(self) -> None:
    """The scatter parameters come first and carry no laser."""
    for table in fcs.PARAMETER_TABLES.values():
      self.assertEqual(table.parameters[0], fcs.Parameter("FSC-H", "FSC-H", "height"))
      self.assertEqual(table.parameters[1], fcs.Parameter("SSC-H", "SSC-H", "height"))

  def test_laser_wavelengths(self) -> None:
    """The wavelength of each laser."""
    self.assertEqual(fcs.LASER_WAVELENGTHS, {"violet": 405, "blue": 488, "yellow": 561, "red": 640})

  def test_table_of_a_system_kind(self) -> None:
    """Each family member maps to its configuration; other kinds are refused."""
    self.assertIs(fcs.parameter_table_for_system_kind("iQue3"), fcs.PARAMETER_TABLES["VBR"])
    self.assertIs(fcs.parameter_table_for_system_kind("iQue3HD"), fcs.PARAMETER_TABLES["VBR"])
    self.assertIs(fcs.parameter_table_for_system_kind("iQuePlusVYB"), fcs.PARAMETER_TABLES["VYB"])
    self.assertIs(fcs.parameter_table_for_system_kind("iQuePlusBR"), fcs.PARAMETER_TABLES["BR"])
    with self.assertRaisesRegex(ValueError, "no parameter table for system kind 'iQue'"):
      fcs.parameter_table_for_system_kind("iQue")

  def test_new_events(self) -> None:
    """A table makes a container with its names."""
    events = fcs.PARAMETER_TABLES["BR"].new_events([0.0] * 36)
    self.assertEqual(events.parameter_names, tuple(BR_NAMES))
    self.assertEqual(events.event_count, 2)


class WriteLayoutTests(unittest.TestCase):
  """The bytes of a written file against a file built by hand."""

  def expected_text(self, data_begin: int, data_end: int, total: int = 2) -> bytes:
    """Return the TEXT segment expected for the small file."""
    return text_of(
      [
        ("$DATATYPE", "F"),
        ("$MODE", "L"),
        ("$BYTEORD", "4,3,2,1"),
        ("$NEXTDATA", "0"),
        ("$PAR", "2"),
        ("$P1B", "32"),
        ("$P2B", "32"),
        ("$P1R", "16777216"),
        ("$P2R", "16777216"),
        ("$P1N", "A"),
        ("$P2N", "Time"),
        ("$P1E", "0,0"),
        ("$P2E", "0,0"),
        ("$P1S", "Alpha"),
        ("$P2S", "Time"),
        ("$TOT", f"{total:010d}"),
        ("$CYT", "Example"),
        ("$BEGINSTEXT", "0"),
        ("$ENDSTEXT", "0"),
        ("$ENDDATA", f"{data_end:010d}"),
        ("$BEGINDATA", f"{data_begin:010d}"),
        ("$BEGINANALYSIS", "0"),
        ("$ENDANALYSIS", "0"),
        ("IC$Note", "x"),
      ]
    )

  def test_whole_file(self) -> None:
    """HEADER, TEXT and DATA of a small file, byte for byte."""
    written = fcs.write_fcs(small_events(), {"IC$Note": "x", "$CYT": "Example"})
    # 48 keys and values of 247 characters in all and 49 delimiters: TEXT is 296 bytes, bytes
    # 58 to 353. DATA (16 bytes) is bytes 354 to 369.
    self.assertEqual(len(self.expected_text(0, 0)), 296)
    header = b"FCS3.0    " + b"      58     353     354     369       0       0"
    self.assertEqual(len(header), 58)
    self.assertEqual(written, header + self.expected_text(354, 369) + BIG_ENDIAN_DATA)
    self.assertEqual(len(written), 370)

  def test_data_is_big_endian_single_precision(self) -> None:
    """Values are IEEE 754 singles, most significant byte first."""
    written = fcs.write_fcs(EventData(["A"], [1.0, -2.0, 0.1]))
    self.assertEqual(written[-12:-8], b"\x3f\x80\x00\x00")
    self.assertEqual(written[-8:-4], b"\xc0\x00\x00\x00")
    self.assertEqual(written[-4:], b"\x3d\xcc\xcc\xcd")

  def test_file_without_events(self) -> None:
    """A file without events has TEXT and no DATA."""
    empty = EventData(["A", "Time"], long_names=["Alpha", "Time"])
    written = fcs.write_fcs(empty, {"$CYT": "Example", "IC$Note": "x"})
    # An empty DATA segment is stated as the one byte after TEXT.
    header = b"FCS3.0    " + b"      58     353     354     354       0       0"
    self.assertEqual(written, header + self.expected_text(354, 354, total=0))
    events, keywords = fcs.read_fcs(written)
    self.assertEqual(events.event_count, 0)
    self.assertEqual(events.parameter_names, ("A", "Time"))
    self.assertEqual(keywords["$TOT"], "0000000000")

  def test_stream_writer_gives_the_same_bytes(self) -> None:
    """Writing to a stream and returning bytes give the same file."""
    stream = io.BytesIO()
    fcs.write_fcs_to(stream, small_events(), {"$CYT": "Example"})
    self.assertEqual(stream.getvalue(), fcs.write_fcs(small_events(), {"$CYT": "Example"}))

  def test_data_longer_than_one_chunk(self) -> None:
    """DATA written in several pieces is whole."""
    count = (1 << 18) + 3
    values = array.array("f", (float(index % 1000) for index in range(count)))
    events, _ = fcs.read_fcs(fcs.write_fcs(EventData(["A"], values)))
    self.assertEqual(events.values, values)

  def test_keyword_order_of_instrument_files(self) -> None:
    """Keywords are written in the order of the instrument's files, whatever order they come in."""
    keywords = {
      "IC$Later": "1",
      "$ETIM": "e",
      "$BTIM": "b",
      "$PROJ": "p",
      "$TIMESTEP": "t",
      "$SPILLOVER": "s",
      "$CYT": "c",
      "$DATE": "d",
      "$SMNO": "n",
      "$OP": "o",
      "$FIL": "f",
      "$SRC": "x",
    }
    text = fcs.write_fcs(EventData(["A"]), keywords)[58:].decode()
    keys = text.strip("/").split("/")[0::2]
    self.assertEqual(
      keys,
      ["$FIL", "$OP", "$SMNO", "$DATATYPE", "$MODE", "$BYTEORD", "$NEXTDATA", "$PAR"]
      + ["$P1B", "$P1R", "$P1N", "$P1E", "$P1S", "$TOT"]
      + ["$DATE", "$CYT", "$SPILLOVER", "$TIMESTEP", "$PROJ", "$BTIM", "$ETIM"]
      + ["$BEGINSTEXT", "$ENDSTEXT", "$ENDDATA", "$BEGINDATA", "$BEGINANALYSIS", "$ENDANALYSIS"]
      + ["IC$Later", "$SRC"],
    )


class HeaderOffsetTests(unittest.TestCase):
  """HEADER offsets, and files too large for them."""

  def test_fields(self) -> None:
    """Six right-justified fields follow the version."""
    self.assertEqual(
      fcs._header_segment(1000, 4000),
      b"FCS3.0    " + b"      58    1057    1058    5057       0       0",
    )

  def test_last_offset_a_header_can_hold(self) -> None:
    """DATA ending at byte 99,999,999 is still stated in the HEADER."""
    # TEXT of 942 bytes ends at byte 999; DATA then starts at byte 1000.
    data_length = 99_999_999 - 1000 + 1
    self.assertEqual(
      fcs._header_segment(942, data_length),
      b"FCS3.0    " + b"      58     999    100099999999       0       0",
    )
    self.assertEqual(fcs._data_offsets(942, data_length), (1000, 99_999_999))

  def test_data_beyond_what_a_header_can_hold(self) -> None:
    """DATA ending later puts 0 in both HEADER fields; the keywords hold the offsets."""
    data_length = 99_999_999 - 1000 + 2
    self.assertEqual(
      fcs._header_segment(942, data_length),
      b"FCS3.0    " + b"      58     999       0       0       0       0",
    )
    # The keywords still carry the real offsets, as 10 digits.
    self.assertEqual(fcs._data_offsets(942, data_length), (1000, 100_000_000))
    self.assertEqual(fcs._offset_text(100_000_000), "0100000000")
    self.assertEqual(fcs._offset_text(9_999_999_999), "9999999999")
    with self.assertRaisesRegex(FcsError, "too large"):
      fcs._offset_text(10_000_000_000)

  def test_text_too_long_for_a_header(self) -> None:
    """TEXT must end within a HEADER field."""
    # TEXT of 99,999,942 bytes ends at byte 99,999,999, the last a field can hold.
    self.assertEqual(fcs._header_segment(99_999_942, 0)[18:26], b"99999999")
    with self.assertRaisesRegex(FcsError, "TEXT is too long"):
      fcs._header_segment(99_999_943, 0)

  def test_reader_uses_the_keywords_when_the_header_says_zero(self) -> None:
    """A file whose HEADER states no DATA offsets is read through its keywords."""
    written = fcs.write_fcs(small_events())
    zeroed = written[:26] + b"       0       0" + written[42:]
    self.assertNotEqual(zeroed, written)
    events, _ = fcs.read_fcs(zeroed)
    self.assertEqual(events, small_events())

  def test_reader_prefers_the_keywords_to_the_header(self) -> None:
    """The DATA keywords take the place of the HEADER fields."""
    written = fcs.write_fcs(small_events())
    misleading = written[:26] + b"      58      73" + written[42:]
    events, _ = fcs.read_fcs(misleading)
    self.assertEqual(events, small_events())


class DelimiterTests(unittest.TestCase):
  """Delimiters inside keys and values."""

  def test_delimiter_is_doubled(self) -> None:
    """A delimiter inside a key or value is written twice and read back as one."""
    written = fcs.write_fcs(small_events(), {"IC$Filter": "530/30 / 675/30", "IC$A/B": "c"})
    self.assertIn(b"/IC$Filter/530//30 // 675//30/IC$A//B/c/", written)
    _, keywords = fcs.read_fcs(written)
    self.assertEqual(keywords["IC$Filter"], "530/30 / 675/30")
    self.assertEqual(keywords["IC$A/B"], "c")

  def test_other_delimiter(self) -> None:
    """Another delimiter leaves slashes alone."""
    written = fcs.write_fcs(small_events(), {"IC$Path": "a/b|c"}, delimiter="|")
    self.assertEqual(written[58:59], b"|")
    self.assertIn(b"|IC$Path|a/b||c|", written)
    self.assertIn(b"|$BYTEORD|4,3,2,1|", written)
    events, keywords = fcs.read_fcs(written)
    self.assertEqual(keywords["IC$Path"], "a/b|c")
    self.assertEqual(events, small_events())

  def test_form_feed_delimiter(self) -> None:
    """A control character can delimit."""
    written = fcs.write_fcs(small_events(), {"IC$Path": "a/b"}, delimiter="\x0c")
    self.assertIn(b"\x0cIC$Path\x0ca/b\x0c", written)
    self.assertEqual(fcs.read_fcs(written)[1]["IC$Path"], "a/b")

  def test_values_that_cannot_be_delimited(self) -> None:
    """Empty keys and values, and ones starting or ending with the delimiter, are refused."""
    for keywords, message in (
      ({"IC$Empty": ""}, "the value of IC\\$Empty is empty"),
      ({"": "x"}, "a keyword is empty"),
      ({"IC$Lead": "/x"}, "starts or ends with the delimiter"),
      ({"IC$Trail": "x/"}, "starts or ends with the delimiter"),
      ({"/IC$Key": "x"}, "starts or ends with the delimiter"),
    ):
      with self.assertRaisesRegex(FcsError, message):
        fcs.write_fcs(small_events(), keywords)
    self.assertIn(b"|IC$Lead|/x|", fcs.write_fcs(small_events(), {"IC$Lead": "/x"}, delimiter="|"))

  def test_delimiters_that_cannot_be_used(self) -> None:
    """The delimiter is one ASCII character other than NUL and DEL."""
    for delimiter in ("", "//", "\x00", "\x7f", "é"):
      with self.assertRaisesRegex(FcsError, "cannot delimit TEXT"):
        fcs.write_fcs(small_events(), delimiter=delimiter)

  def test_keywords_the_writer_owns(self) -> None:
    """Keywords that follow from the events cannot be supplied."""
    for key in ("$TOT", "$tot", "$PAR", "$P1N", "$p2s", "$BEGINDATA", "$BYTEORD", "$P12B"):
      with self.assertRaisesRegex(FcsError, "cannot be supplied"):
        fcs.write_fcs(small_events(), {key: "1"})
    # Other per-parameter keywords are the caller's.
    self.assertIn(b"/$P1G/2/", fcs.write_fcs(small_events(), {"$P1G": "2"}))

  def test_keyword_spelling(self) -> None:
    """Standard keywords are written in upper case; keywords are ASCII and unique."""
    written = fcs.write_fcs(small_events(), {"$cyt": "c", "IC$SerialNumber": "s"})
    self.assertIn(b"/$CYT/c/", written)
    self.assertIn(b"/IC$SerialNumber/s/", written)
    with self.assertRaisesRegex(FcsError, "supplied twice"):
      fcs.write_fcs(small_events(), {"$cyt": "c", "$CYT": "d"})
    with self.assertRaisesRegex(FcsError, "not ASCII"):
      fcs.write_fcs(small_events(), {"IC$Größe": "1"})

  def test_values_outside_ascii_are_utf8(self) -> None:
    """Values are written and read as UTF-8."""
    written = fcs.write_fcs(small_events(), {"$OP": "Zoë"})
    self.assertIn("/$OP/Zoë/".encode("utf-8"), written)
    events, keywords = fcs.read_fcs(written)
    self.assertEqual(keywords["$OP"], "Zoë")
    self.assertEqual(events, small_events())


class RoundTripTests(unittest.TestCase):
  """Reading back what was written."""

  def test_events_and_keywords(self) -> None:
    """A file of the instrument's shape reads back with its events and keywords."""
    table = fcs.PARAMETER_TABLES["VBR"]
    values = [float(event * 100 + parameter) for event in range(7) for parameter in range(32)]
    events = table.new_events(values)
    keywords = fcs.instrument_keywords(
      table,
      operator="A. Operator",
      acquired_on=datetime.date(2026, 11, 5),
      events=events,
      metadata={"SerialNumber": "EX-0001", "Protocol": "Sip Time: 0:1.0, Pump: 29 rpm"},
    )
    read_events, read_keywords = fcs.read_fcs(fcs.write_fcs(events, keywords))
    self.assertEqual(read_events, events)
    for key, value in keywords.items():
      self.assertEqual(read_keywords[key], value)
    self.assertEqual(read_keywords["$PAR"], "32")
    self.assertEqual(read_keywords["$TOT"], "0000000007")
    self.assertEqual(read_keywords["$P32N"], "Time")
    self.assertEqual(read_keywords["$P3S"], "BL1-H")
    self.assertEqual(read_keywords["$P17R"], "16777216")
    self.assertEqual(read_keywords["$BYTEORD"], "4,3,2,1")

  def test_special_values_survive(self) -> None:
    """Values are stored bit for bit."""
    values = [0.0, -0.0, 16777215.0, 1e-30, float("inf"), -1.0]
    events, _ = fcs.read_fcs(fcs.write_fcs(EventData(["A", "B"], values)))
    self.assertEqual(events.values.tobytes(), array.array("f", values).tobytes())

  def test_checksum(self) -> None:
    """The checksum is the standard CRC-32 of the file."""
    self.assertEqual(fcs.file_crc32(b"123456789"), 0xCBF43926)
    self.assertEqual(fcs.file_crc32(b""), 0)
    written = fcs.write_fcs(small_events())
    self.assertNotEqual(fcs.file_crc32(written), fcs.file_crc32(written[:-1] + b"\x01"))


class ReadTests(unittest.TestCase):
  """Reading files this module did not write."""

  def test_little_endian_file(self) -> None:
    """A file in the other byte order is read."""
    data = struct.pack("<4f", *SMALL_VALUES)
    self.assertNotEqual(data, BIG_ENDIAN_DATA)
    events, keywords = fcs.read_fcs(file_of(minimal_pairs("1,2,3,4"), data))
    self.assertEqual(events, EventData(["A", "Time"], SMALL_VALUES))
    self.assertEqual(keywords["$BYTEORD"], "1,2,3,4")

  def test_big_endian_file_with_header_offsets_only(self) -> None:
    """A file without DATA keywords is read through its HEADER."""
    events, keywords = fcs.read_fcs(file_of(minimal_pairs(), BIG_ENDIAN_DATA))
    self.assertEqual(events, EventData(["A", "Time"], SMALL_VALUES))
    self.assertNotIn("$BEGINDATA", keywords)
    self.assertEqual(keywords["$P1R"], "1024")

  def test_other_delimiter_and_version(self) -> None:
    """The delimiter is whatever TEXT starts with; FCS 3.1 is read like 3.0."""
    pairs = minimal_pairs() + [("$P1S", "Alpha"), ("Note", "kept as written")]
    events, keywords = fcs.read_fcs(
      file_of(pairs, BIG_ENDIAN_DATA, delimiter="\x0c", version="FCS3.1")
    )
    self.assertEqual(events, small_events())
    self.assertEqual(keywords["Note"], "kept as written")

  def test_instrument_style_file(self) -> None:
    """A file laid out as the instrument writes it is read."""

    # Zero-padded counts and offsets, the supplemental TEXT keywords pointing at TEXT itself,
    # a spillover matrix and instrument metadata.
    def pairs(text_end: int, data_begin: int, data_end: int) -> List[Tuple[str, str]]:
      """Return the keywords of an instrument file for given offsets."""
      return [
        ("$FIL", "IntelliCyt_iQue3.fcs"),
        ("$OP", "UNK"),
        ("$SMNO", "A01"),
        ("$DATATYPE", "F"),
        ("$MODE", "L"),
        ("$BYTEORD", "4,3,2,1"),
        ("$NEXTDATA", "0"),
        ("$PAR", "2"),
        ("$P1B", "32"),
        ("$P2B", "32"),
        ("$P1R", "16777216"),
        ("$P2R", "16777216"),
        ("$P1N", "A"),
        ("$P2N", "Time"),
        ("$P1E", "0,0"),
        ("$P2E", "0,0"),
        ("$P1S", "A"),
        ("$P2S", "Time"),
        ("$TOT", "0000000002"),
        ("$DATE", "5-NOV-2026"),
        ("$CYT", "IntelliCyt iQue3"),
        ("$SPILLOVER", "2,1,2,1.000000,0.000000,0.000000,1.000000"),
        ("$TIMESTEP", "0.005"),
        ("$PROJ", "iQue3"),
        ("$BTIM", "00:00:01"),
        ("$ETIM", "00:00:02"),
        ("$BEGINSTEXT", "0000000058"),
        ("$ENDSTEXT", f"{text_end:010d}"),
        ("$ENDDATA", f"{data_end:010d}"),
        ("$BEGINDATA", f"{data_begin:010d}"),
        ("$BEGINANALYSIS", "0"),
        ("$ENDANALYSIS", "0"),
        ("IC$SerialNumber", "Unknown"),
      ]

    length = len(text_of(pairs(0, 0, 0)))
    begin = 58 + length
    text = text_of(pairs(begin - 1, begin, begin + 15))
    data = header_of(length, begin, begin + 15) + text + BIG_ENDIAN_DATA
    events, keywords = fcs.read_fcs(data)
    self.assertEqual(events, EventData(["A", "Time"], SMALL_VALUES))
    self.assertEqual(keywords["IC$SerialNumber"], "Unknown")
    self.assertEqual(fcs.time_step(keywords), 0.005)
    self.assertEqual(list(fcs.event_times(events, fcs.time_step(keywords))), [1.0, 2.0])

  def test_instrument_style_empty_file(self) -> None:
    """An empty file states its DATA as the byte after TEXT, which does not exist."""
    pairs = changed(minimal_pairs(), "$TOT", "0000000000")
    text = text_of(pairs)
    begin = 58 + len(text)
    events, _ = fcs.read_fcs(header_of(len(text), begin, begin) + text)
    self.assertEqual(events.event_count, 0)

  def test_supplemental_text(self) -> None:
    """Keywords of a second TEXT segment are added."""
    extra = text_of([("IC$Extra", "more")])
    primary_length = len(
      text_of(minimal_pairs() + [("$BEGINSTEXT", "0" * 10), ("$ENDSTEXT", "0" * 10)])
    )
    data_begin = 58 + primary_length
    extra_begin = data_begin + 16
    pairs = minimal_pairs() + [
      ("$BEGINSTEXT", f"{extra_begin:010d}"),
      ("$ENDSTEXT", f"{extra_begin + len(extra) - 1:010d}"),
    ]
    text = text_of(pairs)
    data = header_of(len(text), data_begin, data_begin + 15) + text + BIG_ENDIAN_DATA + extra
    events, keywords = fcs.read_fcs(data)
    self.assertEqual(events, EventData(["A", "Time"], SMALL_VALUES))
    self.assertEqual(keywords["IC$Extra"], "more")

  def test_padding_after_text(self) -> None:
    """Spaces after the closing delimiter are padding."""
    text = text_of(minimal_pairs()) + b"    "
    begin = 58 + len(text)
    data = header_of(len(text), begin, begin + 15) + text + BIG_ENDIAN_DATA
    self.assertEqual(fcs.read_fcs(data)[0], EventData(["A", "Time"], SMALL_VALUES))

  def test_standard_keywords_are_upper_case(self) -> None:
    """Standard keywords are returned in upper case, others as written."""
    pairs = changed(minimal_pairs(), "$TOT", None) + [("$tot", "2"), ("Mixed", "1")]
    _, keywords = fcs.read_fcs(file_of(pairs, BIG_ENDIAN_DATA))
    self.assertEqual(keywords["$TOT"], "2")
    self.assertIn("Mixed", keywords)

  def test_data_segment_longer_than_the_events(self) -> None:
    """Bytes beyond the events are left out, with a warning."""
    with self.assertLogs(fcs.logger, "WARNING"):
      events, _ = fcs.read_fcs(file_of(minimal_pairs(), BIG_ENDIAN_DATA + b"\x00" * 8))
    self.assertEqual(events, EventData(["A", "Time"], SMALL_VALUES))


class ReadRefusalTests(unittest.TestCase):
  """Files the reader must refuse loudly."""

  def assert_refused(self, data: bytes, message: str) -> None:
    """Check that data is refused with a message."""
    with self.assertRaisesRegex(FcsError, message):
      fcs.read_fcs(data)

  def assert_keyword_refused(self, key: str, value: Optional[str], message: str) -> None:
    """Check that the minimal file with one keyword changed or removed is refused."""
    self.assert_refused(file_of(changed(minimal_pairs(), key, value), BIG_ENDIAN_DATA), message)

  def test_not_fcs(self) -> None:
    """Other data, other versions and a broken HEADER are refused."""
    self.assert_refused(b"", "not an FCS 3 file")
    self.assert_refused(b"PK\x03\x04" + b" " * 100, "not an FCS 3 file")
    self.assert_refused(file_of(minimal_pairs(), BIG_ENDIAN_DATA, version="FCS2.0"), "not an FCS 3")
    self.assert_refused(b"FCS3.0    " + b" " * 20, "ends inside its HEADER")
    self.assert_refused(b"FCS3.0    " + b"      58     x99" + b" " * 32, "is not a number")

  def test_data_types(self) -> None:
    """Integer, double and ASCII data are refused."""
    self.assert_keyword_refused("$DATATYPE", "I", "\\$DATATYPE 'I' is not supported")
    self.assert_keyword_refused("$DATATYPE", "D", "\\$DATATYPE 'D' is not supported")
    self.assert_keyword_refused("$DATATYPE", "A", "\\$DATATYPE 'A' is not supported")

  def test_byte_orders(self) -> None:
    """Mixed and short byte orders are refused."""
    self.assert_keyword_refused("$BYTEORD", "2,1,4,3", "\\$BYTEORD '2,1,4,3' is not supported")
    self.assert_keyword_refused("$BYTEORD", "1,2", "is not supported")

  def test_layout(self) -> None:
    """Histograms, other widths, logarithmic values and further data sets are refused."""
    self.assert_keyword_refused("$MODE", "H", "only list mode")
    self.assert_keyword_refused("$P2B", "64", "\\$P2B is 64")
    self.assert_keyword_refused("$P1E", "4,1", "only linear values")
    self.assert_keyword_refused("$P1E", "x", "only linear values")
    self.assert_keyword_refused("$NEXTDATA", "4096", "more than one data set")
    self.assert_keyword_refused("$PAR", "0", "no parameters")

  def test_linear_amplification_spellings(self) -> None:
    """Zero decades may be spelled many ways."""
    for spelling in ("0,0", "0.0,0.0", "0, 0", "0,1", "0.,0"):
      pairs = changed(minimal_pairs(), "$P1E", spelling)
      self.assertEqual(fcs.read_fcs(file_of(pairs, BIG_ENDIAN_DATA))[0].event_count, 2)

  def test_missing_keywords(self) -> None:
    """A keyword needed to read DATA must be there."""
    for key in ("$TOT", "$PAR", "$DATATYPE", "$BYTEORD", "$MODE", "$P2N", "$P1B"):
      self.assert_keyword_refused(key, None, f"required keyword \\{key} is missing")

  def test_malformed_numbers(self) -> None:
    """Counts are whole, non-negative numbers."""
    self.assert_keyword_refused("$TOT", "two", "\\$TOT 'two' is not a whole number")
    self.assert_keyword_refused("$PAR", "-2", "is not a whole number")

  def test_repeated_parameter_names(self) -> None:
    """Two parameters cannot share a name."""
    self.assert_keyword_refused("$P2N", "A", "parameters of the file are unusable")

  def test_data_that_does_not_fit(self) -> None:
    """DATA shorter than its events, cut off or not located is refused."""
    self.assert_refused(file_of(minimal_pairs(), BIG_ENDIAN_DATA[:-1]), "cannot hold 16 bytes")
    whole = file_of(minimal_pairs(), BIG_ENDIAN_DATA)
    self.assert_refused(whole[:-4], "cannot hold 16 bytes")
    self.assert_refused(file_of(minimal_pairs(events=3), BIG_ENDIAN_DATA), "cannot hold 24 bytes")
    text = text_of(minimal_pairs())
    self.assert_refused(header_of(len(text), 0, 0) + text + BIG_ENDIAN_DATA, "cannot hold 16 bytes")

  def test_malformed_text(self) -> None:
    """TEXT that is cut off, repeats a keyword, is not text or lies outside the file is refused."""
    text = text_of(minimal_pairs())
    begin = 58 + len(text)

    def with_text(changed_text: bytes) -> bytes:
      """Return the minimal file with another TEXT."""
      end = 58 + len(changed_text)
      return header_of(len(changed_text), end, end + 15) + changed_text + BIG_ENDIAN_DATA

    self.assert_refused(with_text(text + b"$CYT/"), "keyword '\\$CYT' has no value")
    self.assert_refused(with_text(text[:-1]), "does not end with its delimiter")
    self.assert_refused(with_text(text + b"$TOT/3/"), "keyword \\$TOT occurs twice")
    self.assert_refused(with_text(text + b"$CYT/\xff\xfe/"), "not UTF-8 text")
    self.assert_refused(header_of(10_000, begin, begin + 15) + text, "not inside the file")
    self.assert_refused(
      b"FCS3.0    " + b"      58      57" + b" " * 32 + text, "not inside the file"
    )

  def test_empty_tokens(self) -> None:
    """Empty keys and values are refused; pairs of delimiters are not."""
    for text in ("//", "//a/", "/a/b/ /c//"):
      with self.assertRaisesRegex(FcsError, "empty key or value|does not end"):
        fcs._split_text(text)
    with self.assertRaisesRegex(FcsError, "TEXT segment is empty"):
      fcs._split_text("")
    self.assertEqual(fcs._split_text("/"), [])
    self.assertEqual(fcs._split_text("/a/b//c/"), ["a", "b/c"])
    self.assertEqual(fcs._split_text("|a|b||c|d|  \x00"), ["a", "b|c", "d"])


class TimeTests(unittest.TestCase):
  """The time parameter."""

  def test_ticks_to_seconds(self) -> None:
    """Times are ticks times the time step, in double precision."""
    events = EventData(["A", "Time"], [9, 0, 9, 1, 9, 200, 9, 16_000_000])
    times = fcs.event_times(events)
    self.assertEqual(times.typecode, "d")
    self.assertEqual(list(times), [0.0, 0.005, 1.0, 80_000.0])
    self.assertEqual(fcs.TIME_STEP, 0.005)
    self.assertEqual(list(fcs.event_times(events, 0.1))[:3], [0.0, 0.1, 20.0])

  def test_time_parameter(self) -> None:
    """The time parameter is the first whose name contains time."""
    self.assertEqual(fcs.time_parameter_index(["FSC-H", "TIME", "Time"]), 1)
    self.assertEqual(fcs.time_parameter_index(["HDR-T", "Event time (ticks)"]), 1)
    with self.assertRaisesRegex(FcsError, "no time parameter"):
      fcs.time_parameter_index(["FSC-H", "Width"])
    with self.assertRaisesRegex(FcsError, "no time parameter"):
      fcs.event_times(EventData(["A", "B"]))

  def test_time_step_of_a_file(self) -> None:
    """The time step is a positive number in a keyword."""
    self.assertEqual(fcs.time_step({"$TIMESTEP": "0.005"}), 0.005)
    self.assertEqual(fcs.time_step({"$TIMESTEP": " 1e-2 "}), 0.01)
    for keywords, message in (
      ({}, "does not state its time step"),
      ({"$TIMESTEP": "0,005"}, "is not a number"),
      ({"$TIMESTEP": "0"}, "is not a positive number"),
      ({"$TIMESTEP": "nan"}, "is not a positive number"),
    ):
      with self.assertRaisesRegex(FcsError, message):
        fcs.time_step(keywords)


class InstrumentKeywordTests(unittest.TestCase):
  """The keywords of this instrument's files."""

  def test_all_keywords(self) -> None:
    """Every keyword of an instrument file; times are elapsed and wrap at 24 hours."""
    table = fcs.PARAMETER_TABLES["BR"]
    # Events at ticks 13000 (65 s) and 17,290,000 (86,450 s = 24 h 0 min 50 s).
    events = table.new_events([0.0] * 17 + [13000.0] + [0.0] * 17 + [17_290_000.0])
    keywords = fcs.instrument_keywords(
      table,
      operator="A. Operator",
      sample="Plate 7",
      acquired_on=datetime.date(2026, 11, 5),
      events=events,
      metadata={"SerialNumber": "EX-0001", "DetectorType": "iQue PLUS - BR"},
    )
    self.assertEqual(
      keywords,
      {
        "$FIL": "IntelliCyt_iQuePlusBR.fcs",
        "$OP": "A. Operator",
        "$SMNO": "Plate 7",
        "$DATE": "05-NOV-2026",
        "$CYT": "IntelliCyt iQuePlus BR",
        "$TIMESTEP": "0.005",
        "$PROJ": "iQuePlusBR",
        "$BTIM": "00:01:05",
        "$ETIM": "00:00:50",
        "IC$SerialNumber": "EX-0001",
        "IC$DetectorType": "iQue PLUS - BR",
      },
    )

  def test_defaults(self) -> None:
    """Without optional arguments only the constants appear."""
    self.assertEqual(
      fcs.instrument_keywords(fcs.PARAMETER_TABLES["VBR"]),
      {
        "$FIL": "IntelliCyt_iQue3.fcs",
        "$OP": "UNK",
        "$SMNO": "A01",
        "$CYT": "IntelliCyt iQue3",
        "$TIMESTEP": "0.005",
        "$PROJ": "iQue3",
      },
    )

  def test_times_without_events(self) -> None:
    """Without events both times are zero."""
    table = fcs.PARAMETER_TABLES["VYB"]
    keywords = fcs.instrument_keywords(table, events=table.new_events())
    self.assertEqual((keywords["$BTIM"], keywords["$ETIM"]), ("00:00:00", "00:00:00"))

  def test_dates(self) -> None:
    """Dates have a two-digit day and an English month."""
    table = fcs.PARAMETER_TABLES["VBR"]
    for date, text in (
      (datetime.date(2026, 1, 31), "31-JAN-2026"),
      (datetime.date(2027, 12, 1), "01-DEC-2027"),
      (datetime.datetime(2026, 8, 9, 23, 59), "09-AUG-2026"),
    ):
      self.assertEqual(fcs.instrument_keywords(table, acquired_on=date)["$DATE"], text)

  def test_time_step_text(self) -> None:
    """A small time step is written without exponent."""
    table = fcs.PARAMETER_TABLES["VBR"]
    self.assertEqual(fcs.instrument_keywords(table, step=0.00001)["$TIMESTEP"], "0.00001")


class WellFileTests(unittest.TestCase):
  """One file per well."""

  def setUp(self) -> None:
    """Make six events and the ranges of three wells, one of them empty."""
    # Six events; parameter A is the event number, Time is 100 ticks per event.
    values = [value for event in range(6) for value in (float(event), float(event * 100))]
    self.events = EventData(["A", "Time"], values)
    self.ranges: Dict[str, Optional[Tuple[int, int]]] = {"A01": (0, 1), "A02": None, "A03": (2, 5)}

  def test_file_names(self) -> None:
    """File names are built from plain well names only."""
    self.assertEqual(fcs.well_file_name("A01"), "Well_A01.fcs")
    self.assertEqual(fcs.well_file_name("AF48"), "Well_AF48.fcs")
    for name in ("", "A/1", "../A1", "A 1"):
      with self.assertRaises(ValueError):
        fcs.well_file_name(name)

  def test_files(self) -> None:
    """Each well gets its events, unshifted times, the shared keywords and its name."""
    files = dict(fcs.well_fcs_files(self.events, self.ranges, {"$CYT": "Example"}))
    self.assertEqual(list(files), ["Well_A01.fcs", "Well_A02.fcs", "Well_A03.fcs"])
    first, first_keywords = fcs.read_fcs(files["Well_A01.fcs"])
    self.assertEqual(first, self.events.slice(0, 2))
    self.assertEqual(first_keywords["IC$WellID"], "A01")
    self.assertEqual(first_keywords["$CYT"], "Example")
    self.assertEqual(first_keywords["$TOT"], "0000000002")
    empty, empty_keywords = fcs.read_fcs(files["Well_A02.fcs"])
    self.assertEqual(empty.event_count, 0)
    self.assertEqual(empty_keywords["$TOT"], "0000000000")
    self.assertEqual(empty_keywords["IC$WellID"], "A02")
    last, _ = fcs.read_fcs(files["Well_A03.fcs"])
    # Times are those of the plate acquisition, not counted from the well's start.
    self.assertEqual(list(last.parameter("Time")), [200.0, 300.0, 400.0, 500.0])
    self.assertEqual(list(last.parameter("A")), [2.0, 3.0, 4.0, 5.0])

  def test_ranges_outside_the_events(self) -> None:
    """A range beyond the events is refused."""
    for event_range in ((0, 6), (-1, 2), (3, 2)):
      with self.assertRaisesRegex(FcsError, "well B01: events .* are not within the acquisition"):
        list(fcs.well_fcs_files(self.events, {"B01": event_range}))

  def test_keywords_that_already_name_a_well(self) -> None:
    """Shared keywords cannot hold the well keyword."""
    with self.assertRaisesRegex(FcsError, "already hold IC\\$WellID"):
      list(fcs.well_fcs_files(self.events, self.ranges, {"IC$WELLID": "X"}))

  def test_writing_a_directory(self) -> None:
    """Files are written into a new directory and not replaced unless asked."""
    with tempfile.TemporaryDirectory() as folder:
      target = pathlib.Path(folder) / "plate 1" / "wells"
      paths = fcs.write_well_fcs_files(target, self.events, self.ranges, {"$CYT": "Example"})
      self.assertEqual(
        paths, [target / "Well_A01.fcs", target / "Well_A02.fcs", target / "Well_A03.fcs"]
      )
      expected = dict(fcs.well_fcs_files(self.events, self.ranges, {"$CYT": "Example"}))
      for path in paths:
        self.assertEqual(path.read_bytes(), expected[path.name])
      with self.assertRaises(FileExistsError):
        fcs.write_well_fcs_files(target, self.events, self.ranges)
      self.assertEqual(paths[0].read_bytes(), expected["Well_A01.fcs"])
      fcs.write_well_fcs_files(target, self.events, self.ranges, overwrite=True)
      self.assertNotEqual(paths[0].read_bytes(), expected["Well_A01.fcs"])


if __name__ == "__main__":
  unittest.main()
