"""Volume planning: contiguity, no protected split, modes A/B/C (Tests 1, 3, 4, 5)."""
import itertools
import random

import pytest

from backend.app.models.pdf_models import Section
from backend.app.services.volume_planner import (
    balanced_partition,
    greedy_groups,
    min_cap,
    plan_volumes,
)

MB = 1_000_000


def secs(sizes_mb, protected=True, pages_each=10):
    out, page = [], 1
    for i, s in enumerate(sizes_mb):
        out.append(Section(title=f"S{chr(65 + i)}", start_page=page, end_page=page + pages_each - 1,
                           protected=protected, size_bytes=int(s * MB)))
        page += pages_each
    return out


def check_invariants(plan, sections):
    # contiguous, ordered, complete
    assert plan.volumes[0].start_page == 1
    for a, b in zip(plan.volumes, plan.volumes[1:]):
        assert b.start_page == a.end_page + 1
    assert plan.volumes[-1].end_page == sections[-1].end_page
    # protected sections inside exactly one volume
    for s in sections:
        if s.protected:
            holders = [v for v in plan.volumes if v.start_page <= s.end_page and v.end_page >= s.start_page]
            assert len(holders) == 1
            assert holders[0].start_page <= s.start_page and s.end_page <= holders[0].end_page


def test_spec_example_max_size():
    sections = secs([8, 7, 18, 9, 6, 12])
    plan = plan_volumes(sections, "max_size", 25 * MB, None)
    check_invariants(plan, sections)
    groups = [[s.title for s in v.sections] for v in plan.volumes]
    assert len(groups) == 4
    assert all(v.estimated_bytes <= 25 * MB for v in plan.volumes)
    assert plan.constraints_satisfied
    # original order kept
    assert [t for g in groups for t in g] == [s.title for s in sections]


def test_ten_sections_grouped_without_splitting():
    """Test 1: 100 pages, 10 sections, 25 MB limit."""
    sections = secs([6, 4, 9, 3, 8, 7, 5, 10, 2, 6])
    plan = plan_volumes(sections, "max_size", 25 * MB, None)
    check_invariants(plan, sections)
    assert all(v.estimated_bytes <= 25 * MB for v in plan.volumes)
    assert len(plan.volumes) == len(greedy_groups([s.size_bytes for s in sections], 25 * MB))


def test_oversized_annexure_is_alone_and_flagged():
    """Test 3: an Annexure larger than the limit is never split, it gets its own volume and a warning."""
    sections = secs([5, 35, 6])
    plan = plan_volumes(sections, "max_size", 25 * MB, None)
    check_invariants(plan, sections)
    big = [v for v in plan.volumes if any(s.title == "SB" for s in v.sections)][0]
    assert [s.title for s in big.sections] == ["SB"]
    assert big.over_limit
    assert not plan.constraints_satisfied
    assert any("will NOT be split" in w for w in plan.warnings)


def test_exact_number_of_volumes():
    """Test 4: exactly 5 volumes when possible, balanced by size not pages."""
    sections = secs([3, 3, 3, 3, 12, 3, 3, 3, 3, 3, 6, 6])
    plan = plan_volumes(sections, "num_volumes", None, 5)
    check_invariants(plan, sections)
    assert len(plan.volumes) == 5
    sizes = [v.estimated_bytes for v in plan.volumes]
    assert max(sizes) == 12 * MB  # optimal: the 12 MB section bounds the maximum


def test_exact_volumes_impossible():
    sections = secs([5, 5, 5])
    plan = plan_volumes(sections, "num_volumes", None, 5)
    check_invariants(plan, sections)
    assert len(plan.volumes) == 3
    assert not plan.constraints_satisfied
    assert "not possible" in plan.warnings[0]


def test_both_constraints_satisfiable():
    """Test 5: 5 volumes and 25 MB."""
    sections = secs([10, 8, 12, 9, 7, 11, 6, 9, 8, 10])
    plan = plan_volumes(sections, "both", 25 * MB, 5)
    check_invariants(plan, sections)
    assert len(plan.volumes) == 5
    assert all(v.estimated_bytes <= 25 * MB for v in plan.volumes)
    assert plan.constraints_satisfied


def test_both_constraints_conflict_keeps_count_and_warns():
    sections = secs([20, 20, 20, 20, 20, 20])
    plan = plan_volumes(sections, "both", 25 * MB, 3)
    check_invariants(plan, sections)
    assert len(plan.volumes) == 3
    assert any("compressed" in w for w in plan.warnings)
    assert all(v.over_limit for v in plan.volumes)


def test_unprotected_section_may_split_at_page():
    sections = secs([30], protected=False, pages_each=30)
    plan = plan_volumes(sections, "max_size", 10 * MB, None)
    check_invariants(plan, sections)
    assert len(plan.volumes) == 3
    assert all(v.estimated_bytes <= 10 * MB for v in plan.volumes)
    assert any(s.partial for v in plan.volumes for s in v.sections)


def _brute_min_max(sizes, k):
    n = len(sizes)
    best = None
    for cuts in itertools.combinations(range(1, n), k - 1):
        bounds = (0,) + cuts + (n,)
        mx = max(sum(sizes[a:b]) for a, b in zip(bounds, bounds[1:]))
        best = mx if best is None else min(best, mx)
    return best


@pytest.mark.parametrize("seed", range(25))
def test_partition_is_optimal_against_brute_force(seed):
    rnd = random.Random(seed)
    sizes = [rnd.randint(1, 40) for _ in range(rnd.randint(3, 9))]
    k = rnd.randint(1, len(sizes))
    cap = min_cap(sizes, k, sum(sizes))
    groups = balanced_partition(sizes, k, cap)
    assert len(groups) == k
    assert [u for g in groups for u in g] == list(range(len(sizes)))
    assert max(sum(sizes[u] for u in g) for g in groups) == _brute_min_max(sizes, k)


@pytest.mark.parametrize("seed", range(40))
def test_random_plans_keep_invariants(seed):
    rnd = random.Random(1000 + seed)
    n = rnd.randint(1, 30)
    sections = []
    page = 1
    for i in range(n):
        pages = rnd.randint(1, 20)
        sections.append(Section(title=f"S{i}", start_page=page, end_page=page + pages - 1,
                                protected=rnd.random() > 0.2, size_bytes=rnd.randint(1, 30) * MB))
        page += pages
    mode = rnd.choice(["max_size", "num_volumes", "both"])
    max_bytes = rnd.randint(5, 40) * MB if mode != "num_volumes" else None
    k = rnd.randint(1, 12) if mode != "max_size" else None
    plan = plan_volumes(sections, mode, max_bytes, k)
    check_invariants(plan, sections)
    if mode == "max_size":
        for v in plan.volumes:
            multi_unit = len(v.sections) > 1 or any(s.partial for s in v.sections) or not v.sections[0].protected
            if v.over_limit:
                assert not multi_unit or v.page_count == 1


def test_adjusted_plan_moves_whole_section_and_keeps_order():
    """User moves Annexure B into Volume 1: only the boundary moves, order is unchanged."""
    sections = secs([14, 7, 12, 5, 16, 9, 11, 7, 11, 7])          # SA..SJ, 10 pages each
    starts = [1, 31, 61, 81]                                        # volumes start at SA, SD, SG, SI
    plan = plan_volumes(sections, "both", 20 * MB, 4, volume_starts=starts)
    check_invariants(plan, sections)
    assert plan.adjusted
    assert [s.title for s in plan.volumes[0].sections] == ["SA", "SB", "SC"]
    assert [s.title for v in plan.volumes for s in v.sections] == [s.title for s in sections]


def test_adjusted_plan_rejects_start_inside_protected_section():
    sections = secs([5, 5, 5])
    with pytest.raises(Exception, match="inside the protected section 'SB'"):
        plan_volumes(sections, "num_volumes", None, 2, volume_starts=[1, 15])


def test_adjusted_plan_reports_volume_count_mismatch():
    sections = secs([5, 5, 5])
    plan = plan_volumes(sections, "num_volumes", None, 3, volume_starts=[1, 11])
    assert len(plan.volumes) == 2 and not plan.constraints_satisfied


def test_adjusted_plan_must_stay_within_required_size():
    """Point 4: a move is allowed only if the volume can still reach the required size."""
    sections = secs([8, 8, 8, 8])
    for s in sections:
        s.min_size_bytes = 3 * MB          # each section can be compressed to about 3 MB
    ok = plan_volumes(sections, "both", 7 * MB, 2, volume_starts=[1, 21])       # 2 sections -> ~6 MB: fine
    assert all(v.achievable for v in ok.volumes)
    with pytest.raises(Exception, match="not possible within the required size"):
        plan_volumes(sections, "both", 7 * MB, 2, volume_starts=[1, 31])        # 3 sections -> ~9 MB: refused
    single = plan_volumes(sections, "both", 2 * MB, 4)                          # unavoidable: warn, don't refuse
    assert not any(v.achievable for v in single.volumes)
    assert any("even at maximum compression" in w for w in single.warnings)
