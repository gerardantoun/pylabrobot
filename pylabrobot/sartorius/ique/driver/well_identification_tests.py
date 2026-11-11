"""Tests of well identification: each rule on its own, then whole synthetic plate reads."""

import array
import math
import random
import unittest
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from pylabrobot.sartorius.ique.driver import well_identification as wid
from pylabrobot.sartorius.ique.driver.well_identification import (
  Peak,
  PeakAverages,
  SampledWell,
  WellIdentificationError,
  WellSpan,
)

EventRange = Optional[Tuple[int, int]]
TICK = 0.005


def well_names(rows: int, columns: int) -> List[str]:
  """Return the well names of a plate, row by row."""
  names = []
  for row in range(rows):
    letters = chr(ord("A") + row % 26)
    if row >= 26:
      letters = chr(ord("A") + row // 26 - 1) + letters
    names += [f"{letters}{column + 1:02d}" for column in range(columns)]
  return names


def poisson(rng: random.Random, mean: float) -> int:
  """Draw a Poisson-distributed count by multiplying uniform numbers (Knuth's method)."""
  limit = math.exp(-mean)
  count = 0
  product = rng.random()
  while product > limit:
    count += 1
    product *= rng.random()
  return count


class PlateRun:
  """A synthetic plate read: the sampler's command times and the events they produce.

  The sampler works through the plate row by row. A sampling command lasts
  `move + sip + up` seconds: the probe travels, draws for `sip` seconds and stays up in air.
  After each row there is a `pause` (a shake). Events appear `transit` seconds after the probe
  drew them: `per_tick` events on each 5 ms tick of the sip. The event clock runs at
  `clock_rate` times the sampler's clock, and each pause lasts `pause_extra` seconds longer in
  the events than the sampler's timing says.

  With a random generator `rng`, each well's events arrive with an error drawn from a normal
  distribution with a standard deviation of `jitter` seconds, and each tick holds a Poisson
  number of events with mean `per_tick`.
  """

  def __init__(
    self,
    rows: int = 8,
    columns: int = 12,
    sip: float = 1.0,
    up: float = 0.5,
    move: float = 1.0,
    pause: float = 4.0,
    transit: float = 25.0,
    per_tick: int = 2,
    empty: Sequence[str] = (),
    clock_rate: float = 1.0,
    pause_extra: float = 0.0,
    keep_tick: Optional[Dict[str, Callable[[int], bool]]] = None,
    rng: Optional[random.Random] = None,
    jitter: float = 0.0,
  ) -> None:
    """Lay out the commands of a plate read and generate its events."""
    self.sip = sip
    self.up = up
    self.wells: List[SampledWell] = []
    self.sip_starts: Dict[str, float] = {}
    self.ranges: Dict[str, EventRange] = {}
    times: List[float] = []
    clock = 3.0
    lag = transit
    for index, name in enumerate(well_names(rows, columns)):
      new_row = index > 0 and index % columns == 0
      if new_row:
        clock += pause
        lag += pause_extra
      self.wells.append(SampledWell(name, clock, clock + move + sip + up, new_row))
      arrival = (clock + move) * clock_rate + lag
      if rng is not None:
        arrival += rng.gauss(0.0, jitter)
      first_tick = round(arrival / TICK)
      self.sip_starts[name] = first_tick * TICK
      first_event = len(times)
      keep = (keep_tick or {}).get(name, lambda tick: True)
      if name not in empty:
        for tick in range(round(sip / TICK)):
          if keep(tick):
            count = per_tick if rng is None else poisson(rng, per_tick)
            times += [(first_tick + tick) * TICK] * count
      self.ranges[name] = (first_event, len(times) - 1) if len(times) > first_event else None
      clock += move + sip + up
    self.times = array.array("f", times)

  def identify(self, times: Optional[Sequence[float]] = None) -> wid.WellIdentification:
    """Identify the wells from the run's events, or from other event times."""
    return wid.identify_wells(self.times if times is None else times, self.wells, self.sip, self.up)


def random_clean_run(rng: random.Random) -> PlateRun:
  """Return a read of a full plate with a random protocol, drift, longer pauses and jitter.

  Half are 96-well reads (sip 0.8 to 2 s, up 0.3 to 1 s, move 0.6 to 1.5 s) and half 384-well
  reads (sip 0.5 to 1 s, up 0.3 to 0.6 s, move 0.4 to 0.8 s). Every well arrives with 50 ms of
  jitter (standard deviation), the event clock runs 0.4 % slow to 0.4 % fast, and pauses last
  from 1.2 s less to 3 s more in the events than timed.
  """
  if rng.random() < 0.5:
    rows, columns = 8, 12
    sip, up = rng.randint(8, 20) / 10, rng.randint(3, 10) / 10
    move, pause = rng.uniform(0.6, 1.5), rng.uniform(2.0, 10.0)
  else:
    rows, columns = 16, 24
    sip, up = rng.randint(5, 10) / 10, rng.randint(3, 6) / 10
    move, pause = rng.uniform(0.4, 0.8), rng.uniform(2.0, 6.0)
  return PlateRun(
    rows=rows,
    columns=columns,
    sip=sip,
    up=up,
    move=move,
    pause=pause,
    transit=rng.uniform(21.0, 40.0),
    per_tick=rng.randint(1, 3),
    clock_rate=rng.uniform(0.996, 1.004),
    pause_extra=rng.uniform(-1.2, 3.0),
    rng=rng,
    jitter=0.05,
  )


def found_ranges(result: wid.WellIdentification) -> Dict[str, EventRange]:
  """Return the event range found for every well."""
  return {well.well: well.event_range for well in result.wells}


def misplaced_wells(result: wid.WellIdentification, run: PlateRun) -> List[str]:
  """Return the wells not given their own events.

  A well is in place when its range holds the middle event of its own events and that of no
  other well; an empty well when it holds no well's middle event. Unlike comparing ranges,
  this allows for an edge bin of a few events lost to Poisson counts.
  """
  middles = {name: (span[0] + span[1]) // 2 for name, span in run.ranges.items() if span}
  misplaced = []
  for well in result.wells:
    found = well.event_range
    holds = [name for name, middle in middles.items() if found and found[0] <= middle <= found[1]]
    if holds != ([well.well] if well.well in middles else []):
      misplaced.append(well.well)
  return misplaced


def peak(start: int, end: int, height: int = 39, event_count: int = 390) -> Peak:
  """Return a peak; by default one as a full well gives."""
  return Peak(start, end, height, event_count)


# ---------------------------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------------------------


class HistogramTests(unittest.TestCase):
  """Binning event times."""

  def test_bins_of_a_tenth_of_a_second(self) -> None:
    """Events are counted per 0.1 s bin from the bin of the first event."""
    histogram = wid.time_histogram([1.0, 1.04, 1.095, 1.1, 1.35, 1.35])
    self.assertEqual(histogram.offset, 10)
    self.assertEqual(histogram.counts, (3, 1, 0, 2))
    self.assertEqual(wid.bin_start(10), 1.0)
    self.assertEqual(wid.bin_start(13), 1.3)

  def test_single_precision_times_fall_in_the_right_bin(self) -> None:
    """Times are taken to the nearest 5 ms before binning."""
    # 0.7 in single precision is 0.69999998..., just below the edge of bin 7.
    stored = array.array("f", [0.7, 29.3, 720.0, 7200.6])
    self.assertLess(stored[0], 0.7)
    self.assertEqual([wid.bin_of(time) for time in stored], [7, 293, 7200, 72006])
    self.assertEqual(wid.bin_of(0.099), 1)
    self.assertEqual(wid.bin_of(0.0), 0)
    self.assertEqual(wid.bin_of(0.095), 0)

  def test_no_events(self) -> None:
    """No events give an empty histogram."""
    self.assertEqual(wid.time_histogram([]), wid.TimeHistogram((), 0))

  def test_times_must_be_in_order(self) -> None:
    """Negative, decreasing and undefined times are refused."""
    with self.assertRaisesRegex(WellIdentificationError, "event 2 has time 1.0 after 1.5"):
      wid.time_histogram([0.5, 1.5, 1.0])
    with self.assertRaisesRegex(WellIdentificationError, "event 0 has time -0.1"):
      wid.time_histogram([-0.1, 1.0])
    with self.assertRaises(WellIdentificationError):
      wid.time_histogram([1.0, math.nan])

  def test_events_before_bins_and_event_ranges(self) -> None:
    """The events of a range of bins are a range of event indices, or none."""
    before = wid.events_before_bins((2, 0, 3, 1))
    self.assertEqual(before, [0, 2, 2, 5, 6])
    self.assertEqual(wid.event_range(before, 0, 0), (0, 1))
    self.assertIsNone(wid.event_range(before, 1, 1))
    self.assertEqual(wid.event_range(before, 1, 2), (2, 4))
    self.assertEqual(wid.event_range(before, 2, 3), (2, 5))
    self.assertEqual(wid.event_range(before, -5, 10), (0, 5))
    self.assertIsNone(wid.event_range(before, 4, 9))
    self.assertIsNone(wid.event_range(before, -9, -1))
    self.assertIsNone(wid.event_range(before, 2, 1))


class ExpectedWellTests(unittest.TestCase):
  """Expected wells and runs from the command log."""

  def test_spans(self) -> None:
    """An expected well lasts from its command's start to its end less the up time."""
    wells = [
      SampledWell("A01", 3.0, 5.5),
      SampledWell("A02", 5.5, 8.0),
      SampledWell("B01", 12.04, 14.5, True),
    ]
    spans = wid.expected_spans(wells, 0.5)
    # A well runs from its command's start to its end less the up time.
    self.assertEqual([(span.start, span.end) for span in spans], [(30, 50), (55, 75), (120, 140)])
    self.assertEqual([span.post_gap for span in spans], [5, 45, 0])
    self.assertEqual([span.flagged for span in spans], [False, False, False])
    self.assertEqual((spans[0].width, spans[0].midpoint), (20, 40))

  def test_midpoint_rounds_towards_the_start(self) -> None:
    """The midpoint of an odd width is the bin nearer the start."""
    self.assertEqual(WellSpan(10, 15).midpoint, 12)
    self.assertEqual(Peak(10, 15, 1, 1).midpoint, 12)
    self.assertEqual(WellSpan(10, 7).midpoint, 9)

  def test_runs(self) -> None:
    """A run ends before every well that follows a pause."""

    def runs(pauses: Sequence[bool]) -> List[Tuple[int, int]]:
      """Return the runs of wells with the given pause markers."""
      return wid.sampling_runs([SampledWell("X", 0.0, 1.0, pause) for pause in pauses])

    self.assertEqual(runs([False, False, True, False, True]), [(0, 1), (2, 3), (4, 4)])
    self.assertEqual(runs([True, False]), [(0, 1)])
    self.assertEqual(runs([False]), [(0, 0)])
    self.assertEqual(runs([False, True, True]), [(0, 0), (1, 1), (2, 2)])

  def test_anchor_on_first_peak(self) -> None:
    """All wells move by what brings the first midpoint onto the first peak's."""
    spans = [WellSpan(30, 50), WellSpan(55, 75)]
    wid.anchor_on_first_peak(spans, peak(104, 114))
    # Midpoint 40 moves onto midpoint 109.
    self.assertEqual([(span.start, span.end) for span in spans], [(99, 119), (124, 144)])


class PeakBuildingTests(unittest.TestCase):
  """Peaks from the histogram."""

  def test_build(self) -> None:
    """A peak starts one bin before its first bin above the threshold; counts are above it."""
    counts = [0, 0, 3, 5, 2, 0, 1, 4, 0]
    self.assertEqual(wid.build_peaks(counts, 1), [Peak(1, 4, 4, 7), Peak(6, 7, 3, 3)])
    self.assertEqual(wid.build_peaks(counts, 2), [Peak(1, 3, 3, 4), Peak(6, 7, 2, 2)])
    self.assertEqual(wid.build_peaks(counts, 5), [])

  def test_build_at_the_ends(self) -> None:
    """Peaks at either end of the histogram stay within it."""
    self.assertEqual(wid.build_peaks([5, 0], 1), [Peak(0, 0, 4, 4)])
    self.assertEqual(wid.build_peaks([0, 5], 1), [Peak(0, 1, 4, 4)])
    self.assertEqual(wid.build_peaks([], 1), [])
    self.assertEqual(wid.build_peaks([1, 1, 1], 1), [])

  def test_merge(self) -> None:
    """Peaks one quiet bin apart are one peak; two bins apart are two."""
    # Stretches one quiet bin apart give peaks whose gap is 1; two quiet bins give a gap of 2.
    peaks = wid.build_peaks([0, 3, 3, 0, 3, 4, 0, 0, 3], 1)
    self.assertEqual(peaks, [Peak(0, 2, 2, 4), Peak(3, 5, 3, 5), Peak(7, 8, 2, 2)])
    self.assertEqual(wid.merge_close_peaks(peaks, 5.0), [Peak(0, 5, 3, 9), Peak(7, 8, 2, 2)])

  def test_merge_needs_an_up_time_that_allows_the_gap(self) -> None:
    """A short up time forbids merging across a quiet bin."""
    peaks = [Peak(0, 2, 2, 4), Peak(3, 5, 3, 5)]
    # A gap of 1 bin is allowed from an up time of 1.1 bins.
    self.assertEqual(wid.merge_close_peaks(peaks, 1.1), [Peak(0, 5, 3, 9)])
    self.assertEqual(wid.merge_close_peaks(peaks, 1.0), peaks)

  def test_merge_chains(self) -> None:
    """Several close peaks in a row become one."""
    peaks = [Peak(0, 2, 1, 1), Peak(3, 5, 2, 2), Peak(6, 8, 3, 3), Peak(10, 12, 4, 4)]
    self.assertEqual(wid.merge_close_peaks(peaks, 5.0), [Peak(0, 8, 3, 6), Peak(10, 12, 4, 4)])
    self.assertEqual(wid.merge_close_peaks([], 5.0), [])

  def test_threshold_rises_until_no_peak_is_too_wide(self) -> None:
    """A low bridge between two wells is cut by a higher threshold."""
    # Two wells of 30 events per bin, bridged by 15 bins of 4 events: one peak 35 bins wide
    # until the threshold reaches 4. With a sip of 10 bins a peak may be 25 bins wide.
    counts = [0] * 5 + [30] * 10 + [4] * 15 + [30] * 10 + [0] * 5
    self.assertEqual(
      wid.find_peaks(counts, 10.0, 5.0), [Peak(4, 14, 26, 260), Peak(29, 39, 26, 260)]
    )

  def test_lowest_threshold_is_one(self) -> None:
    """A bin needs more than one event to be part of a peak."""
    counts = [0, 1, 1, 9, 9, 1, 1, 0]
    self.assertEqual(wid.find_peaks(counts, 10.0, 5.0), [Peak(2, 4, 8, 16)])
    self.assertEqual(wid.find_peaks([1, 0, 1, 1], 10.0, 5.0), [])

  def test_threshold_search_gives_up_after_32(self) -> None:
    """A stretch that no threshold up to 32 narrows is kept at threshold 1."""
    # A flat stretch of 40 bins never narrows; above its height nothing would be left.
    self.assertEqual(wid.find_peaks([0] + [40] * 40 + [0], 10.0, 5.0), [Peak(0, 40, 39, 1560)])
    # One lower than 33 events high vanishes at its height, which ends the search.
    self.assertEqual(wid.find_peaks([0] + [20] * 40 + [0], 10.0, 5.0), [])

  def test_width_limit_is_two_and_a_half_sips(self) -> None:
    """A peak of 2.5 sips is accepted; a wider one raises the threshold."""
    self.assertEqual(wid.find_peaks([0] + [9] * 25 + [0], 10.0, 5.0), [Peak(0, 25, 8, 200)])
    self.assertEqual(wid.find_peaks([0] + [9] * 26 + [0], 10.0, 5.0), [])

  def test_prime_noise(self) -> None:
    """Peaks starting before 20 s on the event clock are dropped."""
    peaks = [peak(0, 9), peak(49, 59), peak(50, 60), peak(90, 99)]
    # With the histogram starting at bin 150, index 50 is 20.0 s.
    self.assertEqual(wid.drop_prime_noise(peaks, 150), [peak(50, 60), peak(90, 99)])
    self.assertEqual(wid.drop_prime_noise(peaks, 0), [])
    self.assertEqual(wid.drop_prime_noise(peaks, 200), peaks)


class PeakFilterTests(unittest.TestCase):
  """Setting aside slight peaks."""

  def test_averages(self) -> None:
    """Averages are plain means."""
    peaks = [Peak(0, 10, 40, 400), Peak(20, 28, 20, 100)]
    self.assertEqual(wid.peak_averages(peaks), PeakAverages(9.0, 30.0, 250.0))

  def test_limits(self) -> None:
    """Limits are fractions of the averages over the attempt number."""
    # 20 % of the width, 15 % of the height and 25 % of the events, divided by the attempt.
    self.assertEqual(wid.filter_limits(PeakAverages(20.0, 200.0, 4000.0), 1), (4, 30, 1000))
    self.assertEqual(wid.filter_limits(PeakAverages(20.0, 200.0, 4000.0), 3), (1, 10, 333))
    self.assertEqual(wid.filter_limits(PeakAverages(10.0, 60.0, 400.0), 1), (2, 9, 100))

  def test_limits_for_small_averages(self) -> None:
    """Small averages and low limits give the weakest limits."""
    # Below an average of 5 bins, 50 events high or 100 events the weakest limit applies.
    self.assertEqual(wid.filter_limits(PeakAverages(4.9, 49.0, 99.0), 1), (1, 1, 10))
    self.assertEqual(wid.filter_limits(PeakAverages(5.0, 50.0, 100.0), 1), (1, 7, 25))
    # A limit that comes out below the weakest one is raised to it.
    self.assertEqual(wid.filter_limits(PeakAverages(10.0, 60.0, 400.0), 10), (1, 1, 10))

  def test_filter(self) -> None:
    """A peak is kept only when it exceeds all three limits."""
    passing = Peak(0, 3, 2, 101)
    too_narrow = Peak(10, 12, 2, 101)
    too_low = Peak(20, 23, 1, 101)
    too_few = Peak(30, 33, 2, 100)
    kept, removed = wid.filter_peaks([passing, too_narrow, too_low, too_few], (2, 1, 100))
    self.assertEqual(kept, [passing])
    self.assertEqual(removed, [too_narrow, too_low, too_few])

  def test_select_sets_slight_peaks_aside(self) -> None:
    """A blip among full peaks is removed."""
    wells = [Peak(0, 10, 39, 390), Peak(25, 35, 39, 390), Peak(50, 60, 39, 390)]
    blip = Peak(40, 42, 2, 4)
    kept, removed = wid.select_peaks([wells[0], wells[1], blip, wells[2]], 3)
    self.assertEqual((kept, removed), (wells, [blip]))

  def test_select_relaxes_when_peaks_are_about_as_many_as_wells(self) -> None:
    """Limits are relaxed only when the peaks number between the wells and 1.2 times as many."""
    big = [Peak(0, 10, 100, 1000), Peak(20, 30, 100, 1000), Peak(40, 50, 100, 1000)]
    weak = Peak(60, 70, 100, 180)
    # Average events 795: the first limit is 198, the second attempt's 99.
    self.assertEqual(wid.select_peaks(big + [weak], 4), (big + [weak], []))
    # With fewer peaks than wells, or many more, the first limits stand.
    self.assertEqual(wid.select_peaks(big + [weak], 5), (big, [weak]))
    self.assertEqual(wid.select_peaks(big + [weak], 3), (big, [weak]))
    weaker = [Peak(60, 70, 100, 100), Peak(80, 90, 100, 100), Peak(100, 110, 100, 100)]
    self.assertEqual(wid.select_peaks(big + weaker, 4), (big, weaker))

  def test_select_ends_with_the_weakest_limits(self) -> None:
    """After ten attempts the weakest limits decide."""
    big = [Peak(0, 10, 100, 1000), Peak(20, 30, 100, 1000), Peak(40, 50, 100, 1000)]
    faint = Peak(60, 62, 2, 11)
    # Average events 752.75: even the tenth attempt asks for more than 18 events.
    self.assertEqual(wid.select_peaks(big + [faint], 4), (big + [faint], []))
    fainter = Peak(60, 62, 2, 10)
    self.assertEqual(wid.select_peaks(big + [fainter], 4), (big, [fainter]))


class WellRuleTests(unittest.TestCase):
  """The rules that place one well."""

  averages = PeakAverages(10.0, 40.0, 400.0)

  def test_score(self) -> None:
    """A candidate scores on width and height against the averages."""
    # Good means more than 30 % of the average width (3) and height (12).
    self.assertEqual(wid.peak_score(Peak(0, 4, 13, 1), self.averages), 1)
    self.assertEqual(wid.peak_score(Peak(0, 3, 13, 1), self.averages), 0)
    self.assertEqual(wid.peak_score(Peak(0, 4, 12, 1), self.averages), 0)
    self.assertEqual(wid.peak_score(Peak(0, 3, 12, 1), self.averages), -1)

  def test_best_candidate(self) -> None:
    """The first candidate with the highest score wins."""
    good, narrow, slight = Peak(0, 10, 39, 1), Peak(0, 3, 39, 1), Peak(0, 3, 12, 1)
    self.assertEqual(wid.best_candidate([narrow, good, good], self.averages), 1)
    self.assertEqual(wid.best_candidate([slight, narrow, narrow], self.averages), 1)
    self.assertEqual(wid.best_candidate([narrow, slight], self.averages), 0)
    self.assertEqual(wid.best_candidate([slight], self.averages), 0)

  def test_combine_two_pieces_of_a_well(self) -> None:
    """Two pieces are joined when the gap between them fits the up time."""
    # Sip 10 bins, up time 5 bins: the gap may be 4 bins (5 / 1.1).
    self.assertTrue(wid.should_combine(peak(4, 8), peak(10, 14), 10.0, 5.0))
    self.assertTrue(wid.should_combine(peak(4, 8), peak(12, 14), 10.0, 5.0))
    self.assertFalse(wid.should_combine(peak(4, 8), peak(13, 14), 10.0, 5.0))

  def test_combine_depends_on_the_width_of_the_whole(self) -> None:
    """Pieces are joined only into something about a sip wide."""
    # Together 12 bins is more than 1.1 sips, which is fine only while the first piece is
    # shorter than 0.8 sips (8 bins).
    self.assertTrue(wid.should_combine(peak(4, 11), peak(12, 16), 10.0, 5.0))
    self.assertFalse(wid.should_combine(peak(4, 12), peak(13, 16), 10.0, 5.0))
    self.assertTrue(wid.should_combine(peak(4, 12), peak(13, 14), 10.0, 5.0))
    # Together 17 bins is more than 1.65 sips, whatever the pieces.
    self.assertTrue(wid.should_combine(peak(4, 9), peak(13, 20), 10.0, 5.0))
    self.assertFalse(wid.should_combine(peak(4, 9), peak(13, 21), 10.0, 5.0))

  def test_end_of_a_narrow_well(self) -> None:
    """A well is at least 0.8 sips wide."""
    # Narrower than 0.8 sips (8 bins): widened to that.
    self.assertEqual(wid.well_end(10, 15, [0] * 40, 10.0), 18)
    self.assertEqual(wid.well_end(10, 17, [9] * 40, 10.0), 18)

  def test_end_of_a_well_of_about_a_sip(self) -> None:
    """A well grows over busy bins while it stays under 1.1 sips."""
    quiet_after = [9] * 20 + [1, 1, 9, 9]
    busy_after = [9] * 20 + [3, 9, 9, 9]
    one_quiet_bin = [9] * 20 + [1, 2, 0, 0]
    # A well 9 bins wide grows while it stays under 1.1 sips (11 bins) ...
    self.assertEqual(wid.well_end(10, 19, busy_after, 10.0), 20)
    self.assertEqual(wid.well_end(10, 18, busy_after, 10.0), 20)
    # ... and stops where the next two bins are quiet; one quiet bin is not enough.
    self.assertEqual(wid.well_end(10, 19, quiet_after, 10.0), 19)
    self.assertEqual(wid.well_end(10, 19, one_quiet_bin, 10.0), 20)
    # Bins beyond the histogram are quiet.
    self.assertEqual(wid.well_end(10, 19, [9] * 20, 10.0), 19)

  def test_end_of_a_wide_well(self) -> None:
    """A wide well ends where its peak ends."""
    self.assertEqual(wid.well_end(10, 21, [9] * 40, 10.0), 21)
    self.assertEqual(wid.well_end(10, 45, [9] * 40, 10.0), 45)

  def test_overlap_moves_the_start(self) -> None:
    """A well that reaches into the previous one starts after it."""
    spans = [WellSpan(10, 20), WellSpan(18, 30), WellSpan(40, 50)]
    wid.resolve_overlap(spans, 1, 10.0)
    self.assertEqual((spans[1].start, spans[1].end), (21, 30))
    touching = [WellSpan(10, 20), WellSpan(20, 30)]
    wid.resolve_overlap(touching, 1, 10.0)
    self.assertEqual((touching[1].start, touching[1].end), (21, 30))

  def test_overlap_leaves_separate_wells_alone(self) -> None:
    """Wells that do not overlap stay put."""
    spans = [WellSpan(10, 20), WellSpan(21, 23), WellSpan(40, 50)]
    wid.resolve_overlap(spans, 1, 10.0)
    wid.resolve_overlap(spans, 0, 10.0)
    self.assertEqual([(span.start, span.end) for span in spans], [(10, 20), (21, 23), (40, 50)])

  def test_overlap_keeps_a_minimum_width_where_there_is_room(self) -> None:
    """A well cut short is widened to 0.8 sips if the next well allows, else left one bin wide."""
    # Moved to start at 21, the well is 3 bins wide; 0.8 sips is 8 bins.
    roomy = [WellSpan(10, 20), WellSpan(18, 24), WellSpan(30, 50)]
    wid.resolve_overlap(roomy, 1, 10.0)
    self.assertEqual((roomy[1].start, roomy[1].end), (21, 29))
    cramped = [WellSpan(10, 20), WellSpan(18, 24), WellSpan(29, 50)]
    wid.resolve_overlap(cramped, 1, 10.0)
    self.assertEqual((cramped[1].start, cramped[1].end), (21, 22))
    last = [WellSpan(10, 20), WellSpan(18, 24)]
    wid.resolve_overlap(last, 1, 10.0)
    self.assertEqual((last[1].start, last[1].end), (21, 29))

  def test_centre_one_missing_well(self) -> None:
    """One flagged well is centred between its found neighbours."""
    spans = [WellSpan(104, 114), WellSpan(125, 145, flagged=True), WellSpan(154, 164)]
    wid.centre_missing_wells(spans, 0, 2)
    # 39 free bins, 115 to 153; a well 20 wide leaves 18, split 9 and 9.
    self.assertEqual((spans[1].start, spans[1].end), (124, 144))
    self.assertTrue(spans[1].flagged)

  def test_centre_two_missing_wells(self) -> None:
    """Two flagged wells are centred in the two halves of the gap."""
    spans = [
      WellSpan(90, 100),
      WellSpan(0, 20, flagged=True),
      WellSpan(500, 520, flagged=True),
      WellSpan(161, 171),
    ]
    wid.centre_missing_wells(spans, 0, 3)
    # 60 free bins, 101 to 160, in two slots of 30.
    self.assertEqual([(span.start, span.end) for span in spans[1:3]], [(105, 125), (135, 155)])

  def test_centre_narrows_wells_to_their_slot(self) -> None:
    """A flagged well wider than its slot takes the slot."""
    spans = [WellSpan(90, 100), WellSpan(0, 20, flagged=True), WellSpan(111, 121)]
    wid.centre_missing_wells(spans, 0, 2)
    # 10 free bins, 101 to 110.
    self.assertEqual((spans[1].start, spans[1].end), (101, 110))

  def test_centre_without_room(self) -> None:
    """Without a bin each, flagged wells cover no bin."""
    spans = [
      WellSpan(90, 100),
      WellSpan(0, 20, flagged=True),
      WellSpan(0, 20, flagged=True),
      WellSpan(102, 112),
    ]
    wid.centre_missing_wells(spans, 0, 3)
    # One free bin for two wells: both are left covering no bin.
    self.assertEqual([(span.start, span.end) for span in spans[1:3]], [(101, 100), (101, 100)])

  def test_centre_needs_a_found_well_before_in_the_same_run(self) -> None:
    """Flagged wells at the start of a run, and unflagged wells, are not moved."""
    spans = [WellSpan(90, 100), WellSpan(0, 20, flagged=True), WellSpan(161, 171)]
    wid.centre_missing_wells(spans, 1, 2)
    self.assertEqual((spans[1].start, spans[1].end), (0, 20))
    unflagged = [WellSpan(90, 100), WellSpan(0, 20), WellSpan(161, 171)]
    wid.centre_missing_wells(unflagged, 0, 2)
    self.assertEqual((unflagged[1].start, unflagged[1].end), (0, 20))

  def test_nearest_peak(self) -> None:
    """The unused peak nearest the expected midpoint is chosen."""
    peaks = [peak(100, 110), peak(130, 140), peak(160, 170)]
    previous = WellSpan(40, 50, post_gap=200)
    # Midpoints 105, 135 and 165; the well is expected around 139.
    self.assertEqual(wid.nearest_peak(peaks, 0, WellSpan(134, 144), previous, self.averages), 1)
    self.assertEqual(wid.nearest_peak(peaks, 0, WellSpan(155, 175), previous, self.averages), 2)
    self.assertEqual(wid.nearest_peak(peaks, 0, WellSpan(60, 80), previous, self.averages), 0)
    self.assertEqual(wid.nearest_peak(peaks, 1, WellSpan(60, 80), previous, self.averages), 1)
    self.assertEqual(wid.nearest_peak(peaks, 2, WellSpan(60, 80), previous, self.averages), 2)

  def test_nearest_peak_keeps_a_peak_that_starts_the_run(self) -> None:
    """A full peak well after the previous run is kept over a nearer, later one."""
    peaks = [peak(100, 110), peak(130, 140), peak(160, 170)]
    expected = WellSpan(125, 145)
    # The first unused peak is a full one, 50 bins after the previous well, where timing
    # gives a pause of 40 bins: it is the start of the run although a later peak is nearer.
    self.assertEqual(wid.nearest_peak(peaks, 0, expected, WellSpan(40, 50, 40), self.averages), 0)
    # Not so when it follows the previous well too closely for the pause ...
    self.assertEqual(wid.nearest_peak(peaks, 0, expected, WellSpan(40, 50, 100), self.averages), 1)
    # ... when it is a slight peak ...
    slight = [peak(100, 110, event_count=200)] + peaks[1:]
    self.assertEqual(wid.nearest_peak(slight, 0, expected, WellSpan(40, 50, 40), self.averages), 1)
    # ... or when the nearer peak starts before the well is expected to.
    self.assertEqual(
      wid.nearest_peak(peaks, 0, WellSpan(130, 150), WellSpan(40, 50, 40), self.averages), 1
    )

  def test_average_run_gap(self) -> None:
    """The usual gap is the mean over matched runs, or 50 bins when unknown."""
    spans = [WellSpan(0, 20), WellSpan(30, 50), WellSpan(90, 110), WellSpan(120, 140)]
    spans += [WellSpan(200, 220), WellSpan(230, 250)]
    runs = [(0, 1), (2, 3), (4, 5)]
    # Run 2 starts 40 bins after run 1 ends, run 3 starts 60 bins after run 2 ends.
    self.assertEqual(wid.average_run_gap(spans, runs, 0), 50.0)
    self.assertEqual(wid.average_run_gap(spans, runs, 1), 50.0)
    self.assertEqual(wid.average_run_gap(spans, runs, 2), 40.0)
    self.assertEqual(wid.average_run_gap(spans, runs, 3), 50.0)
    spans[2].shift(-40)
    spans[3].shift(-40)
    # A mean of 0 counts as unknown.
    self.assertEqual(wid.average_run_gap(spans, runs, 2), 50.0)


class CheckRuleTests(unittest.TestCase):
  """The checks made on found wells after matching."""

  wells = [SampledWell(f"W{index}", 2.5 * index, 2.5 * index + 2.5) for index in range(5)]

  def test_timing_tolerance(self) -> None:
    """A well fails when its offset is more than 0.45 of sip plus up from its run's median."""
    # Every well starts 25 s after its command, except W2 by 25.5 s and W4 by 25.7 s. With a
    # sip of 1 s and an up time of 0.5 s the tolerance is 0.675 s.
    spans = [WellSpan(start, start + 10) for start in (250, 275, 305, 325, 357)]
    self.assertEqual(wid.timing_outliers(self.wells, spans, [(0, 4)], 0, 1.0, 0.5), [4])
    # Positions count from the histogram's first bin.
    shifted = [WellSpan(span.start - 100, span.end - 100) for span in spans]
    self.assertEqual(wid.timing_outliers(self.wells, shifted, [(0, 4)], 100, 1.0, 0.5), [4])
    # A longer sip and up time widen the tolerance.
    self.assertEqual(wid.timing_outliers(self.wells, spans, [(0, 4)], 0, 1.5, 0.5), [])

  def test_timing_against_each_runs_median(self) -> None:
    """Each run has its own offset; within one, wells half a run apart all fail."""
    wells = [
      SampledWell("A1", 0.0, 2.5),
      SampledWell("A2", 2.5, 5.0),
      SampledWell("B1", 9.0, 11.5, True),
      SampledWell("B2", 11.5, 14.0),
    ]
    # Row B arrives 3 s later than timed: another offset, but the same spacing.
    spans = [WellSpan(250, 260), WellSpan(275, 285), WellSpan(370, 380), WellSpan(395, 405)]
    runs = wid.sampling_runs(wells)
    self.assertEqual(wid.timing_outliers(wells, spans, runs, 0, 1.0, 0.5), [])
    # Taken as one run the median lies halfway, 1.5 s from every well.
    self.assertEqual(wid.timing_outliers(wells, spans, [(0, 3)], 0, 1.0, 0.5), [0, 1, 2, 3])

  def test_timing_leaves_flagged_wells_out(self) -> None:
    """Flagged wells are neither checked nor counted in the median."""
    spans = [WellSpan(250, 260), WellSpan(400, 420, flagged=True), WellSpan(300, 310)]
    self.assertEqual(wid.timing_outliers(self.wells[:3], spans, [(0, 2)], 0, 1.0, 0.5), [])
    flagged = [WellSpan(250, 260, flagged=True), WellSpan(400, 420, flagged=True)]
    self.assertEqual(wid.timing_outliers(self.wells[:2], flagged, [(0, 1)], 0, 1.0, 0.5), [])

  def test_width_limits(self) -> None:
    """A peak narrower than half a sip or wider than 1.65 sips fails."""
    # With a sip of 10 bins a peak may be 5 to 16.5 bins wide.
    spans = [
      WellSpan(0, 8, peak=Peak(0, 4, 39, 160)),
      WellSpan(20, 28, peak=Peak(20, 25, 39, 200)),
      WellSpan(40, 56, peak=Peak(40, 56, 39, 640)),
      WellSpan(60, 77, peak=Peak(60, 77, 39, 680)),
      WellSpan(80, 100, flagged=True),
    ]
    self.assertEqual(wid.width_outliers(spans, 10.0), [0, 3])

  def test_width_needs_the_peak_of_a_found_well(self) -> None:
    """A well that is neither flagged nor on a peak is an error."""
    with self.assertRaisesRegex(RuntimeError, "well 0 is not flagged but took no peak"):
      wid.width_outliers([WellSpan(0, 10)], 10.0)


# ---------------------------------------------------------------------------------------------
# Whole plate reads
# ---------------------------------------------------------------------------------------------


class CleanRunTests(unittest.TestCase):
  """Every well gives a clean burst of events."""

  def test_96_wells(self) -> None:
    """Every well of a clean 96-well read gets exactly its events."""
    run = PlateRun()
    result = run.identify()
    self.assertEqual(result.verdict, "identified")
    self.assertEqual(result.message, "all 96 wells were found")
    self.assertEqual(result.flagged_wells, ())
    self.assertEqual([well.well for well in result.wells], well_names(8, 12))
    self.assertEqual(found_ranges(result), run.ranges)
    self.assertEqual(result.event_ranges(), run.ranges)
    self.assertEqual(result.wells[0].event_range, (0, 399))
    self.assertEqual(result.wells[0].event_count, 400)
    self.assertEqual(result.wells[95].event_range, (38000, 38399))

  def test_times_of_wells(self) -> None:
    """Wells are timed on the event clock, by bins."""
    run = PlateRun()
    result = run.identify()
    # A01 is drawn 4.0 s into the command log and reaches the detector 25 s later. The
    # histogram starts there, so the well starts in its first bin.
    self.assertEqual((result.wells[0].start_time, result.wells[0].end_time), (29.0, 29.9))
    # A02 follows 2.5 s later; a well starts one bin before its first busy bin.
    self.assertEqual((result.wells[1].start_time, result.wells[1].end_time), (31.4, 32.4))
    # B01 comes after eleven more wells and a 4 s pause: 29.0 + 12 * 2.5 + 4.0 = 63.0 s.
    self.assertEqual((result.wells[12].start_time, result.wells[12].end_time), (62.9, 63.9))

  def test_384_wells(self) -> None:
    """Every well of a clean 384-well read gets exactly its events."""
    run = PlateRun(rows=16, columns=24, sip=0.5, up=0.4, move=0.6, pause=3.0)
    result = run.identify()
    self.assertEqual(result.verdict, "identified")
    self.assertEqual(len(result.wells), 384)
    self.assertEqual(found_ranges(result), run.ranges)
    self.assertEqual(result.wells[383].well, "P24")
    self.assertEqual(result.wells[383].event_range, (76600, 76799))

  def test_times_given_as_a_list_or_in_double_precision(self) -> None:
    """Any sequence of times gives the same result."""
    run = PlateRun(rows=2, columns=6)
    expected = found_ranges(run.identify())
    self.assertEqual(expected, run.ranges)
    self.assertEqual(found_ranges(run.identify(list(run.times))), expected)
    self.assertEqual(found_ranges(run.identify(array.array("d", run.times))), expected)

  def test_command_clock_offset_does_not_matter(self) -> None:
    """Only the spacing of the command times is used."""
    run = PlateRun(rows=2, columns=6, empty=("A04",))
    expected = found_ranges(run.identify())
    shifted = [
      SampledWell(well.well, well.start + 86400.0, well.end + 86400.0, well.after_pause)
      for well in run.wells
    ]
    result = wid.identify_wells(run.times, shifted, run.sip, run.up)
    self.assertEqual(found_ranges(result), expected)


class EmptyWellTests(unittest.TestCase):
  """Wells that gave no events."""

  def assert_only_flagged(self, run: PlateRun, flagged: Sequence[str]) -> wid.WellIdentification:
    """Check that exactly the given wells are flagged and every well has its events."""
    result = run.identify()
    self.assertEqual(result.verdict, "needs_review")
    self.assertEqual(result.flagged_wells, tuple(flagged))
    self.assertEqual(found_ranges(result), run.ranges)
    return result

  def test_one_empty_well(self) -> None:
    """An empty well is flagged and centred between its neighbours; the verdict warns."""
    run = PlateRun(empty=("C05",))
    with self.assertLogs(wid.logger, "WARNING"):
      result = self.assert_only_flagged(run, ["C05"])
    self.assertEqual(
      result.message,
      "1 of 96 wells were placed by timing because no peak was found for them: C05",
    )
    before, missing, after = result.wells[27:30]
    self.assertEqual((missing.well, missing.event_range, missing.event_count), ("C05", None, 0))
    # C04 ends at 105.4 s and C06 starts at 109.4 s; C05, 2 s wide as its command says, is
    # put in the middle of the 39 bins between them.
    self.assertEqual((before.end_time, after.start_time), (105.4, 109.4))
    self.assertEqual((missing.start_time, missing.end_time), (106.4, 108.4))
    self.assertEqual((before.flagged, after.flagged), (False, False))

  def test_neighbouring_empty_wells(self) -> None:
    """Two empty wells in a row are spread over the gap."""
    result = self.assert_only_flagged(PlateRun(empty=("C05", "C06")), ["C05", "C06"])
    c05, c06 = result.wells[28], result.wells[29]
    # 64 free bins between C04 and C07 in two slots of 32; each well is 20 bins wide.
    self.assertEqual((c05.start_time, c05.end_time), (106.0, 108.0))
    self.assertEqual((c06.start_time, c06.end_time), (109.2, 111.2))

  def test_empty_wells_at_the_ends_of_runs(self) -> None:
    """Empty wells at the end of a run, at the end of the plate and starting a run in pairs."""
    self.assert_only_flagged(PlateRun(empty=("A12",)), ["A12"])
    self.assert_only_flagged(PlateRun(empty=("H12",)), ["H12"])
    self.assert_only_flagged(PlateRun(empty=("B01", "B02")), ["B01", "B02"])

  def test_empty_run(self) -> None:
    """A whole empty row is flagged where timing puts it."""
    row_d = [f"D{column:02d}" for column in range(1, 13)]
    result = self.assert_only_flagged(PlateRun(empty=row_d), row_d)
    # The wells stay where the sampler's timing puts them: 2.5 s apart.
    starts = [well.start_time for well in result.wells[36:48]]
    self.assertEqual(
      [round(later - earlier, 6) for earlier, later in zip(starts, starts[1:])], [2.5] * 11
    )

  def test_empty_last_runs(self) -> None:
    """Rows after the last peak are flagged once no peak is left."""
    rows_g_h = [f"{row}{column:02d}" for row in "GH" for column in range(1, 13)]
    self.assert_only_flagged(PlateRun(empty=rows_g_h), rows_g_h)

  def test_empty_wells_in_384(self) -> None:
    """Empty wells of a 384-well read are flagged."""
    run = PlateRun(rows=16, columns=24, sip=0.5, up=0.4, move=0.6, pause=3.0, empty=("B03", "K17"))
    self.assert_only_flagged(run, ["B03", "K17"])

  def test_an_empty_first_well_of_a_run_is_not_recognised(self) -> None:
    """A single empty well at the start of a run shifts that run by one well."""
    # The first peak after a pause is taken for the first well of the run when it is not
    # too far away. With F01 empty that is F02's peak: row F is off by one well, its last
    # well is left without a peak, and the verdict asks for a review.
    run = PlateRun(empty=("F01",))
    result = run.identify()
    self.assertEqual(result.verdict, "needs_review")
    self.assertEqual(result.flagged_wells, ("F12",))
    found = found_ranges(result)
    self.assertEqual(found["F01"], run.ranges["F02"])
    self.assertEqual(
      {name: found[name] for name in found if name[0] != "F"},
      {name: run.ranges[name] for name in found if name[0] != "F"},
    )

  def test_an_empty_first_well_of_the_plate_is_not_recognised(self) -> None:
    """An empty first well shifts every run by one well."""
    # Expected wells are anchored on the first peak, so every run is off by one well.
    result = PlateRun(empty=("A01",)).identify()
    self.assertEqual(result.verdict, "needs_review")
    self.assertEqual(result.flagged_wells, tuple(f"{row}12" for row in "ABCDEFGH"))


class DistortedPeakTests(unittest.TestCase):
  """Wells whose bursts are split or run into each other."""

  def test_split_peak(self) -> None:
    """Two pieces of one well, two quiet bins apart, are joined."""
    # No events for 0.2 s in the middle of D07: two peaks, two quiet bins apart.
    run = PlateRun(keep_tick={"D07": lambda tick: not 80 <= tick < 120})
    result = run.identify()
    self.assertEqual(result.verdict, "identified")
    self.assertEqual(found_ranges(result), run.ranges)
    self.assertEqual(result.wells[42].event_count, 320)

  def test_split_peak_with_one_quiet_bin(self) -> None:
    """Pieces one quiet bin apart are one peak from the start."""
    run = PlateRun(keep_tick={"A01": lambda tick: not 100 <= tick < 120})
    result = run.identify()
    self.assertEqual(result.verdict, "identified")
    self.assertEqual(found_ranges(result), run.ranges)

  def test_split_first_well_of_a_run(self) -> None:
    """A split well that starts a run is joined as well."""
    split = {"E01": lambda tick: not 60 <= tick < 100}
    run = PlateRun(keep_tick=split, empty=("B07", "C03"))
    result = run.identify()
    self.assertEqual(result.flagged_wells, ("B07", "C03"))
    self.assertEqual(found_ranges(result), run.ranges)

  def test_as_many_peaks_as_wells_are_paired_in_order(self) -> None:
    """With one peak per well, peaks and wells are paired without regard to timing.

    The timing check afterwards finds that the pairing breaks the sampler's spacing.
    """
    # With one peak per well the sampler's timing is not consulted: three bursts at 30 s,
    # 31.5 s and 40 s go to three wells that were sampled 2.5 s apart.
    wells = [SampledWell(name, 2.5 * index, 2.5 * index + 2.5) for index, name in enumerate("ABC")]
    times = [start + tick * TICK for start in (30.0, 31.5, 40.0) for tick in range(200)]
    with self.assertLogs(wid.logger, "WARNING"):
      result = wid.identify_wells(times, wells, 1.0, 0.5)
    self.assertEqual(found_ranges(result), {"A": (0, 199), "B": (200, 399), "C": (400, 599)})
    self.assertEqual([well.start_time for well in result.wells], [30.0, 31.4, 39.9])
    # Less their commands' starts the wells start at 30.0, 28.9 and 34.9 s. B and C are more
    # than 0.675 s (0.45 of sip plus up) from the median, 30.0 s. And slid by two periods the
    # run would leave A's and B's windows for C's peak and an empty one: as good a fit.
    self.assertEqual(result.verdict, "needs_review")
    self.assertEqual(result.flagged_wells, ())
    self.assertEqual(result.inconsistent_wells, ("A", "B", "C"))
    self.assertEqual(
      result.message,
      "3 of 3 wells failed a check: A (ambiguous_anchor), B (timing and ambiguous_anchor), "
      "C (timing and ambiguous_anchor)",
    )

  def bridged(self, run: PlateRun, well: str, per_tick: int) -> Tuple[array.array, int]:
    """Return the run's events with the air gap after a well filled in, and the fill's size."""
    start = round((run.sip_starts[well] + run.sip) / TICK)
    fill = [(start + tick) * TICK for tick in range(300) for _ in range(per_tick)]
    return array.array("f", sorted(list(run.times) + fill)), len(fill)

  def test_two_wells_merged_by_a_low_bridge(self) -> None:
    """Wells joined by a low bridge are separated by the threshold."""
    # Carry-over fills the 1.5 s between D07 and D08 with 20 events per bin, half the
    # height of a well. The threshold rises to 20 and the two wells come apart.
    run = PlateRun()
    times, fill = self.bridged(run, "D07", per_tick=1)
    self.assertEqual(fill, 300)
    result = run.identify(times)
    self.assertEqual(result.verdict, "identified")
    found = found_ranges(result)
    names = well_names(8, 12)
    for name in names[:43]:
      self.assertEqual(found[name], run.ranges[name])
    # D08 starts one bin early, as every well does, and so takes the bridge's last bin.
    self.assertEqual(found["D08"], (17200 + 300 - 20, 17599 + 300))
    for name in names[44:]:
      first, last = run.ranges[name] or (0, 0)
      self.assertEqual(found[name], (first + 300, last + 300))

  def test_two_wells_merged_flat(self) -> None:
    """Wells joined at full height cannot be separated; the verdict warns."""
    # A bridge as high as the wells: no threshold separates D07 from D08. One well takes
    # the whole stretch, the rest of the row slips by one well and its last well is left
    # without a peak. Other rows are not affected.
    run = PlateRun()
    times, fill = self.bridged(run, "D07", per_tick=2)
    result = run.identify(times)
    self.assertEqual(result.verdict, "needs_review")
    self.assertEqual(result.flagged_wells, ("D12",))
    found = found_ranges(result)
    self.assertEqual(found["D07"], (16800, 16800 + 400 + fill + 400 - 1))
    names = well_names(8, 12)
    for name in names[:42]:
      self.assertEqual(found[name], run.ranges[name])
    for name in names[48:]:
      first, last = run.ranges[name] or (0, 0)
      self.assertEqual(found[name], (first + fill, last + fill))

  def test_two_wells_merged_flat_fail_the_checks(self) -> None:
    """The well that took both and the wells that slipped are named, and so is the whole row,
    whose last well has no peak."""
    # D07's peak is 3.5 sips wide; D08 to D11 each start one well, 2.5 s, late; D12 is left
    # without a peak, so row D may be a well late.
    run = PlateRun()
    times, _ = self.bridged(run, "D07", per_tick=2)
    result = run.identify(times)
    self.assertEqual(result.inconsistent_wells, tuple(row_wells("D", range(1, 12))))
    self.assertEqual(result.wells[36].failed_checks, ("run_shift",))
    self.assertEqual(result.wells[42].failed_checks, ("width", "run_shift"))
    self.assertEqual(result.wells[43].failed_checks, ("timing", "run_shift"))
    self.assertTrue(
      result.message.startswith(
        "1 of 96 wells were placed by timing because no peak was found for them: D12; "
        "11 of 96 wells failed a check: D01 (run_shift), "
      )
    )


class ClockTests(unittest.TestCase):
  """The sampler's clock and the event clock disagree."""

  def test_drift_and_longer_pauses(self) -> None:
    """A fast event clock and longer pauses are followed from well to well and run to run."""
    # The event clock runs 0.4 % fast and every pause is 0.8 s longer than timed.
    run = PlateRun(empty=("B03",), clock_rate=1.004, pause_extra=0.8)
    result = run.identify()
    self.assertEqual(result.flagged_wells, ("B03",))
    self.assertEqual(found_ranges(result), run.ranges)
    # H12 is drawn 269.5 s into the command log: 269.5 * 1.004 + 25 + 7 * 0.8 = 301.178 s,
    # 301.18 s on the 5 ms grid. Its events fall in bins 3011 to 3021.
    self.assertAlmostEqual(run.sip_starts["H12"], 301.18, places=9)
    self.assertEqual((result.wells[95].start_time, result.wells[95].end_time), (301.0, 302.1))

  def test_slow_clock_and_much_longer_pauses(self) -> None:
    """A first peak outside the search window is still found by nearness."""
    # Pauses 3 s longer than timed put each row's first peak outside its search window.
    run = PlateRun(empty=("B03", "G10"), clock_rate=0.996, pause_extra=3.0)
    result = run.identify()
    self.assertEqual(result.flagged_wells, ("B03", "G10"))
    self.assertEqual(found_ranges(result), run.ranges)

  def test_shorter_pauses(self) -> None:
    """Pauses shorter than timed are followed too."""
    run = PlateRun(empty=("E05",), pause_extra=-1.2)
    result = run.identify()
    self.assertEqual(result.flagged_wells, ("E05",))
    self.assertEqual(found_ranges(result), run.ranges)


class NoiseTests(unittest.TestCase):
  """Events that belong to no well."""

  def test_noise_before_the_first_well(self) -> None:
    """Priming noise in the first 20 s and a blip after it are not taken for wells."""
    run = PlateRun()
    # A burst from priming, 3 to 5 s into the acquisition, and a blip at 22 s.
    burst = [3.0 + tick * TICK for tick in range(400) for _ in range(3)]
    blip = [22.0 + tick * 0.01 for tick in range(8)]
    result = run.identify(array.array("f", burst + blip + list(run.times)))
    self.assertEqual(result.verdict, "identified")
    noise = len(burst) + len(blip)
    self.assertEqual(noise, 1208)
    for well in result.wells:
      first, last = run.ranges[well.well] or (0, 0)
      self.assertEqual(well.event_range, (first + noise, last + noise))
    # With the burst the histogram starts at 3.0 s, and A01 one bin before its first busy bin.
    self.assertEqual((result.wells[0].start_time, result.wells[0].end_time), (28.9, 29.9))

  def test_stray_events_between_wells(self) -> None:
    """Single stray events make no peaks; a few fall inside wells."""
    run = PlateRun()
    # One event every 0.3 s over the whole read never makes a bin busy.
    stray = [28.05 + index * 0.3 for index in range(900)]
    times = array.array("f", sorted(stray + list(run.times)))
    result = run.identify(times)
    self.assertEqual(result.verdict, "identified")
    in_wells = sum(well.event_count for well in result.wells)
    self.assertGreaterEqual(in_wells, 96 * 400)
    self.assertLess(in_wells, 96 * 400 + 96 * 5)
    self.assertEqual([well.event_count >= 400 for well in result.wells], [True] * 96)


def wrong_ranges(result: wid.WellIdentification, run: PlateRun) -> List[str]:
  """Return the wells whose event range is not the one the read gave them."""
  return [well.well for well in result.wells if well.event_range != run.ranges[well.well]]


def row_wells(row: str, columns: Sequence[int]) -> List[str]:
  """Return the names of some wells of one row of a 96-well plate."""
  return [f"{row}{column:02d}" for column in columns]


class WrongAssignmentTests(unittest.TestCase):
  """Reads matched wrongly, and the wells the checks name."""

  def test_an_empty_and_a_split_well_paired_in_order(self) -> None:
    """One peak per well, from an empty well and a split one, fails the timing check."""
    # C05 is empty and D07 split into two peaks, so there are as many peaks as wells and they
    # are paired in order: from C05 on every well takes the next well's peak, until D06 takes
    # the first piece of D07 and D07 the second. Nothing is flagged.
    run = PlateRun(empty=("C05",), keep_tick={"D07": lambda tick: not 60 <= tick < 100})
    with self.assertLogs(wid.logger, "WARNING"):
      result = run.identify()
    self.assertEqual(result.flagged_wells, ())
    self.assertEqual(
      wrong_ranges(result, run), row_wells("C", range(5, 13)) + row_wells("D", range(1, 8))
    )
    # The median sides with the majority of a run. Most of row C is a well late, so the four
    # wells before C05 fail the timing check, and C12, which took D01's peak after the shake.
    # Row D is split about evenly, its median lies between the two, and every well of it
    # fails. Both rows fit the events nearly as well slid by a well, so all of them are named.
    self.assertEqual(result.verdict, "needs_review")
    self.assertEqual(
      result.inconsistent_wells, tuple(row_wells("C", range(1, 13)) + row_wells("D", range(1, 13)))
    )
    timing = [well.well for well in result.wells if "timing" in well.failed_checks]
    self.assertEqual(timing, row_wells("C", [1, 2, 3, 4, 12]) + row_wells("D", range(1, 13)))
    # D06's piece of D07 is 0.3 sips wide.
    self.assertEqual(result.wells[41].failed_checks, ("timing", "width", "ambiguous_anchor"))

  def test_an_empty_well_and_noise_paired_in_order(self) -> None:
    """One peak per well, from an empty well and a burst of noise, fails the timing check."""
    # C05 is empty and 0.3 s of noise follows E03: from C05 on every well takes the next
    # well's peak, until E03 takes the noise.
    run = PlateRun(empty=("C05",))
    start = run.sip_starts["E03"] + run.sip + 0.6
    noise = [start + tick * TICK for tick in range(60) for _ in range(2)]
    result = run.identify(array.array("f", sorted(list(run.times) + noise)))
    self.assertEqual(result.flagged_wells, ())
    found = found_ranges(result)
    self.assertEqual(found["C05"], run.ranges["C06"])
    self.assertEqual(found["E02"], run.ranges["E03"])
    first, last = run.ranges["E04"] or (0, 0)
    self.assertEqual(found["E04"], (first + len(noise), last + len(noise)))
    # Row D is all a well late and keeps its spacing: only D12, which took E01's peak after
    # the shake, fails the timing check, but the row fits the events as well slid back a well,
    # so every well of it is named. E03's noise is 0.3 sips wide.
    self.assertEqual(result.verdict, "needs_review")
    self.assertEqual(
      result.inconsistent_wells,
      tuple(
        row_wells("C", range(1, 13)) + row_wells("D", range(1, 13)) + row_wells("E", [1, 2, 3])
      ),
    )
    self.assertEqual(result.wells[50].failed_checks, ("timing", "width"))

  def test_an_empty_well_where_the_search_reaches_the_next_well(self) -> None:
    """With short up and move times an empty well takes its neighbour's peak."""
    # Sip 0.5 s, up 0.2 s, move 0.3 s: B09 is looked for over its 0.8 s and 0.5 s either side,
    # which reaches B10's peak. B09 to B11 each take the next well's peak and B12 is flagged.
    # B12 is left without a peak, so the whole row is named.
    run = PlateRun(sip=0.5, up=0.2, move=0.3, empty=("B09",))
    result = run.identify()
    self.assertEqual(wrong_ranges(result, run), row_wells("B", range(9, 13)))
    self.assertEqual(result.flagged_wells, ("B12",))
    self.assertEqual(result.inconsistent_wells, tuple(row_wells("B", range(1, 12))))
    timing = [well.well for well in result.wells if "timing" in well.failed_checks]
    self.assertEqual(timing, row_wells("B", [9, 10, 11]))
    # With B05 empty most of row B slips, and the median names the four wells still in place.
    run = PlateRun(sip=0.5, up=0.2, move=0.3, empty=("B05",))
    result = run.identify()
    self.assertEqual(wrong_ranges(result, run), row_wells("B", range(5, 13)))
    self.assertEqual(result.flagged_wells, ("B12",))
    self.assertEqual(result.inconsistent_wells, tuple(row_wells("B", range(1, 12))))

  def test_a_whole_run_one_well_late_is_named(self) -> None:
    """A run that is all one well off keeps its spacing; its last well has no peak, and every
    well of it is named."""
    # A run's offset is set anew after every pause, so a run one well late cannot be told
    # from one after a longer pause whose last well is empty: the run is named either way.
    run = PlateRun(empty=("F01",))
    result = run.identify()
    self.assertEqual(wrong_ranges(result, run), row_wells("F", range(1, 13)))
    self.assertEqual(result.flagged_wells, ("F12",))
    self.assertEqual(result.inconsistent_wells, tuple(row_wells("F", range(1, 12))))
    self.assertEqual(result.wells[60].failed_checks, ("run_shift", "ambiguous_anchor"))
    # With A01 empty every run is one well late, and every well found is named.
    run = PlateRun(empty=("A01",))
    result = run.identify()
    self.assertEqual(misplaced_unnamed(result, run), [])
    self.assertEqual(len(result.inconsistent_wells), 88)
    self.assertEqual(result.verdict, "needs_review")


class FalseAlarmTests(unittest.TestCase):
  """Reads matched correctly pass the checks."""

  def assert_passes(self, run: PlateRun, flagged: Sequence[str] = ()) -> wid.WellIdentification:
    """Check that every well has its own events, the given ones are flagged and none fails."""
    result = run.identify()
    self.assertEqual(misplaced_wells(result, run), [])
    self.assertEqual(result.flagged_wells, tuple(flagged))
    self.assertEqual(result.inconsistent_wells, (), result.message)
    return result

  def test_clean_reads(self) -> None:
    """Clean 96- and 384-well reads pass."""
    for run in (PlateRun(), PlateRun(rows=16, columns=24, sip=0.5, up=0.4, move=0.6, pause=3.0)):
      self.assertEqual(self.assert_passes(run).verdict, "identified")

  def test_drift_and_pauses_of_other_lengths(self) -> None:
    """A clock running fast or slow and pauses longer or shorter than timed pass."""
    plate_384 = PlateRun(
      rows=16, columns=24, sip=0.5, up=0.4, move=0.6, pause=3.0, clock_rate=1.004, pause_extra=3.0
    )
    cases: List[Tuple[PlateRun, Tuple[str, ...]]] = [
      (PlateRun(clock_rate=1.004, pause_extra=0.8), ()),
      (PlateRun(clock_rate=0.996, pause_extra=3.0), ()),
      (PlateRun(pause_extra=-1.2), ()),
      (plate_384, ()),
      (PlateRun(empty=("B03",), clock_rate=1.004, pause_extra=0.8), ("B03",)),
      (PlateRun(empty=("B03", "G10"), clock_rate=0.996, pause_extra=3.0), ("B03", "G10")),
    ]
    for index, (run, flagged) in enumerate(cases):
      with self.subTest(case=index):
        self.assert_passes(run, flagged)

  def test_realistic_jitter(self) -> None:
    """Wells arriving with 50 ms of jitter, with Poisson counts, pass.

    Assumed: each well's sample reaches the detector with an error of 50 ms (standard
    deviation) against its sampling command, from the sampler's motion, the stamping of its
    commands and the pulsing of the pump together; events per 5 ms tick are Poisson.
    """
    rng = random.Random(20261111)
    for run in (
      PlateRun(rng=rng, jitter=0.05),
      PlateRun(rows=16, columns=24, sip=0.5, up=0.4, move=0.6, pause=3.0, rng=rng, jitter=0.05),
    ):
      self.assertEqual(self.assert_passes(run).verdict, "identified")

  def test_correctly_flagged_empty_wells(self) -> None:
    """Empty wells that are flagged leave the checks of their neighbours alone."""
    cases: List[Tuple[str, ...]] = [
      ("C05",),
      ("C05", "C06"),
      tuple(row_wells("D", range(1, 13))),
      tuple(row_wells("G", range(1, 13)) + row_wells("H", range(1, 13))),
    ]
    for empty in cases:
      with self.subTest(empty=empty):
        result = self.assert_passes(PlateRun(empty=empty), empty)
        self.assertNotIn("failed a check", result.message)
    run = PlateRun(rows=16, columns=24, sip=0.5, up=0.4, move=0.6, pause=3.0, empty=("B03", "K17"))
    self.assert_passes(run, ("B03", "K17"))

  def test_random_clean_reads(self) -> None:
    """No well of 50 random clean reads fails a check."""
    rng = random.Random(1103)
    for index in range(50):
      run = random_clean_run(rng)
      with self.subTest(read=index, sip=run.sip, up=run.up):
        self.assertEqual(self.assert_passes(run).verdict, "identified")


def misplaced_unnamed(result: wid.WellIdentification, run: PlateRun) -> List[str]:
  """Return the wells not given their own events and not named: neither flagged nor failing a
  check."""
  named = set(result.flagged_wells) | set(result.inconsistent_wells)
  return [well for well in misplaced_wells(result, run) if well not in named]


class AmbiguousRunTests(unittest.TestCase):
  """Runs the events cannot place, and how a measured transit delay places them."""

  def test_empty_wells_at_the_ends_of_runs_name_their_run(self) -> None:
    """An empty well at an end of a run leaves the run indistinguishable from one shifted by a
    well: the run is named."""
    cases: List[Tuple[Tuple[str, ...], List[str], Tuple[str, ...]]] = [
      (("A12",), row_wells("A", range(1, 12)), ("run_shift", "ambiguous_anchor")),
      (("H12",), row_wells("H", range(1, 12)), ("run_shift", "ambiguous_anchor")),
      (("B01", "B02"), row_wells("B", range(3, 13)), ("ambiguous_anchor",)),
    ]
    for empty, named, checks in cases:
      with self.subTest(empty=empty):
        run = PlateRun(empty=empty)
        result = run.identify()
        self.assertEqual(misplaced_wells(result, run), [])
        self.assertEqual(result.flagged_wells, empty)
        self.assertEqual(result.inconsistent_wells, tuple(named))
        self.assertEqual(
          set(result.wells[well_names(8, 12).index(named[0])].failed_checks), set(checks)
        )

  def test_misplaced_wells_are_always_named(self) -> None:
    """Random reads with an empty well at the start or end of a run: every well that did not
    get its own events is flagged or named."""
    rng = random.Random(1111)
    for index in range(20):
      column = rng.choice([1, 2, 11, 12])
      row = rng.choice("ABCDEFGH")
      run = PlateRun(
        sip=rng.randint(8, 20) / 10,
        up=rng.randint(5, 10) / 10,
        move=rng.uniform(0.6, 1.5),
        pause=rng.uniform(2.0, 10.0),
        transit=rng.uniform(21.0, 40.0),
        empty=(f"{row}{column:02d}",),
        rng=rng,
        jitter=0.05,
      )
      with self.subTest(read=index, empty=run.wells and f"{row}{column:02d}"):
        result = run.identify()
        self.assertEqual(misplaced_unnamed(result, run), [])
        self.assertEqual(result.verdict, "needs_review")

  def identify_with_transit(self, run: PlateRun, times: Sequence[float], transit: float):
    """Identify with a transit delay; the run's event clock is its command clock."""
    return wid.identify_wells(
      times, run.wells, run.sip, run.up, transit_delay=transit, acquisition_start=0.0
    )

  def test_carry_over_before_the_first_well(self) -> None:
    """Sample left in the line, arriving after 20 s and before the first well, shifts the first
    run without a transit delay, and every shifted well is named; with one it is dropped."""
    run = PlateRun(rows=1, columns=12)
    # A01's command starts at 3 s, its events arrive at 29 s: a transit delay of 26 s.
    self.assertAlmostEqual(run.sip_starts["A01"], 29.0)
    bursts = [
      start + tick * TICK for start in (20.5, 23.0) for tick in range(200) for _ in range(2)
    ]
    times = array.array("f", sorted(bursts + list(run.times)))
    without = run.identify(times)
    self.assertNotEqual(misplaced_wells(without, run), [])
    self.assertEqual(misplaced_unnamed(without, run), [])
    with_transit = self.identify_with_transit(run, times, 26.0)
    self.assertEqual(with_transit.verdict, "identified", with_transit.message)
    first, last = run.ranges["A01"] or (0, 0)
    self.assertEqual(with_transit.wells[0].event_range, (first + len(bursts), last + len(bursts)))

  def test_carry_over_just_before_the_first_well(self) -> None:
    """Carry-over within the margin before the prediction is kept, but the first well is not
    placed on it. Exactly a sampling period before the first well, it looks as a well would
    with the transit delay a period too long, and the run is named."""
    run = PlateRun(rows=1, columns=12)
    bursts = [27.3 + tick * TICK for tick in range(120) for _ in range(2)]
    result = self.identify_with_transit(
      run, array.array("f", sorted(bursts + list(run.times))), 26.0
    )
    self.assertEqual(result.verdict, "identified", result.message)
    self.assertEqual(result.wells[0].start_time, 28.9)
    aligned = [26.5 + tick * TICK for tick in range(200) for _ in range(2)]
    result = self.identify_with_transit(
      run, array.array("f", sorted(aligned + list(run.times))), 26.0
    )
    self.assertEqual(result.wells[0].start_time, 28.9)
    self.assertEqual(result.inconsistent_wells, tuple(row_wells("A", range(1, 13))))

  def test_blank_first_well(self) -> None:
    """With the first well blank, every well takes its neighbour's events and the plate is
    named, unless a transit delay places the first well: then it alone is flagged."""
    run = PlateRun(empty=("A01",))
    self.assertEqual(misplaced_unnamed(run.identify(), run), [])
    result = self.identify_with_transit(run, run.times, 26.0)
    self.assertEqual(result.flagged_wells, ("A01",))
    self.assertEqual(misplaced_wells(result, run), [])
    # The other rows are matched anew after each shake; only the first run is placed.
    self.assertEqual(result.inconsistent_wells, ())

  def test_a_wrong_transit_delay_is_not_trusted(self) -> None:
    """A transit delay a sampling period off places the first run a well wrong; the run is
    named instead of exempted."""
    run = PlateRun(rows=1, columns=12)
    result = self.identify_with_transit(run, run.times, 26.0 + 2.5)
    self.assertEqual(result.verdict, "needs_review")
    self.assertEqual(misplaced_unnamed(result, run), [])

  def test_clean_reads_with_a_transit_delay(self) -> None:
    """Clean reads with their transit delay pass."""
    for run in (PlateRun(), PlateRun(rows=16, columns=24, sip=0.5, up=0.4, move=0.6, pause=3.0)):
      result = self.identify_with_transit(run, run.times, run.sip_starts[run.wells[0].well] - 3.0)
      self.assertEqual(result.verdict, "identified", result.message)
      self.assertEqual(misplaced_wells(result, run), [])

  def test_transit_arguments(self) -> None:
    """A transit delay needs the acquisition start, and must be positive."""
    run = PlateRun(rows=1, columns=6)
    with self.assertRaisesRegex(ValueError, "acquisition start"):
      wid.identify_wells(run.times, run.wells, run.sip, run.up, transit_delay=26.0)
    with self.assertRaisesRegex(ValueError, "positive"):
      self.identify_with_transit(run, run.times, 0.0)


class FailureTests(unittest.TestCase):
  """Input from which wells cannot be identified."""

  def setUp(self) -> None:
    """Make a short clean read."""
    self.plate = PlateRun(rows=2, columns=6)

  def test_too_few_events(self) -> None:
    """Events that form no peak cannot be identified."""
    for times in (
      [],
      [30.0],
      [30.0, 30.1, 30.2, 30.3, 30.4],
      [30.0 + index * 0.06 for index in range(2000)],
    ):
      with self.assertRaisesRegex(WellIdentificationError, "not enough event data"):
        self.plate.identify(times)

  def test_only_noise_before_the_first_sample(self) -> None:
    """Events in the first 20 s alone cannot be identified."""
    burst = [3.0 + tick * TICK for tick in range(400) for _ in range(3)]
    with self.assertRaisesRegex(WellIdentificationError, "not enough event data"):
      self.plate.identify(burst)

  def test_peaks_too_slight_to_be_wells(self) -> None:
    """Peaks that all fall below the filters cannot be identified."""
    # Two events in one bin: a peak, but below every filter.
    with self.assertRaisesRegex(WellIdentificationError, "not enough event data"):
      self.plate.identify([30.0, 30.01, 30.02])

  def test_times_out_of_order(self) -> None:
    """Event times that go backwards are refused."""
    times = list(self.plate.times)
    times[100], times[900] = times[900], times[100]
    with self.assertRaisesRegex(WellIdentificationError, "must not be negative or decrease"):
      self.plate.identify(times)

  def test_unusable_arguments(self) -> None:
    """No wells, bad sip or up times and wells out of order are refused."""
    wells = self.plate.wells
    times = self.plate.times
    with self.assertRaisesRegex(ValueError, "no sampled wells"):
      wid.identify_wells(times, [], 1.0, 0.5)
    with self.assertRaisesRegex(ValueError, "sip time must be positive"):
      wid.identify_wells(times, wells, 0.0, 0.5)
    with self.assertRaisesRegex(ValueError, "up time must not be negative"):
      wid.identify_wells(times, wells, 1.0, -0.5)
    with self.assertRaisesRegex(ValueError, "well A01 is out of order"):
      wid.identify_wells(times, [wells[1], wells[0]], 1.0, 0.5)
    with self.assertRaisesRegex(ValueError, "well X is out of order"):
      wid.identify_wells(times, [SampledWell("X", 5.0, 4.0)], 1.0, 0.5)
    with self.assertRaisesRegex(ValueError, "not a number"):
      wid.identify_wells(times, [SampledWell("X", 5.0, math.inf)], 1.0, 0.5)

  def test_event_ranges_need_unique_well_names(self) -> None:
    """Ranges by well name are refused when a name repeats."""
    repeated = [
      SampledWell("A01", well.start, well.end, well.after_pause) for well in self.plate.wells
    ]
    result = wid.identify_wells(self.plate.times, repeated, 1.0, 0.5)
    self.assertEqual(result.verdict, "identified")
    with self.assertRaisesRegex(ValueError, "A01 was sampled more than once"):
      result.event_ranges()


class SingleContainerTests(unittest.TestCase):
  """A tube: one container holds every event."""

  def test_all_events_belong_to_it(self) -> None:
    """One container covers every event, from the first bin to the last."""
    result = wid.identify_single_well(array.array("f", [1.0, 2.0, 2.05, 7.3]))
    self.assertEqual(
      result,
      wid.WellIdentification(
        wells=(wid.IdentifiedWell("A01", 0, 3, 1.0, 7.3, False),),
        verdict="identified",
        message="all events belong to one container",
      ),
    )
    self.assertEqual(result.event_ranges(), {"A01": (0, 3)})

  def test_name_and_no_events(self) -> None:
    """The container can be named, and may hold no events."""
    result = wid.identify_single_well([], well="Tube")
    self.assertEqual(result.wells, (wid.IdentifiedWell("Tube", None, None, 0.0, 0.0, False),))
    self.assertEqual(result.wells[0].event_count, 0)
    self.assertEqual(result.verdict, "identified")

  def test_times_out_of_order(self) -> None:
    """Event times that go backwards are refused."""
    with self.assertRaises(WellIdentificationError):
      wid.identify_single_well([2.0, 1.0])


if __name__ == "__main__":
  unittest.main()
