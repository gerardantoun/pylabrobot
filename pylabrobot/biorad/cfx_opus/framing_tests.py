import unittest

from pylabrobot.biorad.cfx_opus.framing import (
  CFXProtocolError,
  MessageReader,
  encode_command,
  parse_reply,
)


def _message(payload: bytes, channel: int = 1, flags: int = 0x00) -> bytes:
  return bytes([channel, 0x00, flags]) + len(payload).to_bytes(2, "big") + payload


class EncodeCommandTests(unittest.TestCase):
  def test_frames_an_identification_query(self) -> None:
    self.assertEqual(
      encode_command("*IDN?"),
      bytes.fromhex("01 7f 0c 00 07") + b"*IDN?\r\n",
    )

  def test_rejects_a_command_spanning_lines(self) -> None:
    with self.assertRaises(ValueError):
      encode_command("LID OPEN\r\nLID CLOSE")


class MessageReaderTests(unittest.TestCase):
  def test_reassembles_a_message_split_across_reads(self) -> None:
    reader = MessageReader()
    data = _message(b"True;0000")

    self.assertEqual(reader.feed(data[:3]), [])
    self.assertEqual(reader.feed(data[3:8]), [])
    (message,) = reader.feed(data[8:])

    self.assertEqual(message.payload, b"True;0000")
    self.assertEqual(message.channel, 1)
    self.assertFalse(message.pass_through)
    self.assertEqual(reader.pending, 0)

  def test_returns_every_message_one_read_completes(self) -> None:
    reader = MessageReader()

    messages = reader.feed(_message(b"0000") + _message(b"\x04\x00", channel=2) + b"\x01\x00")

    self.assertEqual([(m.channel, m.payload) for m in messages], [(1, b"0000"), (2, b"\x04\x00")])
    self.assertEqual(reader.pending, 2)

  def test_marks_a_binary_payload(self) -> None:
    (message,) = MessageReader().feed(_message(bytes.fromhex("07 00 09 00"), flags=0x02))

    self.assertTrue(message.pass_through)
    self.assertEqual(message.payload, bytes.fromhex("07 00 09 00"))

  def test_reads_the_handle(self) -> None:
    (message,) = MessageReader().feed(bytes([(3 << 2) | 1, 0, 0, 0, 1]) + b"x")

    self.assertEqual((message.handle, message.channel), (3, 1))


class ParseReplyTests(unittest.TestCase):
  def test_splits_value_and_code(self) -> None:
    reply = parse_reply(b"BIO-RAD LABORATORIES,C1000,CT019138,2.0.231.0;0000\r\n")

    self.assertEqual(reply.value, "BIO-RAD LABORATORIES,C1000,CT019138,2.0.231.0")
    self.assertEqual(reply.code, "0000")

  def test_keeps_an_empty_value(self) -> None:
    self.assertEqual(parse_reply(b";0000").value, "")

  def test_a_bare_code_has_no_value(self) -> None:
    reply = parse_reply(b"0000")

    self.assertIsNone(reply.value)
    self.assertEqual(reply.code, "0000")

  def test_splits_on_the_last_semicolon(self) -> None:
    self.assertEqual(parse_reply(b"true;;0000").value, "true;")

  def test_rejects_a_reply_without_a_code(self) -> None:
    with self.assertRaises(CFXProtocolError):
      parse_reply(b"hello")


if __name__ == "__main__":
  unittest.main()
