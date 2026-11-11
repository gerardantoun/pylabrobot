"""What a read on the iQue Screener PLUS returns: a plate read and a tube read.

A plate is one continuous acquisition: the probe samples well after well into a single event
stream, and nothing in the stream marks a well. :class:`PlateRead` holds the acquisition as the
controller recorded it (every event of the plate, the keywords and bytes of the controller's FCS
file, and the completed worklist with the start and end time of every command) together with
the assignment of its events to wells, and writes FCS 3.0 files for the plate and for each well.
:class:`TubeRead` holds the acquisition of the one tube in the tube holder.

:func:`plate_read_from_run` and :func:`tube_read_from_run` build them from a completed run.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Union

from pylabrobot.sartorius.ique.driver.calibration import (
  PlateFormat,
  format_well_name,
  parse_well_name,
)
from pylabrobot.sartorius.ique.driver.contract import Worklist
from pylabrobot.sartorius.ique.driver.errors import EventDataError, WellIdentificationFailedError
from pylabrobot.sartorius.ique.driver.fcs import (
  WELL_KEYWORD,
  EventData,
  descriptive_keywords,
  event_times,
  time_step,
  write_well_fcs_files,
)
from pylabrobot.sartorius.ique.driver.master import CompletedRun
from pylabrobot.sartorius.ique.driver.well_identification import (
  Verdict,
  WellIdentification,
  WellIdentificationError,
  identify_single_well,
  identify_wells,
)
from pylabrobot.sartorius.ique.driver.worklist import (
  PlateReadSettings,
  TubeReadSettings,
  sampled_wells,
)

logger = logging.getLogger(__name__)

PLATE_FILE_NAME = "Plate.fcs"
"""The name of the whole-plate file when the controller gives its file no name."""
TUBE_FILE_NAME = "Tube.fcs"
"""The name of a tube's file when the controller gives its file no name."""
TUBE_SAMPLE = "A01"
"""The sample name of the tube: the controller samples it as well A01."""

# Keywords of the plate file that would be wrong in the file of one well: the sample name
# (``A01`` for a whole plate), the times of the plate's first and last events, and a well name.
_PLATE_ONLY_KEYWORDS = frozenset({"$SMNO", "$BTIM", "$ETIM", WELL_KEYWORD.upper()})


def _file_name(controller_name: Optional[str], fallback: str) -> str:
  """Return the base name of the controller's FCS file, which may carry a directory of the
  instrument's computer, or `fallback` when it has none."""
  if not controller_name:
    return fallback
  return pathlib.PureWindowsPath(controller_name).name or fallback


def _write_file(path: pathlib.Path, data: bytes, overwrite: bool) -> None:
  """Write bytes to a file, refusing to replace one unless `overwrite`.

  Raises:
    FileExistsError: The file exists and `overwrite` is False.
  """
  with open(path, "wb" if overwrite else "xb") as stream:
    stream.write(data)


@dataclass(frozen=True)
class PlateRead:
  """The read of a plate: its events, the controller's record of the run, and the wells.

  Attributes:
    plate_format: The plate's format.
    plate_model: The plate model of the calibration the wells were sampled with.
    settings: The settings the plate was read with.
    worklist: The worklist as the controller completed it: every command with its start and end
      time on the controller's clock and its notes. Commands passed over when the read was
      stopped early carry the notes ``Skipped``.
    events: Every event of the plate in arrival order, from the controller's FCS file.
    fcs_keywords: The keywords of that file.
    fcs_file: The bytes of that file.
    identification: Where each sampled well's events are among `events`, in sampling order,
      with the verdict of well identification.
    plate_name: The name of the plate resource that was read, or None.
    stopped_early: Whether the read was stopped early; the wells not sampled by then are absent
      from :attr:`wells`.
  """

  plate_format: PlateFormat
  plate_model: str
  settings: PlateReadSettings
  worklist: Worklist
  events: EventData
  fcs_keywords: Dict[str, str]
  fcs_file: bytes
  identification: WellIdentification
  plate_name: Optional[str] = None
  stopped_early: bool = False

  @property
  def verdict(self) -> Verdict:
    """``identified`` when every well was found and passed its checks; ``needs_review`` when a
    well was placed by timing alone or failed a check, or no well was sampled."""
    return self.identification.verdict

  @property
  def message(self) -> str:
    """A sentence describing the verdict, naming the wells that need review."""
    return self.identification.message

  @property
  def wells(self) -> List[str]:
    """The names of the sampled wells, such as ``A01``, in sampling order."""
    return [well.well for well in self.identification.wells]

  def well_events(self, well: str) -> EventData:
    """Return the events of one well, with the times they have in the plate's acquisition.

    Args:
      well: The well's name, such as ``A1`` or ``A01``.

    Returns:
      A copy of the well's events; empty when the well has none.

    Raises:
      ValueError: The text is not a well name.
      KeyError: The well was not read.
    """
    name = format_well_name(*parse_well_name(well))
    for identified in self.identification.wells:
      if identified.well == name:
        if identified.event_range is None:
          return self.events.slice(0, 0)
        first, last = identified.event_range
        return self.events.slice(first, last + 1)
    raise KeyError(f"well {name} was not read")

  def event_counts(self) -> Dict[str, int]:
    """Return the number of events of every sampled well, by well name, in sampling order."""
    return {well.well: well.event_count for well in self.identification.wells}

  def write_fcs(
    self,
    directory: Union[str, pathlib.Path],
    *,
    per_well: bool = True,
    overwrite: bool = False,
  ) -> List[pathlib.Path]:
    """Write the plate's FCS file and, with `per_well`, one FCS file per sampled well.

    The plate's file is the controller's own, byte for byte, under the controller's file name.
    A well's file is ``Well_<well>.fcs``; it holds the well's events with the times they have
    in the plate's acquisition, the keywords of the controller's file that describe the
    acquisition (instrument, date, time step, metadata) and the well name as ``IC$WellID``. A
    well without events gets a file with no events.

    Args:
      directory: Where the files go; it is created if it does not exist.
      per_well: Whether to write the files of the wells too.
      overwrite: Whether files that exist may be replaced.

    Returns:
      The paths written: the plate's file first, then the wells' in sampling order.

    Raises:
      FileExistsError: A file exists and `overwrite` is False; the files before it are written.
    """
    folder = pathlib.Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    plate_path = folder / _file_name(self.worklist.fcs_file_name, PLATE_FILE_NAME)
    _write_file(plate_path, self.fcs_file, overwrite)
    paths = [plate_path]
    if per_well:
      shared = {
        key: value
        for key, value in descriptive_keywords(self.fcs_keywords).items()
        if key.upper() not in _PLATE_ONLY_KEYWORDS
      }
      paths += write_well_fcs_files(
        folder,
        self.events,
        self.identification.event_ranges(),
        shared,
        overwrite=overwrite,
      )
    logger.info("wrote %d FCS files of plate %s to %s", len(paths), self.plate_name, folder)
    return paths


@dataclass(frozen=True)
class TubeRead:
  """The read of the tube in the tube holder.

  Attributes:
    settings: The settings the tube was read with.
    worklist: The worklist as the controller completed it, with the times of every command.
    events: Every event of the acquisition, from the controller's FCS file.
    fcs_keywords: The keywords of that file.
    fcs_file: The bytes of that file.
    identification: The one sample, ``A01``, covering every event.
  """

  settings: TubeReadSettings
  worklist: Worklist
  events: EventData
  fcs_keywords: Dict[str, str]
  fcs_file: bytes
  identification: WellIdentification

  @property
  def verdict(self) -> Verdict:
    """``identified``: every event belongs to the tube."""
    return self.identification.verdict

  @property
  def message(self) -> str:
    """A sentence describing the verdict."""
    return self.identification.message

  @property
  def event_count(self) -> int:
    """The number of events."""
    return self.events.event_count

  def write_fcs(
    self, directory: Union[str, pathlib.Path], *, overwrite: bool = False
  ) -> pathlib.Path:
    """Write the controller's FCS file of the tube, byte for byte, under its file name.

    Args:
      directory: Where the file goes; it is created if it does not exist.
      overwrite: Whether a file that exists may be replaced.

    Returns:
      The path written.

    Raises:
      FileExistsError: The file exists and `overwrite` is False.
    """
    folder = pathlib.Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / _file_name(self.worklist.fcs_file_name, TUBE_FILE_NAME)
    _write_file(path, self.fcs_file, overwrite)
    logger.info("wrote the tube's FCS file to %s", path)
    return path


def _acquisition(run: CompletedRun, what: str) -> Tuple[EventData, Dict[str, str], bytes]:
  """Return the events, keywords and file of a run that acquired.

  Raises:
    EventDataError: The run holds no event data.
  """
  if run.events is None or run.fcs_keywords is None or run.fcs_file is None:
    raise EventDataError(f"the {what} recorded no events, so the controller wrote no FCS file")
  return run.events, run.fcs_keywords, run.fcs_file


def plate_read_from_run(
  run: CompletedRun,
  *,
  plate_format: PlateFormat,
  plate_model: str,
  settings: PlateReadSettings,
  plate_name: Optional[str] = None,
  stopped_early: bool = False,
) -> PlateRead:
  """Assign the events of a completed plate read to its wells and return the read.

  The sampled wells and the times of their sampling commands come from the completed worklist;
  the event times from the time parameter and the time step of the controller's FCS file.

  Args:
    run: The completed run of a worklist from
      :func:`~pylabrobot.sartorius.ique.driver.worklist.plate_read_worklist`.
    plate_format: The plate's format.
    plate_model: The plate model the worklist was built with.
    settings: The settings the worklist was built with.
    plate_name: The name of the plate resource, if any.
    stopped_early: Whether the read was stopped early, so that commands after the last sampled
      well were skipped.

  Raises:
    EventDataError: The run recorded no events.
    WellIdentificationFailedError: The events cannot be assigned to wells; the error holds the
      run.
    IncompleteWorklistError: A command did not complete and the read was not stopped early.
  """
  events, keywords, data = _acquisition(run, "plate read")
  wells = sampled_wells(run.worklist, allow_incomplete=stopped_early)
  if wells:
    times = event_times(events, time_step(keywords))
    try:
      identification = identify_wells(times, wells, settings.sip_time, settings.up_time)
    except WellIdentificationError as error:
      raise WellIdentificationFailedError(
        f"the {events.event_count} events of the plate read cannot be assigned to its "
        f"{len(wells)} wells: {error}",
        run,
      ) from error
  else:
    identification = WellIdentification((), "needs_review", "no well was sampled")
  return PlateRead(
    plate_format=plate_format,
    plate_model=plate_model,
    settings=settings,
    worklist=run.worklist,
    events=events,
    fcs_keywords=keywords,
    fcs_file=data,
    identification=identification,
    plate_name=plate_name,
    stopped_early=stopped_early,
  )


def tube_read_from_run(run: CompletedRun, settings: TubeReadSettings) -> TubeRead:
  """Return the read of a completed tube read; every event belongs to the tube.

  Args:
    run: The completed run of a worklist from
      :func:`~pylabrobot.sartorius.ique.driver.worklist.tube_read_worklist`.
    settings: The settings the worklist was built with.

  Raises:
    EventDataError: The run recorded no events.
  """
  events, keywords, data = _acquisition(run, "tube read")
  times = event_times(events, time_step(keywords))
  return TubeRead(
    settings=settings,
    worklist=run.worklist,
    events=events,
    fcs_keywords=keywords,
    fcs_file=data,
    identification=identify_single_well(times, TUBE_SAMPLE),
  )
