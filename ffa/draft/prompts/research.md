You are helping with a live fantasy football draft. Research the candidates below with web
search, then rank them into tiers for this specific roster and pick.

Several candidates will be gone by the time the pick arrives, so a single name is brittle.
Give a conditional plan: which tier to take from, and who to prefer inside it. Flag any
player worth taking over a higher-ranked one for structural reasons — positional scarcity,
an open starting slot, or a schedule conflict.

**This draft is happening during the {season} NFL season, after Week 1 has been played.**
Weight what actually happened in Week 1 — snap share, target share, carry share, backfield
splits, goal-line work — above preseason projections and above ADP. A player whose Week 1
role contradicts his draft-day reputation is the single most valuable thing you can tell me,
in either direction. Say so explicitly when you find one.

For each candidate, establish:

1. **Current role** — depth chart position, and whether it changed in the last month.
2. **Week 1 usage** — snap share, target or carry share, goal-line and two-minute work.
3. **Injury status** — current designation, and anything that happened in Week 1.
4. **Offensive context** — quarterback situation, pace, competition for touches.
5. **Schedule** — notable early-season matchups or fantasy-playoff (weeks 15-17) factors.

## League

- Format: {format}
- Scoring: {scoring}
- Starting lineup: {lineup}

## My roster so far

{roster}

Open starting slots: {open_slots}

## This pick

{pick_context}

Positional dropoff between this pick and my next one, in Sleeper `search_rank` spots
(bigger means the position dries up before I pick again):

{dropoffs}

## Candidates

{candidates}

`search_rank` is Sleeper's own ordering, an ADP proxy. It is not a projection, and it is
frequently wrong about post-Week-1 reality — that is exactly what you are checking.

## Output

Return **only** a JSON object, no prose and no markdown fences, in this shape:

```
{{
  "recommendation": {{
    "player_id": "<player_id of your pick>",
    "summary": "<two to three sentences: why this player, at this pick, for this roster>"
  }},
  "ranked": [
    {{
      "player_id": "<player_id>",
      "tier": <integer, 1 = take first>,
      "verdict": "<one line, under 90 characters>",
      "confidence": "high" | "medium" | "low",
      "rationale": "<two to three sentences citing Week 1 usage and role>"
    }}
  ]
}}
```

Rank every candidate you researched, best first, and use `tier` to group them (1 is best);
players in the same tier are close enough that availability decides. `player_id` values must
be copied exactly from the candidate list. Note that Sleeper ties many players at the same
`search_rank`, so the ordering you are given is not a strict ranking. Keep the whole response
under 2000 words.
