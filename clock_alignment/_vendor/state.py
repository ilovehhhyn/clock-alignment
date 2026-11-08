"""Grimoire state and Trouble Brewing rules.

The Grimoire holds the truth. Everything the Storyteller decides (setup details,
information for impaired players, Spy/Recluse registration, Mayor bounces) is a
Choice: the full list of legal values plus the scripted Storyteller's style (see
POLICY). The Storyteller sometimes picks uniformly among all legal values, so no
legal option is ever ruled out; the engine never applies an illegal value.
"""
from __future__ import annotations
from dataclasses import dataclass
from itertools import combinations, product
import random
from typing import Callable
from .characters import (ALL, DEMONS, INFO_ROLES, MINIONS, OUTSIDERS, SETUPS, TOWNSFOLK,
                         is_evil_character, kind)


@dataclass
class Player:
    name: str
    seat: int
    character: str          # true character; changes only when a Minion becomes the Imp
    shown: str              # the character this player believes they are (differs only for the Drunk)
    alive: bool = True
    ghost_vote: bool = True

    @property
    def evil(self):
        return is_evil_character(self.character)


def team_sizes(n, baron=False):
    """(Townsfolk, Outsiders, Minions) for n players, from the official table."""
    tf, os_, mn = SETUPS[n]
    return (tf - 2, os_ + 2, mn) if baron else (tf, os_, mn)


def setup_error(characters):
    """Why a list of characters is not a legal Trouble Brewing setup, or None."""
    n = len(characters)
    if n not in SETUPS: return f'Trouble Brewing is for {min(SETUPS)}-{max(SETUPS)} players'
    if len(set(characters)) != n: return 'characters must be distinct'
    bad = [c for c in characters if c not in ALL]
    if bad: return f'not on the script: {bad}'
    counts = {k: sum(1 for c in characters if kind(c) == k) for k in ('Townsfolk', 'Outsider', 'Minion', 'Demon')}
    if counts['Demon'] != 1: return 'need exactly one Imp'
    tf, os_, mn = team_sizes(n, 'Baron' in characters)
    if counts['Minion'] != mn: return f'need exactly {mn} Minion(s)'
    if (counts['Townsfolk'], counts['Outsider']) != (tf, os_):
        return f'need {tf} Townsfolk and {os_} Outsiders' + (' (Baron in play)' if 'Baron' in characters else '')
    return None


def random_setup(rng, minion=None, players=9):
    """A uniformly random legal setup; `minion` forces one of the Minions (batches rotate it)."""
    mn = SETUPS[players][2]
    minions = ([minion] if minion else []) + rng.sample([m for m in MINIONS if m != minion], mn - (1 if minion else 0))
    tf, os_, _ = team_sizes(players, 'Baron' in minions)
    return rng.sample(TOWNSFOLK, tf) + rng.sample(OUTSIDERS, os_) + minions + ['Imp']


# How the scripted Storyteller uses its discretion. Every number is a judgment call about what an
# experienced human Storyteller tends to do. There is no separate random floor: the style is itself
# random and can produce any answer a working ability could give in some legal game (tests check
# this), so no plausible option is ever ruled out, and impossible ones never appear.
POLICY = {
    # impaired players
    'drunk_truthful': 0.2,    # per game: the Drunk gets true information (else from their false world)
    'poisoned_false': 0.8,    # per night: a poisoned player's information comes from a false world (else true)
    'false_vouch_evil': 0.7,  # a misinformed Washerwoman's pair includes an evil player (else any player)
    # registration (weighted toward evil, as a Storyteller balancing the game would)
    'spy_evil': 0.1,          # the Spy registers as evil
    'recluse_evil': 0.5,      # the Recluse registers as evil
    'recluse_as_imp': 0.3,    # a Recluse registering as an evil character registers as the Imp (else a Minion)
    'as_bluff': 0.7,          # an evil player shown as a good character is shown as a Demon bluff,
    'as_in_play': 0.1,        # or as a good character in play (else one not in play)
    'misregister_pair': 0.5,  # a Washerwoman/Librarian/Investigator is shown a misregistering Spy/Recluse
    'recluse_slain': 0.2,     # the Slayer's shot kills the Recluse
    'spy_virgin': 0.2,        # the Spy registers as a Townsfolk to the Virgin
    # setup and other
    'drunk_info_weight': 3,   # the Drunk believes they are an information role, 3x as likely as another
    'bluff_drunk_role': 0.05, # the Drunk's apparent character may be among the Demon's bluffs (else kept out)
    'bluff_drunk': 0.05,      # the Drunk may be among the Demon's bluffs (else kept out)
    'herring_townsfolk': 0.85,  # the red herring is a Townsfolk other than the Fortune Teller (else any good player)
    'pair_partner_good': 0.9, # an Investigator's second player is a good player (else anyone)
    'mayor_bounce': 0.5,      # an attack on the Mayor kills someone else
    'bounce_evil': 0.05,      # a Mayor bounce may land on the Minion (else a good player)
}


def norm(value):
    """A Storyteller value: any of players, character, number, yes, characters."""
    value = value or {}
    return {'players': list(value.get('players') or []), 'character': value.get('character'),
            'number': value.get('number'), 'yes': value.get('yes'),
            'characters': list(value.get('characters') or [])}


def _key(v):
    v = norm(v)
    return tuple(sorted(v['players'])), v['character'], v['number'], v['yes'], tuple(sorted(v['characters']))


class Choice:
    """A decision the Storyteller makes: every legal value, and the scripted style."""

    def __init__(self, id, values, style: Callable[[random.Random], dict]):
        self.id, self.values, self.style = id, [norm(v) for v in values], style
        self.keys = {_key(v) for v in self.values}
        self.auto = len(self.keys) == 1  # only one legal value: nothing to decide

    def legal(self, value):
        return _key(value) in self.keys

    def options(self, r):
        """The scripted Storyteller's pick: the style (random, and always legal)."""
        if self.auto:
            return self.values[0]
        return norm(self.style(r))


class Grimoire:
    def __init__(self, names, characters, drunk_as=None):
        self.players = [Player(n, i, c, drunk_as if c == 'Drunk' else c) for i, (n, c) in enumerate(zip(names, characters))]
        self.by_name = {p.name: p for p in self.players}
        self.red_herring = None
        self.bluffs = []
        self.reminders = []         # what the pair abilities were shown, as on the real Grimoire
        self.poisoned = None        # name of the poisoned player (tonight and tomorrow day)
        self.protected = None       # Monk protection tonight
        self.master = None          # Butler's master for today's votes
        self.slayer_used = False
        self.virgin_used = False
        self.day = 0
        self.executed_death = None  # who died by execution today (Undertaker)
        self.alive_at_execution = 0  # players alive just before that execution
        self.decoys = {}            # impaired player -> the false picture their information follows
        self.truthful = {}          # Drunk -> whether their information is true this game
        self.winner = None
        self.reason = None

    def __getitem__(self, name):
        return self.by_name[name]

    # ---- basic queries -------------------------------------------------------------
    def living(self):
        return [p for p in self.players if p.alive]

    def alive_count(self):
        return len(self.living())

    def with_character(self, character, alive=True):
        return next((p for p in self.players if p.character == character and (p.alive or not alive)), None)

    def demon(self):
        return self.with_character('Imp')

    def is_poisoned(self, p):
        return self.poisoned == p.name

    def impaired(self, p):
        """The player's own ability (as they understand it) is not working."""
        return p.character == 'Drunk' or self.is_poisoned(p)

    def works(self, p, character):
        """p is alive, really is `character`, and is not poisoned."""
        return p.alive and p.character == character and not self.is_poisoned(p)

    def neighbours(self, p):
        """Nearest living players clockwise and anticlockwise (excluding p)."""
        n = len(self.players)
        out = []
        for step in (-1, 1):
            i = p.seat
            for _ in range(n - 1):
                i = (i + step) % n
                q = self.players[i]
                if q.alive and q is not p:
                    out.append(q)
                    break
        if len(out) == 2 and out[0] is out[1]:
            out = out[:1]
        return out

    # ---- registration --------------------------------------------------------------
    def registers_evil(self, p):
        """Set of possible answers to 'is p evil?'"""
        if p.character in ('Spy', 'Recluse') and not self.is_poisoned(p):
            return {True, False}
        return {p.evil}

    def registers_as(self, p):
        """Set of characters p might register as."""
        if p.character == 'Spy' and not self.is_poisoned(p):
            return {'Spy', *TOWNSFOLK, *OUTSIDERS}
        if p.character == 'Recluse' and not self.is_poisoned(p):
            return {'Recluse', *MINIONS, *DEMONS}
        return {p.character}

    def registers_demon_to_ft(self, p):
        if p.character == 'Imp' or p.name == self.red_herring:
            return {True}
        if p.character == 'Recluse' and not self.is_poisoned(p):
            return {True, False}
        return {False}

    def decoy(self, p, r):
        """The false world an impaired player's information comes from, fixed per player so it stays
        consistent from night to night: a framed good player is the Demon, as many other non-Demon
        players as the game has Minions are the Minions, and everyone else (the real Demon included)
        is good. Misinformed results are what
        a working ability would show in that world, so they look like real results: never inverted,
        and sometimes true where the false world agrees with the real one."""
        if p.name not in self.decoys:
            others = [q for q in self.players if q is not p]
            framed = r.choice([q for q in others if not q.evil])
            minions = sum(1 for q in self.players if q.character in MINIONS) or 1
            partners = r.sample([q for q in others if q is not framed and q.character != 'Imp'], minions)
            self.decoys[p.name] = (framed.name, {framed.name, *(q.name for q in partners)})
        return self.decoys[p.name]

    def false_evil(self, p, r):
        """Who registers as evil in p's false world this time: its two evil players, plus the Recluse
        by the usual registration chance (as it would for a working ability)."""
        evil = set(self.decoy(p, r)[1])
        for q in self.players:
            if q.character == 'Recluse' and not self.is_poisoned(q) and r.random() < POLICY['recluse_evil']:
                evil.add(q.name)
        return evil

    def false_result(self, p, r, result, truths):
        """A misinformed result, `result(r)` drawn from p's false world. A poisoned player's false world is
        redrawn until the result is not one a working ability could give (when some result is not), so
        poison makes information false. The Drunk keeps one false world all game, so their story holds."""
        v = result(r)
        for _ in range(30):
            if p.character == 'Drunk' or v not in truths:
                return v
            self.decoys.pop(p.name, None)
            v = result(r)
        return v

    def minions_shown(self):
        """Minion characters a false or misregistered result may name: the Baron only when in play
        (the Outsider count would otherwise rule it out)."""
        return [m for m in MINIONS if m != 'Baron' or 'Baron' in self.in_play()]

    def misinformed(self, p, r):
        """Whether the scripted Storyteller gives this impaired player false information now. The
        Drunk's is decided once per game, so their story stays consistent either way."""
        if p.character == 'Drunk':
            if p.name not in self.truthful:
                self.truthful[p.name] = r.random() < POLICY['drunk_truthful']
            return not self.truthful[p.name]
        return self.is_poisoned(p) and r.random() < POLICY['poisoned_false']

    def sample_evil(self, r):
        """Who registers as evil this time, under the scripted Storyteller's registration policy."""
        out = set()
        for q in self.players:
            chance = {'Spy': POLICY['spy_evil'], 'Recluse': POLICY['recluse_evil']}.get(q.character)
            if chance is not None and not self.is_poisoned(q):
                if r.random() < chance:
                    out.add(q.name)
            elif q.evil:
                out.add(q.name)
        return out

    def fake_good(self, r, kinds=(TOWNSFOLK,), avoid=(), bluffs=True):
        """A good character to show for an evil player: a Demon bluff, one in play, or another. With
        bluffs=False (for a good player) never a Demon bluff: no good player can be one in any world."""
        pool = [c for k in kinds for c in k if c not in avoid and (bluffs or c not in self.bluffs)]
        bluffs = [c for c in pool if c in self.bluffs]
        in_play = [c for c in pool if c in self.in_play() and c not in bluffs]
        other = [c for c in pool if c not in bluffs and c not in in_play]
        groups = [(x, w) for x, w in ((bluffs, POLICY['as_bluff']), (in_play, POLICY['as_in_play']),
                                      (other, 1 - POLICY['as_bluff'] - POLICY['as_in_play'])) if x]
        return r.choice(r.choices([x for x, _ in groups], [w for _, w in groups])[0])

    def evil_character(self, r, imp=True):
        """An evil character to show for a Recluse (the Imp only when `imp` is consistent)."""
        return 'Imp' if imp and r.random() < POLICY['recluse_as_imp'] else r.choice(self.minions_shown())

    def in_play(self):
        return {q.character for q in self.players} | {q.shown for q in self.players}

    # ---- deaths and victory ----------------------------------------------------------
    def die(self, p):
        p.alive = False
        if p.character == 'Poisoner':
            self.poisoned = None

    def scarlet_woman_heir(self, alive_before):
        sw = self.with_character('Scarlet Woman')
        if sw and self.works(sw, 'Scarlet Woman') and alive_before >= 5:
            return sw
        return None

    def check_victory(self):
        if self.winner: return self.winner
        if not self.demon():
            self.winner, self.reason = 'good', 'The Demon is dead.'
        elif self.alive_count() <= 2:
            self.winner, self.reason = 'evil', 'Only two players remain alive.'
        return self.winner

    # ---- views ---------------------------------------------------------------------
    def grimoire_text(self, reminders=()):
        lines = []
        for p in self.players:
            tags = []
            if p.character == 'Drunk': tags.append(f'thinks they are the {p.shown}')
            if not p.alive: tags.append('DEAD' + ('' if p.ghost_vote else ', ghost vote used'))
            if self.is_poisoned(p): tags.append('POISONED')
            if p.name == self.protected: tags.append('protected by the Monk tonight')
            if p.name == self.red_herring: tags.append("Fortune Teller's red herring")
            if p.name == self.master: tags.append("Butler's master")
            team = 'evil' if p.evil else 'good'
            lines.append(f'{p.seat + 1}. {p.name}: {p.character} ({team})' + (f" [{'; '.join(tags)}]" if tags else ''))
        if self.bluffs: lines.append(f"Demon bluffs: {', '.join(self.bluffs)}")
        used = [x for x, flag in (('Slayer', self.slayer_used), ('Virgin', self.virgin_used)) if flag]
        if used: lines.append(f"Spent once-per-game abilities: {', '.join(used)}")
        lines += list(self.reminders) + list(reminders)
        return '\n'.join(lines)


# ---- Storyteller choices -----------------------------------------------------------

def drunk_as_choice(options):
    """The Townsfolk the Drunk believes they are (one not in play)."""
    w = [POLICY['drunk_info_weight'] if c in INFO_ROLES else 1 for c in options]
    return Choice('drunk_as', [{'character': c} for c in options], lambda r: {'character': r.choices(options, w)[0]})


def red_herring_choice(g):
    """A good player who registers as the Demon to the Fortune Teller: usually a Townsfolk other than
    the Fortune Teller (so a good player is framed), else any good player."""
    good = [p.name for p in g.players if not p.evil]
    townsfolk = [p.name for p in g.players if p.character in TOWNSFOLK and p.character != 'Fortune Teller']

    def style(r):
        pool = townsfolk if townsfolk and r.random() < POLICY['herring_townsfolk'] else good
        return {'players': [r.choice(pool)]}
    return Choice('red_herring', [{'players': [x]} for x in good], style)


def pick_bluffs(r, options, drunk, drunk_shown=None):
    """Three Demon bluffs that fit the setup's counts: an information role (so evil can invent checkable
    results), and one Outsider only when the Drunk is in play (the town sees one Outsider fewer than
    the setup has, so one evil Outsider claim fits; otherwise it would break the count). The Drunk's
    apparent character (a sure double claim) and the Drunk (unclaimable) are usually kept out."""
    avoid = {c for c, keep in ((drunk_shown, POLICY['bluff_drunk_role']), ('Drunk', POLICY['bluff_drunk']))
             if c and r.random() >= keep}
    if len([c for c in options if c not in avoid]) >= 3:
        options = [c for c in options if c not in avoid]
    townsfolk = [c for c in options if c not in OUTSIDERS]
    outsiders = [c for c in options if c in OUTSIDERS]
    picks = []
    info = [c for c in townsfolk if c in INFO_ROLES]
    if info:
        picks.append(r.choice(info))
    if drunk and outsiders:
        picks.append(r.choice(outsiders))
    rest = [c for c in townsfolk if c not in picks]
    if len(rest) < 3 - len(picks):  # too few Townsfolk left (never at 10-12 players)
        rest = [c for c in options if c not in picks]
    return picks + r.sample(rest, 3 - len(picks))


def bluffs_choice(options, drunk, drunk_shown=None):
    """Three good characters not in play, shown to the Imp."""
    return Choice('bluffs', [{'characters': list(c)} for c in combinations(options, 3)],
                  lambda r: {'characters': pick_bluffs(r, options, drunk, drunk_shown)})


def _pair(r, first, others, avoid=()):
    """`first` and a second player from `others` (not in `avoid` if possible), in random order."""
    pool = [x for x in others if x != first and x not in avoid] or [x for x in others if x != first]
    pair = [first, r.choice(pool)]
    r.shuffle(pair)
    return pair


def pair_choice(g, p, ability):
    """Washerwoman / Librarian / Investigator starting information."""
    group = {'Washerwoman': TOWNSFOLK, 'Librarian': OUTSIDERS, 'Investigator': MINIONS}[ability]
    others = [q.name for q in g.players if q is not p]
    impaired = g.impaired(p)
    values = [{'players': [a, b], 'character': c} for a, b in combinations(others, 2) for c in group
              if impaired or any(c in g.registers_as(g[x]) for x in (a, b))]
    if ability == 'Librarian' and (impaired or not any(g.registers_as(q) <= set(OUTSIDERS) for q in g.players)):
        values.append({'players': [], 'character': None})  # "zero Outsiders in play"

    def false_pair(r, first, pool):
        """A pair with `first` and a character from `pool` that neither player really is (nor believes
        they are), and never the recipient's own apparent character: false, and not self-revealing."""
        for _ in range(50):
            pair = _pair(r, first, others)
            c = r.choice(pool)
            if c != p.shown and all(c not in (g[x].character, g[x].shown) for x in pair):
                return {'players': pair, 'character': c}
        return {'players': pair, 'character': c}

    def style(r):
        if impaired and g.misinformed(p, r):
            framed, _ = g.decoy(p, r)
            evil = [q.name for q in g.players if q.evil and q is not p]
            if ability == 'Washerwoman':  # usually vouch for an evil player
                shown = r.choice(evil) if evil and r.random() < POLICY['false_vouch_evil'] else r.choice(others)
                return false_pair(r, shown, [g.fake_good(r, bluffs=g[shown].evil) for _ in range(6)])
            if ability == 'Librarian':
                if not any(q.character in OUTSIDERS for q in g.players):
                    return {'players': [], 'character': None}  # the false world has no Outsiders either
                shown = r.choice(evil + [framed])
                return false_pair(r, shown, [g.fake_good(r, (OUTSIDERS,), bluffs=g[shown].evil) for _ in range(6)])
            return false_pair(r, framed, g.minions_shown())  # frame a good player
        real = [(q.name, q.character) for q in g.players if q is not p and q.character in group]
        mis = []  # a misregistering Spy (as a Townsfolk or Outsider) or Recluse (as a Minion)
        for q in g.players:
            if q is p or q.character not in ('Spy', 'Recluse') or q.character in group:
                continue
            options = g.registers_as(q) & set(group)
            if not options:
                continue
            if q.character == 'Spy':  # never the recipient's own character (that would point at the Spy)
                mis.append((q.name, g.fake_good(r, (group,), avoid=(p.shown,))))
            else:
                mis.append((q.name, r.choice(sorted(options & set(g.minions_shown())))))
        pool = mis if mis and r.random() < POLICY['misregister_pair'] else real  # with no real one: 'zero'
        if not pool:
            return {'players': [], 'character': None}
        n, c = r.choice(pool)
        avoid = ()
        if ability == 'Investigator' and r.random() < POLICY['pair_partner_good']:
            avoid = {q.name for q in g.players if q.evil}  # the second player is a good one
        return {'players': _pair(r, n, others, avoid), 'character': c}
    return Choice(f'{ability}:{p.name}', values, style)


def _pairs(ring, evil):
    return sum(1 for i in range(len(ring)) if ring[i].name in evil and ring[(i + 1) % len(ring)].name in evil)


def chef_choice(g, p):
    ring = g.players
    possible = set()
    for combo in product(*[sorted(g.registers_evil(q)) for q in ring]):
        possible.add(sum(1 for i in range(len(ring)) if combo[i] and combo[(i + 1) % len(ring)]))
    values = range(0, 5) if g.impaired(p) else sorted(possible)

    def style(r):
        if g.impaired(p) and g.misinformed(p, r):
            return {'number': g.false_result(p, r, lambda r: _pairs(ring, g.false_evil(p, r)), possible)}
        return {'number': _pairs(ring, g.sample_evil(r))}
    return Choice(f'Chef:{p.name}', [{'number': x} for x in values], style)


def empath_choice(g, p):
    nb = g.neighbours(p)
    possible = {sum(c) for c in product(*[sorted(g.registers_evil(q)) for q in nb])}
    values = [0, 1, 2] if g.impaired(p) else sorted(possible)

    def style(r):
        if g.impaired(p) and g.misinformed(p, r):
            count = lambda r: sum(1 for q in nb if q.name in g.false_evil(p, r))
            return {'number': g.false_result(p, r, count, possible)}
        evil = g.sample_evil(r)
        return {'number': sum(1 for q in nb if q.name in evil)}
    return Choice(f'Empath:{p.name}', [{'number': x} for x in values], style)


def fortune_teller_choice(g, p, targets):
    ts = [g[t] for t in targets]
    possible = {any(c) for c in product(*[sorted(g.registers_demon_to_ft(q)) for q in ts])}
    impaired = g.impaired(p)
    values = [True, False] if impaired else sorted(possible)

    def style(r):
        if impaired and g.misinformed(p, r):  # in the false world the framed player is the Demon
            def yes(r):
                framed, _ = g.decoy(p, r)
                recluse = any(g[t].character == 'Recluse' and r.random() < POLICY['recluse_evil'] for t in targets)
                herring = g.red_herring if p.character == 'Drunk' else None  # a poisoned FT's false world has none
                return recluse or any(t in (framed, herring) for t in targets)
            return {'yes': g.false_result(p, r, yes, possible)}
        if len(possible) == 1:
            return {'yes': next(iter(possible))}
        return {'yes': r.random() < POLICY['recluse_evil']}
    return Choice(f'Fortune Teller:{p.name}', [{'yes': x} for x in values], style)


def character_choice(g, p, ability, target):
    """Undertaker (executed player) and Ravenkeeper (chosen player)."""
    t = g[target]
    impaired = g.impaired(p)
    values = ALL if impaired else sorted(g.registers_as(t))

    def as_good(r):  # an evil player shown as a good character (usually a Demon bluff), never the recipient's
        for _ in range(30):
            c = g.fake_good(r, (TOWNSFOLK, OUTSIDERS))
            if c != p.shown:
                return c
        return c

    def style(r):
        if t is p:  # a Ravenkeeper who picks themselves learns what they think they are (a Drunk too)
            return {'character': p.shown}
        # "Imp" for an executed player is only consistent with the game going on if a Scarlet Woman
        # could have taken over (5+ alive at the execution)
        imp_ok = ability == 'Ravenkeeper' or g.alive_at_execution >= 5

        def false_world(r):  # what a working ability would show in the false world
            framed, evil = g.decoy(p, r)
            if t.name in evil:  # the framed player is the false world's Imp, the others its Minions
                return 'Imp' if t.name == framed and imp_ok else r.choice(g.minions_shown())
            if t.evil:  # the real evil player is good in the false world
                return as_good(r)
            if t.character == 'Recluse' and r.random() < POLICY['recluse_evil']:
                return g.evil_character(r, imp_ok)
            return t.character  # an unframed good player is who they are

        if impaired and g.misinformed(p, r):
            return {'character': g.false_result(p, r, false_world, set(g.registers_as(t)))}
        if t.character == 'Spy' and len(values) > 1 and r.random() > POLICY['spy_evil']:
            return {'character': as_good(r)}
        if t.character == 'Recluse' and len(values) > 1 and r.random() < POLICY['recluse_evil']:
            return {'character': g.evil_character(r, imp_ok)}
        return {'character': t.character}
    return Choice(f'{ability}:{p.name}', [{'character': c} for c in values], style)


def heir_choice(minions):
    """Which living Minion becomes the Imp when the Imp kills themselves (no Scarlet Woman to take over)."""
    return Choice('starpass', [{'players': [m]} for m in minions], lambda r: {'players': [r.choice(minions)]})


def yes_choice(cid, chance):
    return Choice(cid, [{'yes': True}, {'yes': False}], lambda r: {'yes': r.random() < chance})


def mayor_choice(g, mayor, imp):
    """Who dies when the Imp attacks a healthy Mayor: the Mayor, or someone else instead."""
    victims = [q.name for q in g.living() if q is not imp and not g.works(q, 'Soldier') and q.name != g.protected]
    others = [n for n in victims if n != mayor.name]
    good = [n for n in others if not g[n].evil]

    def style(r):
        if not others or r.random() >= POLICY['mayor_bounce']:
            return {'players': [mayor.name]}
        pool = good if good and r.random() >= POLICY['bounce_evil'] else others
        return {'players': [r.choice(pool)]}
    return Choice(f'Mayor:{mayor.name}', [{'players': [x]} for x in victims], style)


def info_text(ability, value, target=None):
    """What the recipient is told."""
    v = norm(value)
    if ability in ('Washerwoman', 'Librarian', 'Investigator'):
        if not v['players']:
            return 'You learn that there are zero Outsiders in play.'
        a, b = v['players']
        return f'You learn that either {a} or {b} is the {v["character"]}.'
    if ability == 'Chef':
        return f'You learn that there {"is" if v["number"] == 1 else "are"} {v["number"]} pair{"" if v["number"] == 1 else "s"} of evil players sitting next to each other.'
    if ability == 'Empath':
        return f'You learn that {v["number"]} of your living neighbours {"is" if v["number"] == 1 else "are"} evil.'
    if ability == 'Fortune Teller':
        return f'You chose {target[0]} and {target[1]}: {"YES, one of them is the Demon." if v["yes"] else "NO, neither is the Demon."}'
    if ability == 'Undertaker':
        return f'You learn that the player executed today ({target}) was the {v["character"]}.'
    if ability == 'Ravenkeeper':
        return f'You learn that {target} is the {v["character"]}.'
    raise KeyError(ability)
