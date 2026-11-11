.. currentmodule:: pylabrobot.sartorius

pylabrobot.sartorius package
============================

Entris II
---------

.. currentmodule:: pylabrobot.sartorius.entris

.. autosummary::
  :toctree: _autosummary
  :nosignatures:
  :recursive:

    SartoriusEntris2
    SartoriusError

iQue Screener PLUS
------------------

.. currentmodule:: pylabrobot.sartorius.ique

.. autosummary::
  :toctree: _autosummary
  :nosignatures:
  :recursive:

    IQue
    IQueDevice
    IQuePlateHolder
    driver.master.IQueDriver
    driver.master.InstrumentStatus
    driver.master.DeviceLimits
    driver.simulator.SimulatedController

Features
^^^^^^^^

.. autosummary::
  :toctree: _autosummary
  :nosignatures:
  :recursive:

    driver.features.plate_rail.PlateRail
    driver.features.shaker.Shaker
    driver.features.pump.Pump
    driver.features.sampler.Sampler
    driver.features.detector.Detector
    driver.features.detector.DetectorStatus

Reads and results
^^^^^^^^^^^^^^^^^

.. autosummary::
  :toctree: _autosummary
  :nosignatures:
  :recursive:

    PlateReadSettings
    TubeReadSettings
    AcquisitionSettings
    TriggerThreshold
    RinseStep
    PlateRead
    TubeRead
    CompletedRun
    RunProgress
    WellIdentification
    IdentifiedWell
    EventData
    read_fcs
    write_fcs
    event_times

Calibration
^^^^^^^^^^^

.. autosummary::
  :toctree: _autosummary
  :nosignatures:
  :recursive:

    Calibration
    read_calibration
    write_calibration
    with_deck_height
    nominal_calibration

Errors
^^^^^^

.. autosummary::
  :toctree: _autosummary
  :nosignatures:
  :recursive:

    IQueError
    NotSetUpError
    SessionRefusedError
    ControllerVersionError
    UnsupportedSystemError
    DeviceResetError
    DeviceFaultedError
    DeviceNotReadyError
    DeviceOperationError
    UnsafeMotionError
    PlateAccessError
    WorklistActiveError
    WorklistFailedError
    WorklistCancelledError
    WorklistTimeoutError
    EventDataError
    WellIdentificationFailedError
    CalibrationError
    CalibrationFormatError
    CalibrationMissingError
    CalibrationRefusedError
    IQueTimeoutError
