"""Wire contract of the iQue Screener PLUS controller and of its watchdog.

The instrument's internal computer runs the controller, a WCF service with a duplex session endpoint
(net.tcp port 9002, path /Controller) and a streamed endpoint (port 9003, path
/ControllerStreaming), and a watchdog with the endpoints /Watchdog on port 9000 and
/WatchdogStreaming on port 9001. This module declares their contracts with the wcf package:

- every enum, as a ``Literal`` alias of its value names and an :class:`EnumType`;
- every class the operations carry, as a :class:`DataContract` and a frozen dataclass with
  snake_case fields (``FCSFileName`` is ``fcs_file_name``);
- every operation of IController, its callbacks, IControllerStreaming, IWatchdogService and
  IWatchdogStreamingService.

:func:`to_wire` and :func:`from_wire` convert between these Python values and what the wcf package
writes and reads. Members travel in the order of their wire names (base class first), and a receiver
silently drops a member out of order, so the declarations keep the contract's names exactly. A wire
object of a class not declared here, or with a member its class lacks or without one it declares,
raises :class:`ContractError`.

Units: sampler coordinates (X, Y, Z, ZUp, ZDown, ZHigh, ZLow, RinseX and the like) are millimetres
in the sampler's frame, X 0 to 131, Y 0 to 195, Z 190 to 250 with larger Z higher. Pump speeds are
hundredths of rpm, shaker speeds rpm. Times are as each member says: SipTime and UpTime are
milliseconds everywhere, RinseDuration is milliseconds in CyclingRinseCommand and seconds in the
timed rinse commands.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from dataclasses import dataclass
from typing import (
  Any,
  Callable,
  Dict,
  List,
  Literal,
  Mapping,
  Optional,
  Sequence,
  Tuple,
  get_args,
)

from pylabrobot.sartorius.ique.driver.wcf import (
  BOOL,
  DATETIME,
  DECIMAL,
  FLOAT,
  GUID,
  INT,
  STREAM,
  STRING,
  TIMESPAN,
  UINT,
  VERSION,
  ArrayOf,
  ContractError,
  DataContract,
  DataObject,
  DictionaryOf,
  EnumType,
  ListOf,
  Nullable,
  Operation,
  ServiceContract,
  XmlType,
  clr_namespace,
)

CONTROLLER_NAMESPACE = clr_namespace("IntelliCyt.Controller")
"""Data contract namespace of the controller's classes and enums."""
SHARED_NAMESPACE = clr_namespace("IntelliCyt.Shared")
"""Data contract namespace of the watchdog's classes."""
COLLECTIONS_NAMESPACE = clr_namespace("System.Collections.Generic")
"""Data contract namespace of ``KeyValuePair``."""


CommandCategory = Literal[
  "Sample",
  "Rinse",
  "Shake",
  "Pump",
  "FCAutomate",
  "FlowCytometer",
  "Info",
  "CirculatingRinseStation",
]
"""Informational grouping of a worklist command; the controller does not act on it."""
COMMAND_CATEGORY = EnumType("CommandCategory", CONTROLLER_NAMESPACE, get_args(CommandCategory))

ContactToleranceType = Literal[
  "NewProbeAndTubing", "DeckHeightValidation", "PlateMapping", "MicrofugeTubeValidation"
]
"""Activity whose allowed Z contact deviation sampler_get_z_contact_tolerance returns."""
CONTACT_TOLERANCE_TYPE = EnumType(
  "ContactToleranceType", CONTROLLER_NAMESPACE, get_args(ContactToleranceType)
)

CytometerFirmwareType = Literal["System", "WeightStation"]
"""Which cytometer firmware cytometer_update_firmware flashes."""
CYTOMETER_FIRMWARE_TYPE = EnumType(
  "CytometerFirmwareType", CONTROLLER_NAMESPACE, get_args(CytometerFirmwareType)
)

DeviceAlert = Literal[
  "None", "CYT_KillCytometerPower", "CYT_InstallationPriming", "CYT_PurgingFluidics"
]
"""Alert raised by a device; CYT_KillCytometerPower also makes the controller shut the cytometer
down.
"""
DEVICE_ALERT = EnumType("DeviceAlert", CONTROLLER_NAMESPACE, get_args(DeviceAlert))

DeviceKind = Literal["Sampler", "Pump", "Shaker", "Cytometer", "PlateRail", "ContactSensor"]
"""A device slot of the instrument."""
DEVICE_KIND = EnumType("DeviceKind", CONTROLLER_NAMESPACE, get_args(DeviceKind))

DeviceState = Literal["Disconnected", "Ready", "Running", "Faulted"]
"""State of a device."""
DEVICE_STATE = EnumType("DeviceState", CONTROLLER_NAMESPACE, get_args(DeviceState))

DeviceStatusType = Literal["Info", "Warning", "Error"]
"""Severity of a device status message."""
DEVICE_STATUS_TYPE = EnumType("DeviceStatusType", CONTROLLER_NAMESPACE, get_args(DeviceStatusType))

EnabledLasers = Literal[
  "VioletBlueRed",
  "VioletBlue",
  "BlueRed",
  "Blue",
  "VioletYellowBlue",
  "VioletYellow",
  "YellowBlue",
  "Yellow",
]
"""Lasers to use for an acquisition."""
ENABLED_LASERS = EnumType("EnabledLasers", CONTROLLER_NAMESPACE, get_args(EnabledLasers))

FlowCytometerSpeed = Literal["Slow", "Medium", "Fast"]
"""Fluidics speed; only older cytometer models use it."""
FLOW_CYTOMETER_SPEED = EnumType(
  "FlowCytometerSpeed", CONTROLLER_NAMESPACE, get_args(FlowCytometerSpeed)
)

IQue3InstallationPrimeStep = Literal[
  "None", "Start", "PrimingSystem", "PrimingSensor", "Priming", "End"
]
"""Step of the installation priming sequence, run one step per call. Numeric values on the
instrument: None=-1, Start=0, PrimingSystem=1, PrimingSensor=2, Priming=3, End=4.
"""
IQUE3_INSTALLATION_PRIME_STEP = EnumType(
  "IQue3InstallationPrimeStep", CONTROLLER_NAMESPACE, get_args(IQue3InstallationPrimeStep)
)

IQue3PurgeStep = Literal["None", "FlushSystem", "PurgeSystem", "PurgeSensor"]
"""Step of the decommissioning purge sequence, run one step per call."""
IQUE3_PURGE_STEP = EnumType("IQue3PurgeStep", CONTROLLER_NAMESPACE, get_args(IQue3PurgeStep))

LaserConfiguration = Literal["Normal", "HalfBlue", "AllBlue"]
"""Laser configuration; only older cytometer models use it."""
LASER_CONFIGURATION = EnumType(
  "LaserConfiguration", CONTROLLER_NAMESPACE, get_args(LaserConfiguration)
)

PumpDirection = Literal["CW", "CCW"]
"""Peristaltic pump direction: CW clockwise, CCW counter-clockwise."""
PUMP_DIRECTION = EnumType("PumpDirection", CONTROLLER_NAMESPACE, get_args(PumpDirection))

RinseStationPump = Literal["S1", "S2", "S3", "S4", "MB"]
"""Rinse station pump named by a command; informational for the controller."""
RINSE_STATION_PUMP = EnumType("RinseStationPump", CONTROLLER_NAMESPACE, get_args(RinseStationPump))

SampleSpeed = Literal["Slow", "Fast"]
"""Sampler XY move speed."""
SAMPLE_SPEED = EnumType("SampleSpeed", CONTROLLER_NAMESPACE, get_args(SampleSpeed))

SystemState = Literal["Red", "Yellow", "Green"]
"""Overall instrument state: Red when a configured device is missing, disconnected or faulted,
Yellow while devices reset or when a device reports a warning, Green otherwise.
"""
SYSTEM_STATE = EnumType("SystemState", CONTROLLER_NAMESPACE, get_args(SystemState))

SystemType = Literal[
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
]
"""Instrument model the controller is configured for."""
SYSTEM_TYPE = EnumType("SystemType", CONTROLLER_NAMESPACE, get_args(SystemType))

WorklistStatus = Literal["NoActiveWorklist", "RunningWorklist", "CompletedUnfinalizedWorklist"]
"""Whether a worklist runs, or has finished and waits for worklist_finalize."""
WORKLIST_STATUS = EnumType("WorklistStatus", CONTROLLER_NAMESPACE, get_args(WorklistStatus))

WorklistStopReason = Literal[
  "Unspecified", "Cancelled", "WorklistError", "DeviceFaulted", "EventLimit"
]
"""Why a worklist run ended."""
WORKLIST_STOP_REASON = EnumType(
  "WorklistStopReason", CONTROLLER_NAMESPACE, get_args(WorklistStopReason)
)


@dataclass(frozen=True)
class Version:
  """A .NET ``System.Version``: build and revision are -1 when the version lacks them.

  Attributes:
    major: First part.
    minor: Second part.
    build: Third part, or -1.
    revision: Fourth part, or -1.
  """

  major: int
  minor: int
  build: int
  revision: int

  def __str__(self) -> str:
    """Return the version as .NET writes it: without the parts that are -1."""
    if self.build == -1:
      return f"{self.major}.{self.minor}"
    if self.revision == -1:
      return f"{self.major}.{self.minor}.{self.build}"
    return f"{self.major}.{self.minor}.{self.build}.{self.revision}"


class _ValueTypeContract(DataContract):
  """A .NET struct with a data contract: written like a class, but never nil."""

  def __init__(self, name: str, namespace: str, members: Sequence[Tuple[str, XmlType]]) -> None:
    """Create the contract; see :class:`DataContract`."""
    super().__init__(name, namespace, members)
    self.nullable = False


KEY_VALUE_PAIR_OF_STRING_STRING = _ValueTypeContract(
  "KeyValuePairOfstringstring", COLLECTIONS_NAMESPACE, [("key", STRING), ("value", STRING)]
)
"""``KeyValuePair<string, string>``; its Python form is a (key, value) tuple."""


WORKLIST_COMMAND = DataContract(
  "WorklistCommand",
  CONTROLLER_NAMESPACE,
  [
    ("Category", COMMAND_CATEGORY),
    ("Completed", BOOL),
    ("CountdownDuration", INT),
    ("Description", STRING),
    ("EndTime", DATETIME),
    ("EstimatedDuration", TIMESPAN),
    ("ExecutionNotes", STRING),
    ("ID", GUID),
    ("Name", STRING),
    ("StartTime", DATETIME),
  ],
)
"""Wire declaration of :class:`WorklistCommand`."""

SAMPLE_COMMAND = DataContract(
  "SampleCommand",
  CONTROLLER_NAMESPACE,
  [
    ("FastMove", BOOL),
    ("SipTime", INT),
    ("UpTime", INT),
    ("X", DECIMAL),
    ("Y", DECIMAL),
    ("ZDown", DECIMAL),
    ("ZUp", DECIMAL),
  ],
  WORKLIST_COMMAND,
)
"""Wire declaration of :class:`SampleCommand`."""

BEAD_SIP_COMMAND = DataContract(
  "BeadSipCommand", CONTROLLER_NAMESPACE, [("SipDescription", STRING)], SAMPLE_COMMAND
)
"""Wire declaration of :class:`BeadSipCommand`."""

BEGIN_SESSION_FC_AUTOMATER_COMMAND = DataContract(
  "BeginSessionFCAutomaterCommand",
  CONTROLLER_NAMESPACE,
  [("Alias", STRING), ("FileNameToDelete", STRING)],
  WORKLIST_COMMAND,
)
"""Wire declaration of :class:`BeginSessionFCAutomaterCommand`."""

CLEAN_FLOW_CYTOMETER_COMMAND = DataContract(
  "CleanFlowCytometerCommand", CONTROLLER_NAMESPACE, [("CleanMethod", STRING)], WORKLIST_COMMAND
)
"""Wire declaration of :class:`CleanFlowCytometerCommand`."""

COMBINED_SHAKE_RINSE_COMMAND = DataContract(
  "CombinedShakeRinseCommand",
  CONTROLLER_NAMESPACE,
  [
    ("FastMove", BOOL),
    ("RPM", INT),
    ("RinseStationPump", RINSE_STATION_PUMP),
    ("RinseX", DECIMAL),
    ("RinseY", DECIMAL),
    ("RinseZDown", DECIMAL),
    ("RinseZUp", DECIMAL),
    ("ShakeDuration", INT),
    ("SipTime", INT),
    ("UpTime", INT),
  ],
  WORKLIST_COMMAND,
)
"""Wire declaration of :class:`CombinedShakeRinseCommand`."""

COMBINED_SHAKE_RINSE_AT_LOCATION_COMMAND = DataContract(
  "CombinedShakeRinseAtLocationCommand",
  CONTROLLER_NAMESPACE,
  [("LocationName", STRING)],
  COMBINED_SHAKE_RINSE_COMMAND,
)
"""Wire declaration of :class:`CombinedShakeRinseAtLocationCommand`."""

COMPENSATION_ENTRY = DataContract(
  "CompensationEntry",
  CONTROLLER_NAMESPACE,
  [("Amount", DECIMAL), ("Param1", STRING), ("Param2", STRING)],
)
"""Wire declaration of :class:`CompensationEntry`."""

COMPLETED_WORKLIST = DataContract(
  "CompletedWorklist",
  CONTROLLER_NAMESPACE,
  [
    ("ClientTimeOffset", TIMESPAN),
    ("ExperimentID", Nullable(GUID)),
    ("ExperimentName", STRING),
    ("HasFCSFile", BOOL),
    ("ID", GUID),
    ("PlateID", Nullable(GUID)),
    ("PlateNumber", STRING),
    ("RunComplete", DATETIME),
    ("RunStart", DATETIME),
  ],
)
"""Wire declaration of :class:`CompletedWorklist`."""

CONNECTION_INFO = DataContract(
  "ConnectionInfo",
  CONTROLLER_NAMESPACE,
  [("Comments", STRING), ("IsClientAlreadyConnected", BOOL), ("Version", VERSION)],
)
"""Wire declaration of :class:`ConnectionInfo`."""

CONTACT_Z_RESULT = DataContract(
  "ContactZResult",
  CONTROLLER_NAMESPACE,
  [
    ("ContactTime", DATETIME),
    ("ContactZLocation", DECIMAL),
    ("ErrorMessage", STRING),
    ("FoundContact", BOOL),
    ("HasError", BOOL),
    ("ID", GUID),
    ("IsSimulated", BOOL),
    ("ZLow", DECIMAL),
  ],
)
"""Wire declaration of :class:`ContactZResult`."""

CONTROLLER_SERVICE_EXCEPTION = DataContract(
  "ControllerServiceException", CONTROLLER_NAMESPACE, [("Message", STRING), ("Type", STRING)]
)
"""Wire declaration of :class:`ControllerServiceException`."""

CYCLING_RINSE_COMMAND = DataContract(
  "CyclingRinseCommand",
  CONTROLLER_NAMESPACE,
  [
    ("Operation", STRING),
    ("RinseDuration", INT),
    ("RinseStationName", STRING),
    ("RinseStationPump", RINSE_STATION_PUMP),
  ],
  SAMPLE_COMMAND,
)
"""Wire declaration of :class:`CyclingRinseCommand`."""

DEVICE_ALERT_MESSAGE = DataContract(
  "DeviceAlertMessage", CONTROLLER_NAMESPACE, [("Alert", DEVICE_ALERT), ("Kind", DEVICE_KIND)]
)
"""Wire declaration of :class:`DeviceAlertMessage`."""

DEVICE_CONFIGURATION = DataContract(
  "DeviceConfiguration",
  CONTROLLER_NAMESPACE,
  [("Configuration", STRING), ("IncludeThisDevice", BOOL)],
)
"""Wire declaration of :class:`DeviceConfiguration`."""

DEVICE_MESSAGE = DataContract(
  "DeviceMessage",
  CONTROLLER_NAMESPACE,
  [("Detail", STRING), ("Message", STRING), ("MessageLevel", DEVICE_STATUS_TYPE)],
)
"""Wire declaration of :class:`DeviceMessage`."""

DEVICE_INFO = DataContract(
  "DeviceInfo",
  CONTROLLER_NAMESPACE,
  [
    ("Kind", DEVICE_KIND),
    ("Messages", ListOf(DEVICE_MESSAGE)),
    ("Model", STRING),
    ("State", DEVICE_STATE),
  ],
)
"""Wire declaration of :class:`DeviceInfo`."""

DEVICE_RESET_OPTIONS = DataContract(
  "DeviceResetOptions",
  CONTROLLER_NAMESPACE,
  [
    ("IsInDevMode", BOOL),
    ("IsSORPC6", BOOL),
    ("OverrideLLDPercentage", FLOAT),
    ("OverrideZArmAdditionalContactDepth", FLOAT),
    ("OverrideZArmPercentage", INT),
    ("PlateRailRC", INT),
  ],
)
"""Wire declaration of :class:`DeviceResetOptions`."""

END_SESSION_FC_AUTOMATER_COMMAND = DataContract(
  "EndSessionFCAutomaterCommand", CONTROLLER_NAMESPACE, [], WORKLIST_COMMAND
)
"""Wire declaration of :class:`EndSessionFCAutomaterCommand`."""

FLOW_CYTOMETER_ACQUIRE_COMMAND = DataContract(
  "FlowCytometerAcquireCommand",
  CONTROLLER_NAMESPACE,
  [
    ("AcquistionDelay", INT),
    ("EstimatedAcquisitionTime", TIMESPAN),
    ("EventMaximum", INT),
    ("EventThreshold", INT),
  ],
  WORKLIST_COMMAND,
)
"""Wire declaration of :class:`FlowCytometerAcquireCommand`."""

FLOW_CYTOMETER_STOP_ACQUIRE_COMMAND = DataContract(
  "FlowCytometerStopAcquireCommand", CONTROLLER_NAMESPACE, [], WORKLIST_COMMAND
)
"""Wire declaration of :class:`FlowCytometerStopAcquireCommand`."""

TIMED_CYCLING_RINSE_COMMAND = DataContract(
  "TimedCyclingRinseCommand",
  CONTROLLER_NAMESPACE,
  [
    ("FastMove", BOOL),
    ("RinseDescription", STRING),
    ("RinseDuration", INT),
    ("RinseStationName", STRING),
    ("RinseStationPump", RINSE_STATION_PUMP),
    ("SipTime", INT),
    ("UpTime", INT),
    ("X", DECIMAL),
    ("Y", DECIMAL),
    ("ZDown", DECIMAL),
    ("ZUp", DECIMAL),
  ],
  WORKLIST_COMMAND,
)
"""Wire declaration of :class:`TimedCyclingRinseCommand`."""

FLUSH_COMMAND = DataContract("FlushCommand", CONTROLLER_NAMESPACE, [], TIMED_CYCLING_RINSE_COMMAND)
"""Wire declaration of :class:`FlushCommand`."""

FLUSH_MATRYOSHKA_COMMAND = DataContract(
  "FlushMatryoshkaCommand", CONTROLLER_NAMESPACE, [], FLUSH_COMMAND
)
"""Wire declaration of :class:`FlushMatryoshkaCommand`."""

GET_FC_AUTOMATER_FCS_DATA_COMMAND = DataContract(
  "GetFCAutomaterFCSDataCommand",
  CONTROLLER_NAMESPACE,
  [("ExperimentID", GUID), ("PlateNumber", INT)],
  WORKLIST_COMMAND,
)
"""Wire declaration of :class:`GetFCAutomaterFCSDataCommand`."""

MOVEMENT_RANGE = DataContract(
  "MovementRange", CONTROLLER_NAMESPACE, [("Max", DECIMAL), ("Min", DECIMAL)]
)
"""Wire declaration of :class:`MovementRange`."""

RINSE_COMMAND = DataContract(
  "RinseCommand",
  CONTROLLER_NAMESPACE,
  [("Operation", STRING), ("RinseStationName", STRING), ("RinseStationPump", RINSE_STATION_PUMP)],
  SAMPLE_COMMAND,
)
"""Wire declaration of :class:`RinseCommand`."""

RUN_FLOW_CYTOMETER_FLUIDICS_FOR_DURATION_COMMAND = DataContract(
  "RunFlowCytometerFluidicsForDurationCommand",
  CONTROLLER_NAMESPACE,
  [("FluidicsDuration", INT), ("Speed", FLOW_CYTOMETER_SPEED)],
  WORKLIST_COMMAND,
)
"""Wire declaration of :class:`RunFlowCytometerFluidicsForDurationCommand`."""

SAMPLE_CONTINUOUSLY_COMMAND = DataContract(
  "SampleContinuouslyCommand",
  CONTROLLER_NAMESPACE,
  [
    ("FastMove", BOOL),
    ("PumpDirection", PUMP_DIRECTION),
    ("PumpSpeed", INT),
    ("SipTime", INT),
    ("X", DECIMAL),
    ("Y", DECIMAL),
    ("ZHigh", DECIMAL),
    ("ZLow", DECIMAL),
  ],
  WORKLIST_COMMAND,
)
"""Wire declaration of :class:`SampleContinuouslyCommand`."""

SAMPLE_WELL_COMMAND = DataContract(
  "SampleWellCommand",
  CONTROLLER_NAMESPACE,
  [("Column", INT), ("Row", STRING), ("WellType", STRING)],
  SAMPLE_COMMAND,
)
"""Wire declaration of :class:`SampleWellCommand`."""

SAMPLE_TEST_TUBE_COMMAND = DataContract(
  "SampleTestTubeCommand", CONTROLLER_NAMESPACE, [("EventThreshold", INT)], SAMPLE_WELL_COMMAND
)
"""Wire declaration of :class:`SampleTestTubeCommand`."""

SAMPLE_VIAL_COMMAND = DataContract(
  "SampleVialCommand", CONTROLLER_NAMESPACE, [("ZTolerance", DECIMAL)], SAMPLE_TEST_TUBE_COMMAND
)
"""Wire declaration of :class:`SampleVialCommand`."""

SAMPLE_WELL_CONTINUOUSLY_COMMAND = DataContract(
  "SampleWellContinuouslyCommand",
  CONTROLLER_NAMESPACE,
  [("PumpDirection", PUMP_DIRECTION), ("PumpSpeed", INT), ("ZHigh", DECIMAL), ("ZLow", DECIMAL)],
  SAMPLE_WELL_COMMAND,
)
"""Wire declaration of :class:`SampleWellContinuouslyCommand`."""

SHUTDOWN_FLOW_CYTOMETER_COMMAND = DataContract(
  "ShutdownFlowCytometerCommand", CONTROLLER_NAMESPACE, [], WORKLIST_COMMAND
)
"""Wire declaration of :class:`ShutdownFlowCytometerCommand`."""

START_FC_AUTOMATER_COMMAND = DataContract(
  "StartFCAutomaterCommand",
  CONTROLLER_NAMESPACE,
  [("ExperimentID", GUID), ("PlateNumber", INT)],
  WORKLIST_COMMAND,
)
"""Wire declaration of :class:`StartFCAutomaterCommand`."""

START_FC_AUTOMATER_FOR_PRIME_COMMAND = DataContract(
  "StartFCAutomaterForPrimeCommand", CONTROLLER_NAMESPACE, [], START_FC_AUTOMATER_COMMAND
)
"""Wire declaration of :class:`StartFCAutomaterForPrimeCommand`."""

THRESHOLD = DataContract(
  "Threshold", CONTROLLER_NAMESPACE, [("ID", INT), ("Maximum", INT), ("Minimum", INT)]
)
"""Wire declaration of :class:`Threshold`."""

START_FLOW_CYTOMETER_FLUIDICS_COMMAND = DataContract(
  "StartFlowCytometerFluidicsCommand",
  CONTROLLER_NAMESPACE,
  [
    ("EnabledLasers", ENABLED_LASERS),
    ("FcsMetadata", ListOf(KEY_VALUE_PAIR_OF_STRING_STRING)),
    ("LaserConfiguration", LASER_CONFIGURATION),
    ("PrepareForAcquisition", BOOL),
    ("Speed", FLOW_CYTOMETER_SPEED),
    ("Thresholds", ListOf(THRESHOLD)),
    ("UserName", STRING),
  ],
  WORKLIST_COMMAND,
)
"""Wire declaration of :class:`StartFlowCytometerFluidicsCommand`."""

START_MATRYOSHKA_FLUIDICS_COMMAND = DataContract(
  "StartMatryoshkaFluidicsCommand",
  CONTROLLER_NAMESPACE,
  [
    ("EnabledLasers", ENABLED_LASERS),
    ("FcsMetadata", ListOf(KEY_VALUE_PAIR_OF_STRING_STRING)),
    ("LaserConfiguration", LASER_CONFIGURATION),
    ("Speed", FLOW_CYTOMETER_SPEED),
    ("Thresholds", ListOf(THRESHOLD)),
    ("UserName", STRING),
  ],
  WORKLIST_COMMAND,
)
"""Wire declaration of :class:`StartMatryoshkaFluidicsCommand`."""

START_PUMP_COMMAND = DataContract(
  "StartPumpCommand",
  CONTROLLER_NAMESPACE,
  [("Direction", PUMP_DIRECTION), ("Speed", INT)],
  WORKLIST_COMMAND,
)
"""Wire declaration of :class:`StartPumpCommand`."""

START_SHAKER_COMMAND = DataContract(
  "StartShakerCommand", CONTROLLER_NAMESPACE, [("RPM", INT)], WORKLIST_COMMAND
)
"""Wire declaration of :class:`StartShakerCommand`."""

STOP_FC_AUTOMATER_COMMAND = DataContract(
  "StopFCAutomaterCommand", CONTROLLER_NAMESPACE, [], WORKLIST_COMMAND
)
"""Wire declaration of :class:`StopFCAutomaterCommand`."""

STOP_FLOW_CYTOMETER_FLUIDICS_COMMAND = DataContract(
  "StopFlowCytometerFluidicsCommand", CONTROLLER_NAMESPACE, [], WORKLIST_COMMAND
)
"""Wire declaration of :class:`StopFlowCytometerFluidicsCommand`."""

STOP_GATE_ADD_FCS_TO_PLATE_COMMAND = DataContract(
  "StopGateAddFCSToPlateCommand",
  CONTROLLER_NAMESPACE,
  [("DstName", STRING), ("ExperimentId", GUID), ("PlateNumber", INT), ("ZipPath", STRING)],
  WORKLIST_COMMAND,
)
"""Wire declaration of :class:`StopGateAddFCSToPlateCommand`."""

STOP_GATE_SAMPLE_WELL_COMMAND = DataContract(
  "StopGateSampleWellCommand", CONTROLLER_NAMESPACE, [], SAMPLE_WELL_COMMAND
)
"""Wire declaration of :class:`StopGateSampleWellCommand`."""

STOP_PUMP_COMMAND = DataContract("StopPumpCommand", CONTROLLER_NAMESPACE, [], WORKLIST_COMMAND)
"""Wire declaration of :class:`StopPumpCommand`."""

STOP_SHAKER_COMMAND = DataContract("StopShakerCommand", CONTROLLER_NAMESPACE, [], WORKLIST_COMMAND)
"""Wire declaration of :class:`StopShakerCommand`."""

TIMED_CONTINUOUS_RINSE_COMMAND = DataContract(
  "TimedContinuousRinseCommand",
  CONTROLLER_NAMESPACE,
  [
    ("RinseDescription", STRING),
    ("RinseDuration", INT),
    ("RinseStationName", STRING),
    ("RinseStationPump", RINSE_STATION_PUMP),
    ("SampleSpeed", SAMPLE_SPEED),
    ("UpTime", INT),
    ("X", DECIMAL),
    ("Y", DECIMAL),
    ("ZDown", DECIMAL),
    ("ZUp", DECIMAL),
  ],
  WORKLIST_COMMAND,
)
"""Wire declaration of :class:`TimedContinuousRinseCommand`."""

TIMED_SHAKE_COMMAND = DataContract(
  "TimedShakeCommand",
  CONTROLLER_NAMESPACE,
  [
    ("FastMove", BOOL),
    ("RinseStationPump", RINSE_STATION_PUMP),
    ("RinseX", DECIMAL),
    ("RinseY", DECIMAL),
    ("RinseZDown", DECIMAL),
    ("RinseZUp", DECIMAL),
    ("Rpm", INT),
    ("ShakeDuration", INT),
  ],
  WORKLIST_COMMAND,
)
"""Wire declaration of :class:`TimedShakeCommand`."""

TIMED_SHAKE_AT_LOCATION_COMMAND = DataContract(
  "TimedShakeAtLocationCommand",
  CONTROLLER_NAMESPACE,
  [("LocationName", STRING)],
  TIMED_SHAKE_COMMAND,
)
"""Wire declaration of :class:`TimedShakeAtLocationCommand`."""

VALIDATE_DECK_HEIGHT_COMMAND = DataContract(
  "ValidateDeckHeightCommand",
  CONTROLLER_NAMESPACE,
  [
    ("IsMicrofugeTubeCheck", BOOL),
    ("X", DECIMAL),
    ("Y", DECIMAL),
    ("ZHigh", DECIMAL),
    ("ZLow", DECIMAL),
    ("ZTolerance", DECIMAL),
  ],
  WORKLIST_COMMAND,
)
"""Wire declaration of :class:`ValidateDeckHeightCommand`."""

WORKLIST = DataContract(
  "Worklist",
  CONTROLLER_NAMESPACE,
  [
    ("AllCommandsCompleted", BOOL),
    ("ClientTimeOffset", TIMESPAN),
    ("Commands", ListOf(WORKLIST_COMMAND)),
    ("DeviceID", STRING),
    ("DevicesToMonitor", ListOf(DEVICE_KIND)),
    ("EndTime", DATETIME),
    ("Error", STRING),
    ("ExecutionNotes", STRING),
    ("ExperimentID", Nullable(GUID)),
    ("ExperimentName", STRING),
    ("FCSDataWritten", UINT),
    ("FCSFileCRC", UINT),
    ("FCSFileName", STRING),
    ("FluidicsRunTimeMinutes", DECIMAL),
    ("ID", GUID),
    ("LeaveFluidicsRunningOnComplete", BOOL),
    ("Name", STRING),
    ("PlateID", Nullable(GUID)),
    ("PlateNumber", STRING),
    ("PumpRunTimeMinutes", DECIMAL),
    ("StartTime", DATETIME),
    ("StopReason", WORKLIST_STOP_REASON),
  ],
)
"""Wire declaration of :class:`Worklist`."""

CONTROLLER_SERVICE_STATUS = DataContract(
  "ControllerServiceStatus", SHARED_NAMESPACE, [("isRunning", BOOL), ("isUpdating", BOOL)]
)
"""Wire declaration of :class:`ControllerServiceStatus`."""

WATCHDOG_EXCEPTION = DataContract(
  "WatchdogException", SHARED_NAMESPACE, [("Message", STRING), ("Type", STRING)]
)
"""Wire declaration of :class:`WatchdogException`."""

CONTROLLER_SERVICE_EXCEPTION.member("InnerException", CONTROLLER_SERVICE_EXCEPTION)
WATCHDOG_EXCEPTION.member("InnerException", WATCHDOG_EXCEPTION)


@dataclass(frozen=True, kw_only=True)
class WorklistCommand:
  """Base of every worklist command; a list of commands holds any of its subclasses.

  Attributes:
    category: Informational grouping set by the client.
    completed: Set by the controller when the command ran to its end without cancellation.
    countdown_duration: Seconds the client shows as a countdown; informational.
    description: Text for the operator; informational.
    end_time: Set by the controller: its clock when the command ended.
    estimated_duration: Client's estimate of the duration; summed into Worklist.ExecutionNotes.
    execution_notes: Set by the controller: step notes, or Skipped for a command passed over by
      worklist_skip_to.
    id: Client-chosen identifier; the target of worklist_skip_to.
    name: Optional name; informational.
    start_time: Set by the controller: its clock when the command started.
  """

  category: CommandCategory
  completed: bool
  countdown_duration: int
  description: Optional[str]
  end_time: datetime.datetime
  estimated_duration: datetime.timedelta
  execution_notes: Optional[str]
  id: uuid.UUID
  name: Optional[str]
  start_time: datetime.datetime


@dataclass(frozen=True, kw_only=True)
class SampleCommand(WorklistCommand):
  """One dip of the probe: move to X, Y, lower to ZDown for SipTime, raise to ZUp for UpTime.

  It has the members of :class:`WorklistCommand` and the following.

  Attributes:
    fast_move: True for the fast XY move, False for the slow one.
    sip_time: Time in ms the probe stays at ZDown.
    up_time: Time in ms waited at ZUp afterwards.
    x: Target X, a sampler coordinate in mm.
    y: Target Y, a sampler coordinate in mm.
    z_down: Z the probe is lowered to, a sampler Z coordinate in mm (larger is higher).
    z_up: Z the probe returns to, a sampler Z coordinate in mm (larger is higher).
  """

  fast_move: bool
  sip_time: int
  up_time: int
  x: decimal.Decimal
  y: decimal.Decimal
  z_down: decimal.Decimal
  z_up: decimal.Decimal


@dataclass(frozen=True, kw_only=True)
class BeadSipCommand(SampleCommand):
  """One dip of the probe into a bead well, like SampleWellCommand.

  It has the members of :class:`SampleCommand` and the following.

  Attributes:
    sip_description: Description; informational.
  """

  sip_description: Optional[str]


@dataclass(frozen=True, kw_only=True)
class BeginSessionFCAutomaterCommand(WorklistCommand):
  """Obsolete automation command; the controller has no executor for it.

  It has the members of :class:`WorklistCommand` and the following.

  Attributes:
    alias: Obsolete; not used.
    file_name_to_delete: Obsolete; not used.
  """

  alias: Optional[str]
  file_name_to_delete: Optional[str]


@dataclass(frozen=True, kw_only=True)
class CleanFlowCytometerCommand(WorklistCommand):
  """Runs one of the cytometer's cleaning or priming methods.

  It has the members of :class:`WorklistCommand` and the following.

  Attributes:
    clean_method: Cleaning method, case-insensitive: DailyClean, QuickClean, LongClean, Unclog,
      Debbubble, Priming or Shutdown.
  """

  clean_method: Optional[str]


@dataclass(frozen=True, kw_only=True)
class CombinedShakeRinseCommand(WorklistCommand):
  """Shakes the plate for a time while the probe cycles at a rinse position.

  It has the members of :class:`WorklistCommand` and the following.

  Attributes:
    fast_move: True for the fast XY move.
    rpm: Shaker speed in rpm.
    rinse_station_pump: Rinse station pump; informational.
    rinse_x: X of the rinse position, a sampler coordinate in mm.
    rinse_y: Y of the rinse position, a sampler coordinate in mm.
    rinse_z_down: Lower Z of each rinse cycle, a sampler Z coordinate in mm (larger is higher).
    rinse_z_up: Upper Z of each rinse cycle, a sampler Z coordinate in mm (larger is higher).
    shake_duration: Shake time in s.
    sip_time: Time in ms at RinseZDown per cycle.
    up_time: Time in ms at RinseZUp per cycle.
  """

  fast_move: bool
  rpm: int
  rinse_station_pump: RinseStationPump
  rinse_x: decimal.Decimal
  rinse_y: decimal.Decimal
  rinse_z_down: decimal.Decimal
  rinse_z_up: decimal.Decimal
  shake_duration: int
  sip_time: int
  up_time: int


@dataclass(frozen=True, kw_only=True)
class CombinedShakeRinseAtLocationCommand(CombinedShakeRinseCommand):
  """CombinedShakeRinseCommand with the name of the rinse location.

  It has the members of :class:`CombinedShakeRinseCommand` and the following.

  Attributes:
    location_name: Name of the rinse location; informational.
  """

  location_name: Optional[str]


@dataclass(frozen=True, kw_only=True)
class CompensationEntry:
  """One spillover compensation value; no operation or command carries it.

  Attributes:
    amount: Compensation amount.
    param1: First parameter name.
    param2: Second parameter name.
  """

  amount: decimal.Decimal
  param1: Optional[str]
  param2: Optional[str]


@dataclass(frozen=True, kw_only=True)
class CompletedWorklist:
  """Summary of an archived worklist.

  Attributes:
    client_time_offset: Client time offset of the run.
    experiment_id: Experiment identifier of the run.
    experiment_name: Experiment name of the run.
    has_fcs_file: Whether the archive holds an FCS file.
    id: Identifier of the archived worklist.
    plate_id: Plate identifier of the run.
    plate_number: Plate number of the run.
    run_complete: Controller clock at the end of the run.
    run_start: Controller clock at the start of the run.
  """

  client_time_offset: datetime.timedelta
  experiment_id: Optional[uuid.UUID]
  experiment_name: Optional[str]
  has_fcs_file: bool
  id: uuid.UUID
  plate_id: Optional[uuid.UUID]
  plate_number: Optional[str]
  run_complete: datetime.datetime
  run_start: datetime.datetime


@dataclass(frozen=True, kw_only=True)
class ConnectionInfo:
  """Answer to controller_connect.

  Attributes:
    comments: Empty, or contains [WorklistIsRunning] while a worklist runs.
    is_client_already_connected: True when another client is registered and still answers; this
      session was then not registered.
    version: Version of the controller software.
  """

  comments: Optional[str]
  is_client_already_connected: bool
  version: Optional[Version]


@dataclass(frozen=True, kw_only=True)
class ContactZResult:
  """Result of a contact-sensing descent (sampler_find_z_contact).

  Attributes:
    contact_time: Controller clock time of the result.
    contact_z_location: Z of the contact, a sampler Z coordinate in mm (larger is higher).
    error_message: Error text when HasError is True.
    found_contact: Whether the probe touched something between zHigh and zLow.
    has_error: Whether the search failed.
    id: Identifier of this result.
    is_simulated: Whether the contact sensor is simulated.
    z_low: The requested lower Z bound, a sampler Z coordinate in mm (larger is higher).
  """

  contact_time: datetime.datetime
  contact_z_location: decimal.Decimal
  error_message: Optional[str]
  found_contact: bool
  has_error: bool
  id: uuid.UUID
  is_simulated: bool
  z_low: decimal.Decimal


@dataclass(frozen=True, kw_only=True)
class ControllerServiceException:
  """Detail of every fault of the controller endpoints: the exception the controller caught.

  Attributes:
    inner_exception: The inner exception, or None.
    message: Message of the exception.
    type: .NET type name of the exception.
  """

  inner_exception: Optional[ControllerServiceException]
  message: Optional[str]
  type: Optional[str]


@dataclass(frozen=True, kw_only=True)
class CyclingRinseCommand(SampleCommand):
  """Dips the probe repeatedly at a rinse position for a total time.

  It has the members of :class:`SampleCommand` and the following.

  Attributes:
    operation: Description; informational.
    rinse_duration: Total time in ms (not s); the probe cycles down for SipTime and up for UpTime
      until it elapses.
    rinse_station_name: Rinse station name; informational.
    rinse_station_pump: Rinse station pump; informational.
  """

  operation: Optional[str]
  rinse_duration: int
  rinse_station_name: Optional[str]
  rinse_station_pump: RinseStationPump


@dataclass(frozen=True, kw_only=True)
class DeviceAlertMessage:
  """An alert raised by a device.

  Attributes:
    alert: The alert.
    kind: Device that raised it.
  """

  alert: DeviceAlert
  kind: DeviceKind


@dataclass(frozen=True, kw_only=True)
class DeviceConfiguration:
  """Whether to include a device in the controller's device set.

  Attributes:
    configuration: Opaque configuration text; the controller stores it and nothing reads it.
    include_this_device: True to add the device, False to remove it; only Shaker, Cytometer,
      PlateRail and ContactSensor are read.
  """

  configuration: Optional[str]
  include_this_device: bool


@dataclass(frozen=True, kw_only=True)
class DeviceMessage:
  """One status message of a device.

  Attributes:
    detail: Longer explanation.
    message: Short status text.
    message_level: Severity.
  """

  detail: Optional[str]
  message: Optional[str]
  message_level: DeviceStatusType


@dataclass(frozen=True, kw_only=True)
class DeviceInfo:
  """State and status messages of one device.

  Attributes:
    kind: Device slot.
    messages: Status messages of the device.
    model: Model of the device; empty when it is missing or disconnected.
    state: State of the device; Disconnected when it is missing.
  """

  kind: DeviceKind
  messages: Optional[List[Optional[DeviceMessage]]]
  model: Optional[str]
  state: DeviceState


@dataclass(frozen=True, kw_only=True)
class DeviceResetOptions:
  """Device settings applied by controller_reset_devices; out-of-range values select the defaults.

  Attributes:
    is_in_dev_mode: Development mode flag; stored, not used.
    is_sorpc6: Option of older cytometer models.
    override_lld_percentage: Sampler liquid-level-detect percentage; used only above 0 and below 10,
      else 2.
    override_z_arm_additional_contact_depth: Extra depth in mm below zLow when finding contact; used
      only above 0 and below 10, else 3.
    override_z_arm_percentage: Sampler Z-arm power in percent; used only from 40 to 100, else 70.
    plate_rail_rc: Plate rail motor run current; used only inside the rail's range.
  """

  is_in_dev_mode: bool
  is_sorpc6: bool
  override_lld_percentage: float
  override_z_arm_additional_contact_depth: float
  override_z_arm_percentage: int
  plate_rail_rc: int


@dataclass(frozen=True, kw_only=True)
class EndSessionFCAutomaterCommand(WorklistCommand):
  """Obsolete automation command; the controller has no executor for it.

  It has the members of :class:`WorklistCommand` and no others.
  """


@dataclass(frozen=True, kw_only=True)
class FlowCytometerAcquireCommand(WorklistCommand):
  """Starts recording events.

  It has the members of :class:`WorklistCommand` and the following.

  Attributes:
    acquistion_delay: Delay in s (the wire name keeps its misspelling).
    estimated_acquisition_time: Expected acquisition time.
    event_maximum: Maximum number of events to record.
    event_threshold: Event count threshold; 2147483647 for no limit.
  """

  acquistion_delay: int
  estimated_acquisition_time: datetime.timedelta
  event_maximum: int
  event_threshold: int


@dataclass(frozen=True, kw_only=True)
class FlowCytometerStopAcquireCommand(WorklistCommand):
  """Stops recording events.

  It has the members of :class:`WorklistCommand` and no others.
  """


@dataclass(frozen=True, kw_only=True)
class TimedCyclingRinseCommand(WorklistCommand):
  """Dips the probe repeatedly at a rinse station for a time.

  It has the members of :class:`WorklistCommand` and the following.

  Attributes:
    fast_move: True for the fast XY move.
    rinse_description: Description; informational.
    rinse_duration: Total time in s (not ms).
    rinse_station_name: Rinse station name; informational.
    rinse_station_pump: Rinse station pump; informational.
    sip_time: Time in ms at ZDown per cycle.
    up_time: Time in ms at ZUp per cycle.
    x: Target X, a sampler coordinate in mm.
    y: Target Y, a sampler coordinate in mm.
    z_down: Lower Z of each cycle, a sampler Z coordinate in mm (larger is higher).
    z_up: Upper Z of each cycle, a sampler Z coordinate in mm (larger is higher).
  """

  fast_move: bool
  rinse_description: Optional[str]
  rinse_duration: int
  rinse_station_name: Optional[str]
  rinse_station_pump: RinseStationPump
  sip_time: int
  up_time: int
  x: decimal.Decimal
  y: decimal.Decimal
  z_down: decimal.Decimal
  z_up: decimal.Decimal


@dataclass(frozen=True, kw_only=True)
class FlushCommand(TimedCyclingRinseCommand):
  """TimedCyclingRinseCommand used to flush the probe on older models.

  It has the members of :class:`TimedCyclingRinseCommand` and no others.
  """


@dataclass(frozen=True, kw_only=True)
class FlushMatryoshkaCommand(FlushCommand):
  """Flushes the probe while the plate rail ejects the plate.

  It has the members of :class:`FlushCommand` and no others.
  """


@dataclass(frozen=True, kw_only=True)
class GetFCAutomaterFCSDataCommand(WorklistCommand):
  """Obsolete automation command; the controller has no executor for it.

  It has the members of :class:`WorklistCommand` and the following.

  Attributes:
    experiment_id: Obsolete; not used.
    plate_number: Obsolete; not used.
  """

  experiment_id: uuid.UUID
  plate_number: int


@dataclass(frozen=True, kw_only=True)
class MovementRange:
  """Range of one sampler axis.

  Attributes:
    max: Upper bound of the axis in mm.
    min: Lower bound of the axis in mm.
  """

  max: decimal.Decimal
  min: decimal.Decimal


@dataclass(frozen=True, kw_only=True)
class RinseCommand(SampleCommand):
  """One dip of the probe at a rinse station.

  It has the members of :class:`SampleCommand` and the following.

  Attributes:
    operation: Description; informational.
    rinse_station_name: Rinse station name; informational.
    rinse_station_pump: Rinse station pump; informational. The pump must be running.
  """

  operation: Optional[str]
  rinse_station_name: Optional[str]
  rinse_station_pump: RinseStationPump


@dataclass(frozen=True, kw_only=True)
class RunFlowCytometerFluidicsForDurationCommand(WorklistCommand):
  """Runs the cytometer fluidics for a time (older models).

  It has the members of :class:`WorklistCommand` and the following.

  Attributes:
    fluidics_duration: Run time in s.
    speed: Fluidics speed; only older cytometer models use it.
  """

  fluidics_duration: int
  speed: FlowCytometerSpeed


@dataclass(frozen=True, kw_only=True)
class SampleContinuouslyCommand(WorklistCommand):
  """Keeps the probe down at a station while the pump runs for a time.

  It has the members of :class:`WorklistCommand` and the following.

  Attributes:
    fast_move: True for the fast XY move.
    pump_direction: Pump direction.
    pump_speed: Pump speed in hundredths of rpm.
    sip_time: Time in ms the pump runs with the probe at ZLow.
    x: Target X, a sampler coordinate in mm.
    y: Target Y, a sampler coordinate in mm.
    z_high: Z the probe returns to, a sampler Z coordinate in mm (larger is higher).
    z_low: Z the probe is lowered to, a sampler Z coordinate in mm (larger is higher).
  """

  fast_move: bool
  pump_direction: PumpDirection
  pump_speed: int
  sip_time: int
  x: decimal.Decimal
  y: decimal.Decimal
  z_high: decimal.Decimal
  z_low: decimal.Decimal


@dataclass(frozen=True, kw_only=True)
class SampleWellCommand(SampleCommand):
  """Samples one well: one dip of the probe.

  It has the members of :class:`SampleCommand` and the following.

  Attributes:
    column: Well column number; informational.
    row: Well row label such as A; informational.
    well_type: Well type label such as Sample; informational.
  """

  column: int
  row: Optional[str]
  well_type: Optional[str]


@dataclass(frozen=True, kw_only=True)
class SampleTestTubeCommand(SampleWellCommand):
  """Samples a tube until an event count or a time is reached.

  It has the members of :class:`SampleWellCommand` and the following.

  Attributes:
    event_threshold: Event count that ends the command early; 2147483647 for no limit.
  """

  event_threshold: int


@dataclass(frozen=True, kw_only=True)
class SampleVialCommand(SampleTestTubeCommand):
  """Samples a microcentrifuge tube.

  It has the members of :class:`SampleTestTubeCommand` and the following.

  Attributes:
    z_tolerance: Z tolerance in mm; informational for the controller.
  """

  z_tolerance: decimal.Decimal


@dataclass(frozen=True, kw_only=True)
class SampleWellContinuouslyCommand(SampleWellCommand):
  """Samples one well with the pump started and stopped around the dip.

  It has the members of :class:`SampleWellCommand` and the following.

  Attributes:
    pump_direction: Pump direction while the probe is down.
    pump_speed: Pump speed in hundredths of rpm while the probe is down.
    z_high: Z the probe returns to, a sampler Z coordinate in mm (larger is higher).
    z_low: Z the probe is lowered to while the pump runs, a sampler Z coordinate in mm (larger is
      higher).
  """

  pump_direction: PumpDirection
  pump_speed: int
  z_high: decimal.Decimal
  z_low: decimal.Decimal


@dataclass(frozen=True, kw_only=True)
class ShutdownFlowCytometerCommand(WorklistCommand):
  """Shuts the cytometer fluidics down.

  It has the members of :class:`WorklistCommand` and no others.
  """


@dataclass(frozen=True, kw_only=True)
class StartFCAutomaterCommand(WorklistCommand):
  """Obsolete automation command; the controller has no executor for it.

  It has the members of :class:`WorklistCommand` and the following.

  Attributes:
    experiment_id: Obsolete; not used.
    plate_number: Obsolete; not used.
  """

  experiment_id: uuid.UUID
  plate_number: int


@dataclass(frozen=True, kw_only=True)
class StartFCAutomaterForPrimeCommand(StartFCAutomaterCommand):
  """Obsolete automation command; the controller has no executor for it.

  It has the members of :class:`StartFCAutomaterCommand` and no others.
  """


@dataclass(frozen=True, kw_only=True)
class Threshold:
  """Event trigger threshold on one cytometer channel.

  Attributes:
    id: Cytometer channel the threshold applies to.
    maximum: Upper threshold value; not used by the controller.
    minimum: Lower threshold value.
  """

  id: int
  maximum: int
  minimum: int


@dataclass(frozen=True, kw_only=True)
class StartFlowCytometerFluidicsCommand(WorklistCommand):
  """Starts the cytometer fluidics, optionally ready for acquisition.

  It has the members of :class:`WorklistCommand` and the following.

  Attributes:
    enabled_lasers: Lasers to use.
    fcs_metadata: Key and value pairs written into the FCS file as IC$<key> keywords; must not be
      None, keys must be unique.
    laser_configuration: Laser configuration; only older cytometer models use it.
    prepare_for_acquisition: True to start the fluidics ready for acquisition.
    speed: Fluidics speed; only older cytometer models use it.
    thresholds: Event trigger thresholds; must not be None. Only ID and Minimum are used.
    user_name: Operator name written into the FCS file.
  """

  enabled_lasers: EnabledLasers
  fcs_metadata: Optional[List[Tuple[Optional[str], Optional[str]]]]
  laser_configuration: LaserConfiguration
  prepare_for_acquisition: bool
  speed: FlowCytometerSpeed
  thresholds: Optional[List[Optional[Threshold]]]
  user_name: Optional[str]


@dataclass(frozen=True, kw_only=True)
class StartMatryoshkaFluidicsCommand(WorklistCommand):
  """Loads the plate and starts the cytometer fluidics for acquisition.

  It has the members of :class:`WorklistCommand` and the following.

  Attributes:
    enabled_lasers: Lasers to use.
    fcs_metadata: Key and value pairs written into the FCS file; must not be None, keys must be
      unique.
    laser_configuration: Laser configuration; only older cytometer models use it.
    speed: Fluidics speed; only older cytometer models use it.
    thresholds: Event trigger thresholds; must not be None. Only ID and Minimum are used.
    user_name: Operator name written into the FCS file.
  """

  enabled_lasers: EnabledLasers
  fcs_metadata: Optional[List[Tuple[Optional[str], Optional[str]]]]
  laser_configuration: LaserConfiguration
  speed: FlowCytometerSpeed
  thresholds: Optional[List[Optional[Threshold]]]
  user_name: Optional[str]


@dataclass(frozen=True, kw_only=True)
class StartPumpCommand(WorklistCommand):
  """Starts the peristaltic pump.

  It has the members of :class:`WorklistCommand` and the following.

  Attributes:
    direction: Pump direction.
    speed: Pump speed in hundredths of rpm.
  """

  direction: PumpDirection
  speed: int


@dataclass(frozen=True, kw_only=True)
class StartShakerCommand(WorklistCommand):
  """Starts the shaker.

  It has the members of :class:`WorklistCommand` and the following.

  Attributes:
    rpm: Shaker speed in rpm.
  """

  rpm: int


@dataclass(frozen=True, kw_only=True)
class StopFCAutomaterCommand(WorklistCommand):
  """Obsolete automation command; the controller has no executor for it.

  It has the members of :class:`WorklistCommand` and no others.
  """


@dataclass(frozen=True, kw_only=True)
class StopFlowCytometerFluidicsCommand(WorklistCommand):
  """Stops the cytometer fluidics.

  It has the members of :class:`WorklistCommand` and no others.
  """


@dataclass(frozen=True, kw_only=True)
class StopGateAddFCSToPlateCommand(WorklistCommand):
  """Obsolete command; the controller has no executor for it.

  It has the members of :class:`WorklistCommand` and the following.

  Attributes:
    dst_name: Obsolete; not used.
    experiment_id: Obsolete; not used (spelled Id here).
    plate_number: Obsolete; not used.
    zip_path: Obsolete; not used.
  """

  dst_name: Optional[str]
  experiment_id: uuid.UUID
  plate_number: int
  zip_path: Optional[str]


@dataclass(frozen=True, kw_only=True)
class StopGateSampleWellCommand(SampleWellCommand):
  """Obsolete command; the controller has no executor for it.

  It has the members of :class:`SampleWellCommand` and no others.
  """


@dataclass(frozen=True, kw_only=True)
class StopPumpCommand(WorklistCommand):
  """Stops the peristaltic pump.

  It has the members of :class:`WorklistCommand` and no others.
  """


@dataclass(frozen=True, kw_only=True)
class StopShakerCommand(WorklistCommand):
  """Stops the shaker.

  It has the members of :class:`WorklistCommand` and no others.
  """


@dataclass(frozen=True, kw_only=True)
class TimedContinuousRinseCommand(WorklistCommand):
  """Keeps the probe down at a rinse station for a time.

  It has the members of :class:`WorklistCommand` and the following.

  Attributes:
    rinse_description: Description; informational.
    rinse_duration: Time in s the probe stays at ZDown.
    rinse_station_name: Rinse station name; informational.
    rinse_station_pump: Rinse station pump; informational.
    sample_speed: XY move speed.
    up_time: Extra wait in ms after the rinse, when positive.
    x: Target X, a sampler coordinate in mm.
    y: Target Y, a sampler coordinate in mm.
    z_down: Z during the rinse, a sampler Z coordinate in mm (larger is higher).
    z_up: Z before and after the rinse, a sampler Z coordinate in mm (larger is higher).
  """

  rinse_description: Optional[str]
  rinse_duration: int
  rinse_station_name: Optional[str]
  rinse_station_pump: RinseStationPump
  sample_speed: SampleSpeed
  up_time: int
  x: decimal.Decimal
  y: decimal.Decimal
  z_down: decimal.Decimal
  z_up: decimal.Decimal


@dataclass(frozen=True, kw_only=True)
class TimedShakeCommand(WorklistCommand):
  """Shakes the plate for a time with the probe waiting at a rinse position.

  It has the members of :class:`WorklistCommand` and the following.

  Attributes:
    fast_move: True for the fast XY move.
    rinse_station_pump: Rinse station pump; informational.
    rinse_x: X where the probe waits during the shake, a sampler coordinate in mm.
    rinse_y: Y where the probe waits, a sampler coordinate in mm.
    rinse_z_down: Z during the shake, a sampler Z coordinate in mm (larger is higher).
    rinse_z_up: Z before and after the shake, a sampler Z coordinate in mm (larger is higher).
    rpm: Shaker speed in rpm (spelled Rpm here, RPM elsewhere).
    shake_duration: Shake time in s.
  """

  fast_move: bool
  rinse_station_pump: RinseStationPump
  rinse_x: decimal.Decimal
  rinse_y: decimal.Decimal
  rinse_z_down: decimal.Decimal
  rinse_z_up: decimal.Decimal
  rpm: int
  shake_duration: int


@dataclass(frozen=True, kw_only=True)
class TimedShakeAtLocationCommand(TimedShakeCommand):
  """TimedShakeCommand with a location name; the controller has no executor for it.

  It has the members of :class:`TimedShakeCommand` and the following.

  Attributes:
    location_name: Location name; informational.
  """

  location_name: Optional[str]


@dataclass(frozen=True, kw_only=True)
class ValidateDeckHeightCommand(WorklistCommand):
  """Checks the deck height, or that a tube is present, by contact sensing.

  It has the members of :class:`WorklistCommand` and the following.

  Attributes:
    is_microfuge_tube_check: False: load the plate rail, move fast and require contact within ZLow
      +/- ZTolerance; True: move slowly and require contact between ZLow + 0.2 and ZLow +
      ZTolerance.
    x: X of the test point, a sampler coordinate in mm.
    y: Y of the test point, a sampler coordinate in mm.
    z_high: Upper Z of the contact search, a sampler Z coordinate in mm (larger is higher).
    z_low: Lower Z of the search and the expected contact height, a sampler Z coordinate in mm
      (larger is higher).
    z_tolerance: Allowed deviation in mm.
  """

  is_microfuge_tube_check: bool
  x: decimal.Decimal
  y: decimal.Decimal
  z_high: decimal.Decimal
  z_low: decimal.Decimal
  z_tolerance: decimal.Decimal


@dataclass(frozen=True, kw_only=True)
class Worklist:
  """An ordered list of commands the controller runs on its own, and the run's results.

  Attributes:
    all_commands_completed: Set by the controller: True when the run reached the end and the last
      command completed.
    client_time_offset: Set by the controller: client time from controller_connect minus the
      controller clock.
    commands: Commands in execution order; must hold at least one.
    device_id: System ID of the instrument; informational.
    devices_to_monitor: Devices whose failure stops the run; empty for all.
    end_time: Set by the controller: 1900-01-01 while running, its clock at the end.
    error: Set by the controller: the error that stopped the run, else empty.
    execution_notes: Set by the controller: Estimate=<total s>|<device>=<model>|...
    experiment_id: When set, worklist_finalize archives the run.
    experiment_name: Experiment name, copied into the archive.
    fcs_data_written: Set by the controller: number of values sent through the event data callbacks.
    fcs_file_crc: Set by the controller: CRC-32 of the controller's FCS file.
    fcs_file_name: Set by the controller: name of its FCS file.
    fluidics_run_time_minutes: Set by the controller: minutes the cytometer fluidics ran.
    id: Replaced by the controller with a new identifier when the run starts.
    leave_fluidics_running_on_complete: False to stop the cytometer fluidics when the run ends.
    name: Name; Prime makes the run stop when the client disconnects, and Test Tube or Vial in it
      skips homing the shaker.
    plate_id: Plate identifier; when set and not all zeros the shaker is homed before the run.
    plate_number: Plate number, copied into the archive.
    pump_run_time_minutes: Set by the controller: minutes the pump ran.
    start_time: Set by the controller: its clock when the run started.
    stop_reason: Set by the controller when the run ends.
  """

  all_commands_completed: bool
  client_time_offset: datetime.timedelta
  commands: Optional[List[Optional[WorklistCommand]]]
  device_id: Optional[str]
  devices_to_monitor: Optional[List[DeviceKind]]
  end_time: datetime.datetime
  error: Optional[str]
  execution_notes: Optional[str]
  experiment_id: Optional[uuid.UUID]
  experiment_name: Optional[str]
  fcs_data_written: int
  fcs_file_crc: int
  fcs_file_name: Optional[str]
  fluidics_run_time_minutes: decimal.Decimal
  id: uuid.UUID
  leave_fluidics_running_on_complete: bool
  name: Optional[str]
  plate_id: Optional[uuid.UUID]
  plate_number: Optional[str]
  pump_run_time_minutes: decimal.Decimal
  start_time: datetime.datetime
  stop_reason: WorklistStopReason


@dataclass(frozen=True, kw_only=True)
class ControllerServiceStatus:
  """Status of the controller service, as the watchdog reports it.

  Attributes:
    is_running: Whether the controller service runs.
    is_updating: Whether the watchdog is updating the controller.
  """

  is_running: bool
  is_updating: bool


@dataclass(frozen=True, kw_only=True)
class WatchdogException:
  """Detail of every watchdog fault: the exception the watchdog caught.

  Attributes:
    inner_exception: The inner exception, or None.
    message: Message of the exception.
    type: .NET type name of the exception.
  """

  inner_exception: Optional[WatchdogException]
  message: Optional[str]
  type: Optional[str]


def _encode_worklist_command(value: WorklistCommand) -> Dict[str, Any]:
  """Return the members of a WorklistCommand by wire name."""
  return {
    "Category": value.category,
    "Completed": value.completed,
    "CountdownDuration": value.countdown_duration,
    "Description": value.description,
    "EndTime": value.end_time,
    "EstimatedDuration": value.estimated_duration,
    "ExecutionNotes": value.execution_notes,
    "ID": value.id,
    "Name": value.name,
    "StartTime": value.start_time,
  }


def _encode_sample_command(value: SampleCommand) -> Dict[str, Any]:
  """Return the members of a SampleCommand by wire name."""
  return {
    **_encode_worklist_command(value),
    "FastMove": value.fast_move,
    "SipTime": value.sip_time,
    "UpTime": value.up_time,
    "X": value.x,
    "Y": value.y,
    "ZDown": value.z_down,
    "ZUp": value.z_up,
  }


def _encode_bead_sip_command(value: BeadSipCommand) -> Dict[str, Any]:
  """Return the members of a BeadSipCommand by wire name."""
  return {**_encode_sample_command(value), "SipDescription": value.sip_description}


def _encode_begin_session_fc_automater_command(
  value: BeginSessionFCAutomaterCommand,
) -> Dict[str, Any]:
  """Return the members of a BeginSessionFCAutomaterCommand by wire name."""
  return {
    **_encode_worklist_command(value),
    "Alias": value.alias,
    "FileNameToDelete": value.file_name_to_delete,
  }


def _encode_clean_flow_cytometer_command(value: CleanFlowCytometerCommand) -> Dict[str, Any]:
  """Return the members of a CleanFlowCytometerCommand by wire name."""
  return {**_encode_worklist_command(value), "CleanMethod": value.clean_method}


def _encode_combined_shake_rinse_command(value: CombinedShakeRinseCommand) -> Dict[str, Any]:
  """Return the members of a CombinedShakeRinseCommand by wire name."""
  return {
    **_encode_worklist_command(value),
    "FastMove": value.fast_move,
    "RPM": value.rpm,
    "RinseStationPump": value.rinse_station_pump,
    "RinseX": value.rinse_x,
    "RinseY": value.rinse_y,
    "RinseZDown": value.rinse_z_down,
    "RinseZUp": value.rinse_z_up,
    "ShakeDuration": value.shake_duration,
    "SipTime": value.sip_time,
    "UpTime": value.up_time,
  }


def _encode_combined_shake_rinse_at_location_command(
  value: CombinedShakeRinseAtLocationCommand,
) -> Dict[str, Any]:
  """Return the members of a CombinedShakeRinseAtLocationCommand by wire name."""
  return {**_encode_combined_shake_rinse_command(value), "LocationName": value.location_name}


def _encode_compensation_entry(value: CompensationEntry) -> Dict[str, Any]:
  """Return the members of a CompensationEntry by wire name."""
  return {"Amount": value.amount, "Param1": value.param1, "Param2": value.param2}


def _encode_completed_worklist(value: CompletedWorklist) -> Dict[str, Any]:
  """Return the members of a CompletedWorklist by wire name."""
  return {
    "ClientTimeOffset": value.client_time_offset,
    "ExperimentID": value.experiment_id,
    "ExperimentName": value.experiment_name,
    "HasFCSFile": value.has_fcs_file,
    "ID": value.id,
    "PlateID": value.plate_id,
    "PlateNumber": value.plate_number,
    "RunComplete": value.run_complete,
    "RunStart": value.run_start,
  }


def _encode_connection_info(value: ConnectionInfo) -> Dict[str, Any]:
  """Return the members of a ConnectionInfo by wire name."""
  return {
    "Comments": value.comments,
    "IsClientAlreadyConnected": value.is_client_already_connected,
    "Version": value.version,
  }


def _encode_contact_z_result(value: ContactZResult) -> Dict[str, Any]:
  """Return the members of a ContactZResult by wire name."""
  return {
    "ContactTime": value.contact_time,
    "ContactZLocation": value.contact_z_location,
    "ErrorMessage": value.error_message,
    "FoundContact": value.found_contact,
    "HasError": value.has_error,
    "ID": value.id,
    "IsSimulated": value.is_simulated,
    "ZLow": value.z_low,
  }


def _encode_controller_service_exception(value: ControllerServiceException) -> Dict[str, Any]:
  """Return the members of a ControllerServiceException by wire name."""
  return {"InnerException": value.inner_exception, "Message": value.message, "Type": value.type}


def _encode_cycling_rinse_command(value: CyclingRinseCommand) -> Dict[str, Any]:
  """Return the members of a CyclingRinseCommand by wire name."""
  return {
    **_encode_sample_command(value),
    "Operation": value.operation,
    "RinseDuration": value.rinse_duration,
    "RinseStationName": value.rinse_station_name,
    "RinseStationPump": value.rinse_station_pump,
  }


def _encode_device_alert_message(value: DeviceAlertMessage) -> Dict[str, Any]:
  """Return the members of a DeviceAlertMessage by wire name."""
  return {"Alert": value.alert, "Kind": value.kind}


def _encode_device_configuration(value: DeviceConfiguration) -> Dict[str, Any]:
  """Return the members of a DeviceConfiguration by wire name."""
  return {"Configuration": value.configuration, "IncludeThisDevice": value.include_this_device}


def _encode_device_message(value: DeviceMessage) -> Dict[str, Any]:
  """Return the members of a DeviceMessage by wire name."""
  return {"Detail": value.detail, "Message": value.message, "MessageLevel": value.message_level}


def _encode_device_info(value: DeviceInfo) -> Dict[str, Any]:
  """Return the members of a DeviceInfo by wire name."""
  return {
    "Kind": value.kind,
    "Messages": value.messages,
    "Model": value.model,
    "State": value.state,
  }


def _encode_device_reset_options(value: DeviceResetOptions) -> Dict[str, Any]:
  """Return the members of a DeviceResetOptions by wire name."""
  return {
    "IsInDevMode": value.is_in_dev_mode,
    "IsSORPC6": value.is_sorpc6,
    "OverrideLLDPercentage": value.override_lld_percentage,
    "OverrideZArmAdditionalContactDepth": value.override_z_arm_additional_contact_depth,
    "OverrideZArmPercentage": value.override_z_arm_percentage,
    "PlateRailRC": value.plate_rail_rc,
  }


def _encode_end_session_fc_automater_command(value: EndSessionFCAutomaterCommand) -> Dict[str, Any]:
  """Return the members of a EndSessionFCAutomaterCommand by wire name."""
  return _encode_worklist_command(value)


def _encode_flow_cytometer_acquire_command(value: FlowCytometerAcquireCommand) -> Dict[str, Any]:
  """Return the members of a FlowCytometerAcquireCommand by wire name."""
  return {
    **_encode_worklist_command(value),
    "AcquistionDelay": value.acquistion_delay,
    "EstimatedAcquisitionTime": value.estimated_acquisition_time,
    "EventMaximum": value.event_maximum,
    "EventThreshold": value.event_threshold,
  }


def _encode_flow_cytometer_stop_acquire_command(
  value: FlowCytometerStopAcquireCommand,
) -> Dict[str, Any]:
  """Return the members of a FlowCytometerStopAcquireCommand by wire name."""
  return _encode_worklist_command(value)


def _encode_timed_cycling_rinse_command(value: TimedCyclingRinseCommand) -> Dict[str, Any]:
  """Return the members of a TimedCyclingRinseCommand by wire name."""
  return {
    **_encode_worklist_command(value),
    "FastMove": value.fast_move,
    "RinseDescription": value.rinse_description,
    "RinseDuration": value.rinse_duration,
    "RinseStationName": value.rinse_station_name,
    "RinseStationPump": value.rinse_station_pump,
    "SipTime": value.sip_time,
    "UpTime": value.up_time,
    "X": value.x,
    "Y": value.y,
    "ZDown": value.z_down,
    "ZUp": value.z_up,
  }


def _encode_flush_command(value: FlushCommand) -> Dict[str, Any]:
  """Return the members of a FlushCommand by wire name."""
  return _encode_timed_cycling_rinse_command(value)


def _encode_flush_matryoshka_command(value: FlushMatryoshkaCommand) -> Dict[str, Any]:
  """Return the members of a FlushMatryoshkaCommand by wire name."""
  return _encode_flush_command(value)


def _encode_get_fc_automater_fcs_data_command(
  value: GetFCAutomaterFCSDataCommand,
) -> Dict[str, Any]:
  """Return the members of a GetFCAutomaterFCSDataCommand by wire name."""
  return {
    **_encode_worklist_command(value),
    "ExperimentID": value.experiment_id,
    "PlateNumber": value.plate_number,
  }


def _encode_movement_range(value: MovementRange) -> Dict[str, Any]:
  """Return the members of a MovementRange by wire name."""
  return {"Max": value.max, "Min": value.min}


def _encode_rinse_command(value: RinseCommand) -> Dict[str, Any]:
  """Return the members of a RinseCommand by wire name."""
  return {
    **_encode_sample_command(value),
    "Operation": value.operation,
    "RinseStationName": value.rinse_station_name,
    "RinseStationPump": value.rinse_station_pump,
  }


def _encode_run_flow_cytometer_fluidics_for_duration_command(
  value: RunFlowCytometerFluidicsForDurationCommand,
) -> Dict[str, Any]:
  """Return the members of a RunFlowCytometerFluidicsForDurationCommand by wire name."""
  return {
    **_encode_worklist_command(value),
    "FluidicsDuration": value.fluidics_duration,
    "Speed": value.speed,
  }


def _encode_sample_continuously_command(value: SampleContinuouslyCommand) -> Dict[str, Any]:
  """Return the members of a SampleContinuouslyCommand by wire name."""
  return {
    **_encode_worklist_command(value),
    "FastMove": value.fast_move,
    "PumpDirection": value.pump_direction,
    "PumpSpeed": value.pump_speed,
    "SipTime": value.sip_time,
    "X": value.x,
    "Y": value.y,
    "ZHigh": value.z_high,
    "ZLow": value.z_low,
  }


def _encode_sample_well_command(value: SampleWellCommand) -> Dict[str, Any]:
  """Return the members of a SampleWellCommand by wire name."""
  return {
    **_encode_sample_command(value),
    "Column": value.column,
    "Row": value.row,
    "WellType": value.well_type,
  }


def _encode_sample_test_tube_command(value: SampleTestTubeCommand) -> Dict[str, Any]:
  """Return the members of a SampleTestTubeCommand by wire name."""
  return {**_encode_sample_well_command(value), "EventThreshold": value.event_threshold}


def _encode_sample_vial_command(value: SampleVialCommand) -> Dict[str, Any]:
  """Return the members of a SampleVialCommand by wire name."""
  return {**_encode_sample_test_tube_command(value), "ZTolerance": value.z_tolerance}


def _encode_sample_well_continuously_command(
  value: SampleWellContinuouslyCommand,
) -> Dict[str, Any]:
  """Return the members of a SampleWellContinuouslyCommand by wire name."""
  return {
    **_encode_sample_well_command(value),
    "PumpDirection": value.pump_direction,
    "PumpSpeed": value.pump_speed,
    "ZHigh": value.z_high,
    "ZLow": value.z_low,
  }


def _encode_shutdown_flow_cytometer_command(value: ShutdownFlowCytometerCommand) -> Dict[str, Any]:
  """Return the members of a ShutdownFlowCytometerCommand by wire name."""
  return _encode_worklist_command(value)


def _encode_start_fc_automater_command(value: StartFCAutomaterCommand) -> Dict[str, Any]:
  """Return the members of a StartFCAutomaterCommand by wire name."""
  return {
    **_encode_worklist_command(value),
    "ExperimentID": value.experiment_id,
    "PlateNumber": value.plate_number,
  }


def _encode_start_fc_automater_for_prime_command(
  value: StartFCAutomaterForPrimeCommand,
) -> Dict[str, Any]:
  """Return the members of a StartFCAutomaterForPrimeCommand by wire name."""
  return _encode_start_fc_automater_command(value)


def _encode_threshold(value: Threshold) -> Dict[str, Any]:
  """Return the members of a Threshold by wire name."""
  return {"ID": value.id, "Maximum": value.maximum, "Minimum": value.minimum}


def _encode_start_flow_cytometer_fluidics_command(
  value: StartFlowCytometerFluidicsCommand,
) -> Dict[str, Any]:
  """Return the members of a StartFlowCytometerFluidicsCommand by wire name."""
  return {
    **_encode_worklist_command(value),
    "EnabledLasers": value.enabled_lasers,
    "FcsMetadata": value.fcs_metadata,
    "LaserConfiguration": value.laser_configuration,
    "PrepareForAcquisition": value.prepare_for_acquisition,
    "Speed": value.speed,
    "Thresholds": value.thresholds,
    "UserName": value.user_name,
  }


def _encode_start_matryoshka_fluidics_command(
  value: StartMatryoshkaFluidicsCommand,
) -> Dict[str, Any]:
  """Return the members of a StartMatryoshkaFluidicsCommand by wire name."""
  return {
    **_encode_worklist_command(value),
    "EnabledLasers": value.enabled_lasers,
    "FcsMetadata": value.fcs_metadata,
    "LaserConfiguration": value.laser_configuration,
    "Speed": value.speed,
    "Thresholds": value.thresholds,
    "UserName": value.user_name,
  }


def _encode_start_pump_command(value: StartPumpCommand) -> Dict[str, Any]:
  """Return the members of a StartPumpCommand by wire name."""
  return {**_encode_worklist_command(value), "Direction": value.direction, "Speed": value.speed}


def _encode_start_shaker_command(value: StartShakerCommand) -> Dict[str, Any]:
  """Return the members of a StartShakerCommand by wire name."""
  return {**_encode_worklist_command(value), "RPM": value.rpm}


def _encode_stop_fc_automater_command(value: StopFCAutomaterCommand) -> Dict[str, Any]:
  """Return the members of a StopFCAutomaterCommand by wire name."""
  return _encode_worklist_command(value)


def _encode_stop_flow_cytometer_fluidics_command(
  value: StopFlowCytometerFluidicsCommand,
) -> Dict[str, Any]:
  """Return the members of a StopFlowCytometerFluidicsCommand by wire name."""
  return _encode_worklist_command(value)


def _encode_stop_gate_add_fcs_to_plate_command(
  value: StopGateAddFCSToPlateCommand,
) -> Dict[str, Any]:
  """Return the members of a StopGateAddFCSToPlateCommand by wire name."""
  return {
    **_encode_worklist_command(value),
    "DstName": value.dst_name,
    "ExperimentId": value.experiment_id,
    "PlateNumber": value.plate_number,
    "ZipPath": value.zip_path,
  }


def _encode_stop_gate_sample_well_command(value: StopGateSampleWellCommand) -> Dict[str, Any]:
  """Return the members of a StopGateSampleWellCommand by wire name."""
  return _encode_sample_well_command(value)


def _encode_stop_pump_command(value: StopPumpCommand) -> Dict[str, Any]:
  """Return the members of a StopPumpCommand by wire name."""
  return _encode_worklist_command(value)


def _encode_stop_shaker_command(value: StopShakerCommand) -> Dict[str, Any]:
  """Return the members of a StopShakerCommand by wire name."""
  return _encode_worklist_command(value)


def _encode_timed_continuous_rinse_command(value: TimedContinuousRinseCommand) -> Dict[str, Any]:
  """Return the members of a TimedContinuousRinseCommand by wire name."""
  return {
    **_encode_worklist_command(value),
    "RinseDescription": value.rinse_description,
    "RinseDuration": value.rinse_duration,
    "RinseStationName": value.rinse_station_name,
    "RinseStationPump": value.rinse_station_pump,
    "SampleSpeed": value.sample_speed,
    "UpTime": value.up_time,
    "X": value.x,
    "Y": value.y,
    "ZDown": value.z_down,
    "ZUp": value.z_up,
  }


def _encode_timed_shake_command(value: TimedShakeCommand) -> Dict[str, Any]:
  """Return the members of a TimedShakeCommand by wire name."""
  return {
    **_encode_worklist_command(value),
    "FastMove": value.fast_move,
    "RinseStationPump": value.rinse_station_pump,
    "RinseX": value.rinse_x,
    "RinseY": value.rinse_y,
    "RinseZDown": value.rinse_z_down,
    "RinseZUp": value.rinse_z_up,
    "Rpm": value.rpm,
    "ShakeDuration": value.shake_duration,
  }


def _encode_timed_shake_at_location_command(value: TimedShakeAtLocationCommand) -> Dict[str, Any]:
  """Return the members of a TimedShakeAtLocationCommand by wire name."""
  return {**_encode_timed_shake_command(value), "LocationName": value.location_name}


def _encode_validate_deck_height_command(value: ValidateDeckHeightCommand) -> Dict[str, Any]:
  """Return the members of a ValidateDeckHeightCommand by wire name."""
  return {
    **_encode_worklist_command(value),
    "IsMicrofugeTubeCheck": value.is_microfuge_tube_check,
    "X": value.x,
    "Y": value.y,
    "ZHigh": value.z_high,
    "ZLow": value.z_low,
    "ZTolerance": value.z_tolerance,
  }


def _encode_worklist(value: Worklist) -> Dict[str, Any]:
  """Return the members of a Worklist by wire name."""
  return {
    "AllCommandsCompleted": value.all_commands_completed,
    "ClientTimeOffset": value.client_time_offset,
    "Commands": value.commands,
    "DeviceID": value.device_id,
    "DevicesToMonitor": value.devices_to_monitor,
    "EndTime": value.end_time,
    "Error": value.error,
    "ExecutionNotes": value.execution_notes,
    "ExperimentID": value.experiment_id,
    "ExperimentName": value.experiment_name,
    "FCSDataWritten": value.fcs_data_written,
    "FCSFileCRC": value.fcs_file_crc,
    "FCSFileName": value.fcs_file_name,
    "FluidicsRunTimeMinutes": value.fluidics_run_time_minutes,
    "ID": value.id,
    "LeaveFluidicsRunningOnComplete": value.leave_fluidics_running_on_complete,
    "Name": value.name,
    "PlateID": value.plate_id,
    "PlateNumber": value.plate_number,
    "PumpRunTimeMinutes": value.pump_run_time_minutes,
    "StartTime": value.start_time,
    "StopReason": value.stop_reason,
  }


def _encode_controller_service_status(value: ControllerServiceStatus) -> Dict[str, Any]:
  """Return the members of a ControllerServiceStatus by wire name."""
  return {"isRunning": value.is_running, "isUpdating": value.is_updating}


def _encode_watchdog_exception(value: WatchdogException) -> Dict[str, Any]:
  """Return the members of a WatchdogException by wire name."""
  return {"InnerException": value.inner_exception, "Message": value.message, "Type": value.type}


def _decode_worklist_command(values: Mapping[str, Any]) -> WorklistCommand:
  """Build a WorklistCommand from its converted members, by wire name."""
  return WorklistCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
  )


def _decode_sample_command(values: Mapping[str, Any]) -> SampleCommand:
  """Build a SampleCommand from its converted members, by wire name."""
  return SampleCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    fast_move=values["FastMove"],
    sip_time=values["SipTime"],
    up_time=values["UpTime"],
    x=values["X"],
    y=values["Y"],
    z_down=values["ZDown"],
    z_up=values["ZUp"],
  )


def _decode_bead_sip_command(values: Mapping[str, Any]) -> BeadSipCommand:
  """Build a BeadSipCommand from its converted members, by wire name."""
  return BeadSipCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    fast_move=values["FastMove"],
    sip_time=values["SipTime"],
    up_time=values["UpTime"],
    x=values["X"],
    y=values["Y"],
    z_down=values["ZDown"],
    z_up=values["ZUp"],
    sip_description=values["SipDescription"],
  )


def _decode_begin_session_fc_automater_command(
  values: Mapping[str, Any],
) -> BeginSessionFCAutomaterCommand:
  """Build a BeginSessionFCAutomaterCommand from its converted members, by wire name."""
  return BeginSessionFCAutomaterCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    alias=values["Alias"],
    file_name_to_delete=values["FileNameToDelete"],
  )


def _decode_clean_flow_cytometer_command(values: Mapping[str, Any]) -> CleanFlowCytometerCommand:
  """Build a CleanFlowCytometerCommand from its converted members, by wire name."""
  return CleanFlowCytometerCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    clean_method=values["CleanMethod"],
  )


def _decode_combined_shake_rinse_command(values: Mapping[str, Any]) -> CombinedShakeRinseCommand:
  """Build a CombinedShakeRinseCommand from its converted members, by wire name."""
  return CombinedShakeRinseCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    fast_move=values["FastMove"],
    rpm=values["RPM"],
    rinse_station_pump=values["RinseStationPump"],
    rinse_x=values["RinseX"],
    rinse_y=values["RinseY"],
    rinse_z_down=values["RinseZDown"],
    rinse_z_up=values["RinseZUp"],
    shake_duration=values["ShakeDuration"],
    sip_time=values["SipTime"],
    up_time=values["UpTime"],
  )


def _decode_combined_shake_rinse_at_location_command(
  values: Mapping[str, Any],
) -> CombinedShakeRinseAtLocationCommand:
  """Build a CombinedShakeRinseAtLocationCommand from its converted members, by wire name."""
  return CombinedShakeRinseAtLocationCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    fast_move=values["FastMove"],
    rpm=values["RPM"],
    rinse_station_pump=values["RinseStationPump"],
    rinse_x=values["RinseX"],
    rinse_y=values["RinseY"],
    rinse_z_down=values["RinseZDown"],
    rinse_z_up=values["RinseZUp"],
    shake_duration=values["ShakeDuration"],
    sip_time=values["SipTime"],
    up_time=values["UpTime"],
    location_name=values["LocationName"],
  )


def _decode_compensation_entry(values: Mapping[str, Any]) -> CompensationEntry:
  """Build a CompensationEntry from its converted members, by wire name."""
  return CompensationEntry(
    amount=values["Amount"], param1=values["Param1"], param2=values["Param2"]
  )


def _decode_completed_worklist(values: Mapping[str, Any]) -> CompletedWorklist:
  """Build a CompletedWorklist from its converted members, by wire name."""
  return CompletedWorklist(
    client_time_offset=values["ClientTimeOffset"],
    experiment_id=values["ExperimentID"],
    experiment_name=values["ExperimentName"],
    has_fcs_file=values["HasFCSFile"],
    id=values["ID"],
    plate_id=values["PlateID"],
    plate_number=values["PlateNumber"],
    run_complete=values["RunComplete"],
    run_start=values["RunStart"],
  )


def _decode_connection_info(values: Mapping[str, Any]) -> ConnectionInfo:
  """Build a ConnectionInfo from its converted members, by wire name."""
  return ConnectionInfo(
    comments=values["Comments"],
    is_client_already_connected=values["IsClientAlreadyConnected"],
    version=values["Version"],
  )


def _decode_contact_z_result(values: Mapping[str, Any]) -> ContactZResult:
  """Build a ContactZResult from its converted members, by wire name."""
  return ContactZResult(
    contact_time=values["ContactTime"],
    contact_z_location=values["ContactZLocation"],
    error_message=values["ErrorMessage"],
    found_contact=values["FoundContact"],
    has_error=values["HasError"],
    id=values["ID"],
    is_simulated=values["IsSimulated"],
    z_low=values["ZLow"],
  )


def _decode_controller_service_exception(values: Mapping[str, Any]) -> ControllerServiceException:
  """Build a ControllerServiceException from its converted members, by wire name."""
  return ControllerServiceException(
    inner_exception=values["InnerException"], message=values["Message"], type=values["Type"]
  )


def _decode_cycling_rinse_command(values: Mapping[str, Any]) -> CyclingRinseCommand:
  """Build a CyclingRinseCommand from its converted members, by wire name."""
  return CyclingRinseCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    fast_move=values["FastMove"],
    sip_time=values["SipTime"],
    up_time=values["UpTime"],
    x=values["X"],
    y=values["Y"],
    z_down=values["ZDown"],
    z_up=values["ZUp"],
    operation=values["Operation"],
    rinse_duration=values["RinseDuration"],
    rinse_station_name=values["RinseStationName"],
    rinse_station_pump=values["RinseStationPump"],
  )


def _decode_device_alert_message(values: Mapping[str, Any]) -> DeviceAlertMessage:
  """Build a DeviceAlertMessage from its converted members, by wire name."""
  return DeviceAlertMessage(alert=values["Alert"], kind=values["Kind"])


def _decode_device_configuration(values: Mapping[str, Any]) -> DeviceConfiguration:
  """Build a DeviceConfiguration from its converted members, by wire name."""
  return DeviceConfiguration(
    configuration=values["Configuration"], include_this_device=values["IncludeThisDevice"]
  )


def _decode_device_message(values: Mapping[str, Any]) -> DeviceMessage:
  """Build a DeviceMessage from its converted members, by wire name."""
  return DeviceMessage(
    detail=values["Detail"], message=values["Message"], message_level=values["MessageLevel"]
  )


def _decode_device_info(values: Mapping[str, Any]) -> DeviceInfo:
  """Build a DeviceInfo from its converted members, by wire name."""
  return DeviceInfo(
    kind=values["Kind"], messages=values["Messages"], model=values["Model"], state=values["State"]
  )


def _decode_device_reset_options(values: Mapping[str, Any]) -> DeviceResetOptions:
  """Build a DeviceResetOptions from its converted members, by wire name."""
  return DeviceResetOptions(
    is_in_dev_mode=values["IsInDevMode"],
    is_sorpc6=values["IsSORPC6"],
    override_lld_percentage=values["OverrideLLDPercentage"],
    override_z_arm_additional_contact_depth=values["OverrideZArmAdditionalContactDepth"],
    override_z_arm_percentage=values["OverrideZArmPercentage"],
    plate_rail_rc=values["PlateRailRC"],
  )


def _decode_end_session_fc_automater_command(
  values: Mapping[str, Any],
) -> EndSessionFCAutomaterCommand:
  """Build a EndSessionFCAutomaterCommand from its converted members, by wire name."""
  return EndSessionFCAutomaterCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
  )


def _decode_flow_cytometer_acquire_command(
  values: Mapping[str, Any],
) -> FlowCytometerAcquireCommand:
  """Build a FlowCytometerAcquireCommand from its converted members, by wire name."""
  return FlowCytometerAcquireCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    acquistion_delay=values["AcquistionDelay"],
    estimated_acquisition_time=values["EstimatedAcquisitionTime"],
    event_maximum=values["EventMaximum"],
    event_threshold=values["EventThreshold"],
  )


def _decode_flow_cytometer_stop_acquire_command(
  values: Mapping[str, Any],
) -> FlowCytometerStopAcquireCommand:
  """Build a FlowCytometerStopAcquireCommand from its converted members, by wire name."""
  return FlowCytometerStopAcquireCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
  )


def _decode_timed_cycling_rinse_command(values: Mapping[str, Any]) -> TimedCyclingRinseCommand:
  """Build a TimedCyclingRinseCommand from its converted members, by wire name."""
  return TimedCyclingRinseCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    fast_move=values["FastMove"],
    rinse_description=values["RinseDescription"],
    rinse_duration=values["RinseDuration"],
    rinse_station_name=values["RinseStationName"],
    rinse_station_pump=values["RinseStationPump"],
    sip_time=values["SipTime"],
    up_time=values["UpTime"],
    x=values["X"],
    y=values["Y"],
    z_down=values["ZDown"],
    z_up=values["ZUp"],
  )


def _decode_flush_command(values: Mapping[str, Any]) -> FlushCommand:
  """Build a FlushCommand from its converted members, by wire name."""
  return FlushCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    fast_move=values["FastMove"],
    rinse_description=values["RinseDescription"],
    rinse_duration=values["RinseDuration"],
    rinse_station_name=values["RinseStationName"],
    rinse_station_pump=values["RinseStationPump"],
    sip_time=values["SipTime"],
    up_time=values["UpTime"],
    x=values["X"],
    y=values["Y"],
    z_down=values["ZDown"],
    z_up=values["ZUp"],
  )


def _decode_flush_matryoshka_command(values: Mapping[str, Any]) -> FlushMatryoshkaCommand:
  """Build a FlushMatryoshkaCommand from its converted members, by wire name."""
  return FlushMatryoshkaCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    fast_move=values["FastMove"],
    rinse_description=values["RinseDescription"],
    rinse_duration=values["RinseDuration"],
    rinse_station_name=values["RinseStationName"],
    rinse_station_pump=values["RinseStationPump"],
    sip_time=values["SipTime"],
    up_time=values["UpTime"],
    x=values["X"],
    y=values["Y"],
    z_down=values["ZDown"],
    z_up=values["ZUp"],
  )


def _decode_get_fc_automater_fcs_data_command(
  values: Mapping[str, Any],
) -> GetFCAutomaterFCSDataCommand:
  """Build a GetFCAutomaterFCSDataCommand from its converted members, by wire name."""
  return GetFCAutomaterFCSDataCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    experiment_id=values["ExperimentID"],
    plate_number=values["PlateNumber"],
  )


def _decode_movement_range(values: Mapping[str, Any]) -> MovementRange:
  """Build a MovementRange from its converted members, by wire name."""
  return MovementRange(max=values["Max"], min=values["Min"])


def _decode_rinse_command(values: Mapping[str, Any]) -> RinseCommand:
  """Build a RinseCommand from its converted members, by wire name."""
  return RinseCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    fast_move=values["FastMove"],
    sip_time=values["SipTime"],
    up_time=values["UpTime"],
    x=values["X"],
    y=values["Y"],
    z_down=values["ZDown"],
    z_up=values["ZUp"],
    operation=values["Operation"],
    rinse_station_name=values["RinseStationName"],
    rinse_station_pump=values["RinseStationPump"],
  )


def _decode_run_flow_cytometer_fluidics_for_duration_command(
  values: Mapping[str, Any],
) -> RunFlowCytometerFluidicsForDurationCommand:
  """Build a RunFlowCytometerFluidicsForDurationCommand from its converted members, by wire name."""
  return RunFlowCytometerFluidicsForDurationCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    fluidics_duration=values["FluidicsDuration"],
    speed=values["Speed"],
  )


def _decode_sample_continuously_command(values: Mapping[str, Any]) -> SampleContinuouslyCommand:
  """Build a SampleContinuouslyCommand from its converted members, by wire name."""
  return SampleContinuouslyCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    fast_move=values["FastMove"],
    pump_direction=values["PumpDirection"],
    pump_speed=values["PumpSpeed"],
    sip_time=values["SipTime"],
    x=values["X"],
    y=values["Y"],
    z_high=values["ZHigh"],
    z_low=values["ZLow"],
  )


def _decode_sample_well_command(values: Mapping[str, Any]) -> SampleWellCommand:
  """Build a SampleWellCommand from its converted members, by wire name."""
  return SampleWellCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    fast_move=values["FastMove"],
    sip_time=values["SipTime"],
    up_time=values["UpTime"],
    x=values["X"],
    y=values["Y"],
    z_down=values["ZDown"],
    z_up=values["ZUp"],
    column=values["Column"],
    row=values["Row"],
    well_type=values["WellType"],
  )


def _decode_sample_test_tube_command(values: Mapping[str, Any]) -> SampleTestTubeCommand:
  """Build a SampleTestTubeCommand from its converted members, by wire name."""
  return SampleTestTubeCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    fast_move=values["FastMove"],
    sip_time=values["SipTime"],
    up_time=values["UpTime"],
    x=values["X"],
    y=values["Y"],
    z_down=values["ZDown"],
    z_up=values["ZUp"],
    column=values["Column"],
    row=values["Row"],
    well_type=values["WellType"],
    event_threshold=values["EventThreshold"],
  )


def _decode_sample_vial_command(values: Mapping[str, Any]) -> SampleVialCommand:
  """Build a SampleVialCommand from its converted members, by wire name."""
  return SampleVialCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    fast_move=values["FastMove"],
    sip_time=values["SipTime"],
    up_time=values["UpTime"],
    x=values["X"],
    y=values["Y"],
    z_down=values["ZDown"],
    z_up=values["ZUp"],
    column=values["Column"],
    row=values["Row"],
    well_type=values["WellType"],
    event_threshold=values["EventThreshold"],
    z_tolerance=values["ZTolerance"],
  )


def _decode_sample_well_continuously_command(
  values: Mapping[str, Any],
) -> SampleWellContinuouslyCommand:
  """Build a SampleWellContinuouslyCommand from its converted members, by wire name."""
  return SampleWellContinuouslyCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    fast_move=values["FastMove"],
    sip_time=values["SipTime"],
    up_time=values["UpTime"],
    x=values["X"],
    y=values["Y"],
    z_down=values["ZDown"],
    z_up=values["ZUp"],
    column=values["Column"],
    row=values["Row"],
    well_type=values["WellType"],
    pump_direction=values["PumpDirection"],
    pump_speed=values["PumpSpeed"],
    z_high=values["ZHigh"],
    z_low=values["ZLow"],
  )


def _decode_shutdown_flow_cytometer_command(
  values: Mapping[str, Any],
) -> ShutdownFlowCytometerCommand:
  """Build a ShutdownFlowCytometerCommand from its converted members, by wire name."""
  return ShutdownFlowCytometerCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
  )


def _decode_start_fc_automater_command(values: Mapping[str, Any]) -> StartFCAutomaterCommand:
  """Build a StartFCAutomaterCommand from its converted members, by wire name."""
  return StartFCAutomaterCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    experiment_id=values["ExperimentID"],
    plate_number=values["PlateNumber"],
  )


def _decode_start_fc_automater_for_prime_command(
  values: Mapping[str, Any],
) -> StartFCAutomaterForPrimeCommand:
  """Build a StartFCAutomaterForPrimeCommand from its converted members, by wire name."""
  return StartFCAutomaterForPrimeCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    experiment_id=values["ExperimentID"],
    plate_number=values["PlateNumber"],
  )


def _decode_threshold(values: Mapping[str, Any]) -> Threshold:
  """Build a Threshold from its converted members, by wire name."""
  return Threshold(id=values["ID"], maximum=values["Maximum"], minimum=values["Minimum"])


def _decode_start_flow_cytometer_fluidics_command(
  values: Mapping[str, Any],
) -> StartFlowCytometerFluidicsCommand:
  """Build a StartFlowCytometerFluidicsCommand from its converted members, by wire name."""
  return StartFlowCytometerFluidicsCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    enabled_lasers=values["EnabledLasers"],
    fcs_metadata=values["FcsMetadata"],
    laser_configuration=values["LaserConfiguration"],
    prepare_for_acquisition=values["PrepareForAcquisition"],
    speed=values["Speed"],
    thresholds=values["Thresholds"],
    user_name=values["UserName"],
  )


def _decode_start_matryoshka_fluidics_command(
  values: Mapping[str, Any],
) -> StartMatryoshkaFluidicsCommand:
  """Build a StartMatryoshkaFluidicsCommand from its converted members, by wire name."""
  return StartMatryoshkaFluidicsCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    enabled_lasers=values["EnabledLasers"],
    fcs_metadata=values["FcsMetadata"],
    laser_configuration=values["LaserConfiguration"],
    speed=values["Speed"],
    thresholds=values["Thresholds"],
    user_name=values["UserName"],
  )


def _decode_start_pump_command(values: Mapping[str, Any]) -> StartPumpCommand:
  """Build a StartPumpCommand from its converted members, by wire name."""
  return StartPumpCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    direction=values["Direction"],
    speed=values["Speed"],
  )


def _decode_start_shaker_command(values: Mapping[str, Any]) -> StartShakerCommand:
  """Build a StartShakerCommand from its converted members, by wire name."""
  return StartShakerCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    rpm=values["RPM"],
  )


def _decode_stop_fc_automater_command(values: Mapping[str, Any]) -> StopFCAutomaterCommand:
  """Build a StopFCAutomaterCommand from its converted members, by wire name."""
  return StopFCAutomaterCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
  )


def _decode_stop_flow_cytometer_fluidics_command(
  values: Mapping[str, Any],
) -> StopFlowCytometerFluidicsCommand:
  """Build a StopFlowCytometerFluidicsCommand from its converted members, by wire name."""
  return StopFlowCytometerFluidicsCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
  )


def _decode_stop_gate_add_fcs_to_plate_command(
  values: Mapping[str, Any],
) -> StopGateAddFCSToPlateCommand:
  """Build a StopGateAddFCSToPlateCommand from its converted members, by wire name."""
  return StopGateAddFCSToPlateCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    dst_name=values["DstName"],
    experiment_id=values["ExperimentId"],
    plate_number=values["PlateNumber"],
    zip_path=values["ZipPath"],
  )


def _decode_stop_gate_sample_well_command(values: Mapping[str, Any]) -> StopGateSampleWellCommand:
  """Build a StopGateSampleWellCommand from its converted members, by wire name."""
  return StopGateSampleWellCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    fast_move=values["FastMove"],
    sip_time=values["SipTime"],
    up_time=values["UpTime"],
    x=values["X"],
    y=values["Y"],
    z_down=values["ZDown"],
    z_up=values["ZUp"],
    column=values["Column"],
    row=values["Row"],
    well_type=values["WellType"],
  )


def _decode_stop_pump_command(values: Mapping[str, Any]) -> StopPumpCommand:
  """Build a StopPumpCommand from its converted members, by wire name."""
  return StopPumpCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
  )


def _decode_stop_shaker_command(values: Mapping[str, Any]) -> StopShakerCommand:
  """Build a StopShakerCommand from its converted members, by wire name."""
  return StopShakerCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
  )


def _decode_timed_continuous_rinse_command(
  values: Mapping[str, Any],
) -> TimedContinuousRinseCommand:
  """Build a TimedContinuousRinseCommand from its converted members, by wire name."""
  return TimedContinuousRinseCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    rinse_description=values["RinseDescription"],
    rinse_duration=values["RinseDuration"],
    rinse_station_name=values["RinseStationName"],
    rinse_station_pump=values["RinseStationPump"],
    sample_speed=values["SampleSpeed"],
    up_time=values["UpTime"],
    x=values["X"],
    y=values["Y"],
    z_down=values["ZDown"],
    z_up=values["ZUp"],
  )


def _decode_timed_shake_command(values: Mapping[str, Any]) -> TimedShakeCommand:
  """Build a TimedShakeCommand from its converted members, by wire name."""
  return TimedShakeCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    fast_move=values["FastMove"],
    rinse_station_pump=values["RinseStationPump"],
    rinse_x=values["RinseX"],
    rinse_y=values["RinseY"],
    rinse_z_down=values["RinseZDown"],
    rinse_z_up=values["RinseZUp"],
    rpm=values["Rpm"],
    shake_duration=values["ShakeDuration"],
  )


def _decode_timed_shake_at_location_command(
  values: Mapping[str, Any],
) -> TimedShakeAtLocationCommand:
  """Build a TimedShakeAtLocationCommand from its converted members, by wire name."""
  return TimedShakeAtLocationCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    fast_move=values["FastMove"],
    rinse_station_pump=values["RinseStationPump"],
    rinse_x=values["RinseX"],
    rinse_y=values["RinseY"],
    rinse_z_down=values["RinseZDown"],
    rinse_z_up=values["RinseZUp"],
    rpm=values["Rpm"],
    shake_duration=values["ShakeDuration"],
    location_name=values["LocationName"],
  )


def _decode_validate_deck_height_command(values: Mapping[str, Any]) -> ValidateDeckHeightCommand:
  """Build a ValidateDeckHeightCommand from its converted members, by wire name."""
  return ValidateDeckHeightCommand(
    category=values["Category"],
    completed=values["Completed"],
    countdown_duration=values["CountdownDuration"],
    description=values["Description"],
    end_time=values["EndTime"],
    estimated_duration=values["EstimatedDuration"],
    execution_notes=values["ExecutionNotes"],
    id=values["ID"],
    name=values["Name"],
    start_time=values["StartTime"],
    is_microfuge_tube_check=values["IsMicrofugeTubeCheck"],
    x=values["X"],
    y=values["Y"],
    z_high=values["ZHigh"],
    z_low=values["ZLow"],
    z_tolerance=values["ZTolerance"],
  )


def _decode_worklist(values: Mapping[str, Any]) -> Worklist:
  """Build a Worklist from its converted members, by wire name."""
  return Worklist(
    all_commands_completed=values["AllCommandsCompleted"],
    client_time_offset=values["ClientTimeOffset"],
    commands=values["Commands"],
    device_id=values["DeviceID"],
    devices_to_monitor=values["DevicesToMonitor"],
    end_time=values["EndTime"],
    error=values["Error"],
    execution_notes=values["ExecutionNotes"],
    experiment_id=values["ExperimentID"],
    experiment_name=values["ExperimentName"],
    fcs_data_written=values["FCSDataWritten"],
    fcs_file_crc=values["FCSFileCRC"],
    fcs_file_name=values["FCSFileName"],
    fluidics_run_time_minutes=values["FluidicsRunTimeMinutes"],
    id=values["ID"],
    leave_fluidics_running_on_complete=values["LeaveFluidicsRunningOnComplete"],
    name=values["Name"],
    plate_id=values["PlateID"],
    plate_number=values["PlateNumber"],
    pump_run_time_minutes=values["PumpRunTimeMinutes"],
    start_time=values["StartTime"],
    stop_reason=values["StopReason"],
  )


def _decode_controller_service_status(values: Mapping[str, Any]) -> ControllerServiceStatus:
  """Build a ControllerServiceStatus from its converted members, by wire name."""
  return ControllerServiceStatus(is_running=values["isRunning"], is_updating=values["isUpdating"])


def _decode_watchdog_exception(values: Mapping[str, Any]) -> WatchdogException:
  """Build a WatchdogException from its converted members, by wire name."""
  return WatchdogException(
    inner_exception=values["InnerException"], message=values["Message"], type=values["Type"]
  )


_BINDINGS: Tuple[
  Tuple[type, DataContract, Callable[[Any], Dict[str, Any]], Callable[[Mapping[str, Any]], Any]],
  ...,
] = (
  (WorklistCommand, WORKLIST_COMMAND, _encode_worklist_command, _decode_worklist_command),
  (SampleCommand, SAMPLE_COMMAND, _encode_sample_command, _decode_sample_command),
  (BeadSipCommand, BEAD_SIP_COMMAND, _encode_bead_sip_command, _decode_bead_sip_command),
  (
    BeginSessionFCAutomaterCommand,
    BEGIN_SESSION_FC_AUTOMATER_COMMAND,
    _encode_begin_session_fc_automater_command,
    _decode_begin_session_fc_automater_command,
  ),
  (
    CleanFlowCytometerCommand,
    CLEAN_FLOW_CYTOMETER_COMMAND,
    _encode_clean_flow_cytometer_command,
    _decode_clean_flow_cytometer_command,
  ),
  (
    CombinedShakeRinseCommand,
    COMBINED_SHAKE_RINSE_COMMAND,
    _encode_combined_shake_rinse_command,
    _decode_combined_shake_rinse_command,
  ),
  (
    CombinedShakeRinseAtLocationCommand,
    COMBINED_SHAKE_RINSE_AT_LOCATION_COMMAND,
    _encode_combined_shake_rinse_at_location_command,
    _decode_combined_shake_rinse_at_location_command,
  ),
  (CompensationEntry, COMPENSATION_ENTRY, _encode_compensation_entry, _decode_compensation_entry),
  (CompletedWorklist, COMPLETED_WORKLIST, _encode_completed_worklist, _decode_completed_worklist),
  (ConnectionInfo, CONNECTION_INFO, _encode_connection_info, _decode_connection_info),
  (ContactZResult, CONTACT_Z_RESULT, _encode_contact_z_result, _decode_contact_z_result),
  (
    ControllerServiceException,
    CONTROLLER_SERVICE_EXCEPTION,
    _encode_controller_service_exception,
    _decode_controller_service_exception,
  ),
  (
    CyclingRinseCommand,
    CYCLING_RINSE_COMMAND,
    _encode_cycling_rinse_command,
    _decode_cycling_rinse_command,
  ),
  (
    DeviceAlertMessage,
    DEVICE_ALERT_MESSAGE,
    _encode_device_alert_message,
    _decode_device_alert_message,
  ),
  (
    DeviceConfiguration,
    DEVICE_CONFIGURATION,
    _encode_device_configuration,
    _decode_device_configuration,
  ),
  (DeviceMessage, DEVICE_MESSAGE, _encode_device_message, _decode_device_message),
  (DeviceInfo, DEVICE_INFO, _encode_device_info, _decode_device_info),
  (
    DeviceResetOptions,
    DEVICE_RESET_OPTIONS,
    _encode_device_reset_options,
    _decode_device_reset_options,
  ),
  (
    EndSessionFCAutomaterCommand,
    END_SESSION_FC_AUTOMATER_COMMAND,
    _encode_end_session_fc_automater_command,
    _decode_end_session_fc_automater_command,
  ),
  (
    FlowCytometerAcquireCommand,
    FLOW_CYTOMETER_ACQUIRE_COMMAND,
    _encode_flow_cytometer_acquire_command,
    _decode_flow_cytometer_acquire_command,
  ),
  (
    FlowCytometerStopAcquireCommand,
    FLOW_CYTOMETER_STOP_ACQUIRE_COMMAND,
    _encode_flow_cytometer_stop_acquire_command,
    _decode_flow_cytometer_stop_acquire_command,
  ),
  (
    TimedCyclingRinseCommand,
    TIMED_CYCLING_RINSE_COMMAND,
    _encode_timed_cycling_rinse_command,
    _decode_timed_cycling_rinse_command,
  ),
  (FlushCommand, FLUSH_COMMAND, _encode_flush_command, _decode_flush_command),
  (
    FlushMatryoshkaCommand,
    FLUSH_MATRYOSHKA_COMMAND,
    _encode_flush_matryoshka_command,
    _decode_flush_matryoshka_command,
  ),
  (
    GetFCAutomaterFCSDataCommand,
    GET_FC_AUTOMATER_FCS_DATA_COMMAND,
    _encode_get_fc_automater_fcs_data_command,
    _decode_get_fc_automater_fcs_data_command,
  ),
  (MovementRange, MOVEMENT_RANGE, _encode_movement_range, _decode_movement_range),
  (RinseCommand, RINSE_COMMAND, _encode_rinse_command, _decode_rinse_command),
  (
    RunFlowCytometerFluidicsForDurationCommand,
    RUN_FLOW_CYTOMETER_FLUIDICS_FOR_DURATION_COMMAND,
    _encode_run_flow_cytometer_fluidics_for_duration_command,
    _decode_run_flow_cytometer_fluidics_for_duration_command,
  ),
  (
    SampleContinuouslyCommand,
    SAMPLE_CONTINUOUSLY_COMMAND,
    _encode_sample_continuously_command,
    _decode_sample_continuously_command,
  ),
  (
    SampleWellCommand,
    SAMPLE_WELL_COMMAND,
    _encode_sample_well_command,
    _decode_sample_well_command,
  ),
  (
    SampleTestTubeCommand,
    SAMPLE_TEST_TUBE_COMMAND,
    _encode_sample_test_tube_command,
    _decode_sample_test_tube_command,
  ),
  (
    SampleVialCommand,
    SAMPLE_VIAL_COMMAND,
    _encode_sample_vial_command,
    _decode_sample_vial_command,
  ),
  (
    SampleWellContinuouslyCommand,
    SAMPLE_WELL_CONTINUOUSLY_COMMAND,
    _encode_sample_well_continuously_command,
    _decode_sample_well_continuously_command,
  ),
  (
    ShutdownFlowCytometerCommand,
    SHUTDOWN_FLOW_CYTOMETER_COMMAND,
    _encode_shutdown_flow_cytometer_command,
    _decode_shutdown_flow_cytometer_command,
  ),
  (
    StartFCAutomaterCommand,
    START_FC_AUTOMATER_COMMAND,
    _encode_start_fc_automater_command,
    _decode_start_fc_automater_command,
  ),
  (
    StartFCAutomaterForPrimeCommand,
    START_FC_AUTOMATER_FOR_PRIME_COMMAND,
    _encode_start_fc_automater_for_prime_command,
    _decode_start_fc_automater_for_prime_command,
  ),
  (Threshold, THRESHOLD, _encode_threshold, _decode_threshold),
  (
    StartFlowCytometerFluidicsCommand,
    START_FLOW_CYTOMETER_FLUIDICS_COMMAND,
    _encode_start_flow_cytometer_fluidics_command,
    _decode_start_flow_cytometer_fluidics_command,
  ),
  (
    StartMatryoshkaFluidicsCommand,
    START_MATRYOSHKA_FLUIDICS_COMMAND,
    _encode_start_matryoshka_fluidics_command,
    _decode_start_matryoshka_fluidics_command,
  ),
  (StartPumpCommand, START_PUMP_COMMAND, _encode_start_pump_command, _decode_start_pump_command),
  (
    StartShakerCommand,
    START_SHAKER_COMMAND,
    _encode_start_shaker_command,
    _decode_start_shaker_command,
  ),
  (
    StopFCAutomaterCommand,
    STOP_FC_AUTOMATER_COMMAND,
    _encode_stop_fc_automater_command,
    _decode_stop_fc_automater_command,
  ),
  (
    StopFlowCytometerFluidicsCommand,
    STOP_FLOW_CYTOMETER_FLUIDICS_COMMAND,
    _encode_stop_flow_cytometer_fluidics_command,
    _decode_stop_flow_cytometer_fluidics_command,
  ),
  (
    StopGateAddFCSToPlateCommand,
    STOP_GATE_ADD_FCS_TO_PLATE_COMMAND,
    _encode_stop_gate_add_fcs_to_plate_command,
    _decode_stop_gate_add_fcs_to_plate_command,
  ),
  (
    StopGateSampleWellCommand,
    STOP_GATE_SAMPLE_WELL_COMMAND,
    _encode_stop_gate_sample_well_command,
    _decode_stop_gate_sample_well_command,
  ),
  (StopPumpCommand, STOP_PUMP_COMMAND, _encode_stop_pump_command, _decode_stop_pump_command),
  (
    StopShakerCommand,
    STOP_SHAKER_COMMAND,
    _encode_stop_shaker_command,
    _decode_stop_shaker_command,
  ),
  (
    TimedContinuousRinseCommand,
    TIMED_CONTINUOUS_RINSE_COMMAND,
    _encode_timed_continuous_rinse_command,
    _decode_timed_continuous_rinse_command,
  ),
  (
    TimedShakeCommand,
    TIMED_SHAKE_COMMAND,
    _encode_timed_shake_command,
    _decode_timed_shake_command,
  ),
  (
    TimedShakeAtLocationCommand,
    TIMED_SHAKE_AT_LOCATION_COMMAND,
    _encode_timed_shake_at_location_command,
    _decode_timed_shake_at_location_command,
  ),
  (
    ValidateDeckHeightCommand,
    VALIDATE_DECK_HEIGHT_COMMAND,
    _encode_validate_deck_height_command,
    _decode_validate_deck_height_command,
  ),
  (Worklist, WORKLIST, _encode_worklist, _decode_worklist),
  (
    ControllerServiceStatus,
    CONTROLLER_SERVICE_STATUS,
    _encode_controller_service_status,
    _decode_controller_service_status,
  ),
  (WatchdogException, WATCHDOG_EXCEPTION, _encode_watchdog_exception, _decode_watchdog_exception),
)

DATA_TYPES: Tuple[Tuple[type, DataContract], ...] = tuple(
  (kind, wire) for kind, wire, _, _ in _BINDINGS
)
"""Each dataclass with the data contract it stands for."""

_ENCODERS: Dict[type, Tuple[DataContract, Callable[[Any], Dict[str, Any]]]] = {
  kind: (wire, encode) for kind, wire, encode, _ in _BINDINGS
}
_DECODERS: Dict[DataContract, Callable[[Mapping[str, Any]], Any]] = {
  wire: decode for _, wire, _, decode in _BINDINGS
}


def to_wire(xml_type: XmlType, value: Any) -> Any:
  """Convert a Python value into the form the wcf package writes for a wire type.

  Dataclass instances become data objects of their own class, which may derive from the
  declared one; a :class:`Version` becomes a tuple, a (key, value) pair a data object, and
  lists and dictionaries are converted item by item. Any other value is passed on as it is:
  the wcf package checks it when it writes it.

  Args:
    xml_type: The declared wire type.
    value: The value; None stands for nil.

  Raises:
    TypeError: The value is a dataclass instance of a class this module does not declare, of a
      class that does not derive from the declared one, or not of the declared kind at all.
  """
  if value is None:
    return None
  if isinstance(xml_type, Nullable):
    return to_wire(xml_type.inner, value)
  if xml_type is VERSION:
    if not isinstance(value, Version):
      raise TypeError(f"expected a Version, got {type(value).__name__}")
    return (value.major, value.minor, value.build, value.revision)
  if xml_type is KEY_VALUE_PAIR_OF_STRING_STRING:
    if not isinstance(value, tuple) or len(value) != 2:
      raise TypeError(f"expected a (key, value) tuple, got {value!r}")
    return KEY_VALUE_PAIR_OF_STRING_STRING.new(key=value[0], value=value[1])
  if isinstance(xml_type, DataContract):
    binding = _ENCODERS.get(type(value))
    if binding is None:
      raise TypeError(f"{type(value).__name__} is not a class of the controller's contract")
    contract, encode = binding
    if xml_type not in contract.lineage():
      raise TypeError(f"{contract.name} does not derive from {xml_type.name}")
    members = encode(value)
    converted = {name: to_wire(kind, members[name]) for name, kind, _ in contract.members()}
    return contract.new(**converted)
  if isinstance(xml_type, DictionaryOf):
    return {
      to_wire(xml_type.key, key): to_wire(xml_type.value, item) for key, item in value.items()
    }
  if isinstance(xml_type, ArrayOf):
    return value
  if isinstance(xml_type, ListOf):
    return [to_wire(xml_type.item, item) for item in value]
  return value


def from_wire(xml_type: XmlType, value: Any) -> Any:
  """Convert a value the wcf package read for a wire type into its Python form.

  Data objects become instances of the dataclass of their own class, which may derive from the
  declared one; a version becomes a :class:`Version`, a key and value pair a tuple, and lists
  and dictionaries are converted item by item. An array of numbers stays an ``array.array``.
  Any other value is already in its Python form.

  Args:
    xml_type: The declared wire type.
    value: What the wcf package read; None stands for nil.

  Raises:
    ContractError: A data object is of a class this module does not declare, holds a member its
      class lacks, or lacks one its class declares.
  """
  if value is None:
    return None
  if isinstance(xml_type, Nullable):
    return from_wire(xml_type.inner, value)
  if xml_type is VERSION:
    parts = tuple(value) + (-1,) * (4 - len(value))
    return Version(major=parts[0], minor=parts[1], build=parts[2], revision=parts[3])
  if isinstance(xml_type, DataContract):
    if not isinstance(value, DataObject):
      raise ContractError(f"expected a {xml_type.name} object, got {type(value).__name__}")
    contract = value.contract
    if value.unknown:
      names = ", ".join(str(element.name) for element in value.unknown)
      raise ContractError(f"{contract.name} holds members its contract lacks: {names}")
    members = contract.members()
    missing = [name for name, _, _ in members if name not in value.values]
    if missing:
      raise ContractError(f"{contract.name} lacks its members {', '.join(missing)}")
    converted = {name: from_wire(kind, value.values[name]) for name, kind, _ in members}
    if contract is KEY_VALUE_PAIR_OF_STRING_STRING:
      return (converted["key"], converted["value"])
    decode = _DECODERS.get(contract)
    if decode is None:
      raise ContractError(f"{contract.name} is not a class of the controller's contract")
    return decode(converted)
  if isinstance(xml_type, DictionaryOf):
    return {
      from_wire(xml_type.key, key): from_wire(xml_type.value, item) for key, item in value.items()
    }
  if isinstance(xml_type, ArrayOf):
    return value
  if isinstance(xml_type, ListOf):
    return [from_wire(xml_type.item, item) for item in value]
  return value


CONTROLLER = ServiceContract("IController")
"""The controller's duplex contract: requests on the session to net.tcp port 9002, path /Controller,
and the callbacks the controller sends on it.
"""

CONTROLLER_CONNECT = CONTROLLER.operation(
  "Controller_Connect", [("clientTime", DATETIME)], result=CONNECTION_INFO
)
CONTROLLER_SET_VERBOSE_LOGGING = CONTROLLER.operation(
  "Controller_SetVerboseLogging", [("verboseLogging", BOOL)]
)
CONTROLLER_ENABLE_DEMO_MODE = CONTROLLER.operation(
  "Controller_EnableDemoMode", [("demoMode", BOOL)]
)
CONTROLLER_IS1536_CAPABLE = CONTROLLER.operation("Controller_Is1536Capable", result=BOOL)
CONTACT_SENSOR_HAS_CONTACT = CONTROLLER.operation("ContactSensor_HasContact", result=BOOL)
CYTOMETER_CLEAR_DATA = CONTROLLER.operation("Cytometer_ClearData")
CYTOMETER_IS_READY_TO_RUN = CONTROLLER.operation("Cytometer_IsReadyToRun", result=BOOL)
CYTOMETER_IS_READY_TO_CLEAN = CONTROLLER.operation("Cytometer_IsReadyToClean", result=BOOL)
CYTOMETER_IS_POWERING_ON = CONTROLLER.operation("Cytometer_IsPoweringOn", result=BOOL)
CYTOMETER_GET_OPERATION_DURATION = CONTROLLER.operation(
  "Cytometer_GetOperationDuration", result=TIMESPAN
)
CYTOMETER_UPDATE_FIRMWARE = CONTROLLER.operation(
  "Cytometer_UpdateFirmware", [("type", CYTOMETER_FIRMWARE_TYPE)]
)
CYTOMETER_CALIBRATE_FLUID_STATIONS = CONTROLLER.operation("Cytometer_CalibrateFluidStations")
CYTOMETER_RESET_CONSUMABLES_ODOMETER = CONTROLLER.operation("Cytometer_ResetConsumablesOdometer")
CYTOMETER_SHUTDOWN = CONTROLLER.operation("Cytometer_Shutdown")
CONTROLLER_GET_SYSTEM_ID = CONTROLLER.operation(
  "Controller_GetSystemID", [("forceRefresh", BOOL)], result=STRING
)
CONTROLLER_GET_SYSTEM_TYPE = CONTROLLER.operation("Controller_GetSystemType", result=SYSTEM_TYPE)
CONTROLLER_DETERMINE_SYSTEM_TYPE = CONTROLLER.operation(
  "Controller_DetermineSystemType", result=SYSTEM_TYPE
)
CONTROLLER_SET_SYSTEM_TYPE = CONTROLLER.operation(
  "Controller_SetSystemType", [("systemType", SYSTEM_TYPE)]
)
CONTROLLER_GET_SYSTEM_STATE = CONTROLLER.operation("Controller_GetSystemState", result=SYSTEM_STATE)
CONTROLLER_POWER_ON_CYTOMETER = CONTROLLER.operation("Controller_PowerOnCytometer")
CONTROLLER_POWER_OFF_CYTOMETER = CONTROLLER.operation("Controller_PowerOffCytometer")
CONTROLLER_CYTOMETER_HAS_POWER = CONTROLLER.operation("Controller_CytometerHasPower", result=BOOL)
CONTROLLER_CONFIGURE_DEVICES = CONTROLLER.operation(
  "Controller_ConfigureDevices",
  [("configuration", DictionaryOf(DEVICE_KIND, DEVICE_CONFIGURATION))],
)
CONTROLLER_RESET_DEVICES = CONTROLLER.operation(
  "Controller_ResetDevices", [("options", DEVICE_RESET_OPTIONS)], result=BOOL
)
CONTROLLER_INITIALIZE_DEVICES = CONTROLLER.operation(
  "Controller_InitializeDevices", [("deviceInitializationList", ListOf(DEVICE_KIND))]
)
CONTROLLER_GET_DEVICE_STATUSES = CONTROLLER.operation(
  "Controller_GetDeviceStatuses", result=ListOf(DEVICE_INFO)
)
CONTROLLER_GET_DEVICE_SUMMARY = CONTROLLER.operation(
  "Controller_GetDeviceSummary", [("deviceKind", DEVICE_KIND)], result=STRING
)
CONTROLLER_GET_SUMMARY = CONTROLLER.operation("Controller_GetSummary", result=STRING)
HTFC_CALIBRATE_FLUIDICS = CONTROLLER.operation("HTFC_CalibrateFluidics")
HTFC_SET_TOP_PEAK_VALUES = CONTROLLER.operation(
  "HTFC_SetTopPeakValues",
  [("fsc", UINT), ("ssc", UINT), ("fl1", UINT), ("fl2", UINT), ("fl3", UINT), ("fl4", UINT)],
)
HTFC_GET_TOP_PEAK_VALUES = CONTROLLER.operation(
  "HTFC_GetTopPeakValues",
  outputs=[
    ("fsc", UINT),
    ("ssc", UINT),
    ("fl1", UINT),
    ("fl2", UINT),
    ("fl3", UINT),
    ("fl4", UINT),
  ],
)
HTFC_IS_GEN_IV = CONTROLLER.operation("HTFC_IsGenIV", result=BOOL)
IQUE3_RUN_NEXT_PURGE_STEP = CONTROLLER.operation("IQue3_RunNextPurgeStep")
IQUE3_GET_CURRENT_PURGE_STEP = CONTROLLER.operation(
  "IQue3_GetCurrentPurgeStep", result=IQUE3_PURGE_STEP, outputs=[("isCompleted", BOOL)]
)
IQUE3_RUN_NEXT_INSTALLATION_PRIMING_STEP = CONTROLLER.operation(
  "IQue3_RunNextInstallationPrimingStep"
)
IQUE3_GET_CURRENT_INSTALLATION_PRIMING_STEP = CONTROLLER.operation(
  "IQue3_GetCurrentInstallationPrimingStep",
  result=IQUE3_INSTALLATION_PRIME_STEP,
  outputs=[("isCompleted", BOOL)],
)
PLATE_RAIL_LOAD = CONTROLLER.operation("PlateRail_Load")
PLATE_RAIL_EJECT = CONTROLLER.operation("PlateRail_Eject")
PUMP_GET_MIN_SPEED = CONTROLLER.operation("Pump_GetMinSpeed", result=INT)
PUMP_GET_MAX_SPEED = CONTROLLER.operation("Pump_GetMaxSpeed", result=INT)
PUMP_GET_DEFAULT_SPEED = CONTROLLER.operation("Pump_GetDefaultSpeed", result=INT)
PUMP_START = CONTROLLER.operation("Pump_Start", [("speed", INT), ("direction", PUMP_DIRECTION)])
PUMP_UPDATE = CONTROLLER.operation("Pump_Update", [("speed", INT), ("direction", PUMP_DIRECTION)])
PUMP_STOP = CONTROLLER.operation("Pump_Stop")
SAMPLER_HOME = CONTROLLER.operation("Sampler_Home")
SAMPLER_GET_X = CONTROLLER.operation("Sampler_GetX", result=DECIMAL)
SAMPLER_GET_Y = CONTROLLER.operation("Sampler_GetY", result=DECIMAL)
SAMPLER_GET_Z = CONTROLLER.operation("Sampler_GetZ", result=DECIMAL)
SAMPLER_GET_X_RANGE = CONTROLLER.operation("Sampler_GetXRange", result=MOVEMENT_RANGE)
SAMPLER_GET_Y_RANGE = CONTROLLER.operation("Sampler_GetYRange", result=MOVEMENT_RANGE)
SAMPLER_GET_Z_RANGE = CONTROLLER.operation("Sampler_GetZRange", result=MOVEMENT_RANGE)
SAMPLER_GET_Z_CONTACT_TOLERANCE = CONTROLLER.operation(
  "Sampler_GetZContactTolerance", [("activity", CONTACT_TOLERANCE_TYPE)], result=DECIMAL
)
SAMPLER_GET_SMALLEST_SUPPORTED_MOVE = CONTROLLER.operation(
  "Sampler_GetSmallestSupportedMove", result=DECIMAL
)
SAMPLER_MOVE_XY = CONTROLLER.operation(
  "Sampler_MoveXY", [("x", DECIMAL), ("y", DECIMAL), ("fast", BOOL)]
)
SAMPLER_MOVE_Z = CONTROLLER.operation("Sampler_MoveZ", [("z", DECIMAL)])
SAMPLER_FIND_Z_CONTACT = CONTROLLER.operation(
  "Sampler_FindZContact",
  [("zHigh", DECIMAL), ("zLow", DECIMAL), ("retractAfterFind", BOOL)],
  result=CONTACT_Z_RESULT,
)
SAMPLER_Y_AXIS_IS_INVERTED = CONTROLLER.operation("Sampler_YAxisIsInverted", result=BOOL)
SHAKER_MIN_RPM = CONTROLLER.operation("Shaker_MinRPM", result=INT)
SHAKER_MAX_RPM = CONTROLLER.operation("Shaker_MaxRPM", result=INT)
SHAKER_START = CONTROLLER.operation("Shaker_Start", [("rpm", INT)])
SHAKER_STOP = CONTROLLER.operation("Shaker_Stop")
SHAKER_HOME = CONTROLLER.operation("Shaker_Home")
SHAKER_OPEN_PLATE_LOCK = CONTROLLER.operation("Shaker_OpenPlateLock")
SHAKER_CLOSE_PLATE_LOCK = CONTROLLER.operation("Shaker_ClosePlateLock")
WORKLIST_RUN = CONTROLLER.operation("Worklist_Run", [("worklist", WORKLIST)])
WORKLIST_SKIP_TO = CONTROLLER.operation("Worklist_SkipTo", [("id", GUID)])
WORKLIST_CANCEL = CONTROLLER.operation("Worklist_Cancel")
WORKLIST_GET_CURRENT = CONTROLLER.operation("Worklist_GetCurrent", result=WORKLIST)
WORKLIST_FINALIZE = CONTROLLER.operation("Worklist_Finalize")
WORKLIST_GET_STATUS = CONTROLLER.operation("Worklist_GetStatus", result=WORKLIST_STATUS)
WORKLIST_IS_FCS_FILE_AVAILABLE = CONTROLLER.operation("Worklist_IsFCSFileAvailable", result=BOOL)
WORKLIST_GET_COMPLETED_WORKLISTS = CONTROLLER.operation(
  "Worklist_GetCompletedWorklists", result=ListOf(COMPLETED_WORKLIST)
)
WORKLIST_GET_COMPLETED_WORKLIST = CONTROLLER.operation(
  "Worklist_GetCompletedWorklist", [("ID", GUID)], result=COMPLETED_WORKLIST
)

TEST_CONNECTION = CONTROLLER.callback("TestConnection")
CYTOMETER_ON_HAS_UINT_FCS_DATA = CONTROLLER.callback(
  "Cytometer_OnHasUIntFCSData", [("fcsData", ArrayOf(UINT)), ("dataIndex", UINT)]
)
CYTOMETER_ON_HAS_FLOAT_FCS_DATA = CONTROLLER.callback(
  "Cytometer_OnHasFloatFCSData", [("fcsData", ArrayOf(FLOAT)), ("dataIndex", UINT)]
)
ON_CYTOMETER_LONG_RUNNING_OPERATION_COMPLETED = CONTROLLER.callback(
  "OnCytometerLongRunningOperationCompleted", [("error", STRING)]
)
ON_SYSTEM_STATE_CHANGED = CONTROLLER.callback("OnSystemStateChanged", [("state", SYSTEM_STATE)])
ON_DEVICE_STATE_CHANGED = CONTROLLER.callback("OnDeviceStateChanged", [("deviceInfo", DEVICE_INFO)])
ON_CONTACT_SENSOR_STATE_CHANGED = CONTROLLER.callback(
  "OnContactSensorStateChanged", [("hasContact", BOOL)]
)
ON_DEVICE_ALERT = CONTROLLER.callback("OnDeviceAlert", [("alert", DEVICE_ALERT_MESSAGE)])
ON_HTFC_CALIBRATE_FLUIDICS_COMPLETED = CONTROLLER.callback(
  "OnHTFCCalibrateFluidicsCompleted", [("error", STRING)]
)
ON_COMMAND_STARTED = CONTROLLER.callback("OnCommandStarted", [("command", WORKLIST_COMMAND)])
ON_COMMAND_COMPLETED = CONTROLLER.callback("OnCommandCompleted", [("command", WORKLIST_COMMAND)])
ON_WORKLIST_COMPLETED = CONTROLLER.callback("OnWorklistCompleted", [("worklist", WORKLIST)])

CONTROLLER_STREAMING = ServiceContract("IControllerStreaming")
"""The controller's streamed contract: one call per connection to net.tcp port 9003, path
/ControllerStreaming.
"""

CONTROLLER_GET_LOG_ARCHIVE = CONTROLLER_STREAMING.operation(
  "Controller_GetLogArchive", result=STREAM
)
CONTROLLER_UPLOAD_FCS_DEMO_FILE = CONTROLLER_STREAMING.operation(
  "Controller_UploadFcsDemoFile", [("source", STREAM)]
)
CYTOMETER_UPLOAD_FIRMWARE = CONTROLLER_STREAMING.operation(
  "Cytometer_UploadFirmware",
  [("FileStream", STREAM)],
  wrapper="CytometerFileUpload",
  headers=[("FileName", STRING)],
)
CONTROLLER_DOWNLOAD_CALIBRATION_SETTINGS = CONTROLLER_STREAMING.operation(
  "Controller_DownloadCalibrationSettings", result=STREAM
)
CONTROLLER_UPLOAD_CALIBRATION_SETTINGS = CONTROLLER_STREAMING.operation(
  "Controller_UploadCalibrationSettings", [("source", STREAM)]
)
CONTROLLER_CLEAR_CALIBRATION_SETTINGS = CONTROLLER_STREAMING.operation(
  "Controller_ClearCalibrationSettings", [("clearReason", STRING)]
)
WORKLIST_GET_CURRENT_FCS_FILE = CONTROLLER_STREAMING.operation(
  "Worklist_GetCurrentFCSFile", result=STREAM
)
WORKLIST_GET_COMPLETED_WORKLIST_FCS_FILE = CONTROLLER_STREAMING.operation(
  "Worklist_GetCompletedWorklistFCSFile", [("ID", GUID)], result=STREAM
)

WATCHDOG = ServiceContract("IWatchdogService", "WatchdogService")
"""The watchdog's contract: requests on a session to net.tcp port 9000, path /Watchdog."""

WATCHDOG_GET_STATUS = WATCHDOG.operation("GetStatus", result=CONTROLLER_SERVICE_STATUS)
WATCHDOG_STOP_SERVICE = WATCHDOG.operation("StopService")
WATCHDOG_INSTALL_UPDATES = WATCHDOG.operation("InstallUpdates")
WATCHDOG_START_SERVICE = WATCHDOG.operation("StartService")

WATCHDOG_STREAMING = ServiceContract("IWatchdogStreamingService", "WatchdogStreamingService")
"""The watchdog's streamed contract: one call per connection to net.tcp port 9001, path
/WatchdogStreaming.
"""

WATCHDOG_UPLOAD_INSTALL_PACKAGE = WATCHDOG_STREAMING.operation(
  "UploadInstallPackage", [("fileStream", STREAM)], wrapper="FileUpload", headers=[("crc32", UINT)]
)
WATCHDOG_DOWNLOAD_WATCHDOG_LOG_FILE = WATCHDOG_STREAMING.operation(
  "DownloadWatchdogLogFile", result=STREAM
)


CONTROLLER_OPERATIONS: Tuple[Operation, ...] = (
  CONTROLLER_CONNECT,
  CONTROLLER_SET_VERBOSE_LOGGING,
  CONTROLLER_ENABLE_DEMO_MODE,
  CONTROLLER_IS1536_CAPABLE,
  CONTACT_SENSOR_HAS_CONTACT,
  CYTOMETER_CLEAR_DATA,
  CYTOMETER_IS_READY_TO_RUN,
  CYTOMETER_IS_READY_TO_CLEAN,
  CYTOMETER_IS_POWERING_ON,
  CYTOMETER_GET_OPERATION_DURATION,
  CYTOMETER_UPDATE_FIRMWARE,
  CYTOMETER_CALIBRATE_FLUID_STATIONS,
  CYTOMETER_RESET_CONSUMABLES_ODOMETER,
  CYTOMETER_SHUTDOWN,
  CONTROLLER_GET_SYSTEM_ID,
  CONTROLLER_GET_SYSTEM_TYPE,
  CONTROLLER_DETERMINE_SYSTEM_TYPE,
  CONTROLLER_SET_SYSTEM_TYPE,
  CONTROLLER_GET_SYSTEM_STATE,
  CONTROLLER_POWER_ON_CYTOMETER,
  CONTROLLER_POWER_OFF_CYTOMETER,
  CONTROLLER_CYTOMETER_HAS_POWER,
  CONTROLLER_CONFIGURE_DEVICES,
  CONTROLLER_RESET_DEVICES,
  CONTROLLER_INITIALIZE_DEVICES,
  CONTROLLER_GET_DEVICE_STATUSES,
  CONTROLLER_GET_DEVICE_SUMMARY,
  CONTROLLER_GET_SUMMARY,
  HTFC_CALIBRATE_FLUIDICS,
  HTFC_SET_TOP_PEAK_VALUES,
  HTFC_GET_TOP_PEAK_VALUES,
  HTFC_IS_GEN_IV,
  IQUE3_RUN_NEXT_PURGE_STEP,
  IQUE3_GET_CURRENT_PURGE_STEP,
  IQUE3_RUN_NEXT_INSTALLATION_PRIMING_STEP,
  IQUE3_GET_CURRENT_INSTALLATION_PRIMING_STEP,
  PLATE_RAIL_LOAD,
  PLATE_RAIL_EJECT,
  PUMP_GET_MIN_SPEED,
  PUMP_GET_MAX_SPEED,
  PUMP_GET_DEFAULT_SPEED,
  PUMP_START,
  PUMP_UPDATE,
  PUMP_STOP,
  SAMPLER_HOME,
  SAMPLER_GET_X,
  SAMPLER_GET_Y,
  SAMPLER_GET_Z,
  SAMPLER_GET_X_RANGE,
  SAMPLER_GET_Y_RANGE,
  SAMPLER_GET_Z_RANGE,
  SAMPLER_GET_Z_CONTACT_TOLERANCE,
  SAMPLER_GET_SMALLEST_SUPPORTED_MOVE,
  SAMPLER_MOVE_XY,
  SAMPLER_MOVE_Z,
  SAMPLER_FIND_Z_CONTACT,
  SAMPLER_Y_AXIS_IS_INVERTED,
  SHAKER_MIN_RPM,
  SHAKER_MAX_RPM,
  SHAKER_START,
  SHAKER_STOP,
  SHAKER_HOME,
  SHAKER_OPEN_PLATE_LOCK,
  SHAKER_CLOSE_PLATE_LOCK,
  WORKLIST_RUN,
  WORKLIST_SKIP_TO,
  WORKLIST_CANCEL,
  WORKLIST_GET_CURRENT,
  WORKLIST_FINALIZE,
  WORKLIST_GET_STATUS,
  WORKLIST_IS_FCS_FILE_AVAILABLE,
  WORKLIST_GET_COMPLETED_WORKLISTS,
  WORKLIST_GET_COMPLETED_WORKLIST,
)
"""The request-reply operations of IController."""

CONTROLLER_CALLBACKS: Tuple[Operation, ...] = (
  TEST_CONNECTION,
  CYTOMETER_ON_HAS_UINT_FCS_DATA,
  CYTOMETER_ON_HAS_FLOAT_FCS_DATA,
  ON_CYTOMETER_LONG_RUNNING_OPERATION_COMPLETED,
  ON_SYSTEM_STATE_CHANGED,
  ON_DEVICE_STATE_CHANGED,
  ON_CONTACT_SENSOR_STATE_CHANGED,
  ON_DEVICE_ALERT,
  ON_HTFC_CALIBRATE_FLUIDICS_COMPLETED,
  ON_COMMAND_STARTED,
  ON_COMMAND_COMPLETED,
  ON_WORKLIST_COMPLETED,
)
"""The callbacks of IController, all one-way."""

CONTROLLER_STREAMING_OPERATIONS: Tuple[Operation, ...] = (
  CONTROLLER_GET_LOG_ARCHIVE,
  CONTROLLER_UPLOAD_FCS_DEMO_FILE,
  CYTOMETER_UPLOAD_FIRMWARE,
  CONTROLLER_DOWNLOAD_CALIBRATION_SETTINGS,
  CONTROLLER_UPLOAD_CALIBRATION_SETTINGS,
  CONTROLLER_CLEAR_CALIBRATION_SETTINGS,
  WORKLIST_GET_CURRENT_FCS_FILE,
  WORKLIST_GET_COMPLETED_WORKLIST_FCS_FILE,
)
"""The operations of IControllerStreaming."""

WATCHDOG_OPERATIONS: Tuple[Operation, ...] = (
  WATCHDOG_GET_STATUS,
  WATCHDOG_STOP_SERVICE,
  WATCHDOG_INSTALL_UPDATES,
  WATCHDOG_START_SERVICE,
)
"""The operations of IWatchdogService."""

WATCHDOG_STREAMING_OPERATIONS: Tuple[Operation, ...] = (
  WATCHDOG_UPLOAD_INSTALL_PACKAGE,
  WATCHDOG_DOWNLOAD_WATCHDOG_LOG_FILE,
)
"""The operations of IWatchdogStreamingService."""
