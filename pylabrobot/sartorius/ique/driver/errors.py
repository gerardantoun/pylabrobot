"""Errors of the iQue Screener PLUS driver.

Every error the driver raises on its own account derives from :class:`IQueError`. A fault the
controller answers a call with is raised unchanged as
:class:`~pylabrobot.sartorius.ique.driver.client.ControllerFault`, and a broken session as the
error of the wcf package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping, Optional, Sequence, Tuple

from pylabrobot.sartorius.ique.driver.contract import (
  DeviceInfo,
  DeviceKind,
  SystemType,
  Version,
  WorklistStatus,
  WorklistStopReason,
)

if TYPE_CHECKING:
  from pylabrobot.sartorius.ique.driver.master import CompletedRun


def describe_device(info: DeviceInfo) -> str:
  """Return one line naming a device, its state and its status messages.

  Args:
    info: The device's state as the controller reported it.
  """
  model = f" ({info.model})" if info.model else ""
  texts = []
  for message in info.messages or []:
    if message is None:
      continue
    text = f"{message.message_level}: {message.message or ''}"
    if message.detail:
      text += f" ({message.detail})"
    texts.append(text)
  messages = "; ".join(texts) if texts else "no messages"
  return f"{info.kind}{model} is {info.state}: {messages}"


class IQueError(Exception):
  """Base of the errors the driver raises on its own account."""


class IQueTimeoutError(IQueError, TimeoutError):
  """Something the driver waited for did not happen in time."""


class NotSetUpError(IQueError):
  """An operation needs a session with the controller, and there is none."""


class SessionRefusedError(IQueError):
  """The controller has another client registered whose session still answers.

  The controller serves one client at a time; this session was not registered and receives no
  callbacks.
  """


class ControllerVersionError(IQueError):
  """The controller runs a software version this driver does not support.

  Attributes:
    version: The version the controller reported, or None when it reported none.
    supported: The (major, minor) versions the driver supports.
  """

  def __init__(self, version: Optional[Version], supported: Sequence[Tuple[int, int]]) -> None:
    """Create the error.

    Args:
      version: The version the controller reported.
      supported: The supported (major, minor) versions.
    """
    names = ", ".join(f"{major}.{minor}" for major, minor in supported)
    super().__init__(f"the controller reports version {version}; this driver supports {names}")
    self.version = version
    self.supported = tuple(supported)


class UnsupportedSystemError(IQueError):
  """The controller is configured for an instrument outside the iQue Screener PLUS family.

  Attributes:
    system_type: The system type the controller reported.
  """

  def __init__(self, system_type: SystemType) -> None:
    """Create the error.

    Args:
      system_type: The system type the controller reported.
    """
    super().__init__(
      f"the controller is configured for system type {system_type}; this driver supports the "
      "iQue Screener PLUS family (iQue3, iQue3HD, iQuePlusVYB, iQuePlusBR)"
    )
    self.system_type = system_type


class DeviceResetError(IQueError):
  """The controller did not bring its devices up."""


class DeviceFaultedError(IQueError):
  """One or more devices are faulted.

  Attributes:
    devices: The faulted devices with their state and messages.
  """

  def __init__(self, action: str, devices: Mapping[DeviceKind, DeviceInfo]) -> None:
    """Create the error.

    Args:
      action: What could not be done, such as "move the plate rail".
      devices: The faulted devices.
    """
    lines = "; ".join(describe_device(info) for info in devices.values())
    super().__init__(f"cannot {action}: {lines}")
    self.devices = dict(devices)


class DeviceNotReadyError(IQueError):
  """A device is not in the state an operation needs.

  Attributes:
    device: The device.
    info: Its state and messages as last reported, or None when the controller has not
      reported the device.
  """

  def __init__(self, action: str, device: DeviceKind, info: Optional[DeviceInfo]) -> None:
    """Create the error.

    Args:
      action: What could not be done.
      device: The device that is not ready.
      info: Its state as last reported.
    """
    what = describe_device(info) if info is not None else f"{device} is not reported"
    super().__init__(f"cannot {action}: {what}")
    self.device = device
    self.info = info


class UnsafeMotionError(IQueError):
  """The driver refused a motion that a safety rule forbids; the controller checks none of
  them itself."""


class PlateAccessError(IQueError):
  """A plate was put on or taken off the plate holder while the plate rail is not out, or an
  operation was given a plate that is not on the plate holder."""


class DeviceOperationError(IQueError):
  """The controller reported that an operation it accepted did not succeed.

  Attributes:
    operation: The operation.
    message: The controller's error text.
  """

  def __init__(self, operation: str, message: str) -> None:
    """Create the error.

    Args:
      operation: The operation.
      message: The controller's error text.
    """
    super().__init__(f"{operation} failed: {message}")
    self.operation = operation
    self.message = message


class WorklistActiveError(IQueError):
  """A worklist runs, or one has finished and was not finalised.

  Attributes:
    status: The controller's worklist status.
  """

  def __init__(self, message: str, status: WorklistStatus) -> None:
    """Create the error.

    Args:
      message: What was refused and why.
      status: The worklist status.
    """
    super().__init__(message)
    self.status = status


class WorklistFailedError(IQueError):
  """A worklist run did not complete.

  The run was finalised before this was raised; :attr:`run` holds what the controller reported
  and whatever event data could be collected.

  Attributes:
    stop_reason: Why the controller stopped the run.
    error: The controller's error text, empty when it gave none.
    run: The partial run.
  """

  def __init__(
    self, message: str, stop_reason: WorklistStopReason, error: str, run: CompletedRun
  ) -> None:
    """Create the error.

    Args:
      message: What happened.
      stop_reason: The controller's stop reason.
      error: The controller's error text.
      run: The partial run.
    """
    super().__init__(message)
    self.stop_reason = stop_reason
    self.error = error
    self.run = run


class WorklistCancelledError(WorklistFailedError):
  """A worklist run was cancelled with ``cancel_worklist`` or by the controller."""


class WorklistTimeoutError(WorklistFailedError, TimeoutError):
  """A worklist run took longer than its timeout; the driver cancelled it."""


class EventDataError(IQueError):
  """The event data of a run is missing or inconsistent: absent FCS file, wrong checksum,
  unexpected parameters, or fewer events in the file than were streamed.

  Attributes:
    run: What the run produced, without parsed events: the completed worklist, the stream
      counts and, as ``fcs_file``, the bytes downloaded; None when the error concerns no run.
    data: The bytes of the controller's FCS file as downloaded, or None when there were none.
  """

  def __init__(
    self, message: str, run: Optional[CompletedRun] = None, data: Optional[bytes] = None
  ) -> None:
    """Create the error.

    Args:
      message: What is wrong with the data.
      run: What the run produced.
      data: The bytes downloaded.
    """
    super().__init__(message)
    self.run = run
    self.data = data


class WellIdentificationFailedError(IQueError):
  """A plate read completed, but its events could not be assigned to wells, usually because
  there are too few of them.

  Attributes:
    run: The completed run, with all its event data and the controller's FCS file.
  """

  def __init__(self, message: str, run: CompletedRun) -> None:
    """Create the error.

    Args:
      message: What went wrong.
      run: The completed run.
    """
    super().__init__(message)
    self.run = run


class CalibrationMissingError(IQueError):
  """The calibration file is missing or unreadable, or lacks a value an operation needs."""


class CalibrationRefusedError(IQueError):
  """The driver refused to store a calibration file on the instrument."""
