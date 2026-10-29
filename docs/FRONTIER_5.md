# Frontier, fifth pass: the options-specific frontier

The first four passes were market-structure and statistics. This one is about the instrument we actually
trade. A desk selling 0DTE index setups is trading an instrument whose intraday behaviour is *manufactured*
by dealer hedging, and none of our 48 modules knows that. This is the most relevant pass in the series and
the one where the gap between us and a professional options desk is widest.

The uncomfortable conclusion up front: three of these four are gated on **one data acquisition** - a daily
options chain snapshot with open interest by strike. That is a more valuable purchase than any further bar
history, and it is a strategy decision, not an engineering one.

---

## 20. Dealer gamma exposure - the mechanism behind 0DTE behaviour  →  `gex.py` (gated)

**What it is.** Option dealers who sell to customers hedge their delta continuously. When dealers are net
**long gamma**, hedging means selling into rallies and buying dips - they dampen moves. When net **short
gamma**, hedging means buying into rallies and selling dips - they amplify. The sign and size of aggregate
dealer gamma therefore predicts whether an intraday move will extend or mean-revert, which is precisely
the question every one of our alerts is implicitly betting on.

**Why it matters more for 0DTE than for anything else.** Same-day options have enormous gamma per dollar
of premium, so the hedging flow they induce is large relative to the premium traded. The SSRN work on
0DTEs (gamma risk and volatility propagation) documents that this activity shifts intraday volatility
rather than simply reflecting it.

**What it would change.** A long alert fired while dealers are short gamma is riding amplification; the
same alert under long gamma is fighting a dampener. Identical text, opposite expectancy - and we currently
treat them as the same trade. This is the single largest source of unexplained variance in our corpus.

**Why it is gated.** GEX needs open interest by strike and an assumption about who is long (the standard
naive assumption - customers long calls, short puts - is stated as an assumption, not a fact). Without a
chain snapshot we cannot compute it, and a made-up GEX would be worse than none.

**Acceptance test.** Given a chain snapshot: positive GEX predicts lower realised intraday range than
negative GEX on the same underlying, tested across sessions; the sign convention is asserted on a
hand-computed example so a flipped sign cannot ship silently.

Sources: SSRN 4692190 (0DTEs: trading, gamma risk and volatility propagation); theintrinsicinvestor's
"Gamma Trap" write-up; harbourfrontquant's extension of the GEX framework.

---

## 21. Expiry-day pinning - the strike wants to be the close  →  `pinning.py` (partly buildable now)

**What it is.** Golez & Jackwerth, and Jeannin et al., document that on expiry the underlying tends to
close near the strike with the heaviest open interest, because delta-hedging of expiring options pulls
spot toward it.

**Why it matters here.** We trade 0DTE index options almost exclusively. If pinning is real, then a trade
entered 2% away from the pinning strike has a different expectancy from the same trade entered 0.2% away,
and a *long* option that needs to move has to overcome a force pulling it back. That is testable against
our own data without a chain: the distance from our strikes to the nearest round-number cluster, and
whether realised close-to-strike distance is smaller than a random-walk baseline would predict.

**What is buildable now.** The distance features and the closeness test, using bars plus strikes. The full
version needs OI to identify *which* strike is the pin.

**Acceptance test.** Realised |close − strike| across our 0DTE trades is compared with a simulated
baseline of the same magnitude; a pinning effect shows as a distribution more concentrated near zero. If
it does not appear, the report says so - a negative result here is worth as much as a positive one,
because it stops us building a rule on folklore.

**Acceptance test (gated version).** With a chain, the heaviest-OI strike predicts the close better than
the at-the-money strike does.

Sources: Golez & Jackwerth, "Pinning in the S&P 500 Futures"; Jeannin et al., City University Economics
WP 0604 on hedging-driven pinning.

---

## 22. Vanna and charm - the second-order flows nobody prices  →  `greeks_flow.py` (gated)

**What it is.** Vanna is ∂²V/∂S∂σ - how delta moves when implied vol moves. Charm is ∂Δ/∂t - how delta
decays as expiry approaches. Both force dealers to hedge *even when spot does not move*, which is the
quiet drift that looks like nothing and is actually flow.

**Why it matters.** Our 0DTE contracts have extreme charm: delta decays toward zero (or one) within hours,
so dealer hedging around expiry is continuous and mechanical. Post-event afternoon drift that looks
mysterious is largely charm and vanna. Industry estimates put vanna-driven hedging in SPX at billions of
dollars on high-vol days.

**Status.** Gated on the chain (needs IV by strike to compute the greeks per contract). Listed because it
is the correct explanation for a phenomenon we would otherwise attribute to the signal.

**Acceptance test.** On a chain, computed vanna/charm hedging estimates correlate with intraday drift after
removing the underlying's own return; the report states the sign convention and refuses when IV is
missing.

Sources: gexradar and flashalpha treatments of vanna/charm; the studylib dealer-flows primer.

---

## 23. Intraday variance risk premium  →  `vrp.py` (buildable now)

**What it is.** The variance risk premium is implied variance minus realised variance; the Wiley
decomposition separates it into overnight and intraday components, and the two behave differently.

**Why it matters for us.** We are structurally **long variance** - we buy options. A long-variance position
earns the VRP only if realised exceeds implied. If the intraday VRP is reliably negative for 0DTE index
options (i.e. implied is rich versus realised), then every alert is fighting a headwind that has nothing to
do with the signal, and the edge must be larger than that premium to exist at all.

**What is buildable without a chain.** A *realised* variance series from bars, and a proxy for the premium
using the corpus's own entry premiums against subsequent realised moves - the option-implied move can be
backed out of the premium with a simple model, labelled as a model.

**Acceptance test.** Realised intraday variance is compared with the premium-implied move across trades;
the report states the model used to invert premium into implied volatility, and refuses to report a VRP
where the premium is missing or the model's assumptions fail.

Sources: Wiley `jfut.22589` (VRP over trading and non-trading periods); Bollerslev, Tauchen & Zhou on
variance risk premia.

---

## The strategic recommendation this pass produces

All four items point at the same gap: **we have price history and no options data.** Bars tell us what the
contract did; they cannot tell us what everyone else holds, and for 0DTE that second thing is the
mechanism.

The priority order for data acquisition changes accordingly:

1. **one historical options chain snapshot per day** - open interest and implied volatility by strike.
   Unlocks items 20, 21 (full), and 22, which is three of the four here plus a better version of the
   selection work in `selection.py`.
2. bars for the 97 uncovered contract-days - unblocks the five measurement subsystems already built.
3. intraday volume by bar - enables item 18 (volume bars) and item 15 (impact estimation).

Ranking chain data above bars is counter-intuitive and deliberate: bars tell us what happened to *our*
contract, the chain tells us what *everyone* holds. For a 0DTE desk the second determines the first.

---

## Revised master sequence

    experts.py      no-regret weighting over variants      no new data   (FRONTIER_4)
    conformal.py    guaranteed intervals                   no new data   (FRONTIER_4)
    vrp.py          long-variance headwind                 bars          (this pass)
    pinning.py      distance features + closeness test     bars + strikes(this pass)
    deflated.py     audit of claims already made           no new data   (FRONTIER_3)
    gex.py          dealer positioning                     CHAIN DATA     (this pass)
    impact.py       square-root law                        own fills      (FRONTIER_4)

Two of the seven need nothing at all, and they are the two that change how we choose and how we report.
That is where the next session should start.
