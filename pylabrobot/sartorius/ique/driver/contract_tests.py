"""Tests of the controller's wire contract: declarations, conversions and recorded .NET values."""

import array
import dataclasses
import datetime
import decimal
import math
import re
import unittest
import uuid
from typing import Any, Dict, List, Optional, Tuple, get_args

from pylabrobot.sartorius.ique.driver import contract as c
from pylabrobot.sartorius.ique.driver import recordings
from pylabrobot.sartorius.ique.driver.wcf import (
  BOOL,
  DATETIME,
  DECIMAL,
  FLOAT,
  GUID,
  INT,
  STRING,
  TIMESPAN,
  UINT,
  VERSION,
  ContractError,
  DataContract,
  DataObject,
  DateTime,
  DictionaryOf,
  EnumType,
  ListOf,
  Nullable,
  Operation,
  XmlType,
  soap,
)
from pylabrobot.sartorius.ique.driver.wcf.recordings import envelopes
from pylabrobot.sartorius.ique.driver.wcf.tree import Element, declared

ENUMS: Tuple[Tuple[object, EnumType, int], ...] = (
  (c.CommandCategory, c.COMMAND_CATEGORY, 8),
  (c.ContactToleranceType, c.CONTACT_TOLERANCE_TYPE, 4),
  (c.CytometerFirmwareType, c.CYTOMETER_FIRMWARE_TYPE, 2),
  (c.DeviceAlert, c.DEVICE_ALERT, 4),
  (c.DeviceKind, c.DEVICE_KIND, 6),
  (c.DeviceState, c.DEVICE_STATE, 4),
  (c.DeviceStatusType, c.DEVICE_STATUS_TYPE, 3),
  (c.EnabledLasers, c.ENABLED_LASERS, 8),
  (c.FlowCytometerSpeed, c.FLOW_CYTOMETER_SPEED, 3),
  (c.IQue3InstallationPrimeStep, c.IQUE3_INSTALLATION_PRIME_STEP, 6),
  (c.IQue3PurgeStep, c.IQUE3_PURGE_STEP, 4),
  (c.LaserConfiguration, c.LASER_CONFIGURATION, 3),
  (c.PumpDirection, c.PUMP_DIRECTION, 2),
  (c.RinseStationPump, c.RINSE_STATION_PUMP, 5),
  (c.SampleSpeed, c.SAMPLE_SPEED, 2),
  (c.SystemState, c.SYSTEM_STATE, 3),
  (c.SystemType, c.SYSTEM_TYPE, 10),
  (c.WorklistStatus, c.WORKLIST_STATUS, 3),
  (c.WorklistStopReason, c.WORKLIST_STOP_REASON, 5),
)

UTC = datetime.timezone.utc


def snake(name: str) -> str:
  """Return the snake_case form of a wire name, by the rule the contract module follows.

  An upper-case run followed by a capitalised word ends before that word, and a lower-case
  letter or digit followed by an upper-case letter starts a new word; IQue and UInt are words.
  """
  name = name.replace("IQue", "Ique").replace("UInt", "Uint")
  name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
  return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()


def dataclass_of(contract: DataContract) -> type:
  """Return the dataclass of a data contract."""
  return next(kind for kind, wire in c.DATA_TYPES if wire is contract)


def sample(kind: XmlType, index: int, depth: int) -> Any:
  """Return a Python value of a wire type that differs for every index.

  A data object at depth 0 or 1 gets a distinct value in every field; one deeper is nil.
  """
  if isinstance(kind, Nullable):
    return sample(kind.inner, index, depth)
  if kind is c.KEY_VALUE_PAIR_OF_STRING_STRING:
    return (f"key {index}", f"value {index}")
  if isinstance(kind, DataContract):
    if depth > 1:
      return None
    dataclass = dataclass_of(kind)
    pairs = enumerate(zip(dataclasses.fields(dataclass), kind.members()))
    return dataclass(
      **{field.name: sample(member, i, depth + 1) for i, (field, (_, member, _)) in pairs}
    )
  if isinstance(kind, DictionaryOf):
    return {sample(kind.key, index, depth + 1): sample(kind.value, index, depth + 1)}
  if isinstance(kind, ListOf):
    return [sample(kind.item, index, depth + 1), sample(kind.item, index + 1, depth + 1)]
  if isinstance(kind, EnumType):
    return kind.values[index % len(kind.values)]
  if kind is BOOL:
    return index % 2 == 0
  if kind is INT:
    return -1000 - index
  if kind is UINT:
    return 4_000_000_000 + index
  if kind is FLOAT:
    return index + 0.5
  if kind is DECIMAL:
    return decimal.Decimal(index) / 4
  if kind is STRING:
    return f"text {index}"
  if kind is DATETIME:
    return DateTime(2026, 1, 1 + index % 28, 12, 30, tzinfo=UTC)
  if kind is TIMESPAN:
    return datetime.timedelta(seconds=index, microseconds=5)
  if kind is GUID:
    return uuid.UUID(int=index + 1)
  if kind is VERSION:
    return c.Version(major=index, minor=2, build=-1, revision=-1)
  raise AssertionError(f"no sample value for {kind.name}")


def build(contract: DataContract) -> Any:
  """Return an instance of a contract's dataclass with a distinct value in every field."""
  return sample(contract, 0, 0)


def body_scope(envelope: Element) -> Dict[str, str]:
  """Return the namespace declarations in force inside an envelope's body."""
  body = envelope.child("Body", soap.SOAP_NAMESPACE)
  if body is None:
    raise AssertionError("an envelope without a body")
  return declared(declared({}, envelope), body)


class DeclarationTests(unittest.TestCase):
  """The declarations hold every type, enum and operation, with the contract's names."""

  def test_counts(self) -> None:
    """Five contracts: 73, 12, 8, 4 and 2 operations; 54 classes; 19 enums."""
    self.assertEqual(len(c.CONTROLLER_OPERATIONS), 73)
    self.assertEqual(len(c.CONTROLLER_CALLBACKS), 12)
    self.assertEqual(len(c.CONTROLLER_STREAMING_OPERATIONS), 8)
    self.assertEqual(len(c.WATCHDOG_OPERATIONS), 4)
    self.assertEqual(len(c.WATCHDOG_STREAMING_OPERATIONS), 2)
    self.assertEqual(len(c.DATA_TYPES), 54)
    self.assertEqual(len(ENUMS), 19)

  def test_enum_literals_are_the_wire_values(self) -> None:
    """Each Literal alias holds the value names of its enum, in order."""
    for alias, enum, count in ENUMS:
      self.assertEqual(get_args(alias), enum.values)
      self.assertEqual(len(enum.values), count, enum.name)
    self.assertEqual(c.IQUE3_INSTALLATION_PRIME_STEP.values[0], "None")
    self.assertIn("iQuePlusVYB", c.SYSTEM_TYPE.values)

  def test_field_names_are_snake_case_of_the_wire_names(self) -> None:
    """Every dataclass has one field per member, in wire order, named by the snake_case rule."""
    self.assertEqual(snake("IsClientAlreadyConnected"), "is_client_already_connected")
    self.assertEqual(snake("RPM"), "rpm")
    self.assertEqual(snake("ZHigh"), "z_high")
    self.assertEqual(snake("FCSFileName"), "fcs_file_name")
    for kind, contract in c.DATA_TYPES:
      fields = [field.name for field in dataclasses.fields(kind)]
      wire = [snake(name) for name, _, _ in contract.members()]
      self.assertEqual(fields, wire, contract.name)

  def test_member_order(self) -> None:
    """Members travel base first, each level in ordinal order of the names."""
    self.assertEqual([name for name, _, _ in c.DEVICE_ALERT_MESSAGE.members()], ["Alert", "Kind"])
    names = [name for name, _, _ in c.SAMPLE_WELL_CONTINUOUSLY_COMMAND.members()]
    self.assertEqual(names[:3], ["Category", "Completed", "CountdownDuration"])
    self.assertEqual(
      names[-7:], ["Column", "Row", "WellType", "PumpDirection", "PumpSpeed", "ZHigh", "ZLow"]
    )
    names = [name for name, _, _ in c.WORKLIST.members()]
    self.assertLess(names.index("FCSFileName"), names.index("FluidicsRunTimeMinutes"))
    names = [name for name, _, _ in c.COMBINED_SHAKE_RINSE_COMMAND.members()]
    self.assertLess(names.index("RPM"), names.index("RinseStationPump"))
    names = [name for name, _, _ in c.TIMED_SHAKE_COMMAND.members()]
    self.assertLess(names.index("RinseZUp"), names.index("Rpm"))

  def test_every_command_class_is_a_known_type_of_worklist_command(self) -> None:
    """The 38 subclasses of WorklistCommand all derive from it."""
    commands = [kind for kind, _ in c.DATA_TYPES if issubclass(kind, c.WorklistCommand)]
    self.assertEqual(len(commands), 39)
    for kind, contract in c.DATA_TYPES:
      if issubclass(kind, c.WorklistCommand):
        self.assertIn(c.WORKLIST_COMMAND, contract.lineage())

  def test_actions(self) -> None:
    """Actions follow namespace, contract and operation; callbacks use the parent contract."""
    self.assertEqual(
      c.CONTROLLER_CONNECT.action, "http://tempuri.org/IController/Controller_Connect"
    )
    self.assertEqual(
      c.ON_SYSTEM_STATE_CHANGED.action, "http://tempuri.org/IController/OnSystemStateChanged"
    )
    self.assertEqual(c.WATCHDOG_GET_STATUS.action, "WatchdogService/IWatchdogService/GetStatus")
    self.assertEqual(c.WATCHDOG_GET_STATUS.namespace, "WatchdogService")
    upload = c.WATCHDOG_UPLOAD_INSTALL_PACKAGE.action
    self.assertEqual(
      upload, "WatchdogStreamingService/IWatchdogStreamingService/UploadInstallPackage"
    )
    fault = c.CONTROLLER.fault_action(c.PUMP_STOP, c.CONTROLLER_SERVICE_EXCEPTION)
    self.assertEqual(
      fault, "http://tempuri.org/IController/Pump_StopControllerServiceExceptionFault"
    )
    self.assertTrue(all(callback.one_way for callback in c.CONTROLLER_CALLBACKS))

  def test_message_contract_operations(self) -> None:
    """The uploads with a message contract carry their header and wrapper names."""
    self.assertEqual(c.CYTOMETER_UPLOAD_FIRMWARE.wrapper, "CytometerFileUpload")
    self.assertEqual([name for name, _ in c.CYTOMETER_UPLOAD_FIRMWARE.headers], ["FileName"])
    self.assertEqual([name for name, _ in c.CYTOMETER_UPLOAD_FIRMWARE.parameters], ["FileStream"])
    self.assertEqual(c.WATCHDOG_UPLOAD_INSTALL_PACKAGE.wrapper, "FileUpload")
    self.assertEqual([name for name, _ in c.WATCHDOG_UPLOAD_INSTALL_PACKAGE.headers], ["crc32"])

  def test_dictionary_item_name(self) -> None:
    """The item name of the device configuration dictionary carries its namespace hash."""
    parameter = c.CONTROLLER_CONFIGURE_DEVICES.parameters[0][1]
    self.assertIsInstance(parameter, DictionaryOf)
    if isinstance(parameter, DictionaryOf):
      self.assertEqual(parameter.entry, "KeyValueOfDeviceKindDeviceConfigurationB_SIY0oB6")


class ConversionTests(unittest.TestCase):
  """to_wire and from_wire map every field to its member and back, and refuse what is unknown."""

  def test_every_dataclass_round_trips(self) -> None:
    """Each field lands in its own member, and converting back gives the same instance."""
    for _, contract in c.DATA_TYPES:
      instance = build(contract)
      wire = c.to_wire(contract, instance)
      self.assertIs(wire.contract, contract)
      for index, (name, member, _) in enumerate(contract.members()):
        self.assertEqual(c.from_wire(member, wire.values[name]), sample(member, index, 1), name)
      self.assertEqual(c.from_wire(contract, wire), instance, contract.name)

  def test_subclass_in_place_of_its_base(self) -> None:
    """A command in a list of WorklistCommand keeps its own class both ways."""
    command = build(c.SAMPLE_WELL_CONTINUOUSLY_COMMAND)
    items = c.to_wire(ListOf(c.WORKLIST_COMMAND), [command, None])
    self.assertIs(items[0].contract, c.SAMPLE_WELL_CONTINUOUSLY_COMMAND)
    self.assertIsNone(items[1])
    self.assertEqual(c.from_wire(ListOf(c.WORKLIST_COMMAND), items), [command, None])

  def test_unknown_class_is_refused(self) -> None:
    """A subclass the contract does not declare, or a class outside the lineage, raises."""

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class OwnCommand(c.StopPumpCommand):
      """A command the controller does not know."""

    own = OwnCommand(**dataclasses.asdict(build(c.STOP_PUMP_COMMAND)))
    with self.assertRaises(TypeError):
      c.to_wire(c.WORKLIST_COMMAND, own)
    with self.assertRaises(TypeError):
      c.to_wire(c.WORKLIST_COMMAND, build(c.DEVICE_MESSAGE))

  def test_unknown_or_missing_members_are_refused(self) -> None:
    """A data object with a member its class lacks, or without one it declares, raises."""
    wire = c.to_wire(c.DEVICE_MESSAGE, build(c.DEVICE_MESSAGE))
    extra = DataObject(c.DEVICE_MESSAGE, dict(wire.values), [Element("Severity", "", "")])
    with self.assertRaises(ContractError):
      c.from_wire(c.DEVICE_MESSAGE, extra)
    missing = DataObject(c.DEVICE_MESSAGE, {"Detail": None, "Message": "m"})
    with self.assertRaises(ContractError):
      c.from_wire(c.DEVICE_MESSAGE, missing)
    other = DataContract("DeviceMessage", "http://example.org/", [])
    with self.assertRaises(ContractError):
      c.from_wire(other, DataObject(other, {}))

  def test_pairs_and_versions(self) -> None:
    """A key and value pair is a tuple; a version a Version whose absent parts are -1."""
    pair = c.to_wire(c.KEY_VALUE_PAIR_OF_STRING_STRING, ("key", None))
    self.assertEqual(pair.values, {"key": "key", "value": None})
    self.assertEqual(c.from_wire(c.KEY_VALUE_PAIR_OF_STRING_STRING, pair), ("key", None))
    with self.assertRaises(TypeError):
      c.to_wire(c.KEY_VALUE_PAIR_OF_STRING_STRING, ["key", "value"])
    self.assertFalse(c.KEY_VALUE_PAIR_OF_STRING_STRING.nullable)
    version = c.Version(major=7, minor=2, build=6010, revision=25563)
    self.assertEqual(c.to_wire(VERSION, version), (7, 2, 6010, 25563))
    self.assertEqual(
      c.from_wire(VERSION, (7, 2)), c.Version(major=7, minor=2, build=-1, revision=-1)
    )
    self.assertEqual(str(version), "7.2.6010.25563")
    self.assertEqual(str(c.Version(major=1, minor=0, build=-1, revision=-1)), "1.0")


class RecordedValuesTests(unittest.TestCase):
  """Values .NET wrote, decoded with the declarations alone."""

  calls: List[Dict[str, Any]]
  requests: List[Element]
  replies: Dict[str, Element]
  callbacks: List[Tuple[str, Element]]

  @classmethod
  def setUpClass(cls) -> None:
    """Index the recorded controller session by call."""
    recording = recordings.load("controller_session")
    session = recording["connections"][0]
    cls.calls = [call for call in recording["calls"] if call["connection"] == 0]
    cls.requests = envelopes(session, "c")
    cls.replies = {}
    cls.callbacks = []
    for envelope in envelopes(session, "s"):
      message = soap.from_envelope(envelope)
      if message.relates_to is None:
        cls.callbacks.append((message.action, envelope))
      else:
        cls.replies[message.relates_to] = envelope

  def reply(self, operation: Operation, case: int) -> Any:
    """Return what an operation returned in one of its recorded cases."""
    index = next(
      i
      for i, call in enumerate(self.calls)
      if (call["operation"], call["case"]) == (operation.name, case)
    )
    message_id = soap.from_envelope(self.requests[index]).message_id
    envelope = self.replies[message_id or ""]
    value = operation.read_reply(soap.from_envelope(envelope).body, body_scope(envelope))
    if operation.outputs:
      kinds = ([operation.result] if operation.result else []) + [
        kind for _, kind in operation.outputs
      ]
      return tuple(c.from_wire(kind, item) for kind, item in zip(kinds, value))
    return c.from_wire(operation.result, value) if operation.result else None

  def request(self, operation: Operation, case: int) -> List[Any]:
    """Return the arguments the .NET client sent in one of the recorded cases."""
    index = next(
      i
      for i, call in enumerate(self.calls)
      if (call["operation"], call["case"]) == (operation.name, case)
    )
    envelope = self.requests[index]
    values = operation.read_request(soap.from_envelope(envelope).body, body_scope(envelope))
    return [c.from_wire(kind, value) for (_, kind), value in zip(operation.parameters, values)]

  def callback_arguments(self, callback: Operation) -> List[List[Any]]:
    """Return the arguments of every recorded callback of one kind."""
    found = []
    for action, envelope in self.callbacks:
      if action == callback.action:
        values = callback.read_request(soap.from_envelope(envelope).body, body_scope(envelope))
        found.append(
          [c.from_wire(kind, value) for (_, kind), value in zip(callback.parameters, values)]
        )
    return found

  def test_connection_info(self) -> None:
    """ConnectionInfo with its System.Version and a 120-character comment."""
    info = self.reply(c.CONTROLLER_CONNECT, 0)
    version = c.Version(major=7, minor=2, build=6010, revision=25563)
    self.assertEqual(
      info, c.ConnectionInfo(comments="x" * 120, is_client_already_connected=True, version=version)
    )
    self.assertIsNone(self.reply(c.CONTROLLER_CONNECT, 1))

  def test_date_time_kinds(self) -> None:
    """A local time travels as its UTC instant marked local; the kinds survive both ways."""
    (local,) = self.request(c.CONTROLLER_CONNECT, 1)
    self.assertEqual(local.kind, "local")
    self.assertEqual(local, datetime.datetime(2026, 11, 10, 7, 0, 0, tzinfo=UTC))
    (utc,) = self.request(c.CONTROLLER_CONNECT, 0)
    self.assertEqual((utc.kind, utc.tick), ("utc", 7))
    (unspecified,) = self.request(c.CONTROLLER_CONNECT, 2)
    self.assertEqual(unspecified.kind, "unspecified")
    self.assertIsNone(unspecified.tzinfo)

  def test_out_parameters(self) -> None:
    """Operations with out parameters return them in order after the result."""
    peaks = self.reply(c.HTFC_GET_TOP_PEAK_VALUES, 0)
    self.assertEqual(peaks, (0, 1, 2147483648, 4294967295, 3000000000, 7))
    step, completed = self.reply(c.IQUE3_GET_CURRENT_PURGE_STEP, 0)
    self.assertIn(step, c.IQUE3_PURGE_STEP.values)
    self.assertIsInstance(completed, bool)

  def test_worklist_with_every_command(self) -> None:
    """A worklist holding one command of every class, a plain WorklistCommand and nil."""
    worklist = self.reply(c.WORKLIST_GET_CURRENT, 0)
    self.assertIsInstance(worklist, c.Worklist)
    commands = worklist.commands
    classes = {kind for kind, _ in c.DATA_TYPES if issubclass(kind, c.WorklistCommand)}
    self.assertEqual({type(command) for command in commands if command is not None}, classes)
    self.assertIsNone(commands[-1])
    pumped = next(item for item in commands if isinstance(item, c.SampleWellContinuouslyCommand))
    self.assertIsInstance(pumped.z_high, decimal.Decimal)
    self.assertIn(pumped.pump_direction, c.PUMP_DIRECTION.values)
    cycling = next(item for item in commands if type(item) is c.CyclingRinseCommand)
    self.assertIsInstance(cycling.rinse_duration, int)
    fluidics = next(
      item for item in commands if isinstance(item, c.StartFlowCytometerFluidicsCommand)
    )
    for pair in fluidics.fcs_metadata or []:
      self.assertIsInstance(pair, tuple)

  def test_event_data(self) -> None:
    """Event data arrives as array.array, nil or empty; floats keep NaN and infinities."""
    floats = [
      arguments[0] for arguments in self.callback_arguments(c.CYTOMETER_ON_HAS_FLOAT_FCS_DATA)
    ]
    self.assertIn(None, floats)
    filled = [item for item in floats if item]
    self.assertTrue(all(isinstance(item, array.array) and item.typecode == "f" for item in filled))
    longest = max(filled, key=len)
    self.assertEqual((len(longest), longest[0], longest[-1]), (300, -10.0, 64.75))
    self.assertTrue(any(math.isnan(value) for item in filled for value in item))
    self.assertTrue(any(math.isinf(value) for item in filled for value in item))
    uints = self.callback_arguments(c.CYTOMETER_ON_HAS_UINT_FCS_DATA)
    self.assertEqual(
      [list(data) if data is not None else None for data, _ in uints], [[7, 65536, 0], None, []]
    )
    self.assertEqual([index for _, index in uints], [1, 4294967295, 7])

  def test_device_alert_plain_type(self) -> None:
    """DeviceAlertMessage, a class without a data contract attribute, carries Alert and Kind."""
    alerts = [arguments[0] for arguments in self.callback_arguments(c.ON_DEVICE_ALERT)]
    present = [alert for alert in alerts if alert is not None]
    self.assertTrue(present)
    self.assertEqual({alert.alert for alert in present}, set(c.DEVICE_ALERT.values))
    self.assertIn(None, alerts)

  def test_movement_range(self) -> None:
    """A movement range keeps the scale of its decimals."""
    reply: Optional[c.MovementRange] = self.reply(c.SAMPLER_GET_X_RANGE, 0)
    self.assertIsInstance(reply, c.MovementRange)
    if reply is not None:
      self.assertIsInstance(reply.min, decimal.Decimal)


if __name__ == "__main__":
  unittest.main()
