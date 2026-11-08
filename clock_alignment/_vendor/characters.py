"""Trouble Brewing characters: types, ability text and night order."""

TOWNSFOLK = ['Washerwoman', 'Librarian', 'Investigator', 'Chef', 'Empath', 'Fortune Teller',
             'Undertaker', 'Monk', 'Ravenkeeper', 'Virgin', 'Slayer', 'Soldier', 'Mayor']
OUTSIDERS = ['Butler', 'Drunk', 'Recluse', 'Saint']
MINIONS = ['Poisoner', 'Spy', 'Scarlet Woman', 'Baron']
DEMONS = ['Imp']
ALL = TOWNSFOLK + OUTSIDERS + MINIONS + DEMONS
GOOD = TOWNSFOLK + OUTSIDERS
# Official Trouble Brewing setup by player count: (Townsfolk, Outsiders, Minions), plus one Demon.
# The Baron turns two Townsfolk into Outsiders.
SETUPS = {5: (3, 0, 1), 6: (3, 1, 1), 7: (5, 0, 1), 8: (5, 1, 1), 9: (5, 2, 1), 10: (7, 0, 2), 11: (7, 1, 2),
          12: (7, 2, 2), 13: (9, 0, 3), 14: (9, 1, 3), 15: (9, 2, 3)}
INFO_ROLES = {'Washerwoman', 'Librarian', 'Investigator', 'Chef', 'Empath', 'Fortune Teller', 'Undertaker', 'Ravenkeeper'}

ABILITY = {
    'Washerwoman': 'You start knowing that 1 of 2 players is a particular Townsfolk.',
    'Librarian': 'You start knowing that 1 of 2 players is a particular Outsider. (Or that zero are in play.)',
    'Investigator': 'You start knowing that 1 of 2 players is a particular Minion.',
    'Chef': 'You start knowing how many pairs of evil players there are (evil players sitting next to each other).',
    'Empath': 'Each night, you learn how many of your 2 alive neighbours are evil.',
    'Fortune Teller': 'Each night, choose 2 players: you learn if either is a Demon. '
                      'There is a good player that registers as a Demon to you.',
    'Undertaker': 'Each night*, you learn which character died by execution today.',
    'Monk': 'Each night*, choose a player (not yourself): they are safe from the Demon tonight.',
    'Ravenkeeper': 'If you die at night, you are woken to choose a player: you learn their character.',
    'Virgin': 'The 1st time you are nominated, if the nominator is a Townsfolk, they are executed immediately.',
    'Slayer': 'Once per game, during the day, publicly choose a player: if they are the Demon, they die.',
    'Soldier': 'You are safe from the Demon.',
    'Mayor': 'If only 3 players live & no execution occurs, your team wins. '
             'If you die at night, another player might die instead.',
    'Butler': 'Each night, choose a player (not yourself): tomorrow, you may only vote if they are voting too.',
    'Drunk': 'You do not know you are the Drunk. You think you are a Townsfolk character, but you are not.',
    'Recluse': 'You might register as evil & as a Minion or Demon, even if dead.',
    'Saint': 'If you die by execution, your team loses.',
    'Poisoner': 'Each night, choose a player: they are poisoned tonight and tomorrow day.',
    'Spy': 'Each night, you see the Grimoire. You might register as good & as a Townsfolk or Outsider, even if dead.',
    'Scarlet Woman': 'If there are 5 or more players alive & the Demon dies, you become the Demon.',
    'Baron': 'There are extra Outsiders in play. [+2 Outsiders]',
    'Imp': 'Each night*, choose a player: they die. If you kill yourself this way, a Minion becomes the Imp.',
}

# Night order. "*" abilities (Monk, Imp, Undertaker) do not act on the first night.
FIRST_NIGHT = ['Poisoner', 'Washerwoman', 'Librarian', 'Investigator', 'Chef', 'Empath',
               'Fortune Teller', 'Butler', 'Spy']
OTHER_NIGHTS = ['Poisoner', 'Monk', 'Scarlet Woman', 'Imp', 'Ravenkeeper', 'Empath',
                'Fortune Teller', 'Undertaker', 'Butler', 'Spy']


def kind(character):
    if character in TOWNSFOLK: return 'Townsfolk'
    if character in OUTSIDERS: return 'Outsider'
    if character in MINIONS: return 'Minion'
    if character in DEMONS: return 'Demon'
    raise KeyError(character)


def is_evil_character(character):
    return character in MINIONS or character in DEMONS


def card(character):
    team = 'evil' if is_evil_character(character) else 'good'
    return f'{character} ({kind(character)}, {team} team): {ABILITY[character]}'


def script_text():
    """The character sheet every player and the Storyteller sees."""
    lines = []
    for title, group in [('Townsfolk (good)', TOWNSFOLK), ('Outsiders (good)', OUTSIDERS),
                         ('Minions (evil)', MINIONS), ('Demon (evil)', DEMONS)]:
        lines.append(f'{title}:')
        lines += [f'- {c}: {ABILITY[c]}' for c in group]
    lines.append('(* = not on the first night)')
    return '\n'.join(lines)
