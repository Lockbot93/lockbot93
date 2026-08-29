# Following what traders are picking is measurably negative

Researched 2026-08-28 on the owner's instruction to go online and see what
traders are picking and why.

## What the literature says

Recent work on r/wallstreetbets and StockTwits, 2024-2026:

- **"positions created when WSB attention is at its highest realizing
  -8.5% holding period returns"** -- measured on individual trading data.
- Social media sentiment has only a **weak correlation** with prices
  (ChatGPT-annotated Reddit study, July 2025).
- Returns associated with social-media-driven trading **tend to be
  negative**.
- Comment VOLUME and search trends predict better than sentiment does --
  but what they predict is **volatility**, not direction.
- Documented selective exposure: self-described bulls are five times more
  likely to follow another bull on the same stock, and see 62 more
  bullish and 24 fewer bearish messages than bears do. The feed is not a
  sample of opinion; it is a mirror.

## Why this is not new information here

This project already ran the equivalent test independently, on 2026-08-06,
with acceptance criteria pre-registered before any result was seen:

    series           n      win     mean R
    news spike     448    41.1%    -0.073
    random entry   453    49.2%    +0.088
    price-matched  450    48.9%    +0.081

    vs price-matched  -0.154R     negative in all four years

Buying attention spikes was measurably WORSE than entering at random. The
price-matched control was the important part: news follows price, so
"buy the spike" can beat random purely by rediscovering momentum through
a more expensive route. Matching on same-day return isolated what the
attention itself adds, and it subtracts.

**Two independent lines now say the same thing.** One measured on this
account's own universe with costs and controls; one measured by academics
on retail trading data. Attention is a negative signal for direction.

## The inverse is NOT being tested

The obvious next thought is to fade the crowd. CLAUDE.md already refused
that once, on the news result, and the reasoning holds verbatim:

> Testing that now would be post-hoc fishing of exactly the kind the
> pre-registration exists to prevent. If it is ever tested it needs its
> own pre-registration and its own held-out years BEFORE anyone looks at
> a result.

That refusal now has to hold twice, for the same reason, and it is
STRONGER the second time -- an inverse that looks attractive in two
datasets is exactly the kind of thing that gets fitted.

## The capability limit, stated plainly

CORRECTED 2026-08-29. The paragraph below originally said LOCKBOT has no
web access at all. That is FALSE and was stated twice to the owner.

lockbot_brain.py line 132 sets WEB_SEARCH = True, and the brain declares
Anthropic's server-side web_search tool. Its own comment says what it is
for: "what turns 'what LOCKBOT recorded' into 'what is true in the world'
-- why a stock moved, what an earnings date is, what a rule actually
says."

The error was checking the TRADING modules and never checking the brain.
The trading path genuinely reaches nothing but Alpaca, Telegram and
Pushover -- that half was right -- but the brain is the part that thinks,
and it can search.

Note also: --read-only DISABLES it. Every ruling LOCKBOT gave this week
came through that flag, so all of them were answered from its own data
rather than the internet.

What remains true: neither agent is continuously connected. The engineer
exists only while the owner is talking to it. LOCKBOT searches when it is
asked something -- a conversation, its nightly pass -- not on a feed.

So "stay tapped in online" is not available as a live feed. What is
available is research brought back and tested, which is what this is.
