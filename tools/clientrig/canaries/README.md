# Canary fixtures

A canary is a build with a known defect in it. A lane earns the right to report
by being **observed failing** against one and passing against the clean build,
with both observations stored in its credential.

**Passing is the failure condition.** A lane that passes a canary has failed the
meta-test: it just demonstrated it cannot see the class it claims to watch, so
none of its greens for that class mean anything. The only other acceptable
outcome is "blind", and only when the lane's *frozen* credential already
authorises blindness to that class -- widening a blind list to silence a newly
failing canary is the cheapest possible repair, reads as honesty in review, and
leaves the rig blind. That is the failure this whole mechanism exists to prevent.

## Two kinds, and they are not equal evidence

**`recorded`** fixtures are real defects from this project's history that still
exist in HEAD. They carry no patch, because there is nothing to patch in. Two
qualify, both measured and written up:

| Fixture | Class | What makes it observable |
|---|---|---|
| `rail-jump-40px` | jump-landing | one ~40px correction on 6 of 7 letters sampled |
| `pointer-capture-retarget` | click-retarget | brightest-button index across 12 real touch moves |

These are **the credential**. Each lane must reproduce at least one.

**`injected`** fixtures are authored. They cover classes the recorded pair do not
reach, and they are explicitly weaker evidence: an injection shows that a
detector catches defects designed by whoever wrote the detector, which is not
the same as catching one shaped the way real defects arrive. They are additional
controls, never a credential on their own.

## Why a magnitude series rather than one dose

This project's reference defect was a 47-second freeze. A lane credentialed
against an injected stall of that size proves close to nothing about catching a
200ms one -- and because the fixtures are authored, their size is *chosen*
rather than discovered. Each injected fixture declares a descending series, and
titration records the smallest rung the lane still detected above its own
between-run spread. That figure is the lane's **sensitivity floor**, it travels
on every result the lane later produces, and a measured difference below it
renders as "not observed" rather than as a pass or an improvement.

A lane that detects every rung gets a floor too, but it is reported as a *bound*
-- the series did not go low enough to find where the lane stops seeing.

## Why a drifted anchor is fatal

An injected fixture patches by finding exact source text. If that text has
moved, the patch silently does nothing: the "canary" is a clean build, the lane
correctly reports no defect, and the credential records a detection that never
happened. Applying a fixture therefore **raises** unless its anchor matches
exactly once. The suite asserts the same thing statically, so drift surfaces on
an ordinary test run rather than at credentialing time.

This is not hypothetical here. A previous mutation-testing pass in this repo
produced a mutant that accidentally implemented the fix it was meant to break,
and reported a false pass.

## Observables are quantities, not presence checks

Every fixture names a *discriminating observable* -- something that moves with
the defect. The precedent is the magnetic-flare investigation, where the
load-bearing signal was the index of the brightest button per sample, **not**
whether a variable was non-zero: a single frozen frame is also non-zero.

So: which artist opened, not that a drill happened. The count of post-landing
corrections, not whether the jump looked fine. The identity of the row at the
remembered offset, not the pixel offset.

## Fixture descriptors are data

Each is a plain JSON file, reviewable without executing anything. `kind`
determines which fields are required, and validation rejects a `recorded`
fixture carrying a patch (an injection wearing the stronger label) and an
`injected` fixture whose template ignores `{magnitude}` (every rung would inject
the same dose, making the reported floor meaningless).
