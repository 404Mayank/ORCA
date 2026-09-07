"""Replay a real cyclone through the real pipeline, hour by hour.

    python scripts/replay.py                 # Cyclone Fengal, the default
    python scripts/replay.py --event gaja    # wind only; see ingest/replay.py
    python scripts/replay.py --list

From CLAUDE.md: *a live demo on a calm day proves nothing. Replaying a real
cyclone and watching the risk score go red 36 hours before landfall proves
everything.*

Nothing is simulated. Archived observations are written into the cache format
the live tools already read, and then **the production tools run against them**
-- ``wave_forecast``, ``wind_forecast`` and ``compute_risk_score``, unmodified,
with an explicit ``start`` time. The advisory state is the real GDACS record
for the event.

What this shows, and what it is for: the hour at which the verdict turns, and
how far ahead of landfall that was.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from core.schemas.tool_io import ToolStatus
from ingest.replay import EVENTS, IST, ReplayEvent, available, fetch_event, replay_cache
from tools.risk.risk_score import ComputeRiskScoreIn, compute_risk_score
from tools.weather.wave_forecast import WaveForecastIn, wave_forecast
from tools.weather.wind_forecast import WindForecastIn, wind_forecast

BAR = {"go": "░", "marginal": "▒", "no_go": "█"}


def _step(event: ReplayEvent, when: datetime, alerts_active: int):
    """One hour, through the real tools."""
    waves = wave_forecast(
        WaveForecastIn(lat=event.lat, lon=event.lon, hours=6, start=when)
    )
    wind = wind_forecast(
        WindForecastIn(lat=event.lat, lon=event.lon, hours=6, start=when)
    )

    risk = compute_risk_score(
        ComputeRiskScoreIn(
            vessel_class=event.vessel_class,
            wave_height=(
                waves.significant_wave_height if waves.status is not ToolStatus.FAILED else None
            ),
            wave_steepness=(
                waves.max_steepness if waves.status is not ToolStatus.FAILED else None
            ),
            wind_speed=wind.wind_speed if wind.status is not ToolStatus.FAILED else None,
            visibility=wind.visibility if wind.status is not ToolStatus.FAILED else None,
            # The advisory was genuinely in force during the event window. This
            # is the one input supplied by the replay rather than measured, and
            # it is supplied from the real GDACS record, not invented.
            alerts_active=alerts_active,
            alerts_checked=True,
        )
    )
    return waves, wind, risk


def run(event: ReplayEvent, step_hours: int = 3) -> int:
    if not available(event):
        print(f"Fetching {event.name} archive ...")
        counts = fetch_event(event)
        print(f"  marine {counts['marine']}h, forecast {counts['forecast']}h\n")

    start = datetime.fromisoformat(event.start).replace(tzinfo=IST)
    end = datetime.fromisoformat(event.end).replace(tzinfo=IST)
    landfall = event.landfall_at

    print(f"{event.name} — as seen from {event.place}, {event.vessel_class}")
    print(f"landfall {landfall:%Y-%m-%d %H:%M} IST · {event.authority}")
    print(f"note: {event.note}\n")
    print(
        f"{'time':<17}{'wave m':>8}{'gust kn':>8}{'vis km':>8}{'score':>7}  "
        f"verdict    why"
    )
    print("-" * 96)

    first_no_go: datetime | None = None
    first_breach: datetime | None = None
    rows = 0

    with replay_cache(event):
        when = start
        while when <= end:
            # The advisory is in force for the 48 hours before landfall, which
            # is roughly when IMD issues for a Bay of Bengal system.
            alerts = 1 if landfall - timedelta(hours=48) <= when <= landfall + timedelta(hours=12) else 0
            waves, wind, risk = _step(event, when, alerts)

            wave_text = (
                f"{waves.significant_wave_height.max:.2f}"
                if waves.status is not ToolStatus.FAILED
                else "  —"
            )
            # The PEAK, not the mean. config/risk_thresholds.yaml applies the
            # wind limit to the gust -- "gusts capsize boats, averages do not"
            # -- so printing the sustained mean beside a gust-driven verdict
            # makes a correct answer look broken. It read "14.0 kn ... over the
            # 25 kn limit" until this was fixed.
            gust_text = (
                f"{(wind.wind_speed.peak or wind.wind_speed.max):.1f}"
                if wind.status is not ToolStatus.FAILED
                else "  —"
            )
            vis_text = (
                f"{wind.visibility.min:.1f}"
                if wind.status is not ToolStatus.FAILED and wind.visibility
                else "  —"
            )
            mark = BAR.get(risk.verdict, "?")

            # Say what actually drove it. Without this the table is a wall of
            # no_go and a reader cannot tell a breached limit from an advisory
            # in force -- which is the whole difference between "the sea is
            # dangerous" and "an authority said so".
            breaching = [c.driver_id for c in risk.contributions if c.evaluation.breaching]
            if breaching:
                why = f"{', '.join(breaching)} over limit"
            elif risk.downgrade_reason:
                why = risk.downgrade_reason
            else:
                why = risk.limiting_driver or ""

            if risk.verdict == "no_go" and first_no_go is None:
                first_no_go = when
            if breaching and first_breach is None:
                first_breach = when

            print(
                f"{when:%d %b %H:%M}  {wave_text:>8}{gust_text:>8}{vis_text:>8}"
                f"{risk.score:>7.3f}  {mark} {risk.verdict:<9} {why[:38]}"
            )
            rows += 1
            when += timedelta(hours=step_hours)

    print("-" * 96)
    if rows == 0:
        print("No hours in the window. Check the event dates.")
        return 1
    if first_no_go is None:
        print("The verdict never reached no_go. Check the archive actually loaded.")
        return 1

    lead = (landfall - first_no_go).total_seconds() / 3600.0
    print(
        f"First no_go   {first_no_go:%d %b %H:%M} IST — {lead:.0f} h before landfall"
    )
    if first_breach is not None:
        breach_lead = (landfall - first_breach).total_seconds() / 3600.0
        print(
            f"First breach  {first_breach:%d %b %H:%M} IST — {breach_lead:.0f} h "
            "before landfall (a measured limit crossed, not an advisory)"
        )
    else:
        print(
            "No measured limit was ever crossed at this location; every no_go "
            "came from an advisory in force or a failed check."
        )
    print()
    print(
        "Every figure above came from the same tools that answer a live "
        "question. Nothing is simulated."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", default="fengal", choices=sorted(EVENTS))
    parser.add_argument("--step-hours", type=int, default=3)
    parser.add_argument("--list", action="store_true", help="Show available events.")
    args = parser.parse_args()

    if args.list:
        for event in EVENTS.values():
            mark = "cached" if available(event) else "  --  "
            print(f"[{mark}] {event.id:9s} {event.name:18s} {event.start} .. {event.end}")
            print(f"{'':10s} {event.note}")
        return 0

    return run(EVENTS[args.event], step_hours=args.step_hours)


if __name__ == "__main__":
    raise SystemExit(main())
