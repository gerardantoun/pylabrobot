"""Record types of the .NET Binary Format: XML Data Structure ([MC-NBFX] 2.1.1)."""

from __future__ import annotations

import array
from enum import IntEnum
from typing import Dict, Optional, Tuple


class RecordType(IntEnum):
  """Record type octets.

  The single-letter prefix records are a run of 26 codes each; only the first is named. Each
  text record is followed by its WithEndElement form, one higher.
  """

  END_ELEMENT = 0x01
  COMMENT = 0x02
  ARRAY = 0x03
  SHORT_ATTRIBUTE = 0x04
  ATTRIBUTE = 0x05
  SHORT_DICTIONARY_ATTRIBUTE = 0x06
  DICTIONARY_ATTRIBUTE = 0x07
  SHORT_XMLNS_ATTRIBUTE = 0x08
  XMLNS_ATTRIBUTE = 0x09
  SHORT_DICTIONARY_XMLNS_ATTRIBUTE = 0x0A
  DICTIONARY_XMLNS_ATTRIBUTE = 0x0B
  PREFIX_DICTIONARY_ATTRIBUTE_A = 0x0C
  PREFIX_ATTRIBUTE_A = 0x26
  SHORT_ELEMENT = 0x40
  ELEMENT = 0x41
  SHORT_DICTIONARY_ELEMENT = 0x42
  DICTIONARY_ELEMENT = 0x43
  PREFIX_DICTIONARY_ELEMENT_A = 0x44
  PREFIX_ELEMENT_A = 0x5E
  ZERO_TEXT = 0x80
  ZERO_TEXT_WITH_END_ELEMENT = 0x81
  ONE_TEXT = 0x82
  ONE_TEXT_WITH_END_ELEMENT = 0x83
  FALSE_TEXT = 0x84
  FALSE_TEXT_WITH_END_ELEMENT = 0x85
  TRUE_TEXT = 0x86
  TRUE_TEXT_WITH_END_ELEMENT = 0x87
  INT8_TEXT = 0x88
  INT8_TEXT_WITH_END_ELEMENT = 0x89
  INT16_TEXT = 0x8A
  INT16_TEXT_WITH_END_ELEMENT = 0x8B
  INT32_TEXT = 0x8C
  INT32_TEXT_WITH_END_ELEMENT = 0x8D
  INT64_TEXT = 0x8E
  INT64_TEXT_WITH_END_ELEMENT = 0x8F
  FLOAT_TEXT = 0x90
  FLOAT_TEXT_WITH_END_ELEMENT = 0x91
  DOUBLE_TEXT = 0x92
  DOUBLE_TEXT_WITH_END_ELEMENT = 0x93
  DECIMAL_TEXT = 0x94
  DECIMAL_TEXT_WITH_END_ELEMENT = 0x95
  DATETIME_TEXT = 0x96
  DATETIME_TEXT_WITH_END_ELEMENT = 0x97
  CHARS8_TEXT = 0x98
  CHARS8_TEXT_WITH_END_ELEMENT = 0x99
  CHARS16_TEXT = 0x9A
  CHARS16_TEXT_WITH_END_ELEMENT = 0x9B
  CHARS32_TEXT = 0x9C
  CHARS32_TEXT_WITH_END_ELEMENT = 0x9D
  BYTES8_TEXT = 0x9E
  BYTES8_TEXT_WITH_END_ELEMENT = 0x9F
  BYTES16_TEXT = 0xA0
  BYTES16_TEXT_WITH_END_ELEMENT = 0xA1
  BYTES32_TEXT = 0xA2
  BYTES32_TEXT_WITH_END_ELEMENT = 0xA3
  START_LIST_TEXT = 0xA4
  END_LIST_TEXT = 0xA6
  EMPTY_TEXT = 0xA8
  EMPTY_TEXT_WITH_END_ELEMENT = 0xA9
  DICTIONARY_TEXT = 0xAA
  DICTIONARY_TEXT_WITH_END_ELEMENT = 0xAB
  UNIQUE_ID_TEXT = 0xAC
  UNIQUE_ID_TEXT_WITH_END_ELEMENT = 0xAD
  TIMESPAN_TEXT = 0xAE
  TIMESPAN_TEXT_WITH_END_ELEMENT = 0xAF
  UUID_TEXT = 0xB0
  UUID_TEXT_WITH_END_ELEMENT = 0xB1
  UINT64_TEXT = 0xB2
  UINT64_TEXT_WITH_END_ELEMENT = 0xB3
  BOOL_TEXT = 0xB4
  BOOL_TEXT_WITH_END_ELEMENT = 0xB5
  UNICODE_CHARS8_TEXT = 0xB6
  UNICODE_CHARS8_TEXT_WITH_END_ELEMENT = 0xB7
  UNICODE_CHARS16_TEXT = 0xB8
  UNICODE_CHARS16_TEXT_WITH_END_ELEMENT = 0xB9
  UNICODE_CHARS32_TEXT = 0xBA
  UNICODE_CHARS32_TEXT_WITH_END_ELEMENT = 0xBB
  QNAME_DICTIONARY_TEXT = 0xBC
  QNAME_DICTIONARY_TEXT_WITH_END_ELEMENT = 0xBD


_R = RecordType

# Every text record, WithEndElement forms and the two list brackets included.
TEXT_RECORDS = frozenset(int(record) for record in RecordType if record >= _R.ZERO_TEXT)

# The ranges of the attribute and element records (2.2.2 and 2.2.1).
FIRST_ATTRIBUTE, LAST_ATTRIBUTE = 0x04, 0x3F
FIRST_ELEMENT, LAST_ELEMENT = 0x40, 0x77

# Text records whose payload is one little-endian number, with its struct format.
FIXED: Dict[int, str] = {
  _R.INT8_TEXT: "<b",
  _R.INT16_TEXT: "<h",
  _R.INT32_TEXT: "<i",
  _R.INT64_TEXT: "<q",
  _R.UINT64_TEXT: "<Q",
  _R.FLOAT_TEXT: "<f",
  _R.DOUBLE_TEXT: "<d",
}

# Text records whose payload is a byte count and that many bytes, by width of the count:
# UTF-8 characters, binary data and UTF-16 characters. Each maps to the struct format of
# its count. (The count of UnicodeChars32Text is a 32-bit integer like the other two,
# as the example in section 3 of the specification shows.)
CHARS = ((_R.CHARS8_TEXT, "<B"), (_R.CHARS16_TEXT, "<H"), (_R.CHARS32_TEXT, "<i"))
BYTES = ((_R.BYTES8_TEXT, "<B"), (_R.BYTES16_TEXT, "<H"), (_R.BYTES32_TEXT, "<i"))
UNICODE = (
  (_R.UNICODE_CHARS8_TEXT, "<B"),
  (_R.UNICODE_CHARS16_TEXT, "<H"),
  (_R.UNICODE_CHARS32_TEXT, "<i"),
)
COUNTED: Dict[int, str] = {int(record): fmt for record, fmt in CHARS + BYTES + UNICODE}

# Array item records (2.3.3) with the size of one item and, for the types ``array.array``
# can hold, its typecode.
_INT32_TYPECODE = "i" if array.array("i").itemsize == 4 else "l"
ARRAY_ITEMS: Dict[RecordType, Tuple[int, Optional[str]]] = {
  _R.BOOL_TEXT: (1, None),
  _R.INT16_TEXT: (2, "h"),
  _R.INT32_TEXT: (4, _INT32_TYPECODE),
  _R.INT64_TEXT: (8, "q"),
  _R.FLOAT_TEXT: (4, "f"),
  _R.DOUBLE_TEXT: (8, "d"),
  _R.DECIMAL_TEXT: (16, None),
  _R.DATETIME_TEXT: (8, None),
  _R.TIMESPAN_TEXT: (8, None),
  _R.UUID_TEXT: (16, None),
}
