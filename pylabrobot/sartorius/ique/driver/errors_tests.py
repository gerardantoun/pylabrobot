"""Tests of the driver's errors and their messages."""

import unittest

from pylabrobot.sartorius.ique.driver.contract import DeviceInfo, DeviceMessage, Version
from pylabrobot.sartorius.ique.driver.errors import (
  ControllerVersionError,
  DeviceFaultedError,
  DeviceNotReadyError,
  IQueError,
  WorklistCancelledError,
  WorklistFailedError,
  WorklistTimeoutError,
  describe_device,
)

PUMP = DeviceInfo(
  kind="Pump",
  messages=[
    DeviceMessage(detail="no answer", message="Start Faulted", message_level="Error"),
    None,
  ],
  model="Ismatec REGLO Digital",
  state="Faulted",
)


class ErrorTests(unittest.TestCase):
  """Messages and the hierarchy."""

  def test_describe_device(self) -> None:
    """A device is described by kind, model, state and messages."""
    self.assertEqual(
      describe_device(PUMP),
      "Pump (Ismatec REGLO Digital) is Faulted: Error: Start Faulted (no answer)",
    )
    bare = DeviceInfo(kind="Shaker", messages=None, model="", state="Ready")
    self.assertEqual(describe_device(bare), "Shaker is Ready: no messages")

  def test_device_errors(self) -> None:
    """Device errors name what could not be done and the devices."""
    faulted = DeviceFaultedError("start the pump", {"Pump": PUMP})
    self.assertIn("cannot start the pump: Pump", str(faulted))
    self.assertEqual(list(faulted.devices), ["Pump"])
    missing = DeviceNotReadyError("load", "PlateRail", None)
    self.assertIn("PlateRail is not reported", str(missing))

  def test_version_error(self) -> None:
    """The version error names the reported and the supported versions."""
    error = ControllerVersionError(Version(7, 3, -1, -1), [(7, 2)])
    self.assertEqual(str(error), "the controller reports version 7.3; this driver supports 7.2")

  def test_hierarchy(self) -> None:
    """Every error is an IQueError; the timeout is also a TimeoutError."""
    self.assertTrue(issubclass(WorklistCancelledError, WorklistFailedError))
    self.assertTrue(issubclass(WorklistTimeoutError, TimeoutError))
    self.assertTrue(issubclass(WorklistTimeoutError, IQueError))


if __name__ == "__main__":
  unittest.main()
