"""Tests for the typed values of binary XML text records and their character forms."""

import copy
import datetime
import decimal
import math
import pickle
import unittest
import uuid
from typing import Dict, List, Tuple

from pylabrobot.sartorius.ique.driver.wcf import values
from pylabrobot.sartorius.ique.driver.wcf.values import (
  DateTime,
  DictionaryString,
  QName,
  Single,
  TextValue,
  TimeSpan,
  UniqueId,
)

UTC = datetime.timezone.utc
D = decimal.Decimal


class DateTimeTests(unittest.TestCase):
  """A DateTime keeps the 100 ns digit and the kind of a .NET DateTime."""

  def test_ticks_round_trip_with_the_hundred_nanosecond_digit(self) -> None:
    """The digit below microseconds survives the conversion from and to ticks."""
    value = DateTime.from_ticks(639299820301234567, "utc")
    self.assertEqual((value.microsecond, value.tick, value.kind), (123456, 7, "utc"))
    self.assertEqual(value, datetime.datetime(2026, 11, 11, 8, 20, 30, 123456, UTC))
    self.assertEqual(value.ticks, 639299820301234567)

  def test_extreme_values(self) -> None:
    """The smallest and largest .NET DateTime are representable."""
    self.assertEqual(DateTime.from_ticks(0), datetime.datetime(1, 1, 1))
    last = DateTime.from_ticks(values.MAX_DATETIME_TICKS)
    self.assertEqual((last.year, last.microsecond, last.tick), (9999, 999999, 9))
    with self.assertRaises(ValueError):
      DateTime.from_ticks(values.MAX_DATETIME_TICKS + 1)

  def test_kind_is_inferred_from_tzinfo(self) -> None:
    """Naive is unspecified, zero offset is UTC and any other offset is local."""
    plus_two = datetime.timezone(datetime.timedelta(hours=2))
    self.assertEqual(DateTime(2026, 1, 2).kind, "unspecified")
    self.assertEqual(DateTime(2026, 1, 2, tzinfo=UTC).kind, "utc")
    self.assertEqual(DateTime(2026, 1, 2, tzinfo=plus_two).kind, "local")
    self.assertEqual(DateTime(2026, 1, 2, tzinfo=UTC, kind="local").kind, "local")

  def test_kind_must_fit_tzinfo(self) -> None:
    """An aware value cannot be unspecified and a naive one cannot be UTC or local."""
    with self.assertRaises(ValueError):
      DateTime(2026, 1, 2, kind="utc")
    with self.assertRaises(ValueError):
      DateTime(2026, 1, 2, tzinfo=UTC, kind="unspecified")
    with self.assertRaises(ValueError):
      DateTime(2026, 1, 2, tick=10)

  def test_binary_form(self) -> None:
    """The kind occupies the two bits above 62 bits of ticks."""
    # [MC-NBFX] section 3, DateTimeText: FF 3F 37 F4 75 28 CA 2B is 9999-12-31T23:59:59.9999999.
    raw = int.from_bytes(bytes.fromhex("FF3F37F47528CA2B"), "little")
    self.assertEqual(values.datetime_from_binary(raw).ticks, values.MAX_DATETIME_TICKS)
    ticks = 639299820301234567
    for code, kind in enumerate(("unspecified", "utc", "local")):
      value = values.datetime_from_binary(ticks | code << 62)
      self.assertEqual((value.ticks, value.kind), (ticks, kind))
      self.assertEqual(values.datetime_to_binary(value), ticks | code << 62)
    with self.assertRaises(ValueError):
      values.datetime_from_binary(ticks | 3 << 62)

  def test_local_time_before_year_one_is_rejected(self) -> None:
    """.NET wraps a local time whose UTC instant precedes year 1; that has no datetime."""
    with self.assertRaises(ValueError):
      values.datetime_from_binary((2**62 - 36000000000) | 2 << 62)

  def test_plain_datetime_is_encoded_by_its_tzinfo(self) -> None:
    """A datetime that is not a DateTime gets its kind from tzinfo and travels as UTC."""
    plus_two = datetime.timezone(datetime.timedelta(hours=2))
    local = datetime.datetime(2026, 11, 11, 10, 20, 30, tzinfo=plus_two)
    utc = datetime.datetime(2026, 11, 11, 8, 20, 30, tzinfo=UTC)
    self.assertEqual(values.datetime_to_binary(local) >> 62, 2)
    self.assertEqual(
      values.datetime_to_binary(local) & (2**62 - 1), values.datetime_to_binary(utc) & (2**62 - 1)
    )

  def test_copy_and_pickle_keep_tick_and_kind(self) -> None:
    """Copies are made through the tick count."""
    value = DateTime(2026, 11, 11, 8, 20, 30, 123456, UTC, tick=7, kind="local")
    for clone in (copy.copy(value), copy.deepcopy(value), pickle.loads(pickle.dumps(value))):
      self.assertEqual((clone, clone.tick, clone.kind), (value, 7, "local"))

  def test_characters(self) -> None:
    """The character form is ISO 8601 with up to seven fraction digits and the kind's suffix."""
    naive = DateTime(2026, 11, 11, 10, 20, 30, 123456, tick=7)
    self.assertEqual(values.format_datetime(naive), "2026-11-11T10:20:30.1234567")
    self.assertEqual(values.format_datetime(DateTime(1, 1, 1)), "0001-01-01T00:00:00")
    utc = DateTime(2026, 11, 11, 9, 30, 15, 500000, UTC)
    self.assertEqual(values.format_datetime(utc), "2026-11-11T09:30:15.5Z")
    local = DateTime(2026, 11, 11, 9, 30, 15, tzinfo=UTC, kind="local")
    self.assertEqual(values.format_datetime(local), "2026-11-11T09:30:15+00:00")

  def test_parse(self) -> None:
    """Parsing accepts what formatting produces, with any UTC offset."""
    for text in ("2026-11-11T10:20:30.1234567", "0001-01-01T00:00:00", "2026-11-11T09:30:15.5Z"):
      self.assertEqual(values.format_datetime(values.parse_datetime(text)), text)
    value = values.parse_datetime("2026-11-11T10:20:30.5+02:00")
    self.assertEqual((value.kind, value.utcoffset()), ("local", datetime.timedelta(hours=2)))
    self.assertEqual(value, datetime.datetime(2026, 11, 11, 8, 20, 30, 500000, UTC))
    self.assertEqual(values.parse_datetime("2026-11-11"), datetime.datetime(2026, 11, 11))
    for text in ("", "2026-11-11 10:20:30", "2026-13-01T00:00:00", "yesterday"):
      with self.assertRaises(ValueError):
        values.parse_datetime(text)


class TimeSpanTests(unittest.TestCase):
  """A TimeSpan keeps the 100 ns digit of a .NET TimeSpan."""

  def test_ticks_round_trip(self) -> None:
    """Positive and negative tick counts survive, extremes included."""
    for ticks in (0, 1, -1, 15000001, -36000000001, 937840050006, 2**63 - 1, -(2**63)):
      self.assertEqual(TimeSpan.from_ticks(ticks).ticks, ticks)
    self.assertEqual(TimeSpan.from_ticks(15000001), datetime.timedelta(seconds=1.5))
    self.assertEqual(TimeSpan.from_ticks(15000001).tick, 1)
    with self.assertRaises(ValueError):
      TimeSpan.from_ticks(2**63)

  def test_copy_keeps_tick(self) -> None:
    """Copies are made through the tick count."""
    self.assertEqual(copy.deepcopy(TimeSpan.from_ticks(-36000000001)).ticks, -36000000001)

  def test_duration_characters(self) -> None:
    """A TimeSpan is written as .NET writes an XML Schema duration."""
    cases = {
      0: "PT0S",
      20000000: "PT2S",
      15000001: "PT1.5000001S",
      -15000000: "-PT1.5S",
      -36000000001: "-PT1H0.0000001S",
      937840050006: "P1DT2H3M4.0050006S",
      864000000000: "P1D",
      120000000000: "PT3H20M",
      -3440000000: "-PT5M44S",
    }
    for ticks, text in cases.items():
      self.assertEqual(values.format_duration(TimeSpan.from_ticks(ticks)), text)
      self.assertEqual(values.parse_duration(text).ticks, ticks)

  def test_parse_duration_counts_years_and_months_as_dotnet_does(self) -> None:
    """A year is 365 days and a month 30 days."""
    self.assertEqual(values.parse_duration("P1Y2M3D"), datetime.timedelta(days=365 + 60 + 3))
    self.assertEqual(values.parse_duration("P14M"), datetime.timedelta(days=365 + 60))
    for text in ("", "P", "PT", "1S", "PT1.5", "P1H"):
      with self.assertRaises(ValueError):
        values.parse_duration(text)


class NumberTests(unittest.TestCase):
  """Decimals, floats and their character forms."""

  def test_decimal_binary_form(self) -> None:
    """A decimal is the 16-byte DECIMAL structure: reserved, scale, sign, high, low."""
    # Both examples are from [MC-NBFX] section 3 (DecimalText).
    five = bytes.fromhex("0000060000000000802D4E0000000000")
    largest = bytes.fromhex("00000000FFFFFFFFFFFFFFFFFFFFFFFF")
    self.assertEqual(values.decimal_from_binary(five), D("5.123456"))
    self.assertEqual(values.decimal_to_binary(D("5.123456")), five)
    self.assertEqual(values.decimal_from_binary(largest), D(2**96 - 1))
    self.assertEqual(values.decimal_to_binary(D(2**96 - 1)), largest)

  def test_decimal_keeps_scale_and_sign(self) -> None:
    """Trailing zeros and the sign survive the binary form."""
    for text in ("123.4500", "-12.75", "0", "0.0000000000000000000000000001", "-0.0", "1E+2"):
      value = values.decimal_from_binary(values.decimal_to_binary(D(text)))
      self.assertEqual(value, D(text))
    kept = values.decimal_from_binary(values.decimal_to_binary(D("123.4500")))
    self.assertEqual(values.format_decimal(kept), "123.4500")

  def test_decimal_outside_dotnet_range_is_rejected(self) -> None:
    """A .NET decimal has a 96-bit mantissa and at most 28 decimal places."""
    for value in (D(2**96), D("1E-29"), D("NaN"), D("Infinity")):
      with self.assertRaises(ValueError):
        values.decimal_to_binary(value)

  def test_single_rounds_to_single_precision(self) -> None:
    """A Single holds the nearest single-precision value."""
    self.assertEqual(Single(0.1), 0.10000000149011612)
    self.assertEqual(Single(0.5), 0.5)
    self.assertEqual(Single(1e300), math.inf)
    self.assertTrue(math.isnan(Single(math.nan)))

  def test_float_characters(self) -> None:
    """Floats use the fewest digits of their precision and the XML names of special values."""
    cases = [
      (Single(0.1), "0.1"),
      (Single(3.1415927), "3.1415927"),
      (0.1, "0.1"),
      (0.10000000149011612, "0.10000000149011612"),
      (1e20, "1E+20"),
      (1e-5, "1E-05"),
      (-1.5, "-1.5"),
      (100000.0, "100000"),
      (math.inf, "INF"),
      (-math.inf, "-INF"),
      (math.nan, "NaN"),
      (-0.0, "-0"),
    ]
    for value, text in cases:
      self.assertEqual(values.format_float(value), text)
      parsed = values.parse_float(text)
      parsed = Single(parsed) if isinstance(value, Single) else parsed
      self.assertTrue(parsed == value or (math.isnan(parsed) and math.isnan(value)))

  def test_strict_parsing(self) -> None:
    """Python-only spellings are not accepted as XML values."""
    for parse, text in (
      (values.parse_int, "1_000"),
      (values.parse_int, "1.0"),
      (values.parse_float, "nan"),
      (values.parse_float, "1_0.0"),
      (values.parse_decimal, "1E+2"),
      (values.parse_bool, "True"),
      (values.parse_bytes, "not base64!"),
    ):
      with self.assertRaises(ValueError, msg=text):
        parse(text)
    self.assertEqual(values.parse_int(" -42 "), -42)
    self.assertEqual(values.parse_bool(" 1 "), True)
    self.assertEqual(values.parse_decimal("-12.750"), D("-12.750"))
    self.assertEqual(values.parse_bytes("AAEC\nAwQ="), bytes(range(5)))


class TextTests(unittest.TestCase):
  """The characters a typed value stands for."""

  def test_text_of_each_type(self) -> None:
    """Every text value has the character form [MC-NBFX] gives its record."""
    guid = uuid.UUID("33221100-5544-7766-8899-aabbccddeeff")
    cases: List[Tuple[TextValue, str]] = [
      (True, "true"),
      (False, "false"),
      (0, "0"),
      (-128, "-128"),
      (2**64 - 1, "18446744073709551615"),
      (D("5.123456"), "5.123456"),
      (guid, "33221100-5544-7766-8899-aabbccddeeff"),
      (UniqueId(int=guid.int), "urn:uuid:33221100-5544-7766-8899-aabbccddeeff"),
      (b"\x00\x01\x02\x03\x04\x05\x06\x07", "AAECAwQFBgc="),
      (b"\xff", "/w=="),
      (QName("b", "name"), "b:name"),
      (QName("", "name"), "name"),
      ((123, "hello", True), "123 hello true"),
      (DictionaryString("hello"), "hello"),
      (TimeSpan.from_ticks(-3440000000), "-PT5M44S"),
    ]
    for value, text in cases:
      self.assertEqual(values.text_of(value), text)
    with self.assertRaises(TypeError):
      values.text_of(object())  # type: ignore[arg-type]

  def test_unique_id_equals_its_uuid(self) -> None:
    """A UniqueId is a UUID with another character form."""
    guid = uuid.uuid4()
    self.assertEqual(UniqueId(int=guid.int), guid)
    self.assertEqual(str(UniqueId(str(guid))), "urn:uuid:" + str(guid))

  def test_dictionary_string_is_a_string(self) -> None:
    """A DictionaryString compares and hashes as its characters."""
    self.assertEqual(DictionaryString("Envelope"), "Envelope")
    table: Dict[str, int] = {DictionaryString("a"): 1}
    self.assertEqual(table["a"], 1)


if __name__ == "__main__":
  unittest.main()
