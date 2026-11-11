"""Typed clients of the iQue Screener PLUS controller and of its watchdog.

:class:`ControllerClient` holds the duplex session with the controller (net.tcp port 9002, path
/Controller) and makes the streamed calls (port 9003, path /ControllerStreaming): one async
method per operation, named after it in snake_case, taking and returning the values of
:mod:`~pylabrobot.sartorius.ique.driver.contract`. The callbacks the controller sends on the
session go to the handlers set with the ``on_...`` methods. :class:`WatchdogClient` does the same
for the watchdog (ports 9000 and 9001).

The clients add nothing to the protocol: no retries, no state and no checks the controller does
not make itself. A fault the service answers with raises :class:`ControllerFault` or
:class:`WatchdogFault`; a timeout, a closed session or a malformed message raises the error of the
wcf package. Units are those of the contract module: sampler coordinates in mm, pump speeds in
hundredths of rpm, shaker speeds in rpm, times as each operation says.
"""

from __future__ import annotations

import array
import datetime
import decimal
import functools
import logging
import uuid
from contextlib import aclosing
from typing import (
  Any,
  AsyncGenerator,
  AsyncIterator,
  Callable,
  Dict,
  List,
  Optional,
  Sequence,
  Tuple,
  TypeVar,
  cast,
)

from pylabrobot.io.socket import Socket
from pylabrobot.sartorius.ique.driver import contract as wire
from pylabrobot.sartorius.ique.driver.contract import (
  CompletedWorklist,
  ConnectionInfo,
  ContactToleranceType,
  ContactZResult,
  ControllerServiceException,
  ControllerServiceStatus,
  CytometerFirmwareType,
  DeviceAlertMessage,
  DeviceConfiguration,
  DeviceInfo,
  DeviceKind,
  DeviceResetOptions,
  IQue3InstallationPrimeStep,
  IQue3PurgeStep,
  MovementRange,
  PumpDirection,
  SystemState,
  SystemType,
  WatchdogException,
  Worklist,
  WorklistCommand,
  WorklistStatus,
  from_wire,
  to_wire,
)
from pylabrobot.sartorius.ique.driver.wcf import (
  STREAM,
  DataContract,
  DuplexChannel,
  Operation,
  ServiceContract,
  SoapFault,
  StreamedClient,
  read_fault_detail,
)
from pylabrobot.sartorius.ique.driver.wcf.soap import new_message_id

logger = logging.getLogger(__name__)

CONTROLLER_PORT = 9002
"""Default port of the controller's duplex endpoint."""
CONTROLLER_STREAMING_PORT = 9003
"""Default port of the controller's streamed endpoint."""
WATCHDOG_PORT = 9000
"""Default port of the watchdog's endpoint."""
WATCHDOG_STREAMING_PORT = 9001
"""Default port of the watchdog's streamed endpoint."""


class ControllerFault(Exception):
  """The controller answered a call with a SOAP fault.

  Attributes:
    operation: Name of the operation that failed.
    code: The fault code as (namespace, name); the SOAP ``Sender`` for a declared fault.
    subcodes: The nested subcodes, outermost first, each as (namespace, name).
    reason: The fault's reason text: the message of the exception the controller caught.
    action: The Action of the fault message.
    detail: The exception the controller caught, for the fault the operation declares; None for
      any other fault, such as the one WCF sends for an exception the controller did not handle,
      after which the controller ends the session.
  """

  def __init__(
    self, operation: str, fault: SoapFault, detail: Optional[ControllerServiceException]
  ) -> None:
    """Create the error from a fault and its decoded detail."""
    super().__init__(f"{operation}: {fault}")
    self.operation = operation
    self.code = fault.code
    self.subcodes = fault.subcodes
    self.reason = fault.reason
    self.action = fault.action
    self.detail = detail


class WatchdogFault(Exception):
  """The watchdog answered a call with a SOAP fault.

  Attributes:
    operation: Name of the operation that failed.
    code: The fault code as (namespace, name).
    subcodes: The nested subcodes, outermost first, each as (namespace, name).
    reason: The fault's reason text.
    action: The Action of the fault message.
    detail: The exception the watchdog caught, for the fault the operation declares; None for
      any other fault.
  """

  def __init__(self, operation: str, fault: SoapFault, detail: Optional[WatchdogException]) -> None:
    """Create the error from a fault and its decoded detail."""
    super().__init__(f"{operation}: {fault}")
    self.operation = operation
    self.code = fault.code
    self.subcodes = fault.subcodes
    self.reason = fault.reason
    self.action = fault.action
    self.detail = detail


def _arguments(operation: Operation, arguments: Sequence[Any]) -> List[Any]:
  """Convert the arguments of a call to their wire form, leaving out a trailing stream."""
  parts = list(operation.headers + operation.parameters)
  if parts and parts[-1][1] is STREAM:
    parts.pop()
  if len(parts) != len(arguments):
    raise TypeError(f"{operation.name} takes {len(parts)} arguments, got {len(arguments)}")
  return [to_wire(xml_type, value) for (_, xml_type), value in zip(parts, arguments)]


def _reply(operation: Operation, reply: Any) -> Any:
  """Convert what a call returned: the result, or a tuple of the result and out parameters."""
  if operation.outputs:
    kinds = [] if operation.result is None else [operation.result]
    kinds += [xml_type for _, xml_type in operation.outputs]
    return tuple(from_wire(xml_type, value) for xml_type, value in zip(kinds, reply))
  if operation.result is None:
    return None
  return from_wire(operation.result, reply)


def _fault_detail(
  service: ServiceContract, operation: Operation, detail: DataContract, fault: SoapFault
) -> Any:
  """Return the decoded detail of the fault an operation declares, or None for another fault.

  Raises:
    ContractError: The fault has the declared fault's Action but not its detail.
  """
  if fault.action != service.fault_action(operation, detail):
    return None
  return from_wire(detail, read_fault_detail(fault, detail))


def _controller_fault(
  service: ServiceContract, operation: Operation, fault: SoapFault
) -> ControllerFault:
  """Return the error for a fault of a controller operation."""
  detail = _fault_detail(service, operation, wire.CONTROLLER_SERVICE_EXCEPTION, fault)
  return ControllerFault(operation.name, fault, cast(Optional[ControllerServiceException], detail))


def _watchdog_fault(
  service: ServiceContract, operation: Operation, fault: SoapFault
) -> WatchdogFault:
  """Return the error for a fault of a watchdog operation."""
  detail = _fault_detail(service, operation, wire.WATCHDOG_EXCEPTION, fault)
  return WatchdogFault(operation.name, fault, cast(Optional[WatchdogException], detail))


FaultFactory = Callable[[ServiceContract, Operation, SoapFault], Exception]
"""Returns the error to raise for a fault of an operation of a service contract."""

_Self = TypeVar("_Self", bound="_Endpoints")


class _Endpoints:
  """The session endpoint and the streamed endpoint of a service, and the calls they share."""

  def __init__(
    self,
    channel: DuplexChannel,
    streamed: StreamedClient,
    service: ServiceContract,
    streaming: ServiceContract,
    fault: FaultFactory,
  ) -> None:
    """Create the pair.

    Args:
      channel: The session.
      streamed: The client of the streamed endpoint.
      service: The contract of the session endpoint.
      streaming: The contract of the streamed endpoint.
      fault: Returns the error to raise for a fault.
    """
    self._channel = channel
    self._streamed = streamed
    self._service = service
    self._streaming = streaming
    self._fault = fault

  @property
  def is_open(self) -> bool:
    """Whether the session is open for calls."""
    return self._channel.is_open

  async def open(self) -> None:
    """Open the session.

    Raises:
      FramingFault: The service refused the session.
    """
    await self._channel.open()

  async def close(self) -> None:
    """End the session in an orderly way and disconnect."""
    await self._channel.close()

  async def __aenter__(self: _Self) -> _Self:
    """Open the session."""
    await self.open()
    return self

  async def __aexit__(self, *exc_info: object) -> None:
    """Close the session."""
    await self.close()

  async def _call(
    self, operation: Operation, *arguments: Any, timeout: Optional[float] = None
  ) -> Any:
    """Make a call on the session and convert its reply."""
    values = _arguments(operation, arguments)
    try:
      reply = await self._channel.call(operation, *values, timeout=timeout)
    except SoapFault as fault:
      raise self._fault(self._service, operation, fault) from fault
    return _reply(operation, reply)

  async def _streamed_call(self, operation: Operation, *arguments: Any) -> Any:
    """Make a call without streams on the streamed endpoint and convert its reply."""
    values = _arguments(operation, arguments)
    try:
      reply = await self._streamed.call(operation, *values)
    except SoapFault as fault:
      raise self._fault(self._streaming, operation, fault) from fault
    return _reply(operation, reply)

  async def _download(self, operation: Operation, *arguments: Any) -> AsyncIterator[bytes]:
    """Make a call that returns a stream and yield its bytes as they arrive."""
    values = _arguments(operation, arguments)
    pieces = cast(AsyncGenerator[bytes, None], self._streamed.download(operation, *values))
    try:
      async with aclosing(pieces):
        async for piece in pieces:
          yield piece
    except SoapFault as fault:
      raise self._fault(self._streaming, operation, fault) from fault

  async def _upload(self, operation: Operation, *arguments: Any, data: bytes) -> None:
    """Make a call whose last parameter is a stream, sending data as the stream."""
    values = _arguments(operation, arguments)
    try:
      await self._streamed.upload(operation, *values, source=data)
    except SoapFault as fault:
      raise self._fault(self._streaming, operation, fault) from fault


class ControllerClient(_Endpoints):
  """Client of the controller's two endpoints.

  :meth:`open` starts the session on the duplex endpoint; the streamed operations open a
  connection of their own per call and need no session. The controller serves one client at a
  time: :meth:`controller_connect` registers this session for the callbacks. A handler runs in
  the task that receives messages as soon as its callback arrives, which is before the reply of
  a request that caused it; it should return quickly. A callback without a handler is logged at
  debug level and dropped.
  """

  def _dispatch(self, callback: Operation, *arguments: Any) -> None:
    """Convert a callback's arguments and pass them to its handler."""
    kinds = [xml_type for _, xml_type in callback.parameters]
    values = [from_wire(xml_type, value) for xml_type, value in zip(kinds, arguments)]
    handler = self._handlers.get(callback.name)
    if handler is None:
      logger.debug("no handler for the callback %s", callback.name)
      return
    handler(*values)

  def __init__(
    self,
    host: str,
    port: int = CONTROLLER_PORT,
    streaming_port: int = CONTROLLER_STREAMING_PORT,
    request_timeout: float = 60.0,
    io_timeout: float = 30.0,
    stream_reply_timeout: float = 300.0,
    socket: Optional[Socket] = None,
    streaming_socket_factory: Optional[Callable[[], Socket]] = None,
    message_ids: Callable[[], str] = new_message_id,
  ) -> None:
    """Create the client; :meth:`open` connects it.

    Args:
      host: Host name or address of the instrument's internal computer.
      port: Port of the duplex endpoint.
      streaming_port: Port of the streamed endpoint.
      request_timeout: Seconds to wait for the reply to a request on the session, unless the
        call says otherwise.
      io_timeout: Seconds to wait for a write and for the rest of a message that started.
      stream_reply_timeout: Seconds to wait for the reply of a streamed call to begin.
      socket: The transport of the session, instead of a new ``Socket``.
      streaming_socket_factory: Returns the transport of one streamed call, instead of a new
        ``Socket``.
      message_ids: Returns the MessageID of the next request, on either endpoint.
    """
    channel = DuplexChannel(
      f"net.tcp://{host}:{port}/Controller",
      request_timeout=request_timeout,
      io_timeout=io_timeout,
      socket=socket,
      message_ids=message_ids,
    )
    streamed = StreamedClient(
      f"net.tcp://{host}:{streaming_port}/ControllerStreaming",
      reply_timeout=stream_reply_timeout,
      io_timeout=io_timeout,
      socket_factory=streaming_socket_factory,
      message_ids=message_ids,
    )
    super().__init__(
      channel, streamed, wire.CONTROLLER, wire.CONTROLLER_STREAMING, _controller_fault
    )
    self._handlers: Dict[str, Callable[..., None]] = {}
    for callback in wire.CONTROLLER_CALLBACKS:
      channel.on(callback, functools.partial(self._dispatch, callback))

  def _set_handler(self, callback: Operation, handler: Optional[Callable[..., None]]) -> None:
    """Set or remove the handler of a callback."""
    if handler is None:
      self._handlers.pop(callback.name, None)
    else:
      self._handlers[callback.name] = handler

  def on_test_connection(self, handler: Optional[Callable[[], None]]) -> None:
    """Set the handler of the TestConnection callback. Liveness probe the controller sends while a
    client connects, and during a worklist named Prime.

    Args:
      handler: Called without arguments, or None to remove the handler.
    """
    self._set_handler(wire.TEST_CONNECTION, handler)

  def on_cytometer_has_uint_fcs_data(
    self, handler: Optional[Callable[[Optional[array.array[int]], int], None]]
  ) -> None:
    """Set the handler of the Cytometer_OnHasUIntFCSData callback. Event data of older cytometer
    models, as unsigned integers.

    Args:
      handler: Called with fcs_data, data_index, or None to remove the handler. fcs_data: Whole
        events, one value per FCS parameter; None or empty is possible. data_index: Running total of
        values sent in this worklist, this chunk included.
    """
    self._set_handler(wire.CYTOMETER_ON_HAS_UINT_FCS_DATA, handler)

  def on_cytometer_has_float_fcs_data(
    self, handler: Optional[Callable[[Optional[array.array[float]], int], None]]
  ) -> None:
    """Set the handler of the Cytometer_OnHasFloatFCSData callback. Event data during a worklist.

    Args:
      handler: Called with fcs_data, data_index, or None to remove the handler. fcs_data: Whole
        events, one float per FCS parameter in the model's parameter order; None or empty is
        possible. data_index: Running total of values sent in this worklist, this chunk included.
    """
    self._set_handler(wire.CYTOMETER_ON_HAS_FLOAT_FCS_DATA, handler)

  def on_cytometer_long_running_operation_completed(
    self, handler: Optional[Callable[[Optional[str]], None]]
  ) -> None:
    """Set the handler of the OnCytometerLongRunningOperationCompleted callback. End of a firmware
    update, fluid-station calibration or odometer reset.

    Args:
      handler: Called with error, or None to remove the handler. error: Empty on success, else the
        error.
    """
    self._set_handler(wire.ON_CYTOMETER_LONG_RUNNING_OPERATION_COMPLETED, handler)

  def on_system_state_changed(self, handler: Optional[Callable[[SystemState], None]]) -> None:
    """Set the handler of the OnSystemStateChanged callback. The overall instrument state changed.

    Args:
      handler: Called with state, or None to remove the handler. state: The new state.
    """
    self._set_handler(wire.ON_SYSTEM_STATE_CHANGED, handler)

  def on_device_state_changed(
    self, handler: Optional[Callable[[Optional[DeviceInfo]], None]]
  ) -> None:
    """Set the handler of the OnDeviceStateChanged callback. A device's state or status messages
    changed.

    Args:
      handler: Called with device_info, or None to remove the handler. device_info: The device's new
        state and messages.
    """
    self._set_handler(wire.ON_DEVICE_STATE_CHANGED, handler)

  def on_contact_sensor_state_changed(self, handler: Optional[Callable[[bool], None]]) -> None:
    """Set the handler of the OnContactSensorStateChanged callback. The probe contact sensor
    changed.

    Args:
      handler: Called with has_contact, or None to remove the handler. has_contact: True when the
        probe touches something.
    """
    self._set_handler(wire.ON_CONTACT_SENSOR_STATE_CHANGED, handler)

  def on_device_alert(
    self, handler: Optional[Callable[[Optional[DeviceAlertMessage]], None]]
  ) -> None:
    """Set the handler of the OnDeviceAlert callback. A device raised or cleared an alert.

    Args:
      handler: Called with alert, or None to remove the handler. alert: The alert.
    """
    self._set_handler(wire.ON_DEVICE_ALERT, handler)

  def on_htfc_calibrate_fluidics_completed(
    self, handler: Optional[Callable[[Optional[str]], None]]
  ) -> None:
    """Set the handler of the OnHTFCCalibrateFluidicsCompleted callback. End of
    htfc_calibrate_fluidics.

    Args:
      handler: Called with error, or None to remove the handler. error: Empty on success, else the
        error.
    """
    self._set_handler(wire.ON_HTFC_CALIBRATE_FLUIDICS_COMPLETED, handler)

  def on_command_started(
    self, handler: Optional[Callable[[Optional[WorklistCommand]], None]]
  ) -> None:
    """Set the handler of the OnCommandStarted callback. A worklist command is about to execute; its
    StartTime is not yet set.

    Args:
      handler: Called with command, or None to remove the handler. command: The command.
    """
    self._set_handler(wire.ON_COMMAND_STARTED, handler)

  def on_command_completed(
    self, handler: Optional[Callable[[Optional[WorklistCommand]], None]]
  ) -> None:
    """Set the handler of the OnCommandCompleted callback. A worklist command finished or was
    skipped.

    Args:
      handler: Called with command, or None to remove the handler. command: The command with
        StartTime, EndTime, Completed and ExecutionNotes filled in.
    """
    self._set_handler(wire.ON_COMMAND_COMPLETED, handler)

  def on_worklist_completed(self, handler: Optional[Callable[[Optional[Worklist]], None]]) -> None:
    """Set the handler of the OnWorklistCompleted callback. The worklist run ended.

    Args:
      handler: Called with worklist, or None to remove the handler. worklist: The whole worklist
        with the fields the controller fills in.
    """
    self._set_handler(wire.ON_WORKLIST_COMPLETED, handler)

  async def controller_connect(
    self, client_time: datetime.datetime, *, timeout: Optional[float] = None
  ) -> Optional[ConnectionInfo]:
    """Controller_Connect: Register this session as the controller's single client.

    Unless another client's session still answers, this session receives the callbacks from now on.
    The controller stores client_time minus its own clock as the client time offset of later
    worklists; an aware local time travels as a .NET local time.

    Args:
      client_time: The client's current time.
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The controller's version and whether another client is registered.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(
      Optional[ConnectionInfo],
      await self._call(wire.CONTROLLER_CONNECT, client_time, timeout=timeout),
    )

  async def controller_set_verbose_logging(
    self, verbose_logging: bool, *, timeout: Optional[float] = None
  ) -> None:
    """Controller_SetVerboseLogging: Switch the controller's log between debug and warning level;
    the setting is stored.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      verbose_logging: True for debug level.
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.CONTROLLER_SET_VERBOSE_LOGGING, verbose_logging, timeout=timeout)

  async def controller_enable_demo_mode(
    self, demo_mode: bool, *, timeout: Optional[float] = None
  ) -> None:
    """Controller_EnableDemoMode: Switch the device layer to demo drivers that need no hardware.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      demo_mode: True for demo drivers.
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.CONTROLLER_ENABLE_DEMO_MODE, demo_mode, timeout=timeout)

  async def controller_is1536_capable(self, *, timeout: Optional[float] = None) -> bool:
    """Controller_Is1536Capable: Tell whether the instrument can run 1536-well plates.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      True for a high-density model whose shaker supports them, or in demo mode.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(bool, await self._call(wire.CONTROLLER_IS1536_CAPABLE, timeout=timeout))

  async def contact_sensor_has_contact(self, *, timeout: Optional[float] = None) -> bool:
    """ContactSensor_HasContact: Read the probe contact sensor.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The last polled contact state; False when there is no sensor.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(bool, await self._call(wire.CONTACT_SENSOR_HAS_CONTACT, timeout=timeout))

  async def cytometer_clear_data(self, *, timeout: Optional[float] = None) -> None:
    """Cytometer_ClearData: Restart the controller's FCS file and zero its event count.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.CYTOMETER_CLEAR_DATA, timeout=timeout)

  async def cytometer_is_ready_to_run(self, *, timeout: Optional[float] = None) -> bool:
    """Cytometer_IsReadyToRun: Tell whether the cytometer can start a measurement.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      True when no status blocks a run.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(bool, await self._call(wire.CYTOMETER_IS_READY_TO_RUN, timeout=timeout))

  async def cytometer_is_ready_to_clean(self, *, timeout: Optional[float] = None) -> bool:
    """Cytometer_IsReadyToClean: Tell whether the cytometer can start fluidics maintenance.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      True when no status blocks maintenance.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(bool, await self._call(wire.CYTOMETER_IS_READY_TO_CLEAN, timeout=timeout))

  async def cytometer_is_powering_on(self, *, timeout: Optional[float] = None) -> bool:
    """Cytometer_IsPoweringOn: Tell whether the controller is still waiting for the cytometer after
    controller_power_on_cytometer.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      True during the power-on scan.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(bool, await self._call(wire.CYTOMETER_IS_POWERING_ON, timeout=timeout))

  async def cytometer_get_operation_duration(
    self, *, timeout: Optional[float] = None
  ) -> datetime.timedelta:
    """Cytometer_GetOperationDuration: Read the remaining time of the cytometer's current operation.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The remaining time.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(
      datetime.timedelta, await self._call(wire.CYTOMETER_GET_OPERATION_DURATION, timeout=timeout)
    )

  async def cytometer_update_firmware(
    self, type: CytometerFirmwareType, *, timeout: Optional[float] = None
  ) -> None:
    """Cytometer_UpdateFirmware: Flash the firmware file sent before with cytometer_upload_firmware.

    Runs in the background for up to about 10 minutes; the result arrives as the
    OnCytometerLongRunningOperationCompleted callback.

    Args:
      type: Which firmware to flash.
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.CYTOMETER_UPDATE_FIRMWARE, type, timeout=timeout)

  async def cytometer_calibrate_fluid_stations(self, *, timeout: Optional[float] = None) -> None:
    """Cytometer_CalibrateFluidStations: Calibrate the bottle weight stations.

    The result arrives as the OnCytometerLongRunningOperationCompleted callback.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.CYTOMETER_CALIBRATE_FLUID_STATIONS, timeout=timeout)

  async def cytometer_reset_consumables_odometer(self, *, timeout: Optional[float] = None) -> None:
    """Cytometer_ResetConsumablesOdometer: Clear the fluidic consumables work time.

    The result arrives as the OnCytometerLongRunningOperationCompleted callback.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.CYTOMETER_RESET_CONSUMABLES_ODOMETER, timeout=timeout)

  async def cytometer_shutdown(self, *, timeout: Optional[float] = None) -> None:
    """Cytometer_Shutdown: Shut the cytometer down; refused when a leak error is present.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.CYTOMETER_SHUTDOWN, timeout=timeout)

  async def controller_get_system_id(
    self, force_refresh: bool, *, timeout: Optional[float] = None
  ) -> Optional[str]:
    """Controller_GetSystemID: Read the instrument's system ID (the MAC address of its first active
    network card).

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      force_refresh: True to read it again instead of using the stored value.
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The system ID.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(
      Optional[str], await self._call(wire.CONTROLLER_GET_SYSTEM_ID, force_refresh, timeout=timeout)
    )

  async def controller_get_system_type(self, *, timeout: Optional[float] = None) -> SystemType:
    """Controller_GetSystemType: Read the instrument model the controller is configured for.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The configured model.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(SystemType, await self._call(wire.CONTROLLER_GET_SYSTEM_TYPE, timeout=timeout))

  async def controller_determine_system_type(
    self, *, timeout: Optional[float] = None
  ) -> SystemType:
    """Controller_DetermineSystemType: Read the instrument model, asking the simulator first if one
    runs.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The model.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(
      SystemType, await self._call(wire.CONTROLLER_DETERMINE_SYSTEM_TYPE, timeout=timeout)
    )

  async def controller_set_system_type(
    self, system_type: SystemType, *, timeout: Optional[float] = None
  ) -> None:
    """Controller_SetSystemType: Set the instrument model in the controller's settings (held in
    memory until the next save).

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      system_type: The model.
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.CONTROLLER_SET_SYSTEM_TYPE, system_type, timeout=timeout)

  async def controller_get_system_state(self, *, timeout: Optional[float] = None) -> SystemState:
    """Controller_GetSystemState: Read the overall instrument state.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      Red, Yellow or Green.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(SystemState, await self._call(wire.CONTROLLER_GET_SYSTEM_STATE, timeout=timeout))

  async def controller_power_on_cytometer(self, *, timeout: Optional[float] = None) -> None:
    """Controller_PowerOnCytometer: Power the cytometer on and look for it for up to 30 s in the
    background.

    Refused in demo mode. cytometer_is_powering_on tells when the search has ended.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.CONTROLLER_POWER_ON_CYTOMETER, timeout=timeout)

  async def controller_power_off_cytometer(self, *, timeout: Optional[float] = None) -> None:
    """Controller_PowerOffCytometer: Pulse the cytometer's power relay if the cytometer is
    connected; no fluidics shutdown is done.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.CONTROLLER_POWER_OFF_CYTOMETER, timeout=timeout)

  async def controller_cytometer_has_power(self, *, timeout: Optional[float] = None) -> bool:
    """Controller_CytometerHasPower: Tell whether the cytometer is connected.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      True when a cytometer is present and not disconnected.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(bool, await self._call(wire.CONTROLLER_CYTOMETER_HAS_POWER, timeout=timeout))

  async def controller_configure_devices(
    self,
    configuration: Optional[Dict[DeviceKind, Optional[DeviceConfiguration]]],
    *,
    timeout: Optional[float] = None,
  ) -> None:
    """Controller_ConfigureDevices: Add or remove devices from the controller's device set; refused
    while a worklist runs.

    Only the Shaker, Cytometer, PlateRail and ContactSensor entries are read.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      configuration: The devices to include or remove.
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.CONTROLLER_CONFIGURE_DEVICES, configuration, timeout=timeout)

  async def controller_reset_devices(
    self, options: Optional[DeviceResetOptions], *, timeout: Optional[float] = None
  ) -> bool:
    """Controller_ResetDevices: Apply device options, then disconnect and reconnect the devices;
    refused while a worklist runs.

    The system state is Yellow during the call.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      options: Device options.
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      True when the caller should now call controller_initialize_devices; False when the stored
        system ID does not match the instrument (the sampler is then disconnected).

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(bool, await self._call(wire.CONTROLLER_RESET_DEVICES, options, timeout=timeout))

  async def controller_initialize_devices(
    self, device_initialization_list: Optional[List[DeviceKind]], *, timeout: Optional[float] = None
  ) -> None:
    """Controller_InitializeDevices: Initialise devices concurrently and wait up to 50 s for them.

    Args:
      device_initialization_list: Devices to initialise; None or empty for every connected device.
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(
      wire.CONTROLLER_INITIALIZE_DEVICES, device_initialization_list, timeout=timeout
    )

  async def controller_get_device_statuses(
    self, *, timeout: Optional[float] = None
  ) -> Optional[List[Optional[DeviceInfo]]]:
    """Controller_GetDeviceStatuses: Read the state and status messages of every configured device.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      One entry per device.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(
      Optional[List[Optional[DeviceInfo]]],
      await self._call(wire.CONTROLLER_GET_DEVICE_STATUSES, timeout=timeout),
    )

  async def controller_get_device_summary(
    self, device_kind: DeviceKind, *, timeout: Optional[float] = None
  ) -> Optional[str]:
    """Controller_GetDeviceSummary: Read a device's own description, such as model, serial number
    and firmware versions.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      device_kind: The device.
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The description, or <KIND> OFFLINE.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(
      Optional[str],
      await self._call(wire.CONTROLLER_GET_DEVICE_SUMMARY, device_kind, timeout=timeout),
    )

  async def controller_get_summary(self, *, timeout: Optional[float] = None) -> Optional[str]:
    """Controller_GetSummary: Read the controller computer's operating system and .NET versions.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The description.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(Optional[str], await self._call(wire.CONTROLLER_GET_SUMMARY, timeout=timeout))

  async def htfc_calibrate_fluidics(self, *, timeout: Optional[float] = None) -> None:
    """HTFC_CalibrateFluidics: Calibrate the fluidics of an older cytometer model; does nothing on
    other models.

    The OnHTFCCalibrateFluidicsCompleted callback follows.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.HTFC_CALIBRATE_FLUIDICS, timeout=timeout)

  async def htfc_set_top_peak_values(
    self,
    fsc: int,
    ssc: int,
    fl1: int,
    fl2: int,
    fl3: int,
    fl4: int,
    *,
    timeout: Optional[float] = None,
  ) -> None:
    """HTFC_SetTopPeakValues: Set the top peak values of an older cytometer model.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      fsc: FSC top peak value.
      ssc: SSC top peak value.
      fl1: FL1 top peak value.
      fl2: FL2 top peak value.
      fl3: FL3 top peak value.
      fl4: FL4 top peak value.
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.HTFC_SET_TOP_PEAK_VALUES, fsc, ssc, fl1, fl2, fl3, fl4, timeout=timeout)

  async def htfc_get_top_peak_values(
    self, *, timeout: Optional[float] = None
  ) -> Tuple[int, int, int, int, int, int]:
    """HTFC_GetTopPeakValues: Read the top peak values of an older cytometer model; zeros on other
    models.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The values fsc, ssc, fl1, fl2, fl3, fl4, in that order.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(
      Tuple[int, int, int, int, int, int],
      await self._call(wire.HTFC_GET_TOP_PEAK_VALUES, timeout=timeout),
    )

  async def htfc_is_gen_iv(self, *, timeout: Optional[float] = None) -> bool:
    """HTFC_IsGenIV: Tell whether the cytometer is an older generation IV model.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      True for such a model.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(bool, await self._call(wire.HTFC_IS_GEN_IV, timeout=timeout))

  async def ique3_run_next_purge_step(self, *, timeout: Optional[float] = None) -> None:
    """IQue3_RunNextPurgeStep: Start the next step of the decommissioning purge (FlushSystem,
    PurgeSystem, PurgeSensor).

    A step starts only when the previous one is complete; the last one shuts the instrument down.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.IQUE3_RUN_NEXT_PURGE_STEP, timeout=timeout)

  async def ique3_get_current_purge_step(
    self, *, timeout: Optional[float] = None
  ) -> Tuple[IQue3PurgeStep, bool]:
    """IQue3_GetCurrentPurgeStep: Read the current step of the decommissioning purge.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The step and whether it is completed.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(
      Tuple[IQue3PurgeStep, bool],
      await self._call(wire.IQUE3_GET_CURRENT_PURGE_STEP, timeout=timeout),
    )

  async def ique3_run_next_installation_priming_step(
    self, *, timeout: Optional[float] = None
  ) -> None:
    """IQue3_RunNextInstallationPrimingStep: Start the next step of the installation priming
    sequence.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.IQUE3_RUN_NEXT_INSTALLATION_PRIMING_STEP, timeout=timeout)

  async def ique3_get_current_installation_priming_step(
    self, *, timeout: Optional[float] = None
  ) -> Tuple[IQue3InstallationPrimeStep, bool]:
    """IQue3_GetCurrentInstallationPrimingStep: Read the current step of the installation priming
    sequence.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The step and whether it is completed.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(
      Tuple[IQue3InstallationPrimeStep, bool],
      await self._call(wire.IQUE3_GET_CURRENT_INSTALLATION_PRIMING_STEP, timeout=timeout),
    )

  async def plate_rail_load(self, *, timeout: Optional[float] = None) -> None:
    """PlateRail_Load: Move the plate rail in, to the load position (15 s limit).

    The controller does not check the probe position or whether a plate is present.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.PLATE_RAIL_LOAD, timeout=timeout)

  async def plate_rail_eject(self, *, timeout: Optional[float] = None) -> None:
    """PlateRail_Eject: Move the plate rail out, to the eject position (15 s limit).

    The controller does not check the probe position.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.PLATE_RAIL_EJECT, timeout=timeout)

  async def pump_get_min_speed(self, *, timeout: Optional[float] = None) -> int:
    """Pump_GetMinSpeed: Read the pump's lowest speed.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      Speed in hundredths of rpm.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(int, await self._call(wire.PUMP_GET_MIN_SPEED, timeout=timeout))

  async def pump_get_max_speed(self, *, timeout: Optional[float] = None) -> int:
    """Pump_GetMaxSpeed: Read the pump's highest speed.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      Speed in hundredths of rpm.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(int, await self._call(wire.PUMP_GET_MAX_SPEED, timeout=timeout))

  async def pump_get_default_speed(self, *, timeout: Optional[float] = None) -> int:
    """Pump_GetDefaultSpeed: Read the pump's default speed.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      Speed in hundredths of rpm.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(int, await self._call(wire.PUMP_GET_DEFAULT_SPEED, timeout=timeout))

  async def pump_start(
    self, speed: int, direction: PumpDirection, *, timeout: Optional[float] = None
  ) -> None:
    """Pump_Start: Start the pump when it is stopped.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      speed: Speed in hundredths of rpm; clamped to the pump's range.
      direction: Direction.
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.PUMP_START, speed, direction, timeout=timeout)

  async def pump_update(
    self, speed: int, direction: PumpDirection, *, timeout: Optional[float] = None
  ) -> None:
    """Pump_Update: Change speed and direction of the running pump.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      speed: Speed in hundredths of rpm; clamped to the pump's range.
      direction: Direction.
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.PUMP_UPDATE, speed, direction, timeout=timeout)

  async def pump_stop(self, *, timeout: Optional[float] = None) -> None:
    """Pump_Stop: Stop the pump.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.PUMP_STOP, timeout=timeout)

  async def sampler_home(self, *, timeout: Optional[float] = None) -> None:
    """Sampler_Home: Park the sampler slowly: Z to the top, then XY to the maximum X and Y.

    Refused while the contact sensor reports contact.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.SAMPLER_HOME, timeout=timeout)

  async def sampler_get_x(self, *, timeout: Optional[float] = None) -> decimal.Decimal:
    """Sampler_GetX: Read the sampler's X position.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      X, a sampler coordinate in mm; -1 when reading fails.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(decimal.Decimal, await self._call(wire.SAMPLER_GET_X, timeout=timeout))

  async def sampler_get_y(self, *, timeout: Optional[float] = None) -> decimal.Decimal:
    """Sampler_GetY: Read the sampler's Y position.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      Y, a sampler coordinate in mm; -1 when reading fails.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(decimal.Decimal, await self._call(wire.SAMPLER_GET_Y, timeout=timeout))

  async def sampler_get_z(self, *, timeout: Optional[float] = None) -> decimal.Decimal:
    """Sampler_GetZ: Read the sampler's Z position.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      Z, a sampler Z coordinate in mm (larger is higher); -1 when reading fails.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(decimal.Decimal, await self._call(wire.SAMPLER_GET_Z, timeout=timeout))

  async def sampler_get_x_range(
    self, *, timeout: Optional[float] = None
  ) -> Optional[MovementRange]:
    """Sampler_GetXRange: Read the range of the sampler's X axis.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The range in mm.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(
      Optional[MovementRange], await self._call(wire.SAMPLER_GET_X_RANGE, timeout=timeout)
    )

  async def sampler_get_y_range(
    self, *, timeout: Optional[float] = None
  ) -> Optional[MovementRange]:
    """Sampler_GetYRange: Read the range of the sampler's Y axis.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The range in mm.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(
      Optional[MovementRange], await self._call(wire.SAMPLER_GET_Y_RANGE, timeout=timeout)
    )

  async def sampler_get_z_range(
    self, *, timeout: Optional[float] = None
  ) -> Optional[MovementRange]:
    """Sampler_GetZRange: Read the range of the sampler's Z axis.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The range in mm.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(
      Optional[MovementRange], await self._call(wire.SAMPLER_GET_Z_RANGE, timeout=timeout)
    )

  async def sampler_get_z_contact_tolerance(
    self, activity: ContactToleranceType, *, timeout: Optional[float] = None
  ) -> decimal.Decimal:
    """Sampler_GetZContactTolerance: Read the allowed Z contact deviation for an activity.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      activity: The activity.
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The tolerance in mm.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(
      decimal.Decimal,
      await self._call(wire.SAMPLER_GET_Z_CONTACT_TOLERANCE, activity, timeout=timeout),
    )

  async def sampler_get_smallest_supported_move(
    self, *, timeout: Optional[float] = None
  ) -> decimal.Decimal:
    """Sampler_GetSmallestSupportedMove: Read the smallest move the sampler supports.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The step in mm.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(
      decimal.Decimal, await self._call(wire.SAMPLER_GET_SMALLEST_SUPPORTED_MOVE, timeout=timeout)
    )

  async def sampler_move_xy(
    self, x: decimal.Decimal, y: decimal.Decimal, fast: bool, *, timeout: Optional[float] = None
  ) -> None:
    """Sampler_MoveXY: Move the sampler in X and Y and wait until it stops.

    The controller does not check the probe height first.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      x: Target X, a sampler coordinate in mm.
      y: Target Y, a sampler coordinate in mm.
      fast: True for the fast speed, False for the slow one.
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.SAMPLER_MOVE_XY, x, y, fast, timeout=timeout)

  async def sampler_move_z(self, z: decimal.Decimal, *, timeout: Optional[float] = None) -> None:
    """Sampler_MoveZ: Move the sampler in Z; returns without waiting for the move to end.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      z: Target Z, a sampler Z coordinate in mm (larger is higher).
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.SAMPLER_MOVE_Z, z, timeout=timeout)

  async def sampler_find_z_contact(
    self,
    z_high: decimal.Decimal,
    z_low: decimal.Decimal,
    retract_after_find: bool,
    *,
    timeout: Optional[float] = None,
  ) -> Optional[ContactZResult]:
    """Sampler_FindZContact: Lower the probe at the current XY until it touches something.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      z_high: Z to start from, a sampler Z coordinate in mm (larger is higher).
      z_low: Lowest Z to go to, a sampler Z coordinate in mm (larger is higher).
      retract_after_find: True to raise the probe afterwards.
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      Where contact was found, or why not.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(
      Optional[ContactZResult],
      await self._call(
        wire.SAMPLER_FIND_Z_CONTACT, z_high, z_low, retract_after_find, timeout=timeout
      ),
    )

  async def sampler_y_axis_is_inverted(self, *, timeout: Optional[float] = None) -> bool:
    """Sampler_YAxisIsInverted: Tell whether the sampler's Y axis is inverted.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      True for the instrument's sampler model.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(bool, await self._call(wire.SAMPLER_Y_AXIS_IS_INVERTED, timeout=timeout))

  async def shaker_min_rpm(self, *, timeout: Optional[float] = None) -> int:
    """Shaker_MinRPM: Read the shaker's lowest speed.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      Speed in rpm.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(int, await self._call(wire.SHAKER_MIN_RPM, timeout=timeout))

  async def shaker_max_rpm(self, *, timeout: Optional[float] = None) -> int:
    """Shaker_MaxRPM: Read the shaker's highest speed.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      Speed in rpm.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(int, await self._call(wire.SHAKER_MAX_RPM, timeout=timeout))

  async def shaker_start(self, rpm: int, *, timeout: Optional[float] = None) -> None:
    """Shaker_Start: Start the shaker, or change its speed, and wait until it is at speed.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      rpm: Speed in rpm; clamped to the shaker's range.
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.SHAKER_START, rpm, timeout=timeout)

  async def shaker_stop(self, *, timeout: Optional[float] = None) -> None:
    """Shaker_Stop: Stop the shaker and wait until it is back at its home position.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.SHAKER_STOP, timeout=timeout)

  async def shaker_home(self, *, timeout: Optional[float] = None) -> None:
    """Shaker_Home: Close the plate lock and move the shaker to its home position.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.SHAKER_HOME, timeout=timeout)

  async def shaker_open_plate_lock(self, *, timeout: Optional[float] = None) -> None:
    """Shaker_OpenPlateLock: Open the shaker's plate clamp; it closes itself and faults the shaker
    after 5 minutes.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.SHAKER_OPEN_PLATE_LOCK, timeout=timeout)

  async def shaker_close_plate_lock(self, *, timeout: Optional[float] = None) -> None:
    """Shaker_ClosePlateLock: Close the shaker's plate clamp.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.SHAKER_CLOSE_PLATE_LOCK, timeout=timeout)

  async def worklist_run(
    self, worklist: Optional[Worklist], *, timeout: Optional[float] = None
  ) -> None:
    """Worklist_Run: Start running a worklist; returns as soon as the run has started.

    The status must be NoActiveWorklist. Progress arrives as callbacks: OnCommandStarted,
    OnCommandCompleted, the event data callbacks and OnWorklistCompleted.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      worklist: The worklist.
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.WORKLIST_RUN, worklist, timeout=timeout)

  async def worklist_skip_to(self, id: uuid.UUID, *, timeout: Optional[float] = None) -> None:
    """Worklist_SkipTo: Skip forward to a command; takes effect between commands.

    Commands up to that one are reported as Skipped; an unknown ID skips every remaining command.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      id: ID of the command to continue with.
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.WORKLIST_SKIP_TO, id, timeout=timeout)

  async def worklist_cancel(self, *, timeout: Optional[float] = None) -> None:
    """Worklist_Cancel: Ask the running worklist to stop.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.WORKLIST_CANCEL, timeout=timeout)

  async def worklist_get_current(self, *, timeout: Optional[float] = None) -> Optional[Worklist]:
    """Worklist_GetCurrent: Read the current or last worklist with the fields the controller fills
    in.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The worklist.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(Optional[Worklist], await self._call(wire.WORKLIST_GET_CURRENT, timeout=timeout))

  async def worklist_finalize(self, *, timeout: Optional[float] = None) -> None:
    """Worklist_Finalize: Acknowledge a finished worklist, archiving it if it has an ExperimentID;
    required before the next run.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._call(wire.WORKLIST_FINALIZE, timeout=timeout)

  async def worklist_get_status(self, *, timeout: Optional[float] = None) -> WorklistStatus:
    """Worklist_GetStatus: Read the worklist status.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The status.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(WorklistStatus, await self._call(wire.WORKLIST_GET_STATUS, timeout=timeout))

  async def worklist_is_fcs_file_available(self, *, timeout: Optional[float] = None) -> bool:
    """Worklist_IsFCSFileAvailable: Tell whether the current worklist's FCS file exists.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      True when it exists.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(bool, await self._call(wire.WORKLIST_IS_FCS_FILE_AVAILABLE, timeout=timeout))

  async def worklist_get_completed_worklists(
    self, *, timeout: Optional[float] = None
  ) -> Optional[List[Optional[CompletedWorklist]]]:
    """Worklist_GetCompletedWorklists: List the archived worklists, newest first.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The archive entries.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(
      Optional[List[Optional[CompletedWorklist]]],
      await self._call(wire.WORKLIST_GET_COMPLETED_WORKLISTS, timeout=timeout),
    )

  async def worklist_get_completed_worklist(
    self, id: uuid.UUID, *, timeout: Optional[float] = None
  ) -> Optional[CompletedWorklist]:
    """Worklist_GetCompletedWorklist: Read one archived worklist.

    The controller refuses it with a fault until a client has called controller_connect.

    Args:
      id: ID of the archived worklist.
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The archive entry.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return cast(
      Optional[CompletedWorklist],
      await self._call(wire.WORKLIST_GET_COMPLETED_WORKLIST, id, timeout=timeout),
    )

  async def controller_get_log_archive(self) -> bytes:
    """Controller_GetLogArchive: Download a zip archive of the controller's logs.

    Returns:
      The archive.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return b"".join([piece async for piece in self._download(wire.CONTROLLER_GET_LOG_ARCHIVE)])

  async def controller_upload_fcs_demo_file(self, data: bytes) -> None:
    """Controller_UploadFcsDemoFile: Upload the FCS file the demo cytometer replays.

    Args:
      data: The FCS file.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._upload(wire.CONTROLLER_UPLOAD_FCS_DEMO_FILE, data=data)

  async def cytometer_upload_firmware(self, file_name: Optional[str], data: bytes) -> None:
    """Cytometer_UploadFirmware: Upload a cytometer firmware file for cytometer_update_firmware; at
    most 50,000 KB.

    Args:
      file_name: File name only, without folders.
      data: The firmware file.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._upload(wire.CYTOMETER_UPLOAD_FIRMWARE, file_name, data=data)

  async def controller_download_calibration_settings(self) -> bytes:
    """Controller_DownloadCalibrationSettings: Download the calibration file the controller stores
    for the client.

    Returns:
      The calibration file; the controller never reads it.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    return b"".join(
      [piece async for piece in self._download(wire.CONTROLLER_DOWNLOAD_CALIBRATION_SETTINGS)]
    )

  async def controller_upload_calibration_settings(self, data: bytes) -> None:
    """Controller_UploadCalibrationSettings: Store the calibration file; at most 5,000 KB.

    Args:
      data: The calibration file.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._upload(wire.CONTROLLER_UPLOAD_CALIBRATION_SETTINGS, data=data)

  async def controller_clear_calibration_settings(self, clear_reason: Optional[str]) -> None:
    """Controller_ClearCalibrationSettings: Delete the stored calibration file.

    Args:
      clear_reason: Why; written to the controller's log.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    await self._streamed_call(wire.CONTROLLER_CLEAR_CALIBRATION_SETTINGS, clear_reason)

  async def worklist_get_current_fcs_file(self) -> AsyncIterator[bytes]:
    """Worklist_GetCurrentFCSFile: Download the current worklist's FCS file.

    Yields:
      The file, in pieces as they arrive.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    async for piece in self._download(wire.WORKLIST_GET_CURRENT_FCS_FILE):
      yield piece

  async def worklist_get_completed_worklist_fcs_file(self, id: uuid.UUID) -> AsyncIterator[bytes]:
    """Worklist_GetCompletedWorklistFCSFile: Download the FCS file of an archived worklist.

    Args:
      id: ID of the archived worklist.

    Yields:
      The file, in pieces as they arrive.

    Raises:
      ControllerFault: The service answered with a fault.
    """
    async for piece in self._download(wire.WORKLIST_GET_COMPLETED_WORKLIST_FCS_FILE, id):
      yield piece


class WatchdogClient(_Endpoints):
  """Client of the watchdog's two endpoints, which start and stop the controller service.

  :meth:`open` starts the session on the watchdog's endpoint; the streamed operations open a
  connection of their own per call.
  """

  def __init__(
    self,
    host: str,
    port: int = WATCHDOG_PORT,
    streaming_port: int = WATCHDOG_STREAMING_PORT,
    request_timeout: float = 60.0,
    io_timeout: float = 30.0,
    stream_reply_timeout: float = 300.0,
    socket: Optional[Socket] = None,
    streaming_socket_factory: Optional[Callable[[], Socket]] = None,
    message_ids: Callable[[], str] = new_message_id,
  ) -> None:
    """Create the client; :meth:`open` connects it.

    Args:
      host: Host name or address of the instrument's internal computer.
      port: Port of the watchdog's endpoint.
      streaming_port: Port of the watchdog's streamed endpoint.
      request_timeout: Seconds to wait for the reply to a request on the session, unless the
        call says otherwise.
      io_timeout: Seconds to wait for a write and for the rest of a message that started.
      stream_reply_timeout: Seconds to wait for the reply of a streamed call to begin.
      socket: The transport of the session, instead of a new ``Socket``.
      streaming_socket_factory: Returns the transport of one streamed call, instead of a new
        ``Socket``.
      message_ids: Returns the MessageID of the next request, on either endpoint.
    """
    channel = DuplexChannel(
      f"net.tcp://{host}:{port}/Watchdog",
      request_timeout=request_timeout,
      io_timeout=io_timeout,
      socket=socket,
      message_ids=message_ids,
    )
    streamed = StreamedClient(
      f"net.tcp://{host}:{streaming_port}/WatchdogStreaming",
      reply_timeout=stream_reply_timeout,
      io_timeout=io_timeout,
      socket_factory=streaming_socket_factory,
      message_ids=message_ids,
    )
    super().__init__(channel, streamed, wire.WATCHDOG, wire.WATCHDOG_STREAMING, _watchdog_fault)

  async def get_status(
    self, *, timeout: Optional[float] = None
  ) -> Optional[ControllerServiceStatus]:
    """GetStatus: Read whether the controller service runs.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Returns:
      The status.

    Raises:
      WatchdogFault: The service answered with a fault.
    """
    return cast(
      Optional[ControllerServiceStatus], await self._call(wire.WATCHDOG_GET_STATUS, timeout=timeout)
    )

  async def stop_service(self, *, timeout: Optional[float] = None) -> None:
    """StopService: Stop the controller service.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      WatchdogFault: The service answered with a fault.
    """
    await self._call(wire.WATCHDOG_STOP_SERVICE, timeout=timeout)

  async def install_updates(self, *, timeout: Optional[float] = None) -> None:
    """InstallUpdates: Install the package sent with upload_install_package and restart the
    controller service.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      WatchdogFault: The service answered with a fault.
    """
    await self._call(wire.WATCHDOG_INSTALL_UPDATES, timeout=timeout)

  async def start_service(self, *, timeout: Optional[float] = None) -> None:
    """StartService: Start the controller service.

    Args:
      timeout: Seconds to wait for the reply; the client's request timeout when None.

    Raises:
      WatchdogFault: The service answered with a fault.
    """
    await self._call(wire.WATCHDOG_START_SERVICE, timeout=timeout)

  async def upload_install_package(self, crc32: int, data: bytes) -> None:
    """UploadInstallPackage: Upload a controller install package for install_updates.

    Args:
      crc32: CRC-32 of the package.
      data: The package.

    Raises:
      WatchdogFault: The service answered with a fault.
    """
    await self._upload(wire.WATCHDOG_UPLOAD_INSTALL_PACKAGE, crc32, data=data)

  async def download_watchdog_log_file(self) -> bytes:
    """DownloadWatchdogLogFile: Download the watchdog's log file.

    Returns:
      The log file.

    Raises:
      WatchdogFault: The service answered with a fault.
    """
    return b"".join(
      [piece async for piece in self._download(wire.WATCHDOG_DOWNLOAD_WATCHDOG_LOG_FILE)]
    )
