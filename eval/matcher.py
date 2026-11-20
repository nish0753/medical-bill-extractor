"""Match system lines to answer-key lines (lld.md §11.2).

Per page:
1. Candidates: a key line and a system line with exactly equal amounts.
2. Score each by item-name similarity (rapidfuzz token-set ratio on names lowercased, with punctuation
   turned into spaces); drop < NAME_MIN.
3. Assign one-to-one, highest score first. These are `matched`.
4. Second pass on what is left: same page, name score >= MISREAD_NAME_MIN, amounts differ. These are
   `misread` (right row, wrong amount). They still count as missed and extra for recall and precision;
   they exist so amount accuracy can be reported separately.
Unmatched key lines are `missed`; unmatched system lines are `extra` (invented or misread).

`ignore_page=True` pools all pages into one; used only as a diagnostic for page-numbering errors.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz, utils

from .adapters import ScoredLine

NAME_MIN = 70
MISREAD_NAME_MIN = 90


def norm_name(name: str) -> str:
    """Lowercase, turn punctuation into spaces and trim, so codes glued to a name ("Equipment(999311)")
    become their own tokens instead of hiding the name from the token-set comparison."""
    return utils.default_process(name or "")


def name_score(a: str, b: str) -> float:
    return fuzz.token_set_ratio(norm_name(a), norm_name(b))


@dataclass
class MatchResult:
    matched: list[tuple[ScoredLine, ScoredLine]] = field(default_factory=list)  # (key, system)
    misread: list[tuple[ScoredLine, ScoredLine]] = field(default_factory=list)
    missed: list[ScoredLine] = field(default_factory=list)
    extra: list[ScoredLine] = field(default_factory=list)


def _assign(pairs: list[tuple[float, int, int]], used_k: set, used_s: set) -> list[tuple[int, int]]:
    out = []
    for _score, i, j in sorted(pairs, key=lambda p: (-p[0], p[1], p[2])):
        if i not in used_k and j not in used_s:
            used_k.add(i)
            used_s.add(j)
            out.append((i, j))
    return out


def match_lines(key: list[ScoredLine], system: list[ScoredLine], ignore_page: bool = False) -> MatchResult:
    by_page_k: dict = defaultdict(list)
    by_page_s: dict = defaultdict(list)
    for i, ln in enumerate(key):
        by_page_k[None if ignore_page else ln.page_no].append(i)
    for j, ln in enumerate(system):
        by_page_s[None if ignore_page else ln.page_no].append(j)

    result = MatchResult()
    used_k: set[int] = set()
    used_s: set[int] = set()
    for page in set(by_page_k) | set(by_page_s):
        ks, ss = by_page_k.get(page, []), by_page_s.get(page, [])
        exact = []
        for i in ks:
            for j in ss:
                if key[i].amount is not None and key[i].amount == system[j].amount:
                    score = name_score(key[i].item_name, system[j].item_name)
                    if score >= NAME_MIN:
                        exact.append((score, i, j))
        for i, j in _assign(exact, used_k, used_s):
            result.matched.append((key[i], system[j]))

        near = []
        for i in ks:
            if i in used_k:
                continue
            for j in ss:
                if j in used_s:
                    continue
                score = name_score(key[i].item_name, system[j].item_name)
                if score >= MISREAD_NAME_MIN:
                    near.append((score, i, j))
        # Misread pairs are reported but stay in missed/extra, so don't mark them used.
        seen_k: set[int] = set()
        seen_s: set[int] = set()
        for i, j in _assign(near, seen_k, seen_s):
            result.misread.append((key[i], system[j]))

    result.missed = [ln for i, ln in enumerate(key) if i not in used_k]
    result.extra = [ln for j, ln in enumerate(system) if j not in used_s]
    return result
