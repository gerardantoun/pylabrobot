"""The service contract the recorded exchanges in this folder were made with.

It declares a slice of a cytometer controller's contract: enough operations and types to
exercise every shape of message the protocol stack handles. ``DUPLEX_CALLS``,
``WORKLIST_CALLS`` and ``ABORT_CALLS`` list the calls the recorded client made.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from typing import Any, List, Tuple

from pylabrobot.sartorius.ique.driver.wcf.contract import STREAM, Operation, ServiceContract
from pylabrobot.sartorius.ique.driver.wcf.datacontract import (
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
  ArrayOf,
  DataContract,
  DataObject,
  DictionaryOf,
  EnumType,
  ListOf,
  Nullable,
  clr_namespace,
)
from pylabrobot.sartorius.ique.driver.wcf.values import DateTime, TimeSpan

NS = clr_namespace("IntelliCyt.Controller")

DEVICE_KIND = EnumType(
  "DeviceKind", NS, ["Sampler", "Pump", "Shaker", "Cytometer", "PlateRail", "ContactSensor"]
)
DEVICE_STATE = EnumType("DeviceState", NS, ["Disconnected", "Ready", "Running", "Faulted"])
DEVICE_STATUS_TYPE = EnumType("DeviceStatusType", NS, ["Info", "Warning", "Error"])
SYSTEM_STATE = EnumType("SystemState", NS, ["Red", "Yellow", "Green"])
SYSTEM_TYPE = EnumType(
  "SystemType",
  NS,
  [
    "Unknown",
    "HyperCyt",
    "HTFC",
    "Matryoshka",
    "iQue",
    "iQueHD",
    "iQue3",
    "iQue3HD",
    "iQuePlusVYB",
    "iQuePlusBR",
  ],
)
PUMP_DIRECTION = EnumType("PumpDirection", NS, ["CW", "CCW"])
COMMAND_CATEGORY = EnumType(
  "CommandCategory",
  NS,
  [
    "Sample",
    "Rinse",
    "Shake",
    "Pump",
    "FCAutomate",
    "FlowCytometer",
    "Info",
    "CirculatingRinseStation",
  ],
)
WORKLIST_STOP_REASON = EnumType(
  "WorklistStopReason",
  NS,
  ["Unspecified", "Cancelled", "WorklistError", "DeviceFaulted", "EventLimit"],
)
WORKLIST_STATUS = EnumType(
  "WorklistStatus", NS, ["NoActiveWorklist", "RunningWorklist", "CompletedUnfinalizedWorklist"]
)
RINSE_STATION_PUMP = EnumType("RinseStationPump", NS, ["S1", "S2", "S3", "S4", "MB"])
CONTACT_TOLERANCE_TYPE = EnumType(
  "ContactToleranceType",
  NS,
  ["NewProbeAndTubing", "DeckHeightValidation", "PlateMapping", "MicrofugeTubeValidation"],
)
PURGE_STEP = EnumType("IQue3PurgeStep", NS, ["None", "FlushSystem", "PurgeSystem", "PurgeSensor"])
FIRMWARE_TYPE = EnumType("CytometerFirmwareType", NS, ["System", "WeightStation"])
DEVICE_ALERT = EnumType(
  "DeviceAlert",
  NS,
  ["None", "CYT_KillCytometerPower", "CYT_InstallationPriming", "CYT_PurgingFluidics"],
)

CONNECTION_INFO = DataContract(
  "ConnectionInfo",
  NS,
  [("Version", VERSION), ("IsClientAlreadyConnected", BOOL), ("Comments", STRING)],
)
DEVICE_MESSAGE = DataContract(
  "DeviceMessage",
  NS,
  [("MessageLevel", DEVICE_STATUS_TYPE), ("Message", STRING), ("Detail", STRING)],
)
DEVICE_INFO = DataContract(
  "DeviceInfo",
  NS,
  [
    ("Kind", DEVICE_KIND),
    ("Model", STRING),
    ("State", DEVICE_STATE),
    ("Messages", ListOf(DEVICE_MESSAGE)),
  ],
)
DEVICE_CONFIGURATION = DataContract(
  "DeviceConfiguration", NS, [("IncludeThisDevice", BOOL), ("Configuration", STRING)]
)
DEVICE_RESET_OPTIONS = DataContract(
  "DeviceResetOptions",
  NS,
  [
    ("PlateRailRC", INT),
    ("IsSORPC6", BOOL),
    ("OverrideLLDPercentage", FLOAT),
    ("OverrideZArmPercentage", INT),
    ("OverrideZArmAdditionalContactDepth", FLOAT),
    ("IsInDevMode", BOOL),
  ],
)
MOVEMENT_RANGE = DataContract("MovementRange", NS, [("Min", DECIMAL), ("Max", DECIMAL)])
CONTACT_Z_RESULT = DataContract(
  "ContactZResult",
  NS,
  [
    ("ID", GUID),
    ("ContactTime", DATETIME),
    ("FoundContact", BOOL),
    ("ContactZLocation", DECIMAL),
    ("ZLow", DECIMAL),
    ("HasError", BOOL),
    ("ErrorMessage", STRING),
    ("IsSimulated", BOOL),
  ],
)
COMPLETED_WORKLIST = DataContract(
  "CompletedWorklist",
  NS,
  [
    ("ID", GUID),
    ("ExperimentID", Nullable(GUID)),
    ("ExperimentName", STRING),
    ("PlateID", Nullable(GUID)),
    ("PlateNumber", STRING),
    ("RunStart", DATETIME),
    ("RunComplete", DATETIME),
    ("ClientTimeOffset", TIMESPAN),
    ("HasFCSFile", BOOL),
  ],
)
WORKLIST_COMMAND = DataContract(
  "WorklistCommand",
  NS,
  [
    ("ID", GUID),
    ("Name", STRING),
    ("Description", STRING),
    ("Category", COMMAND_CATEGORY),
    ("Completed", BOOL),
    ("StartTime", DATETIME),
    ("EndTime", DATETIME),
    ("EstimatedDuration", TIMESPAN),
    ("CountdownDuration", INT),
    ("ExecutionNotes", STRING),
  ],
)
START_SHAKER_COMMAND = DataContract("StartShakerCommand", NS, [("RPM", INT)], WORKLIST_COMMAND)
STOP_PUMP_COMMAND = DataContract("StopPumpCommand", NS, [], WORKLIST_COMMAND)
SAMPLE_COMMAND = DataContract(
  "SampleCommand",
  NS,
  [
    ("X", DECIMAL),
    ("Y", DECIMAL),
    ("FastMove", BOOL),
    ("ZUp", DECIMAL),
    ("ZDown", DECIMAL),
    ("SipTime", INT),
    ("UpTime", INT),
  ],
  WORKLIST_COMMAND,
)
SAMPLE_WELL_COMMAND = DataContract(
  "SampleWellCommand",
  NS,
  [("WellType", STRING), ("Row", STRING), ("Column", INT)],
  SAMPLE_COMMAND,
)
RINSE_COMMAND = DataContract(
  "RinseCommand",
  NS,
  [
    ("RinseStationName", STRING),
    ("Operation", STRING),
    ("RinseStationPump", RINSE_STATION_PUMP),
  ],
  SAMPLE_COMMAND,
)
START_PUMP_COMMAND = DataContract(
  "StartPumpCommand", NS, [("Speed", INT), ("Direction", PUMP_DIRECTION)], WORKLIST_COMMAND
)
TIMED_SHAKE_COMMAND = DataContract(
  "TimedShakeCommand",
  NS,
  [
    ("ShakeDuration", INT),
    ("Rpm", INT),
    ("RinseX", DECIMAL),
    ("RinseY", DECIMAL),
    ("FastMove", BOOL),
    ("RinseZUp", DECIMAL),
    ("RinseZDown", DECIMAL),
    ("RinseStationPump", RINSE_STATION_PUMP),
  ],
  WORKLIST_COMMAND,
)
WORKLIST = DataContract(
  "Worklist",
  NS,
  [
    ("ID", GUID),
    ("Name", STRING),
    ("DeviceID", STRING),
    ("ExperimentName", STRING),
    ("PlateNumber", STRING),
    ("ExperimentID", Nullable(GUID)),
    ("PlateID", Nullable(GUID)),
    ("AllCommandsCompleted", BOOL),
    ("Error", STRING),
    ("StopReason", WORKLIST_STOP_REASON),
    ("StartTime", DATETIME),
    ("EndTime", DATETIME),
    ("ClientTimeOffset", TIMESPAN),
    ("Commands", ListOf(WORKLIST_COMMAND)),
    ("LeaveFluidicsRunningOnComplete", BOOL),
    ("FCSFileName", STRING),
    ("FCSDataWritten", UINT),
    ("FCSFileCRC", UINT),
    ("DevicesToMonitor", ListOf(DEVICE_KIND)),
    ("ExecutionNotes", STRING),
    ("FluidicsRunTimeMinutes", DECIMAL),
    ("PumpRunTimeMinutes", DECIMAL),
  ],
)
CONTROLLER_SERVICE_EXCEPTION = DataContract(
  "ControllerServiceException", NS, [("Message", STRING), ("Type", STRING)]
)
CONTROLLER_SERVICE_EXCEPTION.member("InnerException", CONTROLLER_SERVICE_EXCEPTION)
DEVICE_ALERT_MESSAGE = DataContract(
  "DeviceAlertMessage", NS, [("Kind", DEVICE_KIND), ("Alert", DEVICE_ALERT)]
)

CONTROLLER = ServiceContract("IController")
CONNECT = CONTROLLER.operation(
  "Controller_Connect", [("clientTime", DATETIME)], result=CONNECTION_INFO
)
SET_VERBOSE_LOGGING = CONTROLLER.operation(
  "Controller_SetVerboseLogging", [("verboseLogging", BOOL)]
)
IS_1536_CAPABLE = CONTROLLER.operation("Controller_Is1536Capable", result=BOOL)
GET_OPERATION_DURATION = CONTROLLER.operation("Cytometer_GetOperationDuration", result=TIMESPAN)
UPDATE_FIRMWARE = CONTROLLER.operation("Cytometer_UpdateFirmware", [("type", FIRMWARE_TYPE)])
GET_SYSTEM_ID = CONTROLLER.operation(
  "Controller_GetSystemID", [("forceRefresh", BOOL)], result=STRING
)
GET_SYSTEM_TYPE = CONTROLLER.operation("Controller_GetSystemType", result=SYSTEM_TYPE)
GET_SYSTEM_STATE = CONTROLLER.operation("Controller_GetSystemState", result=SYSTEM_STATE)
CONFIGURE_DEVICES = CONTROLLER.operation(
  "Controller_ConfigureDevices",
  [("configuration", DictionaryOf(DEVICE_KIND, DEVICE_CONFIGURATION))],
)
RESET_DEVICES = CONTROLLER.operation(
  "Controller_ResetDevices", [("options", DEVICE_RESET_OPTIONS)], result=BOOL
)
INITIALIZE_DEVICES = CONTROLLER.operation(
  "Controller_InitializeDevices", [("deviceInitializationList", ListOf(DEVICE_KIND))]
)
GET_DEVICE_STATUSES = CONTROLLER.operation(
  "Controller_GetDeviceStatuses", result=ListOf(DEVICE_INFO)
)
GET_DEVICE_SUMMARY = CONTROLLER.operation(
  "Controller_GetDeviceSummary", [("deviceKind", DEVICE_KIND)], result=STRING
)
GET_SUMMARY = CONTROLLER.operation("Controller_GetSummary", result=STRING)
_PEAKS = [(name, UINT) for name in ("fsc", "ssc", "fl1", "fl2", "fl3", "fl4")]
SET_TOP_PEAK_VALUES = CONTROLLER.operation("HTFC_SetTopPeakValues", _PEAKS)
GET_TOP_PEAK_VALUES = CONTROLLER.operation("HTFC_GetTopPeakValues", outputs=_PEAKS)
GET_CURRENT_PURGE_STEP = CONTROLLER.operation(
  "IQue3_GetCurrentPurgeStep", result=PURGE_STEP, outputs=[("isCompleted", BOOL)]
)
PUMP_GET_MAX_SPEED = CONTROLLER.operation("Pump_GetMaxSpeed", result=INT)
PUMP_START = CONTROLLER.operation("Pump_Start", [("speed", INT), ("direction", PUMP_DIRECTION)])
SAMPLER_HOME = CONTROLLER.operation("Sampler_Home")
SAMPLER_GET_X = CONTROLLER.operation("Sampler_GetX", result=DECIMAL)
SAMPLER_GET_X_RANGE = CONTROLLER.operation("Sampler_GetXRange", result=MOVEMENT_RANGE)
SAMPLER_GET_Z_CONTACT_TOLERANCE = CONTROLLER.operation(
  "Sampler_GetZContactTolerance", [("activity", CONTACT_TOLERANCE_TYPE)], result=DECIMAL
)
SAMPLER_MOVE_XY = CONTROLLER.operation(
  "Sampler_MoveXY", [("x", DECIMAL), ("y", DECIMAL), ("fast", BOOL)]
)
SAMPLER_FIND_Z_CONTACT = CONTROLLER.operation(
  "Sampler_FindZContact",
  [("zHigh", DECIMAL), ("zLow", DECIMAL), ("retractAfterFind", BOOL)],
  result=CONTACT_Z_RESULT,
)
SHAKER_HOME = CONTROLLER.operation("Shaker_Home")
WORKLIST_RUN = CONTROLLER.operation("Worklist_Run", [("worklist", WORKLIST)])
WORKLIST_SKIP_TO = CONTROLLER.operation("Worklist_SkipTo", [("id", GUID)])
WORKLIST_GET_CURRENT = CONTROLLER.operation("Worklist_GetCurrent", result=WORKLIST)
WORKLIST_GET_STATUS = CONTROLLER.operation("Worklist_GetStatus", result=WORKLIST_STATUS)
WORKLIST_GET_COMPLETED_WORKLISTS = CONTROLLER.operation(
  "Worklist_GetCompletedWorklists", result=ListOf(COMPLETED_WORKLIST)
)
WORKLIST_GET_COMPLETED_WORKLIST = CONTROLLER.operation(
  "Worklist_GetCompletedWorklist", [("ID", GUID)], result=COMPLETED_WORKLIST
)

TEST_CONNECTION = CONTROLLER.callback("TestConnection")
ON_UINT_FCS_DATA = CONTROLLER.callback(
  "Cytometer_OnHasUIntFCSData", [("fcsData", ArrayOf(UINT)), ("dataIndex", UINT)]
)
ON_FLOAT_FCS_DATA = CONTROLLER.callback(
  "Cytometer_OnHasFloatFCSData", [("fcsData", ArrayOf(FLOAT)), ("dataIndex", UINT)]
)
ON_LONG_RUNNING_OPERATION_COMPLETED = CONTROLLER.callback(
  "OnCytometerLongRunningOperationCompleted", [("error", STRING)]
)
ON_SYSTEM_STATE_CHANGED = CONTROLLER.callback("OnSystemStateChanged", [("state", SYSTEM_STATE)])
ON_DEVICE_STATE_CHANGED = CONTROLLER.callback("OnDeviceStateChanged", [("deviceInfo", DEVICE_INFO)])
ON_CONTACT_SENSOR_STATE_CHANGED = CONTROLLER.callback(
  "OnContactSensorStateChanged", [("hasContact", BOOL)]
)
ON_DEVICE_ALERT = CONTROLLER.callback("OnDeviceAlert", [("alert", DEVICE_ALERT_MESSAGE)])
ON_COMMAND_STARTED = CONTROLLER.callback("OnCommandStarted", [("command", WORKLIST_COMMAND)])
ON_COMMAND_COMPLETED = CONTROLLER.callback("OnCommandCompleted", [("command", WORKLIST_COMMAND)])
ON_WORKLIST_COMPLETED = CONTROLLER.callback("OnWorklistCompleted", [("worklist", WORKLIST)])
CALLBACKS = (
  TEST_CONNECTION,
  ON_UINT_FCS_DATA,
  ON_FLOAT_FCS_DATA,
  ON_LONG_RUNNING_OPERATION_COMPLETED,
  ON_SYSTEM_STATE_CHANGED,
  ON_DEVICE_STATE_CHANGED,
  ON_CONTACT_SENSOR_STATE_CHANGED,
  ON_DEVICE_ALERT,
  ON_COMMAND_STARTED,
  ON_COMMAND_COMPLETED,
  ON_WORKLIST_COMPLETED,
)

STREAMING = ServiceContract("IControllerStreaming")
GET_LOG_ARCHIVE = STREAMING.operation("Controller_GetLogArchive", result=STREAM)
UPLOAD_FCS_DEMO_FILE = STREAMING.operation("Controller_UploadFcsDemoFile", [("source", STREAM)])
UPLOAD_FIRMWARE = STREAMING.operation(
  "Cytometer_UploadFirmware",
  [("FileStream", STREAM)],
  wrapper="CytometerFileUpload",
  headers=[("FileName", STRING)],
)
DOWNLOAD_CALIBRATION_SETTINGS = STREAMING.operation(
  "Controller_DownloadCalibrationSettings", result=STREAM
)
UPLOAD_CALIBRATION_SETTINGS = STREAMING.operation(
  "Controller_UploadCalibrationSettings", [("source", STREAM)]
)
CLEAR_CALIBRATION_SETTINGS = STREAMING.operation(
  "Controller_ClearCalibrationSettings", [("clearReason", STRING)]
)
GET_CURRENT_FCS_FILE = STREAMING.operation("Worklist_GetCurrentFCSFile", result=STREAM)
GET_COMPLETED_WORKLIST_FCS_FILE = STREAMING.operation(
  "Worklist_GetCompletedWorklistFCSFile", [("ID", GUID)], result=STREAM
)

_MIN_DATE = DateTime(1, 1, 1)
_ZERO = datetime.timedelta(0)


def command_id(number: int) -> uuid.UUID:
  """Return the ID of one of the sample worklist's objects."""
  return uuid.UUID(f"c0ffee00-0000-4000-8000-{number:012x}")


def _command(
  contract: DataContract, number: int, name: str, category: str, **own: object
) -> DataObject:
  """Build a worklist command with default values for the members not given."""
  values = {
    "ID": command_id(number),
    "Name": name,
    "Description": None,
    "Category": category,
    "Completed": False,
    "StartTime": _MIN_DATE,
    "EndTime": _MIN_DATE,
    "EstimatedDuration": _ZERO,
    "CountdownDuration": 0,
    "ExecutionNotes": None,
  }
  values.update(own)
  return contract.new(**values)


def sample_worklist() -> DataObject:
  """Build the worklist the recorded sessions run: one command of each kind."""
  d = decimal.Decimal
  sample = {"X": d(0), "Y": d(0), "FastMove": False, "ZUp": d(0), "ZDown": d(0)}
  sample.update(SipTime=0, UpTime=0)
  commands = [
    _command(
      START_SHAKER_COMMAND,
      0x10,
      "Start shaker",
      "Shake",
      Description="Shake at 2000 rpm",
      EstimatedDuration=datetime.timedelta(seconds=2),
      CountdownDuration=2,
      RPM=2000,
    ),
    _command(
      SAMPLE_WELL_COMMAND,
      0x11,
      "Sample A1",
      "Sample",
      StartTime=DateTime(2026, 11, 11, 10, 0, 1),
      EstimatedDuration=TimeSpan.from_ticks(15000001),
      X=d("10.25"),
      Y=d("20.5"),
      FastMove=True,
      ZUp=d("30"),
      ZDown=d("1.125"),
      SipTime=1000,
      UpTime=500,
      WellType="Sample",
      Row="A",
      Column=1,
    ),
    _command(
      RINSE_COMMAND,
      0x12,
      "Rinse",
      "Rinse",
      **{**sample, "X": d(-1)},
      RinseStationName="Station 2",
      Operation=None,
      RinseStationPump="MB",
    ),
    _command(START_PUMP_COMMAND, 0x13, "Start pump", "Pump", Speed=29, Direction="CCW"),
    _command(STOP_PUMP_COMMAND, 0x14, "Stop pump", "Pump"),
    _command(
      TIMED_SHAKE_COMMAND,
      0x15,
      "Timed shake",
      "Shake",
      ShakeDuration=15,
      Rpm=1800,
      RinseX=d("100.5"),
      RinseY=d("50.25"),
      FastMove=False,
      RinseZUp=d("40"),
      RinseZDown=d("2"),
      RinseStationPump="S3",
    ),
    _command(WORKLIST_COMMAND, 0x16, "Info", "Info", ExecutionNotes="done"),
    None,
  ]
  return WORKLIST.new(
    ID=command_id(1),
    Name="Plate 1",
    DeviceID="IQ3-MOCK-0001",
    ExperimentName=None,
    PlateNumber="",
    ExperimentID=command_id(2),
    PlateID=None,
    AllCommandsCompleted=False,
    Error=None,
    StopReason="Unspecified",
    StartTime=DateTime(2026, 11, 11, 10, 0, 0, tzinfo=datetime.timezone.utc),
    EndTime=_MIN_DATE,
    ClientTimeOffset=datetime.timedelta(milliseconds=-1500),
    Commands=commands,
    LeaveFluidicsRunningOnComplete=True,
    FCSFileName="plate1.fcs",
    FCSDataWritten=3000000000,
    FCSFileCRC=0,
    DevicesToMonitor=["Sampler", "Cytometer"],
    ExecutionNotes="line1\nline2",
    FluidicsRunTimeMinutes=d("1.5"),
    PumpRunTimeMinutes=d("0"),
  )


def empty_worklist() -> DataObject:
  """Build the worklist of the second run of ``net48_duplex_worklist``: nulls and long text."""
  return WORKLIST.new(
    ID=uuid.UUID(int=0),
    Name="n" * 100,
    DeviceID=None,
    ExperimentName=None,
    PlateNumber=None,
    ExperimentID=None,
    PlateID=None,
    AllCommandsCompleted=False,
    Error=None,
    StopReason="Unspecified",
    StartTime=_MIN_DATE,
    EndTime=_MIN_DATE,
    ClientTimeOffset=_ZERO,
    Commands=None,
    LeaveFluidicsRunningOnComplete=False,
    FCSFileName="日" * 40,
    FCSDataWritten=0,
    FCSFileCRC=0,
    DevicesToMonitor=None,
    ExecutionNotes="x" * 300,
    FluidicsRunTimeMinutes=decimal.Decimal(0),
    PumpRunTimeMinutes=decimal.Decimal(0),
  )


Call = Tuple[Operation, Tuple[Any, ...]]

_UTC = datetime.timezone.utc
# 2026-11-11T08:20:30.1234567Z, held by the client as a local time.
CLIENT_TIME = DateTime(2026, 11, 11, 8, 20, 30, 123456, _UTC, tick=7, kind="local")
COMPLETED_WORKLIST_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")

# The calls of ``net48_duplex``, in order, with the arguments the .NET client passed.
DUPLEX_CALLS: List[Call] = [
  (CONNECT, (CLIENT_TIME,)),
  (GET_SUMMARY, ()),
  (SET_VERBOSE_LOGGING, (True,)),
  (GET_SUMMARY, ()),
  (IS_1536_CAPABLE, ()),
  (GET_OPERATION_DURATION, ()),
  (UPDATE_FIRMWARE, ("WeightStation",)),
  (GET_SUMMARY, ()),
  (GET_SYSTEM_ID, (False,)),
  (GET_SYSTEM_ID, (True,)),
  (GET_SYSTEM_TYPE, ()),
  (GET_SYSTEM_STATE, ()),
  (PUMP_GET_MAX_SPEED, ()),
  (PUMP_START, (-200, "CCW")),
  (GET_SUMMARY, ()),
  (SAMPLER_GET_X, ()),
  (SAMPLER_GET_X_RANGE, ()),
  *[(SAMPLER_GET_Z_CONTACT_TOLERANCE, (kind,)) for kind in CONTACT_TOLERANCE_TYPE.values],
  (SAMPLER_MOVE_XY, (decimal.Decimal("12.5"), decimal.Decimal("-0.001"), True)),
  (GET_SUMMARY, ()),
  (SAMPLER_FIND_Z_CONTACT, (decimal.Decimal("45.5"), decimal.Decimal("3.25"), False)),
  (GET_SUMMARY, ()),
  (SET_TOP_PEAK_VALUES, (0, 1, 255, 65536, 2147483648, 4294967295)),
  (GET_SUMMARY, ()),
  (GET_TOP_PEAK_VALUES, ()),
  (GET_CURRENT_PURGE_STEP, ()),
  (
    CONFIGURE_DEVICES,
    (
      {
        "Sampler": DEVICE_CONFIGURATION.new(IncludeThisDevice=True, Configuration="COM3"),
        "Pump": DEVICE_CONFIGURATION.new(IncludeThisDevice=False, Configuration=None),
        "ContactSensor": None,
      },
    ),
  ),
  (GET_SUMMARY, ()),
  (CONFIGURE_DEVICES, ({},)),
  (GET_SUMMARY, ()),
  (CONFIGURE_DEVICES, (None,)),
  (GET_SUMMARY, ()),
  (
    RESET_DEVICES,
    (
      DEVICE_RESET_OPTIONS.new(
        PlateRailRC=3,
        IsSORPC6=True,
        OverrideLLDPercentage=0.5,
        OverrideZArmPercentage=-1,
        OverrideZArmAdditionalContactDepth=0.001,
        IsInDevMode=False,
      ),
    ),
  ),
  (GET_SUMMARY, ()),
  (INITIALIZE_DEVICES, (["Sampler", "PlateRail", "ContactSensor"],)),
  (GET_SUMMARY, ()),
  (INITIALIZE_DEVICES, ([],)),
  (GET_SUMMARY, ()),
  (GET_DEVICE_STATUSES, ()),
  (GET_DEVICE_SUMMARY, ("PlateRail",)),
  (WORKLIST_RUN, (sample_worklist(),)),
  (GET_SUMMARY, ()),
  (WORKLIST_GET_CURRENT, ()),
  (WORKLIST_SKIP_TO, (command_id(0x12),)),
  (GET_SUMMARY, ()),
  (WORKLIST_GET_STATUS, ()),
  (WORKLIST_GET_COMPLETED_WORKLISTS, ()),
  (WORKLIST_GET_COMPLETED_WORKLIST, (COMPLETED_WORKLIST_ID,)),
  (WORKLIST_GET_COMPLETED_WORKLIST, (uuid.UUID(int=0),)),
  (SAMPLER_HOME, ()),
  (CONNECT, (DateTime(2026, 11, 11, 10, 20, 31, tzinfo=_UTC),)),
  (CONNECT, (DateTime(2026, 11, 11, 10, 20, 32),)),
]

# The calls of ``net48_duplex_worklist``.
WORKLIST_CALLS: List[Call] = [
  (CONNECT, (DateTime(2026, 11, 11, 10, 20, 31, tzinfo=_UTC),)),
  (WORKLIST_RUN, (sample_worklist(),)),
  (GET_SUMMARY, ()),
  (WORKLIST_GET_CURRENT, ()),
  (GET_DEVICE_STATUSES, ()),
  (WORKLIST_RUN, (empty_worklist(),)),
  (GET_SUMMARY, ()),
]

# The calls of ``net48_duplex_abort``: the second ends the session.
ABORT_CALLS: List[Call] = [(IS_1536_CAPABLE, ()), (SHAKER_HOME, ())]
