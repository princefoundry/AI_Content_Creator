# Skill: news_ranking

## Purpose
Rank verified stories so the most valuable, teachable items are drafted first.
Ranking is primarily deterministic (see app/research/ranking.py); this skill
documents the weighting and the rules an LLM assist must also follow.

## Strict market priority
Market priority is a hard ordering applied before the numeric relevance score:

1. India: Indian tech, AI, companies, startups, investment, markets, business,
   and economy.
2. United States: US tech, AI, startups, companies, business, markets, and economy.
3. Global: eligible tech, AI, business, investment, market, and economy stories
   from all other markets.

A US or global story must never outrank an eligible India story merely because
its numeric score is higher. Numeric score, freshness, and trend signals order
stories only within the same market tier.

## Weighted model (current implementation)
- Technical relevance: 28%
- Engineering significance: 17%
- Freshness: 30%
- Audience relevance: 13%
- Original insight opportunity: 12%
Each story scored 0-100.

## Required inputs
- Verified stories with claims, freshness, source tier.

## Expected outputs
- A score (0-100) and per-dimension sub-scores with a short rationale.

## Operational constraints
- Reject stories that fail basic verification even if popular.
- Prefer diverse topics; avoid multiple posts about one announcement.
- Do not reward misleading or clickbait headlines.
- Reject off-topic entertainment, sport, lifestyle, and general political news.
- Prefer breaking developments, strong current coverage, recent announcements,
  meaningful investments, market moves, and other defensible trend signals.

## When to run
- After verification, before technical analysis and selection.

## When NOT to run
- Never to rank unverified or contradicted stories above verified ones.

## Rules preventing unsupported claims
- Ranking must not alter the factual content or verification status of a story.
