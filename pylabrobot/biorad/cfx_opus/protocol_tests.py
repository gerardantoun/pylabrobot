import unittest

from pylabrobot.biorad.cfx_opus.protocol import (
  Cycle,
  Gradient,
  Hold,
  MeltCurve,
  PlateRead,
  Protocol,
  ProtocolError,
  compile_protocol,
  expected_plate_reads,
)


def _qpcr() -> Protocol:
  return Protocol(
    steps=[
      Hold(95, 60),
      Cycle([Hold(95, 10), Hold(60, 30), PlateRead()], count=45),
    ],
    volume=20,
  )


class CompileProtocolTests(unittest.TestCase):
  def test_compiles_a_qpcr_run(self) -> None:
    self.assertEqual(
      compile_protocol(_qpcr()),
      [
        "METHOD CALC",
        "HOTLID 105,30",
        "VOLUME 20",
        "TEMP 95.0,60",
        "TEMP 95.0,10",
        "TEMP 60.0,30",
        "PLATEREAD #h3F",
        "GOTO 2,44",
        "END",
      ],
    )

  def test_controls_the_block_without_a_volume(self) -> None:
    directives = compile_protocol(Protocol(steps=[Hold(37, 0)], lid_temperature=0))

    self.assertEqual(directives, ["METHOD BLOCK", "HOTLID 0,30", "VOLUME 0", "TEMP 37.0,0", "END"])

  def test_expands_a_melt_curve(self) -> None:
    protocol = Protocol(
      steps=[Hold(95, 60), Hold(56.5, 30), MeltCurve(start=56.5, end=91.5, increment=5.0, hold=5)],
      volume=20,
    )

    self.assertEqual(
      compile_protocol(protocol)[3:],
      [
        "TEMP 95.0,60",
        "TEMP 56.5,30",
        "TEMP 56.5,31",
        "TEMP 56.5,5",
        "INC 5.0",
        "RATE 5.0",
        "PLATEREAD #h3F",
        "GOTO 4,7",
        "END",
      ],
    )

  def test_clamps_a_melt_ramp_rate_to_the_increment(self) -> None:
    directives = compile_protocol(Protocol([MeltCurve(65, 95, increment=0.5)], volume=20))

    self.assertIn("INC 0.5", directives)
    self.assertIn("RATE 0.5", directives)
    self.assertIn("GOTO 2,60", directives)

  def test_writes_modifiers_after_their_step(self) -> None:
    protocol = Protocol(
      steps=[Cycle([Hold(64, 30, ramp_rate=2.5, increment=-0.5, extend=5, beep=True)], count=3)]
    )

    self.assertEqual(
      compile_protocol(protocol)[3:],
      ["TEMP 64.0,30", "INC -0.5", "RATE 2.5", "EXT 5", "BEEP", "GOTO 1,2", "END"],
    )

  def test_compiles_a_gradient_and_the_fast_scan(self) -> None:
    protocol = Protocol(steps=[Gradient(55, 65, 30), PlateRead(scan="fast")])

    self.assertEqual(compile_protocol(protocol)[3:5], ["GRAD 55.0,65.0,30", "PLATEREAD #h81"])

  def test_numbers_steps_after_an_earlier_cycle(self) -> None:
    protocol = Protocol(
      steps=[Cycle([Hold(95, 5), Hold(60, 5)], count=2), Cycle([Hold(72, 5)], count=3)]
    )

    self.assertEqual(
      compile_protocol(protocol)[3:],
      [
        "TEMP 95.0,5",
        "TEMP 60.0,5",
        "GOTO 1,1",
        "TEMP 72.0,5",
        "GOTO 4,2",
        "END",
      ],
    )

  def test_rejects_values_outside_the_instruments_limits(self) -> None:
    cases = {
      "temperature": Protocol([Hold(101, 10)]),
      "hold": Protocol([Hold(95, 64801)]),
      "fractional seconds": Protocol([Hold(95, 1.5)]),  # type: ignore[arg-type]
      "hundredths": Protocol([Hold(95.25, 10)]),
      "volume": Protocol([Hold(95, 10)], volume=126),
      "lid": Protocol([Hold(95, 10)], lid_temperature=111),
      "ramp rate": Protocol([Hold(95, 10, ramp_rate=6)]),
      "gradient spread": Protocol([Gradient(55, 85, 30)]),
      "single cycle": Protocol([Cycle([Hold(95, 10)], count=1)]),
      "nested cycle": Protocol([Cycle([Cycle([Hold(95, 10)], count=2)], count=2)]),  # type: ignore[list-item]
      "uneven melt": Protocol([MeltCurve(65, 95.2, increment=0.5)]),
      "empty": Protocol([]),
    }
    for name, protocol in cases.items():
      with self.subTest(name):
        with self.assertRaises(ProtocolError):
          compile_protocol(protocol)


class ExpectedPlateReadsTests(unittest.TestCase):
  def test_counts_reads_in_cycles_and_melts(self) -> None:
    protocol = Protocol(
      steps=[
        Cycle([Hold(95, 10), Hold(60, 30), PlateRead()], count=40),
        PlateRead(),
        MeltCurve(65, 95, increment=0.5),
      ]
    )

    self.assertEqual(expected_plate_reads(protocol), 40 + 1 + 61)

  def test_a_thermal_protocol_reads_nothing(self) -> None:
    self.assertEqual(expected_plate_reads(Protocol([Hold(37, 0)])), 0)


if __name__ == "__main__":
  unittest.main()
