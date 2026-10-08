"""Grouping indivisible sections into volumes.

Rules enforced here:
  * Units are never reordered: every volume is a contiguous run of units in document order.
  * A protected section is one unit and is therefore never split.
  * An *unprotected* section is broken into single-page units so it may be split at a page.

Algorithm (contiguous partitioning, sizes in bytes):
  1. ``greedy_groups(sizes, cap)`` packs units left-to-right with a size cap. A unit larger
     than the cap becomes a volume of its own (it will be compressed later). For a fixed cap this
     gives the minimum possible number of volumes.
  2. ``min_cap(sizes, k, upper)`` binary-searches the smallest cap for which k volumes suffice,
     i.e. it minimises the largest volume (ignoring stand-alone oversized units).
  3. ``balanced_partition`` then runs a dynamic programme that keeps every multi-unit volume
     within that cap and, among all such partitions into exactly k volumes, minimises the sum of
     squared volume sizes - which spreads the size as evenly as possible.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from ..models.pdf_models import Section
from ..models.volume_models import PlannedSection, PlannedVolume, VolumePlan
from ..utils.size_utils import format_size

DP_WORK_LIMIT = 4_000_000  # max inner-loop iterations before falling back to greedy splitting


@dataclass
class Unit:
    section_index: int
    start_page: int
    end_page: int
    size: int
    protected: bool
    min_size: Optional[int] = None  # None = not measured (never blocks a plan)


class PlanningError(ValueError):
    pass


# ----------------------------------------------------------------------------- building units

def build_units(sections: Sequence[Section], weights: Optional[Sequence[int]] = None) -> List[Unit]:
    units: List[Unit] = []
    for idx, s in enumerate(sections):
        size = int(s.size_bytes or 0)
        min_size = s.min_size_bytes
        if s.protected or s.start_page == s.end_page:
            units.append(Unit(idx, s.start_page, s.end_page, size, s.protected, min_size))
            continue
        pages = list(range(s.start_page, s.end_page + 1))
        if weights:
            w = [max(1, weights[p - 1]) for p in pages]
        else:
            w = [1] * len(pages)
        total_w = sum(w)
        allocated = 0
        for i, p in enumerate(pages):
            share = size - allocated if i == len(pages) - 1 else int(size * w[i] / total_w)
            allocated += share
            units.append(Unit(idx, p, p, max(share, 1), False,
                              None if min_size is None else int(min_size * w[i] / total_w)))
    return units


# ----------------------------------------------------------------------------- partitioning

def greedy_groups(sizes: Sequence[int], cap: float) -> List[List[int]]:
    groups: List[List[int]] = []
    cur: List[int] = []
    cur_sum = 0
    for i, s in enumerate(sizes):
        if s > cap:
            if cur:
                groups.append(cur)
                cur, cur_sum = [], 0
            groups.append([i])
            continue
        if cur and cur_sum + s > cap:
            groups.append(cur)
            cur, cur_sum = [], 0
        cur.append(i)
        cur_sum += s
    if cur:
        groups.append(cur)
    return groups


def min_cap(sizes: Sequence[int], k: int, upper: int) -> int:
    """Smallest integer cap <= upper such that greedy packing needs at most k volumes."""
    lo, hi = 0, int(upper)
    while lo < hi:
        mid = (lo + hi) // 2
        if len(greedy_groups(sizes, mid)) <= k:
            hi = mid
        else:
            lo = mid + 1
    return lo


def _split_to_k(sizes: Sequence[int], groups: List[List[int]], k: int) -> List[List[int]]:
    """Split groups (largest first) until there are exactly k. Splitting never breaks a cap."""
    groups = [list(g) for g in groups]
    while len(groups) < k:
        candidates = [i for i, g in enumerate(groups) if len(g) > 1]
        if not candidates:
            break
        gi = max(candidates, key=lambda i: sum(sizes[u] for u in groups[i]))
        g = groups[gi]
        total = sum(sizes[u] for u in g)
        best_cut, best_diff, run = 1, None, 0
        for cut in range(1, len(g)):
            run += sizes[g[cut - 1]]
            diff = abs(total - 2 * run)
            if best_diff is None or diff < best_diff:
                best_cut, best_diff = cut, diff
        groups[gi:gi + 1] = [g[:best_cut], g[best_cut:]]
    return groups


def balanced_partition(sizes: Sequence[int], k: int, cap: int) -> List[List[int]]:
    """Exactly k contiguous groups; multi-unit groups <= cap; minimise sum of squared group sizes."""
    n = len(sizes)
    if k >= n:
        return [[i] for i in range(n)]
    if k <= 1:
        return [list(range(n))]

    prefix = [0]
    for s in sizes:
        prefix.append(prefix[-1] + s)

    # Estimate DP work; fall back to greedy + splitting for very large inputs.
    window = 0
    j = 0
    for i in range(n):
        j = max(j, i + 1)
        while j < n and prefix[j + 1] - prefix[i] <= cap:
            j += 1
        window = max(window, j - i)
    if n * k * max(window, 1) > DP_WORK_LIMIT:
        return _split_to_k(sizes, greedy_groups(sizes, cap), k)

    INF = float("inf")
    dp = [[INF] * (n + 1) for _ in range(k + 1)]
    back = [[-1] * (n + 1) for _ in range(k + 1)]
    dp[0][0] = 0
    for g in range(1, k + 1):
        prev = dp[g - 1]
        cur = dp[g]
        bk = back[g]
        for end in range(g, n - (k - g) + 1):
            best, best_i = INF, -1
            for start in range(end - 1, g - 2, -1):
                s = prefix[end] - prefix[start]
                if end - start > 1 and s > cap:
                    break
                if prev[start] == INF:
                    continue
                cost = prev[start] + s * s
                if cost < best:
                    best, best_i = cost, start
            cur[end] = best
            bk[end] = best_i
    if dp[k][n] == INF:  # should not happen; be safe
        return _split_to_k(sizes, greedy_groups(sizes, cap), k)
    groups: List[List[int]] = []
    end = n
    for g in range(k, 0, -1):
        start = back[g][end]
        groups.append(list(range(start, end)))
        end = start
    groups.reverse()
    return groups


def groups_from_starts(
    units: Sequence[Unit], sections: Sequence[Section], volume_starts: Sequence[int]
) -> List[List[int]]:
    """Unit groups for explicit volume start pages (a user-adjusted plan).

    Every start page must be the first page of a unit, i.e. the start of a section (or any page of
    an unprotected section). A start inside a protected section is rejected - never split.
    Order cannot change: volumes are cut from the document at the given pages, nothing is moved.
    """
    first = units[0].start_page
    starts = sorted(set(int(p) for p in volume_starts) | {first})
    unit_starts = {u.start_page: i for i, u in enumerate(units)}
    for p in starts:
        if p not in unit_starts:
            owner = next((s for s in sections if s.start_page <= p <= s.end_page), None)
            if owner is None:
                raise PlanningError(f"Volume start page {p} is outside the document.")
            raise PlanningError(
                f"A volume cannot start at page {p}: it is inside the protected section '{owner.title}' "
                f"(pages {owner.start_page}–{owner.end_page}). Volumes may only start where a section starts."
            )
    cuts = [unit_starts[p] for p in starts] + [len(units)]
    return [list(range(a, b)) for a, b in zip(cuts, cuts[1:])]


# ----------------------------------------------------------------------------- public API

def plan_volumes(
    sections: Sequence[Section],
    mode: str,
    max_bytes: Optional[int],
    num_volumes: Optional[int],
    weights: Optional[Sequence[int]] = None,
    plan_id: str = "",
    document_id: str = "",
    volume_starts: Optional[Sequence[int]] = None,
) -> VolumePlan:
    if not sections:
        raise PlanningError("There are no sections to plan.")
    units = build_units(sections, weights)
    sizes = [u.size for u in units]
    n = len(units)
    total = sum(sizes)
    warnings: List[str] = []
    satisfied = True
    protected_units = sum(1 for u in units if u.protected)

    if volume_starts is not None:
        # User-adjusted plan: boundaries are given explicitly (validated to be section starts).
        groups = groups_from_starts(units, sections, volume_starts)
        if mode != "max_size" and num_volumes and len(groups) != num_volumes:
            warnings.append(
                f"The adjusted plan has {len(groups)} volume(s), but {num_volumes} were requested."
            )
            satisfied = False
    elif mode == "max_size":
        if not max_bytes:
            raise PlanningError("A maximum volume size is required.")
        k = len(greedy_groups(sizes, max_bytes))
        cap = min_cap(sizes, k, max_bytes)
    elif mode == "num_volumes":
        if not num_volumes:
            raise PlanningError("A number of volumes is required.")
        k = num_volumes
        if n < k:
            warnings.append(
                f"Exactly {num_volumes} volumes are not possible: the document has only {n} indivisible "
                f"unit(s) ({protected_units} protected section(s)). Creating {num_volumes} volumes would require "
                f"splitting a protected section, so {n} volume(s) were planned instead."
            )
            satisfied = False
            k = n
        cap = min_cap(sizes, k, total)
    elif mode == "both":
        if not max_bytes or not num_volumes:
            raise PlanningError("Both a maximum size and a number of volumes are required.")
        k = num_volumes
        if n < k:
            warnings.append(
                f"Exactly {num_volumes} volumes are not possible: the document has only {n} indivisible "
                f"unit(s). {n} volume(s) were planned; no protected section was split."
            )
            satisfied = False
            k = n
        k_min = len(greedy_groups(sizes, max_bytes))
        if k_min <= k:
            cap = min_cap(sizes, k, max_bytes)
        else:
            cap = min_cap(sizes, k, total)
            warnings.append(
                f"At original size, at least {k_min} volumes are needed to keep every volume under "
                f"{format_size(max_bytes)}, but {k} were requested. {k} volumes were planned (the volume count "
                f"takes priority); volumes above the limit will be compressed during generation."
            )
    else:
        raise PlanningError(f"Unknown planning mode: {mode}")

    if volume_starts is None:
        groups = balanced_partition(sizes, k, cap)
    volumes: List[PlannedVolume] = []
    for vi, g in enumerate(groups, start=1):
        g_units = [units[i] for i in g]
        start, end = g_units[0].start_page, g_units[-1].end_page
        est = sum(u.size for u in g_units)
        psecs: List[PlannedSection] = []
        for si in dict.fromkeys(u.section_index for u in g_units):
            s = sections[si]
            s_start = max(s.start_page, start)
            s_end = min(s.end_page, end)
            psecs.append(
                PlannedSection(
                    title=s.title,
                    start_page=s_start,
                    end_page=s_end,
                    protected=s.protected,
                    partial=(s_start, s_end) != (s.start_page, s.end_page),
                )
            )
        over = bool(max_bytes) and est > max_bytes
        floor = None if any(u.min_size is None for u in g_units) else sum(u.min_size for u in g_units)
        achievable = not max_bytes or floor is None or floor <= max_bytes
        volumes.append(
            PlannedVolume(
                index=vi,
                start_page=start,
                end_page=end,
                page_count=end - start + 1,
                sections=psecs,
                estimated_bytes=est,
                over_limit=over,
                min_estimated_bytes=floor,
                achievable=achievable,
            )
        )
        if not achievable:
            names = ", ".join(p.title for p in psecs)
            if volume_starts is not None and len(g_units) > 1:
                raise PlanningError(
                    f"This change is not possible within the required size: Volume {vi} ({names}) would be about "
                    f"{format_size(floor)} even at maximum compression, above the {format_size(max_bytes)} limit. "
                    f"The plan was not changed."
                )
            warnings.append(
                f"Volume {vi} ({names}) is estimated to need about {format_size(floor)} even at maximum "
                f"compression, above the {format_size(max_bytes)} limit. It will be compressed as far as possible "
                f"and you will be asked how to proceed."
            )
        if over and achievable:
            names = ", ".join(p.title for p in psecs)
            if len(g_units) == 1 and g_units[0].protected:
                warnings.append(
                    f"Volume {vi} ({names}) is estimated at {format_size(est)}, above the {format_size(max_bytes)} "
                    f"limit. It is a single protected section and will NOT be split; automatic compression "
                    f"will be attempted during generation."
                )
            else:
                warnings.append(
                    f"Volume {vi} ({names}) is estimated at {format_size(est)}, above the "
                    f"{format_size(max_bytes)} limit. Automatic compression will be attempted during generation."
                )

    if any(p.partial for v in volumes for p in v.sections):
        warnings.append(
            "One or more UNPROTECTED sections were split across volumes at a page boundary. "
            "Mark them as protected if they must stay together."
        )

    _assert_plan_integrity(volumes, sections)
    return VolumePlan(
        plan_id=plan_id,
        document_id=document_id,
        mode=mode,
        max_bytes=max_bytes,
        num_volumes_requested=num_volumes,
        volumes=volumes,
        warnings=warnings,
        constraints_satisfied=satisfied and not any(v.over_limit for v in volumes),
        total_estimated_bytes=total,
        sections=list(sections),
        adjusted=volume_starts is not None,
    )


def _assert_plan_integrity(volumes: Sequence[PlannedVolume], sections: Sequence[Section]) -> None:
    """Hard safety net: contiguous coverage, no overlap, no protected section split."""
    expected = sections[0].start_page
    for v in volumes:
        if v.start_page != expected or v.end_page < v.start_page:
            raise PlanningError("Internal error: volume plan does not cover pages contiguously.")
        expected = v.end_page + 1
    if expected != sections[-1].end_page + 1:
        raise PlanningError("Internal error: volume plan does not cover every page.")
    for s in sections:
        if not s.protected:
            continue
        holders = [v for v in volumes if v.start_page <= s.end_page and v.end_page >= s.start_page]
        if len(holders) != 1 or not (holders[0].start_page <= s.start_page and s.end_page <= holders[0].end_page):
            raise PlanningError(f"Internal error: protected section '{s.title}' would be split.")
