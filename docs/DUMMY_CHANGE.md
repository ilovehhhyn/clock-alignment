# The dummy change

The dummy change is a set of rule changes written after game 3 (protocol v5) and shelved
unapplied. Helen named it on 2026-11-10 and refers to it as "the dummy change". The code
in this repository plays the game 3 rules; the dummy change lives only in
`docs/dummy-change.patch`, a unified diff against this tree.

## What it changes

- The Imp is never offered itself as a night target, and the Poisoner is never offered a red
  player. In games 2 and 3 the Imp chose its own alias on night 2 and the Poisoner poisoned
  its own Imp on nights 1 and 2, each believing it protected the Imp.
- The branch where an Imp eliminates itself and a Minion inherits the role is removed; the
  rules text says the Imp role passes only through the Scarlet Woman. This departs from the
  tabletop game.
- The Imp and Poisoner effect lines in the briefing say what cannot be chosen.
- The confession rule also rejects "my role as Poisoner", "my ability as the Scarlet Woman",
  "let's ... eliminate blue players" and "our red teammates".
- The protocol label becomes `dynamic-delegates-v6`, and the tests follow.

Not part of it, and kept in the tree: the probe scans nomination texts for self-reveals and
reports a delivered self-reveal as a warning, never as a harness failure.

## How to apply or inspect it

```bash
git apply --check docs/dummy-change.patch
```

```bash
git apply docs/dummy-change.patch
```

The patch was cut from the game 3 code copy on Della against the local tree of 2026-11-10;
it covers `clock_alignment/{briefing,config,engine,rules,secrecy}.py`, `docs/PROTOCOL.md`
and three test files. Nothing in it has run in a game.

Since protocol v7 (the Storyteller's confirmation check, 2026-11-11) the tree's protocol label
and the rules' succession sentence differ from the patch's context, so `git apply` reports a
conflict on `config.py`, `engine.py` and `rules.py`; apply the other hunks with
`git apply --reject` and move the three remaining changes by hand (the label would then be a
new version, not v6).
