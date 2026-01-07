"""
Council deliberation pipeline adapted for BoardMeeting.

Stages:
  1) Members propose independent answers
  2) Members rank anonymized proposals
  3) Chairman synthesizes a final answer

This file is intentionally dependency-light: it only needs asyncio + stdlib.
"""

from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional, Tuple


@dataclass
class CouncilMember:
    name: str
    call: Callable[[str], Awaitable[str]]


def _label_set(n: int) -> List[str]:
    # A, B, C, ... (supports up to 26)
    if n > 26:
        raise ValueError("Too many council responses (max 26).")
    return [chr(ord("A") + i) for i in range(n)]


def _sanitize(text: str) -> str:
    # Keep it simple; prevent giant outputs from breaking prompts
    t = (text or "").strip()
    # Hard cap to keep ranking prompt safe
    if len(t) > 6000:
        t = t[:6000].rstrip() + "…"
    return t


def _parse_ranking(text: str, labels: List[str]) -> Optional[List[str]]:
    """
    Accepts formats like:
      RANKING: A, B, C
      A > B > C
      A B C
    Returns list of labels in ranked order, or None if not parseable.
    """
    if not text:
        return None
    t = text.upper()

    # Pull all labels in order of appearance
    found = re.findall(r"\b[A-Z]\b", t)
    # Filter to our label set
    found = [x for x in found if x in labels]

    if not found:
        return None

    # Deduplicate preserving order
    seen = set()
    ordered = []
    for x in found:
        if x not in seen:
            seen.add(x)
            ordered.append(x)

    # Must contain all labels exactly once
    if sorted(ordered) != sorted(labels):
        return None
    return ordered


def _ranking_prompt(user_query: str, labeled_answers: List[Tuple[str, str]]) -> str:
    blocks = []
    for label, ans in labeled_answers:
        blocks.append(f"{label}) {ans}")

    return (
        "You are ranking candidate answers from best to worst.\n"
        "Rules:\n"
        "- Output ONLY the ranking in one line.\n"
        "- Format examples:\n"
        "  RANKING: A, B, C\n"
        "  A > B > C\n"
        "- Include ALL labels exactly once.\n\n"
        f"USER QUERY / TASK:\n{user_query}\n\n"
        "CANDIDATE ANSWERS:\n"
        + "\n".join(blocks)
    )


def _chairman_prompt(user_query: str, best_answer: str, notes: str) -> str:
    return (
        "You are the Chairman. Produce the final best answer.\n"
        "Constraints:\n"
        "- Be direct and specific.\n"
        "- If uncertain, say what is missing.\n"
        "- Do not mention internal stages.\n\n"
        f"USER QUERY / TASK:\n{user_query}\n\n"
        f"BEST CANDIDATE ANSWER:\n{best_answer}\n\n"
        f"CRITIQUE / NOTES (from other members):\n{notes}\n"
    )


def _borda_from_rankings(labels: List[str], rankings: List[List[str]]) -> Dict[str, int]:
    # Higher is better
    scores = {l: 0 for l in labels}
    n = len(labels)
    for r in rankings:
        for i, label in enumerate(r):
            scores[label] += (n - i)
    return scores


async def run_council(
    user_query: str,
    members: List[CouncilMember],
    chairman: CouncilMember,
    *,
    timeout_s: float = 60.0,
) -> Dict[str, object]:
    """
    Returns:
      {
        "stage1": {member_name: answer, ...},
        "stage2_rankings": {member_name: ["A","B","C"], ...},
        "stage2_scores": {"A": 10, "B": 7, ...},
        "winner_label": "A",
        "winner_answer": "...",
        "final": "..."
      }
    """
    user_query = _sanitize(user_query)

    # Stage 1: independent proposals
    async def _call_member(m: CouncilMember) -> Tuple[str, str]:
        ans = await asyncio.wait_for(m.call(user_query), timeout=timeout_s)
        return m.name, _sanitize(ans)

    stage1_results = await asyncio.gather(*[_call_member(m) for m in members])
    stage1 = {name: ans for name, ans in stage1_results}

    # Label answers
    labels = _label_set(len(members))
    labeled = list(zip(labels, [stage1[m.name] for m in members]))

    # Stage 2: rankings
    async def _rank_member(m: CouncilMember) -> Tuple[str, List[str]]:
        prompt = _ranking_prompt(user_query, labeled)
        raw = await asyncio.wait_for(m.call(prompt), timeout=timeout_s)
        parsed = _parse_ranking(raw, labels)
        if parsed is None:
            # fallback: random stable shuffle of labels (but deterministic-ish per member)
            rng = random.Random(hash(m.name) & 0xFFFFFFFF)
            parsed = labels[:]
            rng.shuffle(parsed)
        return m.name, parsed

    rankings_list = await asyncio.gather(*[_rank_member(m) for m in members])
    stage2_rankings = {name: r for name, r in rankings_list}

    # Score with Borda count
    rankings_only = [r for _, r in rankings_list]
    scores = _borda_from_rankings(labels, rankings_only)

    # Pick winner label
    winner_label = max(scores.items(), key=lambda kv: kv[1])[0]
    winner_answer = dict(labeled)[winner_label]

    # Collect short critique notes
    notes_lines = []
    for name, r in rankings_list:
        notes_lines.append(f"{name} ranked: {' > '.join(r)}")
    notes = "\n".join(notes_lines)

    # Stage 3: chairman synthesis
    chair_prompt = _chairman_prompt(user_query, winner_answer, notes)
    final = await asyncio.wait_for(chairman.call(chair_prompt), timeout=timeout_s)
    final = _sanitize(final)

    return {
        "stage1": stage1,
        "stage2_rankings": stage2_rankings,
        "stage2_scores": scores,
        "winner_label": winner_label,
        "winner_answer": winner_answer,
        "final": final,
    }
