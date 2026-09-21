"""User battle observations dated 2026-09-21, not an amended official rulebook."""
OBSERVED_WAVES = {
    1: (30, 5, 0, 0), 2: (35, 10, 0, 0), 3: (40, 15, 3, 0),
    4: (45, 20, 4, 0), 5: (50, 20, 4, 1), 6: (55, 25, 5, 1),
    7: (57, 27, 5, 2), 8: (59, 29, 5, 3),
}


def observed_wave(day, unknown="hold8"):
    if day in OBSERVED_WAVES:
        return OBSERVED_WAVES[day]
    if unknown == "hold8":
        return OBSERVED_WAVES[8]
    if unknown == "growth":
        extra = day - 8
        return 59 + 5 * extra, 29 + 3 * extra, 5 + extra, 3 + extra
    raise ValueError("unknown day 9/10 wave scenario")
