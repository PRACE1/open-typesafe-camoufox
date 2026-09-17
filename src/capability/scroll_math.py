"""
Pure scroll-trajectory math — no page/DOM access, no I/O.

Generates human-like wheel-delta sequences (bell-curve velocity, ease-in/
ease-out) that scroll_motion.py then executes against a real page or
container. Kept separate so the trajectory shape can be unit-tested without
a browser.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

@dataclass
class ScrollStep:
    """One wheel delta in a human scroll trajectory."""
    delta_y: float
    delay_ms: int


def _bell_phase(
    distance: float,
    *,
    segments_per_kpx: float = 18.0,
    segment_delay_ms: float = 32.0,
    delay_jitter: float = 0.20,
    rng: random.Random,
) -> list[ScrollStep]:
    """Bell-curve-velocity scroll segments (HumanJS planScroll port).

    Weights follow a half-sine so motion accelerates from rest, peaks at
    midpoint, and decelerates to a stop. Normalisation guarantees
    sum(delta_y) == distance exactly — no final-correction spike.

    Distance-proportional speed: short scrolls collapse to fewer steps with
    a lower per-step delay (a flick); long scrolls keep the full ramp.
    Per-step delay scales DOWN with distance so a short flick is fast even
    before counting its fewer steps. The floor at 12ms keeps the motion
    perceptible (sub-12ms reads as a teleport, not a scroll).
    """
    if distance == 0:
        return []

    abs_dist = abs(distance)
    # Floor at 1 step so very short scrolls still move — but they collapse
    # to a near-instant flick rather than paying the old max(2) + 64ms.
    count = max(1, math.ceil(abs_dist / 1000.0 * segments_per_kpx))

    # Distance-proportional delay: a 200px flick scales the 32ms base down
    # toward the 12ms floor; a 5200px scroll keeps the full 32ms. Linear
    # interpolation in log-distance so the curve is smooth across orders of
    # magnitude. 0px -> 12ms (floor), 5000px -> 32ms (full).
    short_floor_ms = 12.0
    long_full_ms = segment_delay_ms  # 32ms
    long_ref_px = 5000.0
    if abs_dist >= long_ref_px:
        delay_base = long_full_ms
    else:
        # log-scale between floor and full so mid-distances don't jump.
        t = math.log1p(abs_dist) / math.log1p(long_ref_px)
        delay_base = short_floor_ms + (long_full_ms - short_floor_ms) * t

    direction = 1.0 if distance > 0 else -1.0
    weights = [
        math.sin(((i + 0.5) / count) * math.pi)
        for i in range(count)
    ]
    total_weight = sum(weights)

    steps: list[ScrollStep] = []
    for weight in weights:
        delta = direction * abs(distance) * weight / total_weight
        delay = max(12, int(delay_base * rng.uniform(1.0 - delay_jitter, 1.0 + delay_jitter)))
        steps.append(ScrollStep(delta_y=delta, delay_ms=delay))
    return steps

def human_scroll_trajectory(
    distance: float,
    *,
    duration_ms: int | None = None,
    step_px: tuple[int, int] = (18, 55),
    seed: int | None = None,
) -> list[ScrollStep]:
    """Generate a human-like scroll trajectory via a bell-curve planner.

    Pure port of HumanJS ``planScroll``. Segment deltas are normalised so
    their sum equals ``distance`` exactly — there is no final-correction
    spike. The overshoot fraction is inversely proportional to distance:
    short flicks overshoot more (momentum), long scrolls ease in (small
    overshoot). The per-step delay also scales down with distance so short
    scrolls are fast flicks, not slow crawls.
    """
    del duration_ms, step_px  # kept in signature for API compat
    rng = random.Random(seed)
    if not distance:
        return []

    direction = 1 if distance > 0 else -1
    abs_dist = abs(distance)

    # Overshoot FRACTION is inversely proportional to distance: a short flick
    # has more momentum relative to its distance and the overshoot is what
    # makes a flick feel like a flick, so it gets a larger fraction (up to
    # ~25% on a 200px flick — but that's only ~50px, so wall-clock stays
    # tiny). A long deliberate scroll eases into its target, so it gets a
    # smaller fraction (down to ~3% at 5000px+). Linear in log-distance.
    short_overshoot = 0.25   # 25% on a near-zero flick
    long_overshoot = 0.03    # 3% on a 5000px+ scroll
    long_ref_px = 5000.0
    if abs_dist >= long_ref_px:
        fraction = long_overshoot
    else:
        t = math.log1p(abs_dist) / math.log1p(long_ref_px)
        # t goes 0 (short) -> 1 (long); fraction goes short_overshoot -> long_overshoot
        fraction = short_overshoot + (long_overshoot - short_overshoot) * t
    extra = abs_dist * fraction

    # Forward bell phase: 0 -> target + overshoot
    forward = _bell_phase(abs_dist + extra, rng=rng)

    # Reverse bell phase: target + overshoot -> target
    # A shorter, faster correction — fewer segments, lower delay — so the
    # settle reads as a quick recoil, not a second full scroll.
    reverse = _bell_phase(
        extra,
        segments_per_kpx=10.0,
        segment_delay_ms=20.0,
        rng=rng,
    ) if extra >= 1.0 else []

    steps = forward + reverse

    # Sprinkle mid-scroll pauses (not at first or last segment, and not in
    # the reverse phase — pauses mid-recoil read as a stutter).
    fwd_len = len(forward)
    paused: list[tuple[ScrollStep, bool]] = []  # (step, is_forward)
    for idx, step in enumerate(steps):
        is_fwd = idx < fwd_len
        paused.append((step, is_fwd))
        if 0 < idx < fwd_len - 1 and rng.random() < 0.08:
            paused.append((ScrollStep(delta_y=0.0, delay_ms=rng.randint(100, 240)), is_fwd))

    # Apply direction: forward steps go in the scroll direction, reverse
    # steps go in the OPPOSITE direction (they recoil the overshoot).
    result: list[ScrollStep] = []
    for s, is_fwd in paused:
        d = direction if is_fwd else -direction
        result.append(ScrollStep(delta_y=s.delta_y * d, delay_ms=s.delay_ms))
    return result
