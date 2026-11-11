import unittest

from pylabrobot.biorad.cfx_opus.framing import CFXProtocolError
from pylabrobot.biorad.cfx_opus.status import (
  parse_error_list,
  parse_optics_status,
  parse_status,
)

IDLE = '17.04;18.3;0;0;IDLE;0;"",BLOCK,OFF;0;0.00;0.00;0.00;0.00;0.00;0;0;0;17.04;CLOSED;0'
RUNNING = (
  '60.25;104.9;2;2;TEMP 95.0,10;2;"SINGLETE",CALC,ON;96;110.22;0.00;75.15;2.10;0.00;0;0;6;'
  "55.61;CLOSED;0"
)


def _with(record: str, index: int, value: str) -> str:
  fields = record.split(";")
  fields[index] = value
  return ";".join(fields)


class ParseStatusTests(unittest.TestCase):
  def test_reads_an_idle_block(self) -> None:
    (block,) = parse_status(IDLE)

    self.assertEqual(block.block_temperature, 17.04)
    self.assertEqual(block.lid_temperature, 18.3)
    self.assertEqual(block.step_text, "IDLE")
    self.assertEqual(block.run_name, "")
    self.assertEqual(block.method, "BLOCK")
    self.assertFalse(block.lid_heater_on)
    self.assertEqual(block.lid_position, "CLOSED")
    self.assertEqual(block.errors, ())
    self.assertEqual(block.run_state, "idle")

  def test_reads_a_running_block(self) -> None:
    (block,) = parse_status(RUNNING)

    self.assertEqual((block.cycle, block.step, block.step_count), (2, 2, 6))
    self.assertEqual(block.step_text, "TEMP 95.0,10")
    self.assertEqual(block.run_name, "SINGLETE")
    self.assertEqual(block.method, "CALC")
    self.assertTrue(block.lid_heater_on)
    self.assertEqual(block.elapsed_time, 110.22)
    self.assertEqual(block.remaining_time, 75.15)
    self.assertEqual(block.sample_temperature, 55.61)
    self.assertFalse(block.at_target)
    self.assertEqual(block.run_state, "running")

  def test_derives_the_run_state_from_the_register(self) -> None:
    finished = _with(_with(IDLE, 6, '"SINGLETE",CALC,OFF'), 7, "32")
    cases = {
      _with(RUNNING, 7, "97"): "running",
      _with(RUNNING, 7, "104"): "running",
      _with(RUNNING, 7, "98"): "paused",
      finished: "finished",
      _with(finished, 7, "160"): "aborted",
      _with(finished, 7, "97"): "finished",
      _with(RUNNING, 7, "0"): "running",
    }
    for record, state in cases.items():
      with self.subTest(state=state, record=record):
        (block,) = parse_status(record)
        self.assertEqual(block.run_state, state)

  def test_reads_the_lid_preheat_and_at_target_bits(self) -> None:
    (preheating,) = parse_status(_with(RUNNING, 7, "104"))
    (at_target,) = parse_status(_with(RUNNING, 7, "97"))

    self.assertTrue(preheating.lid_preheating)
    self.assertFalse(preheating.at_target)
    self.assertTrue(at_target.at_target)

  def test_reads_block_errors(self) -> None:
    (block,) = parse_status(_with(IDLE, 14, "0x41:0x42"))

    self.assertEqual(block.errors, ("0x41", "0x42"))

  def test_reads_one_record_per_block(self) -> None:
    blocks = parse_status(IDLE + ";" + RUNNING)

    self.assertEqual([b.run_state for b in blocks], ["idle", "running"])

  def test_rejects_a_truncated_reply(self) -> None:
    with self.assertRaises(CFXProtocolError):
      parse_status(IDLE.rsplit(";", 1)[0])

  def test_rejects_an_unknown_lid_position(self) -> None:
    with self.assertRaises(CFXProtocolError):
      parse_status(_with(IDLE, 17, "AJAR"))


class ParseOpticsStatusTests(unittest.TestCase):
  def test_reads_a_healthy_head(self) -> None:
    status = parse_optics_status("18.31;26;")

    self.assertEqual((status.shuttle_temperature, status.ambient_temperature), (18.31, 26.0))
    self.assertEqual(status.faults, ())

  def test_reads_faults(self) -> None:
    self.assertEqual(parse_optics_status("45.2;24;0x1001,3;0x2002").faults, ("0x1001,3", "0x2002"))


class ParseErrorListTests(unittest.TestCase):
  def test_an_empty_list(self) -> None:
    self.assertEqual(parse_error_list(""), ())


if __name__ == "__main__":
  unittest.main()
