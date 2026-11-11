"""Tests for the string dictionaries of [MC-NBFS] and [MC-NBFSE]."""

import unittest

from pylabrobot.sartorius.ique.driver.wcf import dictionary
from pylabrobot.sartorius.ique.driver.wcf.dictionary import (
  ReaderSession,
  StaticString,
  WriterSession,
  static_key,
  static_lookup,
)
from pylabrobot.sartorius.ique.driver.wcf.errors import NbfxError
from pylabrobot.sartorius.ique.driver.wcf.nbfx import decode, encode
from pylabrobot.sartorius.ique.driver.wcf.tree import Element, Text, to_xml
from pylabrobot.sartorius.ique.driver.wcf.values import DictionaryString

# The StringTable of the example of [MC-NBFSE] section 3: "action" and "Inventory".
_EXAMPLE_TABLE = bytes.fromhex("1106616374696f6e09496e76656e746f7279")


class StaticDictionaryTests(unittest.TestCase):
  """The table of [MC-NBFS] section 2: even keys, twice the position."""

  def test_entries_have_the_keys_of_the_specification(self) -> None:
    """Spot checks across the table, from its first entry to its last."""
    cases = {
      0x00: "mustUnderstand",
      0x02: "Envelope",
      0x04: "http://www.w3.org/2003/05/soap-envelope",
      0x06: "http://www.w3.org/2005/08/addressing",
      0x0A: "Action",
      0x14: "http://www.w3.org/2005/08/addressing/anonymous",
      0x1A: "MessageID",
      0x2A: "Address",
      0x86: "Fault",
      0x9C: "Subcode",
      0x1A0: "RequestType",
      # .NET's table has a leading space here, which the specification's listing drops.
      0x1A8: " http://schemas.xmlsoap.org/ws/2005/02/trust/tlsnego",
      0x36A: "http://schemas.microsoft.com/ws/2005/05/routing",
      0x372: "http://www.w3.org/2001/XMLSchema-instance",
      0x376: "nil",
      0x3CC: "detail",
    }
    for key, value in cases.items():
      with self.subTest(key=hex(key)):
        self.assertEqual(static_lookup(key), value)
        self.assertEqual(static_key(StaticString(value)), key)
    self.assertEqual(len(dictionary.STATIC_STRINGS), 0x3CC // 2 + 1)

  def test_odd_and_unknown_keys_are_rejected(self) -> None:
    """Without a session table only even keys inside the table are valid."""
    for key in (1, 0x3CE, 2**31 - 2):
      with self.assertRaises(NbfxError):
        static_lookup(key)

  def test_only_static_strings_get_static_keys(self) -> None:
    """A plain dictionary string with the same characters is not a static reference."""
    self.assertIsNone(static_key(DictionaryString("Envelope")))
    with self.assertRaises(ValueError):
      StaticString("not in the table")

  def test_specification_example_document(self) -> None:
    """The example document of [MC-NBFS] section 3 reads with the static table."""
    records = [
      "5602",  # <s:Envelope
      "0b016106",  # xmlns:a="http://www.w3.org/2005/08/addressing"
      "0b017304",  # xmlns:s="http://www.w3.org/2003/05/soap-envelope">
      "5608",  # <s:Header>
      "440a",  # <a:Action
      "1e0082",  # s:mustUnderstand="1">
      "9906616374696f6e",  # action</a:Action>
      "01",  # </s:Header>
      "560e",  # <s:Body>
      "4009496e76656e746f7279",  # <Inventory>
      "81",  # 0</Inventory>
      "01",  # </s:Body>
      "01",  # </s:Envelope>
    ]
    data = bytes.fromhex("".join(records))
    envelope = decode(data, static_lookup)
    self.assertEqual(
      to_xml(envelope),
      '<s:Envelope xmlns:a="http://www.w3.org/2005/08/addressing" '
      'xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Header>'
      '<a:Action s:mustUnderstand="1">action</a:Action></s:Header>'
      "<s:Body><Inventory>0</Inventory></s:Body></s:Envelope>",
    )
    self.assertEqual(encode(envelope, static_key), data)


class ReaderSessionTests(unittest.TestCase):
  """The strings a peer announces in the StringTable at the head of each envelope."""

  def test_specification_example(self) -> None:
    """The example of [MC-NBFSE] section 3: the table, then records that refer to it."""
    session = ReaderSession()
    # ShortDictionaryElement 3 and DictionaryTextWithEndElement 1.
    rest = session.read_string_table(_EXAMPLE_TABLE + bytes.fromhex("4203ab01"))
    self.assertEqual(rest, bytes.fromhex("4203ab01"))
    self.assertEqual((session.lookup(1), session.lookup(3)), ("action", "Inventory"))
    self.assertEqual(to_xml(decode(rest, session.lookup)), "<Inventory>action</Inventory>")

  def test_keys_continue_across_envelopes(self) -> None:
    """Each table adds to what earlier ones announced; even keys stay static."""
    session = ReaderSession()
    session.read_string_table(_EXAMPLE_TABLE)
    self.assertEqual(session.read_string_table(b"\x00\x42\x02\x01"), b"\x42\x02\x01")
    session.read_string_table(bytes.fromhex("0605") + b"third")
    self.assertEqual(session.lookup(5), "third")
    self.assertEqual(session.lookup(2), "Envelope")
    self.assertIsInstance(session.lookup(2), StaticString)
    with self.assertRaises(NbfxError):
      session.lookup(7)

  def test_malformed_tables_are_rejected(self) -> None:
    """A table must fit its size, its envelope, and hold UTF-8 strings."""
    cases = {
      "size beyond the envelope": "0503616263",
      "string beyond the table": "0304616263",
      "truncated size": "80",
      "truncated string length": "0180",
      "malformed UTF-8": "0302c328",
    }
    for name, data in cases.items():
      with self.subTest(name):
        session = ReaderSession()
        with self.assertRaises(NbfxError):
          session.read_string_table(bytes.fromhex(data))
        with self.assertRaises(NbfxError):
          session.lookup(1)


class WriterSessionTests(unittest.TestCase):
  """The strings this side announces, within the size the peer accepts."""

  def test_strings_get_odd_keys_in_order_of_first_use(self) -> None:
    """The first use adds a string; later uses repeat its key; static strings stay static."""
    session = WriterSession()
    self.assertEqual(session.key_of(DictionaryString("action")), 1)
    self.assertEqual(session.key_of(StaticString("Envelope")), 2)
    self.assertEqual(session.key_of(DictionaryString("Inventory")), 3)
    self.assertEqual(session.key_of(DictionaryString("action")), 1)
    self.assertEqual(session.string_table(), _EXAMPLE_TABLE)
    self.assertEqual(session.string_table(), b"\x00")
    self.assertEqual(session.key_of(DictionaryString("Inventory")), 3)
    self.assertEqual(session.string_table(), b"\x00")

  def test_writer_and_reader_sessions_agree(self) -> None:
    """What one side announces, the other resolves to the same strings."""
    writer, reader = WriterSession(), ReaderSession()
    for names in (["Inventory", "action"], ["action", "Item"]):
      element = Element(DictionaryString(names[0]), children=[Text(DictionaryString(names[1]))])
      document = encode(element, writer.key_of)
      received = decode(reader.read_string_table(writer.string_table() + document), reader.lookup)
      self.assertEqual(received, element)
      self.assertEqual(to_xml(received), to_xml(element))

  def test_size_limit_counts_each_string_with_its_length(self) -> None:
    """A string fits when its UTF-8 bytes and their length prefix fit what is left."""
    session = WriterSession(max_size=10 + 1 + 130)
    self.assertEqual(session.key_of(DictionaryString("é" * 5)), 1)
    self.assertEqual(session.key_of(DictionaryString("x" * 128)), 3)
    table = session.string_table()
    self.assertEqual(table[:2], bytes.fromhex("8d01"))
    self.assertEqual(len(table), 2 + 141)

  def test_first_string_that_does_not_fit_closes_the_table(self) -> None:
    """After one string is refused, no later string is added, however short."""
    session = WriterSession(max_size=8)
    self.assertEqual(session.key_of(DictionaryString("abc")), 1)
    self.assertIsNone(session.key_of(DictionaryString("defghi")))
    self.assertIsNone(session.key_of(DictionaryString("d")))
    self.assertEqual(session.key_of(DictionaryString("abc")), 1)
    self.assertEqual(session.key_of(StaticString("Envelope")), 2)
    self.assertEqual(session.string_table(), bytes.fromhex("0403616263"))
    element = Element(DictionaryString("d"), children=[Text(DictionaryString("abc"))])
    self.assertEqual(encode(element, session.key_of).hex(), "400164ab01")

  def test_default_limit_is_dotnet_default(self) -> None:
    """A .NET binding accepts 2048 bytes of strings from one sender unless configured."""
    session = WriterSession()
    self.assertEqual(session.key_of(DictionaryString("x" * 2045)), 1)
    self.assertIsNone(session.key_of(DictionaryString("y")))
    self.assertEqual(dictionary.DEFAULT_MAX_SESSION_SIZE, 2048)


if __name__ == "__main__":
  unittest.main()
