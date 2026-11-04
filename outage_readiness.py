"""Will the battery last through the next planned outage? (/api/outage-readiness)

Compares the energy the home needs for the next outage window (the recent
average load times the window's length) with what the battery can give before
it reaches the inverter's shutdown SOC. While the battery is charging before
the outage, the charge it will have at the start is estimated too.

While the house is on battery already (no grid, e.g. an unscheduled outage
before the planned one), the battery's own measured draw is used instead of the
load estimate (the same ~10-minute average as the battery banner's "lasts
about", the inverter's losses included), and the charge at the outage's start
is what's left after drawing that until then.
"""

# Energy taken from the battery per Wh of home load: the inverter's conversion
# losses and its own consumption while running from the battery come on top
BATTERY_LOSS_FACTOR = 1.1
# Below this much spare energy (available / needed) the verdict is "tight"
COMFORT_MARGIN = 1.2


def merged_windows(days):
    """All windows of the schedule's days, sorted, with touching ones joined:
    08:00-10:00 + 10:00-12:00 is one 4-hour outage for the battery."""
    windows = sorted((start, end) for day in days for start, end in day.get("windows", []))
    merged = []
    for start, end in windows:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def next_outage(days, now):
    """(start, end) of the outage under way or the next one, or None."""
    return next(((start, end) for start, end in merged_windows(days) if end > now), None)


def outage_readiness(data, days, now, average_load_w, capacity_wh):
    """Readiness for the next outage window.

    data: inverter readings (battery_soc, battery_shutdown_soc,
    battery_charge_minutes, grid_online, battery_discharge_average_w); days: the outage schedule's days
    ([{"windows": [(start, end), ...]}], datetimes); average_load_w: recent
    average home load; capacity_wh: full battery capacity.
    """
    window = next_outage(days, now)
    if window is None:
        return {"status": "no_outage"}
    start, end = window
    during = start <= now
    begins = max(start, now)
    outage_minutes = (end - begins).total_seconds() / 60
    result = {
        "status": "ok",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "during": during,
        "outage_minutes": round(outage_minutes),
        "starts_in_minutes": None if during else round((start - now).total_seconds() / 60),
    }
    if not capacity_wh or average_load_w is None:
        result["status"] = "no_data"
        return result

    soc = data.get("battery_soc") or 0
    shutdown = data.get("battery_shutdown_soc") or 0
    if not 0 <= shutdown < 100:
        shutdown = 0
    discharge_w = data.get("battery_discharge_average_w")
    on_battery = (data.get("grid_online") is False and isinstance(discharge_w, (int, float))
                  and discharge_w > 0)
    draw_w = discharge_w if on_battery else max(average_load_w, 0) * BATTERY_LOSS_FACTOR
    soc_at_start = soc
    charge_minutes = data.get("battery_charge_minutes")
    if not during and on_battery:
        # Drawn from the battery until the outage starts
        drawn_wh = draw_w * result["starts_in_minutes"] / 60
        soc_at_start = max(0.0, soc - drawn_wh / capacity_wh * 100)
    elif not during and charge_minutes and charge_minutes > 0:
        # Still charging until the outage: linear, like the "full in" estimate
        soc_at_start = soc + (100 - soc) * min(1.0, result["starts_in_minutes"] / charge_minutes)

    available_wh = capacity_wh * max(0, soc_at_start - shutdown) / 100
    needed_wh = draw_w * outage_minutes / 60
    lasts_minutes = available_wh / draw_w * 60 if draw_w > 0 else None

    if needed_wh <= 0 or available_wh >= needed_wh * COMFORT_MARGIN:
        verdict = "enough"
    elif available_wh >= needed_wh:
        verdict = "tight"
    else:
        verdict = "short"
    result.update({
        "verdict": verdict,
        "average_load_w": round(average_load_w),
        # What the estimate draws from the battery: its measured draw ("battery")
        # or the load plus losses ("load")
        "basis": "battery" if on_battery else "load",
        "draw_w": round(draw_w),
        "soc": round(soc),
        "soc_at_start": round(soc_at_start),
        "shutdown_soc": shutdown,
        "needed_kwh": round(needed_wh / 1000, 2),
        "available_kwh": round(available_wh / 1000, 2),
        "lasts_minutes": None if lasts_minutes is None else round(lasts_minutes),
        "short_minutes": round(outage_minutes - lasts_minutes) if verdict == "short" else None,
    })
    return result

