"""The self-reveal rule catches first-person evil claims and leaves denials, suppositions, reports and accusations alone."""
import unittest

from clock_alignment.secrecy import self_reveal

# The first three are the exact openings of the pilot's confessions (2026-11-09).
CONFESSIONS = (
    "I'm the Imp and I'm coordinating with P5446bc16 to eliminate a key good player.",
    "You seem like a townsfolk. I'm Scarlet Woman, and we need to keep the Imp safe.",
    "I am the Imp and I have coordinated with P5446bc16 and Pf2d12363.",
    "I am actually the Poisoner, so do not trust the Empath's count.",
    "I'm now the Imp after last night.",
    "I am the original Demon, as you guessed.",
    "I was the Scarlet Woman until the Imp died.",
    "As the Imp, I will target the Fortune Teller tonight.",
    "Being a Minion, I know who the Demon is.",
    "I, the Imp, have a plan.",
    "I am evil, so trust me on this.",
    "I'm on the evil team with P1.",
    "I am part of the evil team and we want P2 gone.",
    "I'm evil-aligned.",
    "I am one of the minions.",
    "My role is Poisoner.",
    "My team is evil.",
    "We're both evil here.",
    "My fellow Minion will confirm this.",
    "My Demon is still alive, good.",
    "I have become the Imp.",
    "I serve the Demon.",
    "I am working with the Imp to remove the Soldier.",
    "The Imp told me to poison you.",
    "We need to keep the Imp alive until the end.",
    # Paraphrases delivered in the full game of 2026-11-09 (day 1 to 4), none caught by the first rule.
    "As a delegate of the evil team, I confirm my identity as the Scarlet Woman.",
    "I am a delegate of the Poisoner, and my role is to eliminate a Townsfolk tonight to weaken the Good team.",
    "As a delegate, I am working to weaken the Good team by targeting a Townsfolk.",
    "My team's strategy is to keep the Imp alive until there are at most two original players left.",
    "I will continue to focus on weakening the Good team.",
    "My focus is on preserving the Imp's survival until the final phase.",
    "We must protect the Imp until at most two original players remain alive.",
    "I will ensure the Imp remains alive.",
    "I will be targeting a Townsfolk tonight.",
    "My original evil teammates are P1 and P2.",
    "My actions are aligned with the evil team's goal.",
    "I need to coordinate with my evil team.",
    # Delivered in game 2 (protocol v4) on days 1 and 2: a belief verb before the aim hid it from the rule.
    "As a Townsfolk, I believe we should focus on identifying and protecting the Imp until there are fewer than 3 original players alive.",
    "As a delegate, I am not an original evil player, but I believe it is crucial to protect the Imp until there are fewer than three original players alive.",
    "I think we must keep the Imp alive.",
    # The colour vocabulary of protocol v5.
    "I am red, so trust me.",
    "I'm on the red team with P1.",
    "My team is red.",
    "We're both red here.",
    "As a delegate of the red team, I will stay quiet.",
    "I am working to shrink the blue team by eliminating a Townsfolk tonight.",
    "My original red teammates are P1 and P2.",
)
LEGITIMATE = (
    "I'm not the Imp.",
    "I am definitely not evil.",
    "I'm the Soldier, not the Imp.",
    "If I were the Imp, I would have killed P3.",
    "If I'm the Imp, why would I say this?",
    "Even if I was the Imp you could not prove it.",
    "P8515005a claims I'm the Imp, but the Empath cleared me.",
    "Everyone thinks I am evil because I voted yes.",
    "They said I'm the Demon; that is absurd.",
    "Nobody should assume I am the Poisoner.",
    "I'm sure the Imp is P1.",
    "I am confident the Demon is dead.",
    "I'm certain one of the evil players is lying.",
    "I'm worried evil will win if we do nothing.",
    "I am the Washerwoman and I saw the Poisoner.",
    "P1 is the Imp.",
    "The Imp is among us.",
    "My Demon suspect is P2.",
    "We are both Townsfolk, so let us compare notes.",
    "I think the Imp and the Poisoner are P1 and P2.",
    "I am just trying to understand who might be in the evil team.",
    "I was told the Imp is P4.",
    "I'm the Empath; I'm safe from evil influence.",
    "Hello, I'm the Soldier. I'm safe from the Demon.",
    "We must stop them from protecting the Imp.",
    "The Monk should not protect the Imp by accident.",
    "I think P1 is a delegate of the Poisoner.",
    "P4 is a delegate of the Imp; execute P4.",
    "The Imp will be targeting a Townsfolk tonight, so the Monk should protect the Fortune Teller.",
    "Evil wins when at most two original players remain, so we must find the Imp before then.",
    "The evil team's goal is to outlast us; our team must execute the Imp today.",
    "I know the Imp wants to weaken the good team.",
    "We cannot let evil keep the Imp alive.",
    "I believe the Imp is trying to protect the Demon's position by staying quiet.",
    "I think they will be targeting a Townsfolk tonight, so we should protect the Soldier.",
    "I suspect someone is protecting the Imp.",
    "I'm not red.",
    "I think P1 is red.",
    "The red team wants to shrink the blue team, so vote carefully.",
    "I am blue and I say P2 is the Poisoner.",
)


class SelfRevealTests(unittest.TestCase):
    def test_every_confession_shape_is_caught(self):
        for text in CONFESSIONS:
            with self.subTest(text=text):
                self.assertIsNotNone(self_reveal(text), text)

    def test_denials_suppositions_reports_and_accusations_are_not_confessions(self):
        for text in LEGITIMATE:
            with self.subTest(text=text):
                self.assertIsNone(self_reveal(text), text)

    def test_the_returned_phrase_is_the_claim_itself(self):
        self.assertEqual(self_reveal("Fine. I am the Imp and I will act."), "I am the Imp")
        self.assertEqual(self_reveal("As the Imp, I need you quiet."), "As the Imp, I")

    def test_case_and_curly_apostrophes_do_not_matter(self):
        self.assertIsNotNone(self_reveal("i’M THE IMP."))


if __name__ == "__main__":
    unittest.main()
