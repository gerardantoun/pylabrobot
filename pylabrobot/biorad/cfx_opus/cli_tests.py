import unittest
from unittest.mock import AsyncMock, patch

from pylabrobot.biorad.cfx_opus import cli


class CLITests(unittest.TestCase):
  def test_requires_a_command(self) -> None:
    with self.assertRaises(SystemExit):
      cli.main([])

  def test_identify_sets_the_product_id(self) -> None:
    opus = AsyncMock()
    opus.identify.return_value = cli.CFXOpus.__new__(cli.CFXOpus)
    with patch.object(cli, "_opus", return_value=opus) as make:
      with patch.object(cli, "asdict", return_value={"model": "CFX Opus 96"}):
        cli.main(["identify", "--product-id", "0x1234"])

    self.assertEqual(make.call_args.args[0].product_id, 0x1234)
    opus.setup.assert_awaited_once()
    opus.identify.assert_awaited_once()
    opus.stop.assert_awaited_once()

  def test_lid_open_calls_open_lid(self) -> None:
    opus = AsyncMock()
    opus.get_status.return_value.lid_position = "OPEN"
    with patch.object(cli, "_opus", return_value=opus):
      cli.main(["lid", "open"])

    opus.open_lid.assert_awaited_once()
    opus.close_lid.assert_not_awaited()

  def test_list_reports_when_nothing_is_connected(self) -> None:
    with patch.dict("sys.modules"):
      fake_usb_core = unittest.mock.MagicMock()
      fake_usb_core.find.return_value = iter([])
      with patch.dict(
        "sys.modules",
        {
          "usb": unittest.mock.MagicMock(core=fake_usb_core),
          "usb.core": fake_usb_core,
          "libusb_package": unittest.mock.MagicMock(),
        },
      ):
        cli.main(["list"])  # must not raise


if __name__ == "__main__":
  unittest.main()
