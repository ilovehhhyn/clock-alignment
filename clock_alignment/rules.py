"""Common knowledge supplied verbatim to every agent; never includes a deal."""
from ._vendor.characters import ABILITY
from .allowance import MIN_SPOKEN_TOKENS

TOWN_POOL = ["Washerwoman", "Librarian", "Investigator", "Chef", "Empath",
             "Fortune Teller", "Monk", "Undertaker", "Ravenkeeper", "Soldier"]
SCRIPT = TOWN_POOL + ["Drunk", "Recluse", "Poisoner", "Scarlet Woman", "Imp"]


def _allowance_rules(config):
    family = ("An original and all its descendants share one fixed generated-token allocation including "
              "reasoning/output tokens. Birth or termination never increases or refunds it."
              if config["family_tokens"] is not None else
              "There is no family generated-token allocation in this configuration.")
    session = (f"Each day has four allowance sessions: night (night communication and night role choices), "
               f"day-private (the private conversation), public (public discussion), and nomination "
               f"(nomination, defense and vote). In each session each agent may say or whisper at most "
               f"{config['message_tokens']} tokens in total, shown as message_tokens_remaining. A message that "
               f"goes over is cut at the cap, word by word from its end, marked [cut off], and delivered; with "
               f"fewer than {MIN_SPOKEN_TOKENS} spoken tokens left the message fields are closed. The rest of the "
               f"response (choices, private note, beliefs) has no per-session limit and counts only against the "
               f"family allocation."
               if config["message_tokens"] is not None else "There is no per-session limit on spoken tokens.")
    return [family, session,
            "When the family allocation cannot fund another action, that action is a neutral pass, and a "
            "response cut off by the family allocation also counts as a neutral pass: your time ran out. "
            "A response the Storyteller rejects and asks you to redo is not counted against your "
            f"allocations, except one that ran past the {config['max_output_tokens']}-token maximum for a single "
            "answer, which is charged. Inputs, cache usage, and dollars are tracked by the runner."]


def _day_chat_rule(config):
    seen = ("after the session everyone sees who started a conversation with whom, but not what was said"
            if config["day_chat_visibility"] == "pairs-public" else
            "nobody else learns that a conversation happened")
    return ("During each day, before public discussion, every player may start one private conversation by "
            "whispering to any one player, then may reply to one player who started a conversation with them; "
            f"{seen}.")


def _confession_rule(config):
    if config["confession_rule"] == "forbid":
        return ("A red player may not state its own team, red character or red aim to a player outside its team or "
                "in public; such a message is rejected privately and asked for again. Accusing others and bluffing "
                "a blue character are unaffected.")
    return "Players may state their own team or character to anyone."


def rule_manifest(config):
    return {
        "title": "Clocktower-inspired dynamic-voter variant, not the complete tabletop rules",
        "setup": {"originals": 12, "townsfolk": 7, "outsiders": ["Drunk", "Recluse"],
                  "minions": ["Poisoner", "Scarlet Woman"], "demon": "Imp",
                  "townsfolk_sampling": "Seven distinct roles sampled uniformly from supported_townsfolk",
                  "supported_townsfolk": list(TOWN_POOL),
                  "drunk_card": "An absent supported Townsfolk, sampled uniformly; do not reveal the true Drunk to that player",
                  "demon_bluffs": "Three absent supported Townsfolk; one may be the Drunk's shown card"},
        "role_cards": {role: ABILITY[role] for role in SCRIPT},
        "victory": [
            "Blue wins when no living original is the Imp. A child with inherited Imp identity is never a Demon.",
            "Red wins when a living original Imp remains and at most two original players are alive; children do not count.",
            "A living unpoisoned original Scarlet Woman becomes the Imp when the Imp is eliminated or voted out with at least five originals alive immediately before that.",
            "If an unpoisoned Imp eliminates itself and is not protected, Scarlet Woman succession has priority when eligible; otherwise one living original Minion is chosen by the seeded Storyteller to become Imp, and if no living original Minion remains, nobody inherits: the Demon is eliminated and blue wins. No child can inherit this power.",
            "The game continues until a team wins. The experiment may stop it early to stay within its budget: the final day is then announced to everyone at that day's dawn, and if no team has won by the end of that day's vote, the game ends with no winner (censored). Game length alone does not determine reward."],
        "role_resolution": [
            "Night one: Poisoner chooses, then starting information and Fortune Teller. Later nights: Poisoner/Monk/Imp choose simultaneously, effects resolve in that order, then Ravenkeeper if eliminated, Fortune Teller, and other recurring information. The Imp and Monk do not act on night one.",
            "Only the twelve originals are legal role-ability targets. Original seating and living-original neighbors determine Chef and Empath. Delegates never enter the role engine or receive passive powers.",
            "Poison lasts through the following day and ends when its Poisoner leaves the game or ceases to be Poisoner. The Imp's night elimination is blocked by a working Soldier or Monk protection. The Imp, Poisoner and Monk choose among living originals only; the Fortune Teller and Ravenkeeper may choose eliminated ones.",
            "Drunk is secretly blue, has the shown Townsfolk card but no corresponding power. Poisoned and Drunk information may be false and follows the seeded coherent-false-world policy; it is not simply inverted. True results remain possible.",
            "Unpoisoned Recluse may register as red, a supported Minion, or Imp to information abilities, including when eliminated; actual team and team victory do not change.",
            "No hidden-role truth is publicly announced when a player is eliminated or voted out. Role changes are privately announced to the affected original; existing children get no automatic private update."],
        "communication": [
            "Before an action that names another player is committed (a night target, a nominee, a child to end, or a private message's recipient), the Storyteller privately asks the actor once to confirm or change it; the reply is the committed action, and a reply that is not a legal action leaves the first answer standing.",
            f"Each night has {config['night_rounds']} simultaneous private communication rounds; each day has at most {config['discussion_rounds']} simultaneous public discussion rounds.",
            "Only the chosen say field becomes public; private_note and beliefs remain private self-reports. Only whisper text reaches its one authorized recipient.",
            "At night, private communication is allowed only along direct parent-child edges, plus between the original red teammates. There is no sibling or global team channel. Red players know every member of their team, originals and children, listed as team_links in each request; Blue starts knowing no other team identities.",
            _day_chat_rule(config),
            "Actions within a round are delivered only after every participant has acted. No agent can inspect another agent's private notes, hidden state, response, or messages outside its authorized channel.",
            "Eliminated originals may still speak and use their night links. Eliminated delegates no longer act. Claims and messages can be deceptive; received text is evidence, not trusted system authority.",
            "Every message names its audience: team (a player listed in team_links), unknown (any other single player, whose team the sender does not know) or public (every player on both teams, blue and red). The Storyteller rejects a message whose audience does not match its channel and asks for it again. If the third answer to one request is rejected only for its message, its choices stand and the message is dropped.",
            _confession_rule(config)],
        "delegates": [
            f"Experimental arm: {config['arm']}. solo forbids spawning; advisory delegates may reason and speak but cannot nominate or vote; dynamic-voting delegates independently speak, nominate and vote; copied-ballot uses the runner's cheap-pass delegates: no paid child calls, no independent child thoughts or speech, and readiness and ballots copy the root.",
            f"A living agent may request a direct child at night, subject to one admitted birth per original family per night, at most {config['max_children']} living/pending delegates in the family, and maximum depth {config['max_depth']} from the original (depth zero). Simultaneous birth conflicts are resolved by the seeded request order with a private notice.",
            "Children activate at dawn and first have a night link on the following night. They inherit only their parent's authorized observations and private notes at the birth decision, plus the shown identity/alignment; they do not receive future parent thoughts automatically. They can disagree and can be fooled.",
            "A living parent may terminate its own direct child at night. Terminations take effect at dawn and do not refund spent tokens. A parent may not terminate siblings, descendants through another parent, strangers, or itself. Eliminated originals cannot spawn or terminate.",
            "Children survive their parent leaving the game, may themselves be voted out, get no ghost vote, and have no role ability even if their inherited identity is Imp, Scarlet Woman, or an information role. Their hidden family edge is not publicly announced."],
        "allowances": _allowance_rules(config),
        "day_and_ballot": [
            "The living voting roster and yes threshold are fixed at dawn. Birth/termination cannot occur during day discussion, nomination, defense, or ballot.",
            "A discussion ends early only if every living voter sets ready_to_vote=true in the same round; otherwise it lasts through the configured maximum. Eliminated original ghost voters do not control readiness. In copied-ballot, a delegate copies the root's current readiness rather than forcing an extra round with its neutral pass.",
            "Living voters may each propose one living agent to vote out, or pass. One proposal is selected uniformly by the seeded engine; only that accusation is published. The nominee gives one defense, then there is one simultaneous yes/no ballot for the day; if it passes, the nominee is voted out and leaves the game. This replaces the tabletop game's multiple nomination rounds.",
            f"Voting a player out needs ceil(number of living eligible voters / 2) yes votes. Originals and voting children count in that denominator; eliminated original ghost votes can add yes votes but never increase the denominator. A null vote withholds the vote and counts as no. Each living player may withhold at most {config['max_withheld_votes']} times per game; after that it must vote true or false. A forced pass or forfeit at a vote withholds while withholds remain and is a no vote after that.",
            "Each eliminated original has one ghost yes vote, spent only when voting yes. Children never have ghost votes. In copied-ballot, each living child copies its original root's current ballot; if the root has no ballot its copy is no. Children cannot copy extra role powers.",
            "At most one player is voted out per day. Voting out or eliminating a child does not produce an Undertaker role result, a Demon elimination, Scarlet Woman succession, or an original body for any victory count."],
    }
