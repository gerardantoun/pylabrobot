"""Typed values of .NET Binary XML text records, and the characters they stand for.

[MC-NBFX] section 2.2.3 gives every text record a typed payload and the XML characters it
represents. The types here keep what Python's own types cannot (100 ns resolution, the
DateTime kind, whether a string may travel as a dictionary reference). The functions convert
between a typed value and its characters, for peers that send a value as plain text.
"""

from __future__ import annotations

import base64
import datetime
import decimal
import math
import re
import struct
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional, SupportsIndex, Tuple, Union

DateTimeKind = Literal["unspecified", "utc", "local"]

TICKS_PER_SECOND = 10_000_000
MAX_DATETIME_TICKS = 3_155_378_975_999_999_999
MIN_TIMESPAN_TICKS = -(2**63)
MAX_TIMESPAN_TICKS = 2**63 - 1

_ORIGIN = datetime.datetime(1, 1, 1)
_UTC_ORIGIN = datetime.datetime(1, 1, 1, tzinfo=datetime.timezone.utc)
_KIND_CODES: Dict[DateTimeKind, int] = {"unspecified": 0, "utc": 1, "local": 2}
_KINDS: Dict[int, DateTimeKind] = {code: kind for kind, code in _KIND_CODES.items()}
_TICKS_MASK = 2**62 - 1


class DictionaryString(str):
  """A string that may be written as a dictionary reference ([MC-NBFX] 2.1.4).

  It compares and hashes as the plain string. A writer asks its dictionary for the string's
  key and falls back to inline characters when there is none; a plain ``str`` is always
  written inline.
  """

  __slots__ = ()


@dataclass(frozen=True)
class QName:
  """A qualified name used as text, such as the value of an ``xsi:type`` attribute."""

  prefix: str
  name: str

  def __str__(self) -> str:
    """Return ``prefix:name``, or the bare name when there is no prefix."""
    return f"{self.prefix}:{self.name}" if self.prefix else self.name


class UniqueId(uuid.UUID):
  """A UUID whose characters carry the ``urn:uuid:`` prefix (UniqueIdText, 2.2.3.22)."""

  __slots__ = ()

  def __str__(self) -> str:
    """Return the identifier as ``urn:uuid:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx``."""
    return "urn:uuid:" + super().__str__()


def to_single(value: float) -> float:
  """Round a float to single precision; a magnitude beyond its range becomes infinite."""
  try:
    rounded: float = struct.unpack("<f", struct.pack("<f", value))[0]
  except OverflowError:
    return math.copysign(math.inf, value)
  return rounded


class Single(float):
  """A single-precision float, as carried by a FloatText record.

  The value is rounded to single precision when created; a magnitude beyond the
  single-precision range becomes infinite.
  """

  __slots__ = ()

  def __new__(cls, value: float) -> "Single":
    """Create the value nearest to ``value`` that single precision can hold."""
    return super().__new__(cls, to_single(value))


class DateTime(datetime.datetime):
  """A ``datetime`` that also holds what a .NET DateTime adds: a 100 ns digit and a kind.

  ``kind`` is one of the three DateTimeKind values. An "unspecified" value is naive. A "utc"
  or "local" value is aware; "local" marks a time the sender holds in its own time zone,
  which travels as the UTC instant and is shown by each receiver in that receiver's zone.
  Values read from the wire with kind "local" therefore carry ``tzinfo`` UTC.

  ``tick`` is the digit below microseconds (0 to 9). Comparison, hashing, arithmetic and
  ``replace`` behave as on ``datetime``: they ignore the tick, and their results have tick 0
  and a kind inferred from ``tzinfo``. Without an explicit kind, a naive value is
  "unspecified", an aware value with zero UTC offset is "utc" and any other is "local".
  """

  __slots__ = ("_tick", "_kind")
  _tick: int
  _kind: DateTimeKind

  def __new__(
    cls, *args: Any, tick: int = 0, kind: Optional[DateTimeKind] = None, **kwargs: Any
  ) -> "DateTime":
    """Create a value from ``datetime`` arguments plus an optional tick and kind."""
    self = super().__new__(cls, *args, **kwargs)
    if not 0 <= tick <= 9:
      raise ValueError(f"tick must be between 0 and 9, got {tick}")
    offset = self.utcoffset()
    if kind is None:
      kind = "unspecified" if offset is None else ("local" if offset else "utc")
    elif (kind == "unspecified") != (offset is None):
      raise ValueError(f"kind {kind!r} does not fit tzinfo {self.tzinfo!r}")
    self._tick = tick
    self._kind = kind
    return self

  @property
  def tick(self) -> int:
    """The 100 ns digit below the microsecond."""
    return self._tick

  @property
  def kind(self) -> DateTimeKind:
    """The DateTimeKind of this value."""
    return self._kind

  @property
  def ticks(self) -> int:
    """100 ns intervals since 0001-01-01: of the wall clock if naive, else of the UTC instant."""
    origin = _ORIGIN if self.utcoffset() is None else _UTC_ORIGIN
    delta = datetime.datetime.__sub__(self, origin)
    seconds = delta.days * 86400 + delta.seconds
    return seconds * TICKS_PER_SECOND + delta.microseconds * 10 + self._tick

  @classmethod
  def from_ticks(cls, ticks: int, kind: DateTimeKind = "unspecified") -> "DateTime":
    """Create a value from a tick count as defined by :attr:`ticks`."""
    if not 0 <= ticks <= MAX_DATETIME_TICKS:
      raise ValueError(f"DateTime ticks out of range: {ticks}")
    microseconds, tick = divmod(ticks, 10)
    origin = _ORIGIN if kind == "unspecified" else _UTC_ORIGIN
    base = origin + datetime.timedelta(microseconds=microseconds)
    return cls(
      base.year,
      base.month,
      base.day,
      base.hour,
      base.minute,
      base.second,
      base.microsecond,
      base.tzinfo,
      tick=tick,
      kind=kind,
    )

  @classmethod
  def from_datetime(
    cls, value: datetime.datetime, kind: Optional[DateTimeKind] = None
  ) -> "DateTime":
    """Convert a ``datetime``, keeping the tick and kind of a :class:`DateTime` argument."""
    tick = 0
    if isinstance(value, DateTime):
      tick = value.tick
      kind = value.kind if kind is None else kind
    return cls(
      value.year,
      value.month,
      value.day,
      value.hour,
      value.minute,
      value.second,
      value.microsecond,
      value.tzinfo,
      fold=value.fold,
      tick=tick,
      kind=kind,
    )

  def __reduce_ex__(self, protocol: SupportsIndex) -> Tuple[Any, ...]:
    """Pickle and copy through the tick count, so the tick and kind survive."""
    return (type(self).from_ticks, (self.ticks, self._kind))


class TimeSpan(datetime.timedelta):
  """A ``timedelta`` that also holds the 100 ns digit of a .NET TimeSpan.

  ``tick`` is the digit below microseconds (0 to 9), counted upwards as ``microseconds``
  is. Comparison and arithmetic behave as on ``timedelta`` and ignore the tick.
  """

  __slots__ = ("_tick",)
  _tick: int

  def __new__(cls, *args: Any, tick: int = 0, **kwargs: Any) -> "TimeSpan":
    """Create a value from ``timedelta`` arguments plus an optional tick."""
    self = super().__new__(cls, *args, **kwargs)
    if not 0 <= tick <= 9:
      raise ValueError(f"tick must be between 0 and 9, got {tick}")
    self._tick = tick
    return self

  @property
  def tick(self) -> int:
    """The 100 ns digit below the microsecond."""
    return self._tick

  @property
  def ticks(self) -> int:
    """The duration in 100 ns intervals."""
    seconds = self.days * 86400 + self.seconds
    return seconds * TICKS_PER_SECOND + self.microseconds * 10 + self._tick

  @classmethod
  def from_ticks(cls, ticks: int) -> "TimeSpan":
    """Create a value from a duration in 100 ns intervals."""
    if not MIN_TIMESPAN_TICKS <= ticks <= MAX_TIMESPAN_TICKS:
      raise ValueError(f"TimeSpan ticks out of range: {ticks}")
    microseconds, tick = divmod(ticks, 10)
    return cls(microseconds=microseconds, tick=tick)

  @classmethod
  def from_timedelta(cls, value: datetime.timedelta) -> "TimeSpan":
    """Convert a ``timedelta``, keeping the tick of a :class:`TimeSpan` argument."""
    tick = value.tick if isinstance(value, TimeSpan) else 0
    return cls(days=value.days, seconds=value.seconds, microseconds=value.microseconds, tick=tick)

  def __reduce_ex__(self, protocol: SupportsIndex) -> Tuple[Any, ...]:
    """Pickle and copy through the tick count, so the tick survives."""
    return (type(self).from_ticks, (self.ticks,))


TextValue = Union[
  bool,
  int,
  float,
  decimal.Decimal,
  datetime.datetime,
  datetime.timedelta,
  uuid.UUID,
  bytes,
  str,
  QName,
  Tuple[Any, ...],
]


def datetime_to_binary(value: datetime.datetime) -> int:
  """Return the 64-bit DateTimeText payload: 62 bits of ticks below 2 bits of kind (2.2.3.12)."""
  exact = DateTime.from_datetime(value)
  return exact.ticks | (_KIND_CODES[exact.kind] << 62)


def datetime_from_binary(raw: int) -> DateTime:
  """Decode a 64-bit DateTimeText payload."""
  code = raw >> 62
  if code not in _KINDS:
    raise ValueError(f"DateTime time zone bits {code} are reserved")
  ticks = raw & _TICKS_MASK
  if ticks > MAX_DATETIME_TICKS:
    # .NET stores a local time earlier than 0001-01-01 UTC as a wrapped tick count.
    raise ValueError(f"DateTime ticks {ticks} are outside 0001-01-01..9999-12-31")
  return DateTime.from_ticks(ticks, _KINDS[code])


def decimal_to_binary(value: decimal.Decimal) -> bytes:
  """Encode a decimal as the 16-byte DECIMAL structure of [MS-OAUT] 2.2.26 (2.2.3.11)."""
  sign, digits, exponent = value.as_tuple()
  if not isinstance(exponent, int):
    raise ValueError(f"{value} has no .NET decimal form")
  mantissa = int("".join(map(str, digits)))
  scale = -exponent
  if scale < 0:
    mantissa *= 10**-scale
    scale = 0
  if scale > 28 or mantissa >= 2**96:
    raise ValueError(f"{value} does not fit a .NET decimal (96-bit mantissa, scale 0 to 28)")
  return struct.pack(
    "<HBBIQ", 0, scale, 0x80 if sign else 0, mantissa >> 64, mantissa & (2**64 - 1)
  )


def decimal_from_binary(data: bytes) -> decimal.Decimal:
  """Decode the 16-byte DECIMAL structure of [MS-OAUT] 2.2.26."""
  _, scale, sign, high, low = struct.unpack("<HBBIQ", data)
  if scale > 28 or sign not in (0, 0x80):
    raise ValueError(f"invalid decimal scale {scale} or sign {sign:#x}")
  digits = tuple(int(digit) for digit in str((high << 64) | low))
  return decimal.Decimal((1 if sign else 0, digits, -scale))


def _fraction(ticks: int) -> str:
  """Return the fractional-second digits of a tick count, without trailing zeros."""
  digits = f"{ticks % TICKS_PER_SECOND:07d}".rstrip("0")
  return f".{digits}" if digits else ""


def format_datetime(value: datetime.datetime) -> str:
  """Return the characters of a DateTime: ISO 8601 with up to seven fraction digits."""
  exact = DateTime.from_datetime(value)
  text = f"{value.year:04d}-{value.month:02d}-{value.day:02d}T"
  text += f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}"
  text += _fraction(value.microsecond * 10 + exact.tick)
  offset = value.utcoffset()
  if exact.kind == "utc":
    return text + "Z"
  if exact.kind == "local" and offset is not None:
    minutes = round(offset.total_seconds() / 60)
    sign = "-" if minutes < 0 else "+"
    return f"{text}{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
  return text


_DATETIME = re.compile(
  r"(\d{4})-(\d\d)-(\d\d)(?:T(\d\d):(\d\d):(\d\d)(?:\.(\d+))?)?(Z|[+-]\d\d:\d\d)?"
)


def parse_datetime(text: str) -> DateTime:
  """Parse the characters of a DateTime (see :func:`format_datetime`)."""
  match = _DATETIME.fullmatch(text.strip())
  if match is None:
    raise ValueError(f"not a DateTime: {text!r}")
  year, month, day, hour, minute, second, fraction, zone = match.groups()
  ticks = int((fraction or "")[:7].ljust(7, "0"))
  tzinfo: Optional[datetime.tzinfo] = None
  kind: DateTimeKind = "unspecified"
  if zone == "Z":
    tzinfo, kind = datetime.timezone.utc, "utc"
  elif zone is not None:
    minutes = int(zone[1:3]) * 60 + int(zone[4:6])
    delta = datetime.timedelta(minutes=-minutes if zone[0] == "-" else minutes)
    tzinfo, kind = datetime.timezone(delta), "local"
  return DateTime(
    int(year),
    int(month),
    int(day),
    int(hour or 0),
    int(minute or 0),
    int(second or 0),
    ticks // 10,
    tzinfo,
    tick=ticks % 10,
    kind=kind,
  )


def format_duration(value: datetime.timedelta) -> str:
  """Return a duration in the XML Schema form .NET uses for a TimeSpan, such as ``PT1.5S``."""
  ticks = TimeSpan.from_timedelta(value).ticks
  sign = "-" if ticks < 0 else ""
  ticks = abs(ticks)
  days, rest = divmod(ticks, 86400 * TICKS_PER_SECOND)
  hours, rest = divmod(rest, 3600 * TICKS_PER_SECOND)
  minutes, rest = divmod(rest, 60 * TICKS_PER_SECOND)
  text = f"{sign}P" + (f"{days}D" if days else "")
  if hours or minutes or rest:
    text += "T" + (f"{hours}H" if hours else "") + (f"{minutes}M" if minutes else "")
    if rest:
      text += f"{rest // TICKS_PER_SECOND}{_fraction(rest)}S"
  return text + "T0S" if text.endswith("P") else text


_DURATION = re.compile(
  r"(-)?P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?"
  r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)(?:\.(\d+))?S)?)?"
)


def parse_duration(text: str) -> TimeSpan:
  """Parse an XML Schema duration as .NET does: a year is 365 days and a month 30."""
  match = _DURATION.fullmatch(text.strip())
  if match is None or text.strip().endswith(("P", "T")):
    raise ValueError(f"not a duration: {text!r}")
  sign, years, months, days, hours, minutes, seconds, fraction = match.groups()
  total_months = int(years or 0) * 12 + int(months or 0)
  total_days = (total_months // 12) * 365 + (total_months % 12) * 30 + int(days or 0)
  total_seconds = (total_days * 24 + int(hours or 0)) * 3600 + int(minutes or 0) * 60
  total_seconds += int(seconds or 0)
  ticks = total_seconds * TICKS_PER_SECOND + int((fraction or "")[:7].ljust(7, "0"))
  return TimeSpan.from_ticks(-ticks if sign else ticks)


def _shortest(value: float) -> str:
  """Return the fewest significant digits that reproduce a float, in ``repr`` notation."""
  if not isinstance(value, Single):
    return repr(value)
  for precision in range(1, 10):
    candidate = float(f"{value:.{precision}g}")
    if to_single(candidate) == value:
      return repr(candidate)
  raise ValueError(f"{value!r} is not a single-precision value")


def format_float(value: float) -> str:
  """Return the characters of a floating-point value (2.2.3.9).

  A :class:`Single` is written with the digits single precision needs, any other float
  with those double precision needs.
  """
  if math.isnan(value):
    return "NaN"
  if math.isinf(value):
    return "INF" if value > 0 else "-INF"
  if value == 0:
    return "-0" if math.copysign(1.0, value) < 0 else "0"
  mantissa, _, exponent = _shortest(value).partition("e")
  if mantissa.endswith(".0"):
    mantissa = mantissa[:-2]
  return f"{mantissa}E{int(exponent):+03d}" if exponent else mantissa


_FLOAT = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")
_SPECIAL_FLOATS = {"INF": math.inf, "-INF": -math.inf, "NaN": math.nan}


def parse_float(text: str) -> float:
  """Parse the characters of a floating-point value, including INF, -INF and NaN."""
  stripped = text.strip()
  if stripped in _SPECIAL_FLOATS:
    return _SPECIAL_FLOATS[stripped]
  if _FLOAT.fullmatch(stripped) is None:
    raise ValueError(f"not a floating-point number: {text!r}")
  return float(stripped)


def format_decimal(value: decimal.Decimal) -> str:
  """Return the characters of a decimal: plain digits, keeping its scale."""
  return format(value, "f")


_DECIMAL = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)")


def parse_decimal(text: str) -> decimal.Decimal:
  """Parse the characters of a decimal; exponents are not part of the form."""
  stripped = text.strip()
  if _DECIMAL.fullmatch(stripped) is None:
    raise ValueError(f"not a decimal: {text!r}")
  return decimal.Decimal(stripped)


_INTEGER = re.compile(r"[+-]?\d+")


def parse_int(text: str) -> int:
  """Parse the characters of an integer."""
  stripped = text.strip()
  if _INTEGER.fullmatch(stripped) is None:
    raise ValueError(f"not an integer: {text!r}")
  return int(stripped)


_BOOLEANS = {"true": True, "1": True, "false": False, "0": False}


def parse_bool(text: str) -> bool:
  """Parse the characters of a boolean: true, false, 1 or 0."""
  stripped = text.strip()
  if stripped not in _BOOLEANS:
    raise ValueError(f"not a boolean: {text!r}")
  return _BOOLEANS[stripped]


def parse_bytes(text: str) -> bytes:
  """Parse base64 characters, ignoring XML white space."""
  return base64.b64decode("".join(text.split()), validate=True)


def text_of(value: TextValue) -> str:
  """Return the XML characters a typed text value stands for ([MC-NBFX] 2.2.3)."""
  if isinstance(value, str):
    return value
  if isinstance(value, bool):
    return "true" if value else "false"
  if isinstance(value, int):
    return str(value)
  if isinstance(value, float):
    return format_float(value)
  if isinstance(value, decimal.Decimal):
    return format_decimal(value)
  if isinstance(value, datetime.datetime):
    return format_datetime(value)
  if isinstance(value, datetime.timedelta):
    return format_duration(value)
  if isinstance(value, (uuid.UUID, QName)):
    return str(value)
  if isinstance(value, bytes):
    return base64.b64encode(value).decode("ascii")
  if isinstance(value, tuple):
    return " ".join(text_of(item) for item in value)
  raise TypeError(f"{type(value).__name__} is not a text value")
