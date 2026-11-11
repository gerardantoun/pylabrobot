"""Tests of the controller and watchdog clients, replaying exchanges recorded with .NET.

The recordings hold every call of the controller's and the watchdog's contracts, made by a
genuine .NET client against .NET services that declare the full contract. Each test replays
one: for every recorded call it decodes the request the .NET client sent, makes the same call
with the Python client, whose bytes must equal the recorded ones, and checks the reply, fault
and callbacks it decodes against the canonical text of what the .NET service sent.
"""

import collections
import dataclasses
import logging
import unittest
from typing import Any, Callable, Deque, Dict, List, Mapping, Sequence, Tuple, Union

from pylabrobot.io.socket import Socket
from pylabrobot.sartorius.ique.driver import contract as c
from pylabrobot.sartorius.ique.driver import recordings
from pylabrobot.sartorius.ique.driver.client import (
  ControllerClient,
  ControllerFault,
  WatchdogClient,
  WatchdogFault,
)
from pylabrobot.sartorius.ique.driver.wcf import STREAM, Operation, XmlType, soap
from pylabrobot.sartorius.ique.driver.wcf.datacontract import read_value
from pylabrobot.sartorius.ique.driver.wcf.recordings import Connection, ReplaySocket, envelopes
from pylabrobot.sartorius.ique.driver.wcf.tree import Element, declared

# The recordings were made through a relay at this address, which the Via and To headers name.
HOST = "127.0.0.2"
CONTROLLER_PORT = 29002
CONTROLLER_STREAMING_PORT = 29003
WATCHDOG_PORT = 29000
WATCHDOG_STREAMING_PORT = 29001

# Downloads whose method yields the file in pieces instead of returning it whole.
ITERATED = {"Worklist_GetCurrentFCSFile", "Worklist_GetCompletedWorklistFCSFile"}

Call = Dict[str, Any]
Received = List[Tuple[str, List[str]]]

CONTROLLER_METHODS: Dict[str, Callable[..., Any]] = {
  "Controller_Connect": ControllerClient.controller_connect,
  "Controller_SetVerboseLogging": ControllerClient.controller_set_verbose_logging,
  "Controller_EnableDemoMode": ControllerClient.controller_enable_demo_mode,
  "Controller_Is1536Capable": ControllerClient.controller_is1536_capable,
  "ContactSensor_HasContact": ControllerClient.contact_sensor_has_contact,
  "Cytometer_ClearData": ControllerClient.cytometer_clear_data,
  "Cytometer_IsReadyToRun": ControllerClient.cytometer_is_ready_to_run,
  "Cytometer_IsReadyToClean": ControllerClient.cytometer_is_ready_to_clean,
  "Cytometer_IsPoweringOn": ControllerClient.cytometer_is_powering_on,
  "Cytometer_GetOperationDuration": ControllerClient.cytometer_get_operation_duration,
  "Cytometer_UpdateFirmware": ControllerClient.cytometer_update_firmware,
  "Cytometer_CalibrateFluidStations": ControllerClient.cytometer_calibrate_fluid_stations,
  "Cytometer_ResetConsumablesOdometer": ControllerClient.cytometer_reset_consumables_odometer,
  "Cytometer_Shutdown": ControllerClient.cytometer_shutdown,
  "Controller_GetSystemID": ControllerClient.controller_get_system_id,
  "Controller_GetSystemType": ControllerClient.controller_get_system_type,
  "Controller_DetermineSystemType": ControllerClient.controller_determine_system_type,
  "Controller_SetSystemType": ControllerClient.controller_set_system_type,
  "Controller_GetSystemState": ControllerClient.controller_get_system_state,
  "Controller_PowerOnCytometer": ControllerClient.controller_power_on_cytometer,
  "Controller_PowerOffCytometer": ControllerClient.controller_power_off_cytometer,
  "Controller_CytometerHasPower": ControllerClient.controller_cytometer_has_power,
  "Controller_ConfigureDevices": ControllerClient.controller_configure_devices,
  "Controller_ResetDevices": ControllerClient.controller_reset_devices,
  "Controller_InitializeDevices": ControllerClient.controller_initialize_devices,
  "Controller_GetDeviceStatuses": ControllerClient.controller_get_device_statuses,
  "Controller_GetDeviceSummary": ControllerClient.controller_get_device_summary,
  "Controller_GetSummary": ControllerClient.controller_get_summary,
  "HTFC_CalibrateFluidics": ControllerClient.htfc_calibrate_fluidics,
  "HTFC_SetTopPeakValues": ControllerClient.htfc_set_top_peak_values,
  "HTFC_GetTopPeakValues": ControllerClient.htfc_get_top_peak_values,
  "HTFC_IsGenIV": ControllerClient.htfc_is_gen_iv,
  "IQue3_RunNextPurgeStep": ControllerClient.ique3_run_next_purge_step,
  "IQue3_GetCurrentPurgeStep": ControllerClient.ique3_get_current_purge_step,
  "IQue3_RunNextInstallationPrimingStep": ControllerClient.ique3_run_next_installation_priming_step,
  "IQue3_GetCurrentInstallationPrimingStep": (
    ControllerClient.ique3_get_current_installation_priming_step
  ),
  "PlateRail_Load": ControllerClient.plate_rail_load,
  "PlateRail_Eject": ControllerClient.plate_rail_eject,
  "Pump_GetMinSpeed": ControllerClient.pump_get_min_speed,
  "Pump_GetMaxSpeed": ControllerClient.pump_get_max_speed,
  "Pump_GetDefaultSpeed": ControllerClient.pump_get_default_speed,
  "Pump_Start": ControllerClient.pump_start,
  "Pump_Update": ControllerClient.pump_update,
  "Pump_Stop": ControllerClient.pump_stop,
  "Sampler_Home": ControllerClient.sampler_home,
  "Sampler_GetX": ControllerClient.sampler_get_x,
  "Sampler_GetY": ControllerClient.sampler_get_y,
  "Sampler_GetZ": ControllerClient.sampler_get_z,
  "Sampler_GetXRange": ControllerClient.sampler_get_x_range,
  "Sampler_GetYRange": ControllerClient.sampler_get_y_range,
  "Sampler_GetZRange": ControllerClient.sampler_get_z_range,
  "Sampler_GetZContactTolerance": ControllerClient.sampler_get_z_contact_tolerance,
  "Sampler_GetSmallestSupportedMove": ControllerClient.sampler_get_smallest_supported_move,
  "Sampler_MoveXY": ControllerClient.sampler_move_xy,
  "Sampler_MoveZ": ControllerClient.sampler_move_z,
  "Sampler_FindZContact": ControllerClient.sampler_find_z_contact,
  "Sampler_YAxisIsInverted": ControllerClient.sampler_y_axis_is_inverted,
  "Shaker_MinRPM": ControllerClient.shaker_min_rpm,
  "Shaker_MaxRPM": ControllerClient.shaker_max_rpm,
  "Shaker_Start": ControllerClient.shaker_start,
  "Shaker_Stop": ControllerClient.shaker_stop,
  "Shaker_Home": ControllerClient.shaker_home,
  "Shaker_OpenPlateLock": ControllerClient.shaker_open_plate_lock,
  "Shaker_ClosePlateLock": ControllerClient.shaker_close_plate_lock,
  "Worklist_Run": ControllerClient.worklist_run,
  "Worklist_SkipTo": ControllerClient.worklist_skip_to,
  "Worklist_Cancel": ControllerClient.worklist_cancel,
  "Worklist_GetCurrent": ControllerClient.worklist_get_current,
  "Worklist_Finalize": ControllerClient.worklist_finalize,
  "Worklist_GetStatus": ControllerClient.worklist_get_status,
  "Worklist_IsFCSFileAvailable": ControllerClient.worklist_is_fcs_file_available,
  "Worklist_GetCompletedWorklists": ControllerClient.worklist_get_completed_worklists,
  "Worklist_GetCompletedWorklist": ControllerClient.worklist_get_completed_worklist,
}
STREAMING_METHODS: Dict[str, Callable[..., Any]] = {
  "Controller_GetLogArchive": ControllerClient.controller_get_log_archive,
  "Controller_UploadFcsDemoFile": ControllerClient.controller_upload_fcs_demo_file,
  "Cytometer_UploadFirmware": ControllerClient.cytometer_upload_firmware,
  "Controller_DownloadCalibrationSettings": (
    ControllerClient.controller_download_calibration_settings
  ),
  "Controller_UploadCalibrationSettings": ControllerClient.controller_upload_calibration_settings,
  "Controller_ClearCalibrationSettings": ControllerClient.controller_clear_calibration_settings,
  "Worklist_GetCurrentFCSFile": ControllerClient.worklist_get_current_fcs_file,
  "Worklist_GetCompletedWorklistFCSFile": (
    ControllerClient.worklist_get_completed_worklist_fcs_file
  ),
}
WATCHDOG_METHODS: Dict[str, Callable[..., Any]] = {
  "GetStatus": WatchdogClient.get_status,
  "StopService": WatchdogClient.stop_service,
  "InstallUpdates": WatchdogClient.install_updates,
  "StartService": WatchdogClient.start_service,
}
WATCHDOG_STREAMING_METHODS: Dict[str, Callable[..., Any]] = {
  "UploadInstallPackage": WatchdogClient.upload_install_package,
  "DownloadWatchdogLogFile": WatchdogClient.download_watchdog_log_file,
}

OPERATIONS: Dict[str, Operation] = {
  operation.name: operation
  for operation in c.CONTROLLER_OPERATIONS
  + c.CONTROLLER_STREAMING_OPERATIONS
  + c.WATCHDOG_OPERATIONS
  + c.WATCHDOG_STREAMING_OPERATIONS
}


def _texts(kinds: Sequence[XmlType], values: Sequence[Any]) -> List[str]:
  """Return the canonical text of Python values of these wire types."""
  return [recordings.render(kind, c.to_wire(kind, value)) for kind, value in zip(kinds, values)]


def _inputs(operation: Operation) -> List[XmlType]:
  """Return the wire types of an operation's headers and parameters, without a stream."""
  return [kind for _, kind in operation.headers + operation.parameters if kind is not STREAM]


def _outputs(operation: Operation) -> List[XmlType]:
  """Return the wire types of what an operation returns: its result, then its out parameters."""
  kinds = [] if operation.result is None or operation.result is STREAM else [operation.result]
  return kinds + [kind for _, kind in operation.outputs]


def _request_arguments(operation: Operation, envelope: Element) -> List[Any]:
  """Return the Python arguments of a recorded request: its headers, then its parameters."""
  message = soap.from_envelope(envelope)
  scope = declared({}, envelope)
  values: List[Any] = []
  for name, kind in operation.headers:
    header = next(
      item for item in message.headers if (item.name, item.namespace) == (name, operation.namespace)
    )
    values.append(c.from_wire(kind, read_value(kind, header, scope)))
  parameters = tuple(part for part in operation.parameters if part[1] is not STREAM)
  body = envelope.child("Body", soap.SOAP_NAMESPACE)
  if body is None:
    raise AssertionError(f"{operation.name}: the recorded request has no body")
  without_stream = dataclasses.replace(operation, parameters=parameters)
  wire = without_stream.read_request(message.body, declared(scope, body))
  return values + [c.from_wire(kind, value) for (_, kind), value in zip(parameters, wire)]


def _message_ids(requests: Sequence[Element]) -> Deque[str]:
  """Return the MessageIDs of recorded requests, in order."""
  ids: Deque[str] = collections.deque()
  for envelope in requests:
    message_id = soap.from_envelope(envelope).message_id
    if message_id is None:
      raise AssertionError("a recorded request has no MessageID")
    ids.append(message_id)
  return ids


def _requests(connections: Sequence[Connection]) -> List[Element]:
  """Return the requests of recorded connections, in order."""
  return [envelope for connection in connections for envelope in envelopes(connection, "c")]


def _streamed_sockets(
  connections: Sequence[Connection],
) -> Tuple[Callable[[], Socket], List[ReplaySocket]]:
  """Return a socket factory that replays the connections one per call, and the sockets."""
  sockets = [ReplaySocket(connection) for connection in connections]
  waiting: Deque[Socket] = collections.deque(sockets)
  return waiting.popleft, sockets


def _client_bytes(connections: Sequence[Connection]) -> List[bytes]:
  """Return what the client sent on each recorded connection.

  A streamed call whose reply is a fault stops reading once it has the fault, so the rest of
  the reply stays unread; what the client sent must still be exactly what was recorded.
  """
  return [connection.sent_by("c") for connection in connections]


def _record_callbacks(client: ControllerClient, received: Received) -> None:
  """Set a handler for every callback that records its name and the text of its arguments."""

  def recorder(callback: Operation) -> Callable[..., None]:
    """Return the handler of one callback."""
    kinds = [kind for _, kind in callback.parameters]

    def record(*values: Any) -> None:
      """Record a callback."""
      received.append((callback.name, _texts(kinds, values)))

    return record

  client.on_test_connection(recorder(c.TEST_CONNECTION))
  client.on_cytometer_has_uint_fcs_data(recorder(c.CYTOMETER_ON_HAS_UINT_FCS_DATA))
  client.on_cytometer_has_float_fcs_data(recorder(c.CYTOMETER_ON_HAS_FLOAT_FCS_DATA))
  client.on_cytometer_long_running_operation_completed(
    recorder(c.ON_CYTOMETER_LONG_RUNNING_OPERATION_COMPLETED)
  )
  client.on_system_state_changed(recorder(c.ON_SYSTEM_STATE_CHANGED))
  client.on_device_state_changed(recorder(c.ON_DEVICE_STATE_CHANGED))
  client.on_contact_sensor_state_changed(recorder(c.ON_CONTACT_SENSOR_STATE_CHANGED))
  client.on_device_alert(recorder(c.ON_DEVICE_ALERT))
  client.on_htfc_calibrate_fluidics_completed(recorder(c.ON_HTFC_CALIBRATE_FLUIDICS_COMPLETED))
  client.on_command_started(recorder(c.ON_COMMAND_STARTED))
  client.on_command_completed(recorder(c.ON_COMMAND_COMPLETED))
  client.on_worklist_completed(recorder(c.ON_WORKLIST_COMPLETED))


async def _invoke(
  method: Callable[..., Any], client: Any, name: str, arguments: List[Any], call: Call
) -> Any:
  """Call a client method with recorded arguments; a download is read to its end."""
  if "upload" in call:
    data = recordings.pattern(call["upload"]["length"], call["upload"]["seed"])
    return await method(client, *arguments, data=data)
  if name in ITERATED:
    return b"".join([piece async for piece in method(client, *arguments)])
  return await method(client, *arguments)


def _check_fault(
  test: unittest.TestCase, error: Union[ControllerFault, WatchdogFault], call: Call
) -> None:
  """Check a raised fault against the recorded one."""
  where = f"{call['operation']} case {call['case']}"
  test.assertIn("fault", call, where)
  expected = call["fault"]
  if not expected.get("declared", True):
    test.assertIsNone(error.detail, where)
    return
  detail = c.CONTROLLER_SERVICE_EXCEPTION
  if isinstance(error, WatchdogFault):
    detail = c.WATCHDOG_EXCEPTION
  test.assertEqual(error.reason, expected["reason"], where)
  test.assertEqual(error.action, expected["action"], where)
  test.assertEqual(error.code[1], "Sender", where)
  text = recordings.render(detail, c.to_wire(detail, error.detail))
  test.assertEqual(text, expected["detail"], where)


async def _replay(
  test: unittest.TestCase,
  client: Union[ControllerClient, WatchdogClient],
  methods: Mapping[str, Callable[..., Any]],
  calls: Sequence[Call],
  requests: Sequence[Element],
  received: Received,
) -> None:
  """Make the recorded calls; check what each returns or raises and the callbacks before it."""
  test.assertEqual(len(calls), len(requests))
  for call, request in zip(calls, requests):
    name = call["operation"]
    operation = OPERATIONS[name]
    where = f"{name} case {call['case']}"
    arguments = _request_arguments(operation, request)
    test.assertEqual(_texts(_inputs(operation), arguments), call["arguments"], where)
    try:
      value = await _invoke(methods[name], client, name, arguments, call)
    except (ControllerFault, WatchdogFault) as error:
      _check_fault(test, error, call)
    else:
      test.assertNotIn("fault", call, where)
      if "download" in call:
        test.assertEqual(recordings.describe_bytes(value), call["download"], where)
      else:
        kinds = _outputs(operation)
        values = list(value) if operation.outputs else [value][: len(kinds)]
        test.assertEqual(_texts(kinds, values), call["result"], where)
    expected = [(item["name"], item["arguments"]) for item in call["callbacks"]]
    test.assertEqual(received, expected, where)
    received.clear()


class ControllerSessionTests(unittest.IsolatedAsyncioTestCase):
  """Every IController operation and callback, replayed on the controller's session."""

  async def _replay_session(self, record: bool) -> None:
    """Replay the recorded session, recording the callbacks or leaving them without handlers."""
    recording = recordings.load("controller_session")
    session = recording["connections"][0]
    calls = [call for call in recording["calls"] if call["connection"] == 0]
    requests = envelopes(session, "c")
    socket = ReplaySocket(session)
    client = ControllerClient(
      HOST, port=CONTROLLER_PORT, socket=socket, message_ids=_message_ids(requests).popleft
    )
    received: Received = []
    if record:
      _record_callbacks(client, received)
    await client.open()
    await _replay(self, client, CONTROLLER_METHODS, calls, requests, received)
    await client.close()
    self.assertTrue(socket.finished)

  async def test_every_operation_and_callback(self) -> None:
    """Every call sends the recorded bytes and decodes the reply and callbacks .NET sent."""
    await self._replay_session(record=True)

  async def test_callbacks_without_handler_are_logged(self) -> None:
    """A callback without a handler is dropped with a debug log line naming it."""
    logger = "pylabrobot.sartorius.ique.driver.client"
    with self.assertLogs(logger, logging.DEBUG) as logs:
      recording = recordings.load("controller_session")
      session = recording["connections"][0]
      calls = [call for call in recording["calls"] if call["connection"] == 0]
      requests = envelopes(session, "c")
      socket = ReplaySocket(session)
      client = ControllerClient(
        HOST, port=CONTROLLER_PORT, socket=socket, message_ids=_message_ids(requests).popleft
      )
      await client.open()
      for call, request in zip(calls, requests):
        arguments = _request_arguments(OPERATIONS[call["operation"]], request)
        try:
          await _invoke(CONTROLLER_METHODS[call["operation"]], client, "", arguments, call)
        except ControllerFault as error:
          _check_fault(self, error, call)
      await client.close()
    self.assertTrue(socket.finished)
    logged = {line.rsplit(" ", 1)[-1] for line in logs.output if "no handler" in line}
    self.assertEqual(logged, {callback.name for callback in c.CONTROLLER_CALLBACKS})

  async def test_undeclared_fault_ends_the_session(self) -> None:
    """An exception outside the fault contract raises a fault without detail, ends the session."""
    recording = recordings.load("controller_session")
    aborted = recording["connections"][1]
    socket = ReplaySocket(aborted)
    requests = envelopes(aborted, "c")
    client = ControllerClient(
      HOST, port=CONTROLLER_PORT, socket=socket, message_ids=_message_ids(requests).popleft
    )
    await client.open()
    with self.assertRaises(ControllerFault) as caught:
      await client.controller_get_summary()
    self.assertIsNone(caught.exception.detail)
    self.assertEqual(caught.exception.code[1], "Receiver")
    self.assertEqual([name for _, name in caught.exception.subcodes], ["InternalServiceFault"])
    self.assertFalse(client.is_open)
    await client.close()
    # The client answers the end of the session with its own End record; it does not read the
    # service's End record that follows the fault.
    self.assertEqual(bytes(socket.written), aborted.sent_by("c"))


class ControllerStreamingTests(unittest.IsolatedAsyncioTestCase):
  """Every IControllerStreaming operation, one recorded connection per call."""

  async def test_every_operation(self) -> None:
    """Uploads send the recorded bytes, downloads return the recorded streams, faults raise."""
    recording = recordings.load("controller_streaming")
    connections = recording["connections"]
    factory, sockets = _streamed_sockets(connections)
    requests = _requests(connections)
    client = ControllerClient(
      HOST,
      port=CONTROLLER_PORT,
      streaming_port=CONTROLLER_STREAMING_PORT,
      streaming_socket_factory=factory,
      message_ids=_message_ids(requests).popleft,
    )
    await _replay(self, client, STREAMING_METHODS, recording["calls"], requests, [])
    self.assertEqual([bytes(item.written) for item in sockets], _client_bytes(connections))


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
  """Every watchdog operation: the session, then one connection per streamed call."""

  async def test_every_operation(self) -> None:
    """Every call sends the recorded bytes and decodes the reply or fault .NET sent."""
    recording = recordings.load("watchdog")
    session, *streamed = recording["connections"]
    factory, sockets = _streamed_sockets(streamed)
    requests = _requests([session] + streamed)
    socket = ReplaySocket(session)
    client = WatchdogClient(
      HOST,
      port=WATCHDOG_PORT,
      streaming_port=WATCHDOG_STREAMING_PORT,
      socket=socket,
      streaming_socket_factory=factory,
      message_ids=_message_ids(requests).popleft,
    )
    methods = {**WATCHDOG_METHODS, **WATCHDOG_STREAMING_METHODS}
    session_calls = [call for call in recording["calls"] if call["connection"] == 0]
    streamed_calls = [call for call in recording["calls"] if call["connection"] != 0]
    await client.open()
    await _replay(self, client, methods, session_calls, envelopes(session, "c"), [])
    await client.close()
    await _replay(self, client, methods, streamed_calls, _requests(streamed), [])
    self.assertTrue(socket.finished)
    self.assertEqual([bytes(item.written) for item in sockets], _client_bytes(streamed))


class CoverageTests(unittest.TestCase):
  """The recordings and the method tables cover every operation and callback of the contract."""

  def test_method_tables_cover_every_operation(self) -> None:
    """Each table holds exactly the operations of its contract."""
    for table, operations in (
      (CONTROLLER_METHODS, c.CONTROLLER_OPERATIONS),
      (STREAMING_METHODS, c.CONTROLLER_STREAMING_OPERATIONS),
      (WATCHDOG_METHODS, c.WATCHDOG_OPERATIONS),
      (WATCHDOG_STREAMING_METHODS, c.WATCHDOG_STREAMING_OPERATIONS),
    ):
      self.assertEqual(set(table), {operation.name for operation in operations})

  def test_recordings_cover_every_operation_with_a_fault(self) -> None:
    """Every operation is recorded answering normally and with its declared fault."""
    calls = [
      call
      for name in ("controller_session", "controller_streaming", "watchdog")
      for call in recordings.load(name)["calls"]
    ]
    answered = {call["operation"] for call in calls if "fault" not in call}
    faulted = {call["operation"] for call in calls if call.get("fault", {}).get("declared", True)}
    faulted &= {call["operation"] for call in calls if "fault" in call}
    self.assertEqual(answered, set(OPERATIONS))
    self.assertEqual(faulted, set(OPERATIONS))

  def test_recordings_cover_every_callback(self) -> None:
    """Every callback is recorded; the event data ones with nil, empty and filled arrays."""
    calls = recordings.load("controller_session")["calls"]
    sent = [item for call in calls for item in call["callbacks"]]
    self.assertEqual({item["name"] for item in sent}, {cb.name for cb in c.CONTROLLER_CALLBACKS})
    floats = [
      item["arguments"][0] for item in sent if item["name"] == "Cytometer_OnHasFloatFCSData"
    ]
    self.assertIn("null", floats)
    self.assertIn("[]", floats)
    self.assertTrue(any(text.count(",") > 100 for text in floats))


if __name__ == "__main__":
  unittest.main()
