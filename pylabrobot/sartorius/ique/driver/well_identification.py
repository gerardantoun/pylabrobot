"""Assigning the events of a plate read to wells, by time.

The instrument samples every well of a plate through one probe into one continuous event
stream. Nothing in the stream marks a well. Between two wells the probe lifts and draws air,
so a well shows up as a burst of events followed by a quiet gap, and wells are found afterwards
from the event times and the times at which the sampler worked through the wells.

The method works on a histogram of event times in bins of 0.1 s; all positions below are in
bins, counted from the bin of the first event.

1. Expected wells. Each sampled well is expected from the start of its sampling command to its
   end less the up time. The sampler's clock and the event clock are not synchronised, and the
   sample takes a while to travel from the probe to the detector, so only the spacing of the
   expected wells is meaningful at this point.
2. Runs. Wells sampled one after another without a shake, rinse or skipped well between them
   form a run. Timing is trusted within a run and re-established at the start of each.
3. Peaks. Bins holding more events than a threshold form peaks; the threshold is the lowest
   at which no peak is wider than 2.5 sips. Peaks in the first 20 s are noise from priming.
   Peaks that are much narrower, lower or smaller than the average are set aside.
4. Matching. If there are exactly as many peaks as wells, they are paired in order. Otherwise
   the expected wells are slid so that the first lies on the first peak, and each run is
   matched in turn: its first well to a peak near where it is expected, the rest of the run
   moved along with it, and then every well to a peak overlapping the place it is expected at,
   following the drift between the two clocks from well to well.
5. A well without a peak keeps the place timing predicts for it and is flagged; flagged wells
   between two found wells are spread evenly between them.

The events of a well are those in its bins. Events arrive in time order, so they form one
contiguous range of event indices.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

Verdict = Literal["identified", "needs_review"]
FirstWellOutcome = Literal["matched", "too_far", "no_peaks"]

# Width of a histogram bin, in seconds.
BIN_WIDTH = 0.1
# Event times are whole multiples of this, in seconds.
EVENT_TIME_RESOLUTION = 0.005
TICKS_PER_BIN = round(BIN_WIDTH / EVENT_TIME_RESOLUTION)

# Peaks that start earlier than this after the acquisition started are priming noise: the
# first sample needs about this long to travel from the probe to the detector.
TUBING_TRANSIT_TIME = 20.0
# No peak may be wider than this many sips; the threshold is raised until none is.
WIDTH_LIMIT_FACTOR = 2.5
# The highest threshold tried before settling for the lowest.
MAX_THRESHOLD = 32
# Two runs of bins this close together, and no further apart than the up time allows, are one
# peak.
MAX_MERGE_GAP = 1
# Peak filters are relaxed only when there were at most this many times the expected peaks.
TOO_MANY_PEAKS_FACTOR = 1.2
# A peak is kept when it exceeds these fractions of the average width, height and event count.
FILTER_WIDTH_FRACTION = 0.2
FILTER_HEIGHT_FRACTION = 0.15
FILTER_EVENT_FRACTION = 0.25
# The weakest filters: used when an average is small, a limit comes out lower, or relaxing
# the filters did not keep enough peaks.
MIN_FILTER_WIDTH = 1
MIN_FILTER_HEIGHT = 1
MIN_FILTER_EVENTS = 10
SMALL_AVERAGE_WIDTH = 5
SMALL_AVERAGE_HEIGHT = 50
SMALL_AVERAGE_EVENTS = 100
MAX_FILTER_ATTEMPTS = 10
# A well is looked for this many bins either side of where it is expected.
SEARCH_MARGIN = 5
# A well is at least this fraction of a sip wide.
MIN_WELL_WIDTH_FACTOR = 0.8
# Tolerance on a sip: a well is extended up to this many sips, and two pieces of a peak are
# joined when the gap between them is at most the up time divided by this.
COMBINE_FACTOR = 1.1
# Two pieces of a peak are never joined into something this many sips wide or wider.
MAX_COMBINED_WIDTH_FACTOR = 1.65
# A candidate peak is a good one above this fraction of the average width and height.
GOOD_PEAK_FRACTION = 0.3
# A peak with more than this fraction of the average event count is a real well's.
SUBSTANTIAL_PEAK_FRACTION = 0.5
# ... and it starts a run when it lies further after the previous well than this fraction of
# the pause the sampler's timing gives.
PAUSE_FRACTION = 0.5
# Distance between two runs, in bins, assumed until one has been seen.
DEFAULT_RUN_GAP = 50
# A bin with at most this many events is quiet.
QUIET_BIN_EVENTS = 1

_PRIME_BINS = round(TUBING_TRANSIT_TIME / BIN_WIDTH)
_BINS_PER_SECOND = round(1 / BIN_WIDTH)


class WellIdentificationError(Exception):
  """Wells cannot be identified from the given events."""


@dataclass(frozen=True)
class SampledWell:
  """One sampled well, from the completed command log of a plate read.

  Attributes:
    well: The well name, such as ``A01``.
    start: When the well's sampling command started, in seconds on the command-log clock.
    end: When that command ended, on the same clock.
    after_pause: Whether something interrupted sampling between the previous well and this
      one: a shake, a rinse or a skipped well.
  """

  well: str
  start: float
  end: float
  after_pause: bool = False


@dataclass(frozen=True)
class IdentifiedWell:
  """Where one well is in the event stream.

  Attributes:
    well: The well name.
    first_event: Index of the well's first event, or ``None`` when it has no events.
    last_event: Index of the well's last event (included), or ``None``.
    start_time: Start of the well's first histogram bin, in seconds since the acquisition
      started.
    end_time: Start of the well's last histogram bin; that bin belongs to the well.
    flagged: Whether the position was predicted from timing because no peak was found for
      the well.
  """

  well: str
  first_event: Optional[int]
  last_event: Optional[int]
  start_time: float
  end_time: float
  flagged: bool

  @property
  def event_range(self) -> Optional[Tuple[int, int]]:
    """The first and last event index, both included, or ``None`` without events."""
    if self.first_event is None or self.last_event is None:
      return None
    return self.first_event, self.last_event

  @property
  def event_count(self) -> int:
    """The number of events of the well."""
    if self.first_event is None or self.last_event is None:
      return 0
    return self.last_event - self.first_event + 1


@dataclass(frozen=True)
class WellIdentification:
  """The outcome of identifying the wells of an acquisition.

  Attributes:
    wells: One entry per expected well, in sampling order.
    verdict: ``identified`` when a peak was found for every well; ``needs_review`` when the
      position of at least one well was predicted, in which case the caller should warn.
    message: A sentence describing the verdict.
  """

  wells: Tuple[IdentifiedWell, ...]
  verdict: Verdict
  message: str

  @property
  def flagged_wells(self) -> Tuple[str, ...]:
    """The names of the wells whose position was predicted."""
    return tuple(well.well for well in self.wells if well.flagged)

  def event_ranges(self) -> Dict[str, Optional[Tuple[int, int]]]:
    """Return the event range of every well by well name.

    Raises:
      ValueError: If a well name occurs twice, so the ranges cannot be keyed by name.
    """
    ranges: Dict[str, Optional[Tuple[int, int]]] = {}
    for well in self.wells:
      if well.well in ranges:
        raise ValueError(f"well {well.well} was sampled more than once")
      ranges[well.well] = well.event_range
    return ranges


# ---------------------------------------------------------------------------------------------
# Histogram
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TimeHistogram:
  """Events per 0.1 s bin, from the bin of the first event to the bin of the last.

  Attributes:
    counts: Number of events in each bin.
    offset: The bin number of `counts[0]`: bin `i` covers the time from
      `(i + offset) * 0.1` s up to the next bin.
  """

  counts: Tuple[int, ...]
  offset: int


def bin_of(seconds: float) -> int:
  """Return the number of the 0.1 s bin a time falls in.

  The time is first taken to the nearest 5 ms, the resolution of event times, so that a time
  held in single precision cannot land on the wrong side of a bin edge.
  """
  return round(seconds / EVENT_TIME_RESOLUTION) // TICKS_PER_BIN


def bin_start(bin_number: int) -> float:
  """Return the time at which a bin starts, in seconds."""
  return bin_number / _BINS_PER_SECOND


def time_histogram(event_times: Sequence[float]) -> TimeHistogram:
  """Count events per 0.1 s bin.

  Args:
    event_times: The time of every event, in seconds since the acquisition started, in the
      order the events arrived.

  Raises:
    WellIdentificationError: If a time is negative or not a number, or times go backwards.
  """
  counts: List[int] = []
  offset = 0
  previous = 0.0
  for index, time in enumerate(event_times):
    if not time >= previous:
      raise WellIdentificationError(
        f"event {index} has time {time} after {previous}: times must not be negative or decrease"
      )
    previous = time
    position = bin_of(time)
    if not counts:
      offset = position
    counts.extend([0] * (position - offset + 1 - len(counts)))
    counts[position - offset] += 1
  return TimeHistogram(tuple(counts), offset)


# ---------------------------------------------------------------------------------------------
# Expected wells and runs
# ---------------------------------------------------------------------------------------------


def _midpoint(start: int, end: int) -> int:
  """Return the middle bin of a span, rounding towards the start."""
  return start + int((end - start) / 2)


@dataclass
class WellSpan:
  """The place of one well in the histogram, as matching moves it about.

  Attributes:
    start: First bin.
    end: Last bin.
    post_gap: Bins from the end of this well to the start of the next, as the sampler's
      timing gives them. It is not changed by matching.
    flagged: Whether no peak was found for the well.
  """

  start: int
  end: int
  post_gap: int = 0
  flagged: bool = False

  @property
  def width(self) -> int:
    """Bins from start to end."""
    return self.end - self.start

  @property
  def midpoint(self) -> int:
    """The middle bin."""
    return _midpoint(self.start, self.end)

  def shift(self, bins: int) -> None:
    """Move the span later by a number of bins."""
    self.start += bins
    self.end += bins


def expected_spans(wells: Sequence[SampledWell], up_time: float) -> List[WellSpan]:
  """Return where the sampler's timing puts each well, in bins of the command-log clock.

  A well runs from the start of its command to the end of it less the up time, the time the
  probe spends raised in air at the end of the command.
  """
  spans = [WellSpan(bin_of(well.start), bin_of(well.end - up_time)) for well in wells]
  for span, following in zip(spans, spans[1:]):
    span.post_gap = following.start - span.end
  return spans


def sampling_runs(wells: Sequence[SampledWell]) -> List[Tuple[int, int]]:
  """Split wells into runs sampled without a pause.

  Returns:
    The index of the first and of the last well of each run.
  """
  runs: List[Tuple[int, int]] = []
  first = 0
  for index in range(1, len(wells)):
    if wells[index].after_pause:
      runs.append((first, index - 1))
      first = index
  runs.append((first, len(wells) - 1))
  return runs


def _shift(spans: Sequence[WellSpan], bins: int) -> None:
  """Move spans later by a number of bins."""
  for span in spans:
    span.shift(bins)


# ---------------------------------------------------------------------------------------------
# Peaks
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Peak:
  """A stretch of the histogram that rises above a threshold.

  Attributes:
    start: The bin before the first bin above the threshold (or bin 0).
    end: The last bin above the threshold.
    height: The largest count above the threshold.
    event_count: The sum of the counts above the threshold.
  """

  start: int
  end: int
  height: int
  event_count: int

  @property
  def width(self) -> int:
    """Bins from start to end."""
    return self.end - self.start

  @property
  def midpoint(self) -> int:
    """The middle bin."""
    return _midpoint(self.start, self.end)


def combine_peaks(earlier: Peak, later: Peak) -> Peak:
  """Return one peak spanning two."""
  return Peak(
    start=earlier.start,
    end=later.end,
    height=max(earlier.height, later.height),
    event_count=earlier.event_count + later.event_count,
  )


def build_peaks(counts: Sequence[int], threshold: int) -> List[Peak]:
  """Return every unbroken stretch of bins holding more events than the threshold."""
  peaks: List[Peak] = []
  above: List[int] = []
  for index, count in enumerate(list(counts) + [0]):
    if count > threshold:
      above.append(count - threshold)
    elif above:
      first = index - len(above)
      peaks.append(Peak(max(first - 1, 0), index - 1, max(above), sum(above)))
      above = []
  return peaks


def merge_close_peaks(peaks: Sequence[Peak], up_bins: float) -> List[Peak]:
  """Join neighbouring peaks that are at most a bin apart and no further than the up time.

  The gap is from the end of one peak to the start of the next; it must be at most
  `MAX_MERGE_GAP` and at most the up time divided by `COMBINE_FACTOR`.
  """
  merged: List[Peak] = []
  for peak in peaks:
    gap = peak.start - merged[-1].end if merged else None
    if gap is not None and gap <= MAX_MERGE_GAP and gap <= up_bins / COMBINE_FACTOR:
      merged[-1] = combine_peaks(merged[-1], peak)
    else:
      merged.append(peak)
  return merged


def find_peaks(counts: Sequence[int], sip_bins: float, up_bins: float) -> List[Peak]:
  """Return the peaks at the lowest threshold that leaves none wider than 2.5 sips.

  Thresholds from 1 to `MAX_THRESHOLD` are tried. If every one leaves a peak that is too
  wide, the peaks at threshold 1 are returned.
  """
  limit = int(WIDTH_LIMIT_FACTOR * sip_bins)
  for threshold in range(1, MAX_THRESHOLD + 1):
    peaks = merge_close_peaks(build_peaks(counts, threshold), up_bins)
    if all(peak.width <= limit for peak in peaks):
      return peaks
  return merge_close_peaks(build_peaks(counts, 1), up_bins)


def drop_prime_noise(peaks: Sequence[Peak], offset: int) -> List[Peak]:
  """Drop the peaks that start within the first 20 s of the acquisition.

  Args:
    peaks: Peaks in histogram bins.
    offset: The bin number of the histogram's first bin.
  """
  return [peak for peak in peaks if peak.start + offset >= _PRIME_BINS]


@dataclass(frozen=True)
class PeakAverages:
  """Mean width, height and event count of a set of peaks."""

  width: float
  height: float
  event_count: float


def peak_averages(peaks: Sequence[Peak]) -> PeakAverages:
  """Return the averages of at least one peak."""
  count = len(peaks)
  return PeakAverages(
    width=sum(peak.width for peak in peaks) / count,
    height=sum(peak.height for peak in peaks) / count,
    event_count=sum(peak.event_count for peak in peaks) / count,
  )


def _limit(average: float, fraction: float, attempt: int, small: float, minimum: int) -> int:
  """Return one filter limit: a fraction of the average, relaxed by the attempt number.

  The minimum applies when the average is small or the limit comes out below it.
  """
  limit = int(average * fraction / attempt)
  return minimum if average < small or limit < minimum else limit


def filter_limits(averages: PeakAverages, attempt: int) -> Tuple[int, int, int]:
  """Return the width, height and event count a peak must exceed to be kept.

  Args:
    averages: Averages of the peaks being filtered.
    attempt: 1 for the first attempt; each further attempt divides the limits by its number.
  """
  return (
    _limit(averages.width, FILTER_WIDTH_FRACTION, attempt, SMALL_AVERAGE_WIDTH, MIN_FILTER_WIDTH),
    _limit(
      averages.height, FILTER_HEIGHT_FRACTION, attempt, SMALL_AVERAGE_HEIGHT, MIN_FILTER_HEIGHT
    ),
    _limit(
      averages.event_count,
      FILTER_EVENT_FRACTION,
      attempt,
      SMALL_AVERAGE_EVENTS,
      MIN_FILTER_EVENTS,
    ),
  )


def filter_peaks(
  peaks: Sequence[Peak], limits: Tuple[int, int, int]
) -> Tuple[List[Peak], List[Peak]]:
  """Split peaks into those exceeding all three limits and the rest.

  Returns:
    The kept peaks and the removed ones, each in time order.
  """
  width, height, events = limits
  kept: List[Peak] = []
  removed: List[Peak] = []
  for peak in peaks:
    passes = peak.width > width and peak.height > height and peak.event_count > events
    (kept if passes else removed).append(peak)
  return kept, removed


def select_peaks(peaks: Sequence[Peak], expected_count: int) -> Tuple[List[Peak], List[Peak]]:
  """Set aside the peaks that are too slight to be wells.

  The limits come from the averages of all peaks. When there were about as many peaks as
  wells (at least as many, at most 1.2 times as many) and filtering leaves fewer than wells,
  the limits are relaxed step by step; after `MAX_FILTER_ATTEMPTS` the weakest limits are
  used.

  Args:
    peaks: At least one peak, in time order.
    expected_count: The number of wells.

  Returns:
    The kept peaks and the removed ones.
  """
  averages = peak_averages(peaks)
  kept, removed = filter_peaks(peaks, filter_limits(averages, 1))
  if not expected_count <= len(peaks) <= TOO_MANY_PEAKS_FACTOR * expected_count:
    return kept, removed
  attempt = 1
  while len(kept) < expected_count:
    attempt += 1
    if attempt > MAX_FILTER_ATTEMPTS:
      return filter_peaks(peaks, (MIN_FILTER_WIDTH, MIN_FILTER_HEIGHT, MIN_FILTER_EVENTS))
    kept, removed = filter_peaks(peaks, filter_limits(averages, attempt))
  return kept, removed


# ---------------------------------------------------------------------------------------------
# Rules for one well
# ---------------------------------------------------------------------------------------------


def peak_score(peak: Peak, averages: PeakAverages) -> int:
  """Score a candidate peak: 1 when wide and high enough, 0 when only one, -1 when neither.

  "Enough" is more than `GOOD_PEAK_FRACTION` of the average width and height.
  """
  wide = peak.width > GOOD_PEAK_FRACTION * averages.width
  high = peak.height > GOOD_PEAK_FRACTION * averages.height
  return int(wide) + int(high) - 1


def best_candidate(candidates: Sequence[Peak], averages: PeakAverages) -> int:
  """Return the position of the best candidate: the first one with the highest score."""
  scores = [peak_score(peak, averages) for peak in candidates]
  return scores.index(max(scores))


def should_combine(chosen: Peak, following: Peak, sip_bins: float, up_bins: float) -> bool:
  """Return whether the candidate after the chosen one is the second piece of the same well.

  That is so when the gap between them is at most the up time divided by `COMBINE_FACTOR`,
  the two together are narrower than 1.65 sips, and either they are narrower than 1.1 sips
  or the chosen piece alone is too narrow for a well.
  """
  gap = following.start - chosen.end
  together = following.end - chosen.start
  fits_a_sip = together < COMBINE_FACTOR * sip_bins
  chosen_is_short = chosen.width < int(MIN_WELL_WIDTH_FACTOR * sip_bins)
  return (
    gap <= up_bins / COMBINE_FACTOR
    and (fits_a_sip or chosen_is_short)
    and together < MAX_COMBINED_WIDTH_FACTOR * sip_bins
  )


def _is_quiet(counts: Sequence[int], index: int) -> bool:
  """Return whether a bin holds at most one event; bins beyond the histogram are quiet."""
  return not 0 <= index < len(counts) or counts[index] <= QUIET_BIN_EVENTS


def well_end(start: int, end: int, counts: Sequence[int], sip_bins: float) -> int:
  """Return where a well that takes a peak ends.

  A peak narrower than 0.8 sips is widened to that. One narrower than 1.1 sips is extended
  bin by bin, staying narrower than 1.1 sips, until the next two bins are both quiet. A wider
  one ends where the peak ends.
  """
  minimum = int(MIN_WELL_WIDTH_FACTOR * sip_bins)
  maximum = int(COMBINE_FACTOR * sip_bins)
  if end - start < minimum:
    return start + minimum
  while end + 1 - start < maximum:
    if _is_quiet(counts, end + 1) and _is_quiet(counts, end + 2):
      break
    end += 1
  return end


def resolve_overlap(spans: Sequence[WellSpan], index: int, sip_bins: float) -> None:
  """Make a well start after the previous well ends.

  A well that starts at or before the end of the previous one is moved to start one bin
  after it. If that leaves it narrower than 0.8 sips, it is given that width when it is the
  last well or the next well starts further away than that; otherwise it is left one bin wide.
  """
  if index == 0 or spans[index].start > spans[index - 1].end:
    return
  span = spans[index]
  span.start = spans[index - 1].end + 1
  minimum = int(MIN_WELL_WIDTH_FACTOR * sip_bins)
  if span.width >= minimum:
    return
  is_last = index == len(spans) - 1
  has_room = is_last or spans[index + 1].start - span.start > minimum
  span.end = span.start + (minimum if has_room else 1)


def centre_missing_wells(spans: Sequence[WellSpan], run_first: int, index: int) -> None:
  """Spread the flagged wells just before a found well evenly over the gap before it.

  The flagged wells directly before well `index` are placed between the found well before
  them and well `index`: the free bins are divided into one slot per flagged well, and each
  well is centred in its slot at its own width, or the slot's width if that is less. Without
  a free bin per well they are left covering no bin. Nothing happens unless a found well of
  the same run precedes the flagged ones.

  Args:
    spans: All wells.
    run_first: Index of the first well of the run that `index` belongs to.
    index: A found well.
  """
  first = index
  while first > run_first and spans[first - 1].flagged:
    first -= 1
  missing = spans[first:index]
  if not missing or first == run_first:
    return
  low = spans[first - 1].end + 1
  free = spans[index].start - low
  for position, span in enumerate(missing):
    if free < len(missing):
      span.start, span.end = low, low - 1
      continue
    slot_low = low + free * position // len(missing)
    slot_high = low + free * (position + 1) // len(missing) - 1
    width = max(min(span.width, slot_high - slot_low), 0)
    span.start = slot_low + (slot_high - slot_low - width) // 2
    span.end = span.start + width


def nearest_peak(
  peaks: Sequence[Peak],
  first_unused: int,
  expected: WellSpan,
  previous: WellSpan,
  averages: PeakAverages,
) -> int:
  """Return the unused peak to try for the first well of a run.

  It is the one whose midpoint is nearest the well's expected midpoint, looking forward from
  the first unused peak. A later, nearer peak that starts after the expected start does not
  take the place of the current choice when that choice looks like the start of a run: it
  has more than half the average event count and lies further after the previous well than
  half the pause the sampler's timing gives.

  Args:
    peaks: The kept peaks.
    first_unused: Index of the first peak no well has taken; it must exist.
    expected: Where the first well of the run is expected.
    previous: The last well of the previous run, where matching left it.
    averages: Averages of the kept peaks.
  """
  choice = first_unused
  target = expected.midpoint
  for index in range(first_unused + 1, len(peaks)):
    if abs(peaks[index].midpoint - target) >= abs(peaks[choice].midpoint - target):
      break
    substantial = peaks[choice].event_count > SUBSTANTIAL_PEAK_FRACTION * averages.event_count
    after_pause = peaks[choice].start - previous.end > PAUSE_FRACTION * previous.post_gap
    if peaks[index].start > expected.start and substantial and after_pause:
      break
    choice = index
  return choice


def average_run_gap(
  spans: Sequence[WellSpan], runs: Sequence[Tuple[int, int]], run_index: int
) -> float:
  """Return the mean distance between the runs matched so far, in bins.

  The distance is from the end of one run to the start of the next, over the runs before
  `run_index`. Without any, or when the mean is 0, it is `DEFAULT_RUN_GAP`.
  """
  gaps = [spans[runs[run][0]].start - spans[runs[run - 1][1]].end for run in range(1, run_index)]
  average = sum(gaps) / len(gaps) if gaps else 0.0
  return average if average else float(DEFAULT_RUN_GAP)


# ---------------------------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------------------------


@dataclass
class _Matching:
  """What matching works on, and how far it has come.

  Attributes:
    counts: The histogram.
    peaks: The kept peaks, in time order.
    removed: The peaks set aside by the filters.
    averages: Averages of the kept peaks.
    spans: The wells; matching moves them from their expected places onto peaks.
    sip_bins: The sip time in bins.
    up_bins: The up time in bins.
    next_peak: Index of the first kept peak no well has taken.
  """

  counts: Sequence[int]
  peaks: List[Peak]
  removed: List[Peak]
  averages: PeakAverages
  spans: List[WellSpan]
  sip_bins: float
  up_bins: float
  next_peak: int = 0


def _overlapping(peaks: Sequence[Peak], first: int, low: int, high: int) -> List[int]:
  """Return the indices, from `first` on, of the peaks that overlap bins low to high."""
  indices = []
  for index in range(first, len(peaks)):
    if peaks[index].start > high:
      break
    if peaks[index].end >= low:
      indices.append(index)
  return indices


def _take_peak(matching: _Matching, well: int, peak: Peak) -> None:
  """Put a well on a peak: it starts where the peak starts and ends as :func:`well_end` says."""
  span = matching.spans[well]
  span.start = peak.start
  span.end = well_end(peak.start, peak.end, matching.counts, matching.sip_bins)


def _take_best(matching: _Matching, well: int, candidates: Sequence[int]) -> None:
  """Put a well on the best of its candidate peaks, joined with the next one if it is a piece.

  Every peak up to the one taken counts as used afterwards.
  """
  peaks = [matching.peaks[index] for index in candidates]
  chosen = best_candidate(peaks, matching.averages)
  peak, last = peaks[chosen], candidates[chosen]
  if chosen + 1 < len(peaks) and should_combine(
    peak, peaks[chosen + 1], matching.sip_bins, matching.up_bins
  ):
    peak, last = combine_peaks(peak, peaks[chosen + 1]), candidates[chosen + 1]
  _take_peak(matching, well, peak)
  matching.next_peak = last + 1


def _take_in_order(matching: _Matching, first: int, last: int) -> None:
  """Put wells `first` to `last` on the next unused peaks, one each, then settle overlaps."""
  for well in range(first, last + 1):
    _take_peak(matching, well, matching.peaks[matching.next_peak])
    matching.next_peak += 1
  for well in range(first, last + 1):
    resolve_overlap(matching.spans, well, matching.sip_bins)


def anchor_on_first_peak(spans: Sequence[WellSpan], first_peak: Peak) -> None:
  """Slide all expected wells so the middle of the first lies on the middle of the first peak.

  This removes the unknown offset between the sampler's clock and the event clock, transit
  through the tubing included.
  """
  _shift(spans, first_peak.midpoint - spans[0].midpoint)


def _use_removed_peak(matching: _Matching, span: WellSpan, low: int, high: int) -> None:
  """Give a well without a peak the bounds of a removed peak near it, if there is one.

  Of the removed peaks overlapping bins low to high, the one with the most events is used.
  """
  near = [peak for peak in matching.removed if peak.end >= low and peak.start <= high]
  if near:
    best = max(near, key=lambda peak: peak.event_count)
    span.start, span.end = best.start, best.end


def _match_first_well(
  matching: _Matching, runs: Sequence[Tuple[int, int]], run_index: int
) -> FirstWellOutcome:
  """Find the peak of the first well of a run and move the run along with it.

  Peaks overlapping the expected well, widened by `SEARCH_MARGIN` either side, are the
  candidates. Without any, the nearest unused peak is taken if it starts less than the usual
  distance between runs after the expected start.

  Returns:
    ``matched`` when the well took a peak; the rest of the run is moved by as much as the
    well's midpoint moved. ``too_far`` when the nearest peak is too far away: the well is
    flagged, and if the run is expected closer to the previous run than runs usually are, the
    whole run is moved later by the difference. ``no_peaks`` when no unused peak is left.
  """
  first, last = runs[run_index]
  spans = matching.spans
  span = spans[first]
  if matching.next_peak >= len(matching.peaks):
    return "no_peaks"
  candidates = _overlapping(
    matching.peaks, matching.next_peak, span.start - SEARCH_MARGIN, span.end + SEARCH_MARGIN
  )
  if not candidates:
    usual_gap = average_run_gap(spans, runs, run_index)
    found = matching.next_peak
    if first > 0:
      found = nearest_peak(matching.peaks, found, span, spans[first - 1], matching.averages)
    if matching.peaks[found].start - span.start >= usual_gap:
      span.flagged = True
      gap = span.start - spans[first - 1].end if first > 0 else usual_gap
      if gap < usual_gap:
        _shift(spans[first : last + 1], int(usual_gap - gap))
      return "too_far"
    candidates = [found]
  planned = span.midpoint
  _take_best(matching, first, candidates)
  _shift(spans[first + 1 : last + 1], span.midpoint - planned)
  return "matched"


def _peaks_within(matching: _Matching, first: int, last: int) -> int:
  """Count the kept peaks lying entirely between the start of one well and the end of another."""
  low, high = matching.spans[first].start, matching.spans[last].end
  return sum(1 for peak in matching.peaks if peak.start >= low and peak.end <= high)


def _match_following_wells(matching: _Matching, first: int, last: int) -> None:
  """Match the wells of a run after its first, one by one, following the clock drift.

  Each well is looked for where it is expected, moved by the drift seen at the last found
  well and widened by `SEARCH_MARGIN` either side. A found well takes its best candidate and
  sets the drift to how far its midpoint moved. A well without candidates is flagged and
  stays where it is expected, or takes the bounds of a removed peak there. After every well
  overlaps are settled and flagged wells before a found one are spread evenly.
  """
  spans = matching.spans
  drift = 0
  for well in range(first + 1, last + 1):
    span = spans[well]
    low = span.start - SEARCH_MARGIN + drift
    high = span.end + SEARCH_MARGIN + drift
    candidates = _overlapping(matching.peaks, matching.next_peak, low, high)
    if candidates:
      planned = span.midpoint
      _take_best(matching, well, candidates)
      drift = span.midpoint - planned
    else:
      span.flagged = True
      _use_removed_peak(matching, span, low, high)
    resolve_overlap(spans, well, matching.sip_bins)
    if not span.flagged:
      centre_missing_wells(spans, first, well)


def _match_run(matching: _Matching, runs: Sequence[Tuple[int, int]], run_index: int) -> bool:
  """Match one run of wells.

  When the first well is found and exactly as many kept peaks lie within the run as it has
  wells, the other wells take the following peaks in order. Otherwise they are matched one by
  one. Unless the last well of the run is flagged, all later wells are then moved by as much
  as that well's midpoint moved, so the next run starts from the clock offset this one ended
  with.

  Returns:
    False when no peak was left for the run: its wells and all later ones are flagged where
    they are expected, and matching is over.
  """
  first, last = runs[run_index]
  spans = matching.spans
  planned_end = spans[last].midpoint
  outcome = _match_first_well(matching, runs, run_index)
  if outcome == "no_peaks":
    for span in spans[first:]:
      span.flagged = True
    return False
  if outcome == "matched" and _peaks_within(matching, first, last) == last - first + 1:
    _take_in_order(matching, first + 1, last)
  else:
    _match_following_wells(matching, first, last)
  if not spans[last].flagged:
    _shift(spans[last + 1 :], spans[last].midpoint - planned_end)
  return True


def _match_wells(matching: _Matching, runs: Sequence[Tuple[int, int]]) -> None:
  """Move every well from its expected place onto its peak, or flag it.

  With exactly one kept peak per well, wells and peaks are paired in order. Otherwise the
  expected wells are anchored on the first peak and the runs are matched in turn.
  """
  if len(matching.peaks) == len(matching.spans):
    _take_in_order(matching, 0, len(matching.spans) - 1)
    return
  anchor_on_first_peak(matching.spans, matching.peaks[0])
  for run_index in range(len(runs)):
    if not _match_run(matching, runs, run_index):
      return


# ---------------------------------------------------------------------------------------------
# From bins to events
# ---------------------------------------------------------------------------------------------


def events_before_bins(counts: Sequence[int]) -> List[int]:
  """Return, for every bin and for the end of the histogram, the number of earlier events."""
  totals = [0]
  for count in counts:
    totals.append(totals[-1] + count)
  return totals


def event_range(events_before: Sequence[int], start: int, end: int) -> Optional[Tuple[int, int]]:
  """Return the indices of the first and last event in bins `start` to `end`, both included.

  Events are in time order, so the events of a range of bins are a range of event indices.

  Args:
    events_before: The result of :func:`events_before_bins`.
    start: First bin; bins outside the histogram hold no events.
    end: Last bin.

  Returns:
    The first and last event index, or ``None`` when the bins hold no event.
  """
  bins = len(events_before) - 1
  first = events_before[min(max(start, 0), bins)]
  stop = events_before[min(max(end + 1, 0), bins)]
  return (first, stop - 1) if stop > first else None


def _identified(
  well: SampledWell, span: WellSpan, histogram: TimeHistogram, events_before: Sequence[int]
) -> IdentifiedWell:
  """Describe one matched well by its events and times."""
  events = event_range(events_before, span.start, span.end)
  start_time = bin_start(span.start + histogram.offset)
  return IdentifiedWell(
    well=well.well,
    first_event=None if events is None else events[0],
    last_event=None if events is None else events[1],
    start_time=start_time,
    end_time=max(bin_start(span.end + histogram.offset), start_time),
    flagged=span.flagged,
  )


def _outcome(wells: Sequence[IdentifiedWell]) -> WellIdentification:
  """Wrap identified wells with the verdict a caller can warn on."""
  flagged = [well.well for well in wells if well.flagged]
  if not flagged:
    return WellIdentification(tuple(wells), "identified", f"all {len(wells)} wells were found")
  message = (
    f"{len(flagged)} of {len(wells)} wells were placed by timing because no peak was found "
    f"for them: {', '.join(flagged)}"
  )
  logger.warning("well identification needs review: %s", message)
  return WellIdentification(tuple(wells), "needs_review", message)


def _check_inputs(wells: Sequence[SampledWell], sip_time: float, up_time: float) -> None:
  """Check the arguments of :func:`identify_wells`.

  Raises:
    ValueError: If there is no well, a time is not finite, the sip time is not positive, the
      up time is negative, a well ends before it starts, or wells are not in sampling order.
  """
  if not wells:
    raise ValueError("there are no sampled wells to identify")
  if not (math.isfinite(sip_time) and sip_time > 0):
    raise ValueError(f"the sip time must be positive, not {sip_time}")
  if not (math.isfinite(up_time) and up_time >= 0):
    raise ValueError(f"the up time must not be negative, not {up_time}")
  previous_start = -math.inf
  for well in wells:
    if not (math.isfinite(well.start) and math.isfinite(well.end)):
      raise ValueError(f"well {well.well} has a time that is not a number")
    if well.end < well.start or well.start < previous_start:
      raise ValueError(f"well {well.well} is out of order: {well.start} to {well.end}")
    previous_start = well.start


def identify_wells(
  event_times: Sequence[float],
  wells: Sequence[SampledWell],
  sip_time: float,
  up_time: float,
) -> WellIdentification:
  """Assign the events of a plate read to its wells.

  Args:
    event_times: The time of every event in seconds since the acquisition started, in the
      order the events arrived (which is time order).
    wells: The sampled wells in sampling order, from the completed command log. Their times
      may be on any clock; only their spacing is used.
    sip_time: How long the probe draws from a well, in seconds.
    up_time: How long the probe then stays raised in air, in seconds.

  Returns:
    One entry per well, in the order given, and a verdict. Wells without events of their own
    are flagged and placed where timing predicts them; any flagged well makes the verdict
    ``needs_review``.

  Raises:
    WellIdentificationError: If the events hold no usable peak (too few events), or their
      times are negative or go backwards.
    ValueError: If the wells or times given are unusable.
  """
  _check_inputs(wells, sip_time, up_time)
  histogram = time_histogram(event_times)
  sip_bins = round(sip_time / BIN_WIDTH, 6)
  up_bins = round(up_time / BIN_WIDTH, 6)
  peaks = drop_prime_noise(find_peaks(histogram.counts, sip_bins, up_bins), histogram.offset)
  if not peaks:
    raise WellIdentificationError("not enough event data to identify wells")
  kept, removed = select_peaks(peaks, len(wells))
  if not kept:
    raise WellIdentificationError("not enough event data to identify wells")
  matching = _Matching(
    counts=histogram.counts,
    peaks=kept,
    removed=removed,
    averages=peak_averages(kept),
    spans=expected_spans(wells, up_time),
    sip_bins=sip_bins,
    up_bins=up_bins,
  )
  _match_wells(matching, sampling_runs(wells))
  events_before = events_before_bins(histogram.counts)
  return _outcome(
    [_identified(well, span, histogram, events_before) for well, span in zip(wells, matching.spans)]
  )


def identify_single_well(event_times: Sequence[float], well: str = "A01") -> WellIdentification:
  """Assign all events to one container, as for a tube.

  Args:
    event_times: The time of every event in seconds since the acquisition started.
    well: The name to give the container.

  Returns:
    One well covering every event, from the bin of the first event to the bin of the last.

  Raises:
    WellIdentificationError: If event times are negative or go backwards.
  """
  histogram = time_histogram(event_times)
  count = sum(histogram.counts)
  identified = IdentifiedWell(
    well=well,
    first_event=0 if count else None,
    last_event=count - 1 if count else None,
    start_time=bin_start(histogram.offset),
    end_time=bin_start(histogram.offset + max(len(histogram.counts) - 1, 0)),
    flagged=False,
  )
  return WellIdentification((identified,), "identified", "all events belong to one container")
