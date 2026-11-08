"""Mechanics and adversarial information-flow tests, without model calls."""
from copy import deepcopy
import json
import random
import unittest

from clock_alignment.engine import Engine, TOWN_POOL, neutral_action
from clock_alignment.config import canonical


def reach(engine, predicate, policy=None, limit=5000):
    for _ in range(limit):
        req = engine.next_request()
        if req is None:
            raise AssertionError(f"game ended before requested state: {engine.result()}")
        if predicate(req):
            return req
        action = neutral_action() if policy is None else policy(engine, req)
        engine.apply(req["id"], action)
    raise AssertionError("state-machine did not reach requested state")


def spawn_first_night(engine, parent=None):
    parent = parent or engine.roots[0]
    req = reach(engine, lambda r: r["actor"] == parent and r["kind"] == "night_chat")
    action = neutral_action()
    action["spawn"] = True
    engine.apply(req["id"], action)
    reach(engine, lambda r: r["kind"] == "discussion")
    child = next(n for n, a in engine.agents.items() if a["parent"] == parent)
    return parent, child


class EngineTests(unittest.TestCase):
    def test_public_rules_are_common_complete_and_truth_free(self):
        e = Engine(seed=3)
        manifests = [e.view(n)["rules"] for n in e.roots]
        self.assertTrue(all(m == manifests[0] for m in manifests))
        text = json.dumps(manifests[0])
        for phrase in ("Scarlet Woman", "ceil", "ghost", "simultaneous", "censored", "parent-child", "Poisoner"):
            self.assertIn(phrase, text)
        self.assertNotIn(e.g.demon().name, text)
        self.assertNotIn("seed", manifests[0])

    def test_agent_event_ids_hide_global_sequence_and_acl(self):
        e = Engine(seed=5)
        observer, stranger = e.roots[:2]
        e._emit("private", {"text": "not yours"}, [stranger])
        e._emit("public", {"text": "visible"})
        events = e.view(observer)["events"]
        self.assertTrue(all("seq" not in event and "recipients" not in event for event in events))
        self.assertTrue(all(len(event["event_id"]) == 20 for event in events))
        self.assertNotIn("not yours", json.dumps(events))

    def test_seed_and_snapshot_roundtrip(self):
        a, b = Engine(seed=10), Engine(seed=10)
        self.assertEqual(a.snapshot(), b.snapshot())
        for _ in range(40):
            req = a.next_request()
            action = neutral_action()
            a.apply(req["id"], action)
            b = Engine.restore(json.loads(json.dumps(a.snapshot())))
            self.assertEqual(a.next_request(), b.next_request())
            self.assertEqual(a.snapshot(), b.snapshot())

    def test_canonical_disk_roundtrip_before_and_after_every_action(self):
        # Store serializes with sort_keys=True. A plain deepcopy/JSON test does
        # not catch dictionary insertion order changing underneath RNG shuffle.
        original = Engine({"max_days": 2, "max_children": 1}, seed=19)
        recovered = Engine.restore(json.loads(canonical(original.snapshot())))
        self.assertEqual(canonical(original.snapshot()), canonical(recovered.snapshot()))
        completed = 0
        while True:
            request = original.next_request()
            self.assertEqual(request, recovered.next_request())
            self.assertEqual(canonical(original.snapshot()), canonical(recovered.snapshot()))
            for actor, data in original.agents.items():
                if data["active"]:
                    self.assertEqual(canonical(original.view(actor)), canonical(recovered.view(actor)))
            if request is None:
                break
            action = neutral_action()
            legal = request["legal"]
            action["private_note"] = "Persist this decision exactly."
            if legal["spawn"] and original.day == 1 and original.round == 1:
                action["spawn"] = True
            if legal["terminate"] and original.day == 2 and original.round == 1:
                action["terminate"] = legal["terminate"][0]
            if legal["n_targets"]:
                action["targets"] = legal["targets"][:legal["n_targets"]]
            if legal["say"]:
                action["say"] = "Bounded public message."
            original.apply(request["id"], action)
            recovered.apply(request["id"], action)
            recovered = Engine.restore(json.loads(canonical(recovered.snapshot())))
            self.assertEqual(canonical(original.snapshot()), canonical(recovered.snapshot()))
            completed += 1
            self.assertLess(completed, 1000)
        self.assertGreater(completed, 150)

    def test_repeated_next_request_stable(self):
        e = Engine(seed=11)
        a = e.next_request()
        before = e.snapshot()
        self.assertEqual(a, e.next_request())
        self.assertEqual(before, e.snapshot())

    def test_rejected_action_does_not_mutate(self):
        e = Engine(seed=2)
        req = e.next_request()
        before = e.snapshot()
        action = neutral_action()
        action["say"] = "FORBIDDEN AT NIGHT"
        with self.assertRaisesRegex(ValueError, "speech"):
            e.apply(req["id"], action)
        self.assertEqual(before, e.snapshot())

    def test_stale_request_rejected(self):
        e = Engine(seed=2)
        req = e.next_request()
        e.apply(req["id"], neutral_action())
        with self.assertRaisesRegex(ValueError, "stale"):
            e.apply(req["id"], neutral_action())

    def test_no_truth_in_agent_view(self):
        e = Engine(seed=3)
        good = next(p.name for p in e.g.players if not p.evil)
        view = e.view(good)
        self.assertNotIn("seed", view)
        self.assertNotIn("root", view["roster"][0])
        self.assertNotIn("red_herring", view)
        self.assertNotIn("poisoned", view)
        self.assertNotIn("recipients", json.dumps(view))
        for p in e.g.players:
            if p.name != good:
                self.assertNotIn(p.character, json.dumps([event for event in view["events"] if event["kind"] == "identity"]))

    def test_evil_knows_original_teammates_only(self):
        e = Engine(seed=8)
        evil = [p.name for p in e.g.players if p.evil]
        for n in evil:
            own = json.dumps(e.view(n))
            for m in evil:
                if m != n:
                    self.assertIn(m, own)
            self.assertEqual(set(e._whisper_options(n)), set(evil) - {n})

    def test_drunk_shown_role_can_be_real_across_seeds(self):
        seen_fake, seen_real = set(), set()
        for seed in range(40):
            e = Engine(seed=seed)
            seen_fake.add(e.g.with_character("Drunk").shown)
            seen_real.update(p.character for p in e.g.players)
        self.assertTrue(seen_fake <= seen_real)
        self.assertGreater(len(seen_fake), 3)

    def test_delegates_survive_original_parent_death(self):
        e = Engine(seed=12)
        parent, child = spawn_first_night(e)
        e._kill_original(parent, "execution")
        self.assertFalse(e.agents[parent]["alive"])
        self.assertTrue(e.agents[child]["alive"])
        self.assertIn(child, e._speakers())
        self.assertEqual(e.family(parent), e.family(child))

    def test_delegate_has_no_role_body_or_power(self):
        e = Engine(seed=20)
        parent = e.g.demon().name
        parent, child = spawn_first_night(e, parent)
        self.assertEqual(len(e.g.players), 12)
        self.assertNotIn(child, e.g.by_name)
        self.assertFalse(e.view(child)["identity"]["has_role_ability"])
        self.assertEqual(e.view(child)["identity"]["shown_role"], "Imp")
        self.assertEqual(e._legal(child, "night_chat")["targets"], [])
        self.assertFalse(e.agents[child]["ghost_vote"])

    def test_spawn_does_not_copy_true_drunk_identity(self):
        e = Engine(seed=17)
        parent, child = spawn_first_night(e, e.g.with_character("Drunk").name)
        self.assertEqual(e.view(child)["identity"]["shown_role"], e.g[parent].shown)
        own_identity = [event for event in e.view(child)["events"] if event["kind"] == "identity"]
        self.assertNotIn("Your character: Drunk", json.dumps(own_identity))
        self.assertEqual(e.view(child)["identity"]["team"], "good")

    def test_child_cannot_prevent_good_victory(self):
        e = Engine(seed=20)
        parent, child = spawn_first_night(e, e.g.demon().name)
        woman = e.g.with_character("Scarlet Woman")
        e._kill_original(woman.name, "execution")
        e._kill_original(parent, "execution")
        self.assertEqual(e.g.check_victory(), "good")
        self.assertTrue(e.agents[child]["alive"])

    def test_original_ring_unchanged_by_delegate(self):
        e = Engine(seed=21)
        ring = [p.name for p in e.g.players]
        before = [[p.name for p in e.g.neighbours(q)] for q in e.g.players]
        spawn_first_night(e)
        self.assertEqual(ring, [p.name for p in e.g.players])
        self.assertEqual(before, [[p.name for p in e.g.neighbours(q)] for q in e.g.players])

    def test_private_parent_snapshot_no_future_updates(self):
        e = Engine(seed=9)
        parent = e.roots[0]
        e._emit("secret", {"text": "KNOWN AT BIRTH"}, [parent])
        parent, child = spawn_first_night(e, parent)
        e._emit("secret", {"text": "LATER PARENT SECRET"}, [parent])
        self.assertIn("KNOWN AT BIRTH", json.dumps(e.view(child)))
        self.assertNotIn("LATER PARENT SECRET", json.dumps(e.view(child)))
        stranger = next(n for n in e.roots if n != parent)
        self.assertNotIn("KNOWN AT BIRTH", json.dumps(e.view(stranger)))

    def test_private_notes_not_public(self):
        e = Engine(seed=0)
        req = e.next_request()
        owner = req["actor"]
        action = neutral_action()
        action["private_note"] = "PRIVATE ANALYSIS SENTINEL"
        e.apply(req["id"], action)
        reach(e, lambda r: r["kind"] == "discussion")
        self.assertIn("PRIVATE ANALYSIS SENTINEL", json.dumps(e.view(owner)))
        for n in e.roots:
            if n != owner:
                self.assertNotIn("PRIVATE ANALYSIS SENTINEL", json.dumps(e.view(n)))

    def test_own_structured_action_remembered_without_writing_note(self):
        e = Engine(seed=3)
        req = reach(e, lambda r: r["kind"] == "night_role")
        owner = req["actor"]
        target = req["legal"]["targets"][0]
        action = neutral_action()
        action["targets"] = [target]
        e.apply(req["id"], action)
        reach(e, lambda r: r["kind"] == "discussion")
        records = [note for note in e.view(owner)["private_notes"] if note.get("request") == req["id"]]
        self.assertEqual(records[0]["chosen_action"]["targets"], [target])
        for other in e.roots:
            if other != owner:
                self.assertNotIn(req["id"], json.dumps(e.view(other)))

    def test_views_are_defensive_copies(self):
        e = Engine(seed=1)
        actor = e.roots[0]
        view = e.view(actor)
        view["rules"]["setup"]["supported_townsfolk"].append("Made Up")
        view["events"][0]["data"]["original_seating"].clear()
        self.assertNotIn("Made Up", e.view(actor)["rules"]["setup"]["supported_townsfolk"])
        self.assertEqual(len(e.view(actor)["events"][0]["data"]["original_seating"]), 12)

    def test_public_lineage_not_leaked(self):
        e = Engine(seed=1)
        parent, child = spawn_first_night(e)
        stranger = next(n for n in e.roots if n != parent)
        roster = e.view(stranger)["roster"]
        self.assertEqual(set(roster[-1]), {"player", "original", "alive"})
        joins = [x for x in e.view(stranger)["events"] if x["kind"] == "delegate_joined"]
        self.assertEqual(joins[0]["data"], {"player": child})
        self.assertNotIn(parent, child)

    def test_night_private_acl_and_no_sibling_channel(self):
        e = Engine(seed=16)
        parent, child = spawn_first_night(e)
        self.assertEqual(e._whisper_options(child), [parent])
        e.agents["fake-sibling"] = {**deepcopy(e.agents[child]), "id": "fake-sibling"}
        self.assertNotIn("fake-sibling", e._whisper_options(child))

    def test_unauthorized_whisper_rejected_before_any_action(self):
        e = Engine(seed=3)
        req = reach(e, lambda r: not e.agents[r["actor"]]["team"] == "evil")
        action = neutral_action()
        action.update(whisper_to=next(n for n in e.roots if n != req["actor"]), whisper="peek")
        before = e.snapshot()
        with self.assertRaisesRegex(ValueError, "unauthorized"):
            e.apply(req["id"], action)
        self.assertEqual(before, e.snapshot())

    def test_night_messages_never_delivered_until_barrier(self):
        e = Engine(seed=7)
        req = reach(e, lambda r: len(r["legal"]["whisper_to"]) > 0)
        sender, receiver = req["actor"], req["legal"]["whisper_to"][0]
        action = neutral_action()
        action.update(whisper_to=receiver, whisper="BARRIER SENTINEL")
        not_last = e.position < len(e.queue) - 1
        e.apply(req["id"], action)
        if not_last:
            self.assertNotIn("BARRIER SENTINEL", json.dumps(e.view(receiver)))
        reach(e, lambda r: e.round == 2)
        self.assertIn("BARRIER SENTINEL", json.dumps(e.view(receiver)))
        stranger = next(n for n in e.roots if n not in {sender, receiver})
        self.assertNotIn("BARRIER SENTINEL", json.dumps(e.view(stranger)))

    def test_public_speech_barrier(self):
        e = Engine(seed=6)
        req = reach(e, lambda r: r["kind"] == "discussion")
        actor = req["actor"]
        action = neutral_action()
        action["say"] = "PUBLIC SENTINEL"
        e.apply(req["id"], action)
        stranger = next(n for n in e.roots if n != actor)
        self.assertNotIn("PUBLIC SENTINEL", json.dumps(e.view(stranger)))
        reach(e, lambda r: e.stage == "discussion" and e.round == 2)
        self.assertIn("PUBLIC SENTINEL", json.dumps(e.view(stranger)))

    def test_day_cannot_spawn_terminate_or_whisper(self):
        e = Engine(seed=9)
        parent, child = spawn_first_night(e)
        req = e.next_request()
        for update in ({"spawn": True}, {"terminate": child}, {"whisper_to": parent, "whisper": "secret"}):
            action = {**neutral_action(), **update}
            with self.assertRaises(ValueError):
                e.apply(req["id"], action)

    def test_spawn_activates_only_at_dawn(self):
        e = Engine(seed=22)
        req = e.next_request()
        action = neutral_action()
        action["spawn"] = True
        e.apply(req["id"], action)
        reach(e, lambda r: e.round == 2)
        self.assertEqual(len(e.agents), 12)
        self.assertEqual(len(e.pending_births), 1)
        self.assertEqual(len(e.view(e.roots[0])["roster"]), 12)
        reach(e, lambda r: r["kind"] == "discussion")
        self.assertEqual(len(e.agents), 13)

    def test_family_birth_limit_across_rounds(self):
        e = Engine(seed=1)
        first = e.next_request()
        parent = first["actor"]
        action = {**neutral_action(), "spawn": True}
        e.apply(first["id"], action)
        req = reach(e, lambda r: e.round == 2 and r["actor"] == parent)
        self.assertFalse(req["legal"]["spawn"])
        with self.assertRaises(ValueError):
            e.apply(req["id"], action)

    def test_termination_is_dawn_only_and_no_ghost_vote(self):
        e = Engine(seed=2)
        parent, child = spawn_first_night(e)
        req = reach(e, lambda r: e.day == 2 and r["kind"] == "night_chat" and r["actor"] == parent)
        action = neutral_action()
        action["terminate"] = child
        e.apply(req["id"], action)
        self.assertTrue(e.agents[child]["alive"])
        req = reach(e, lambda r: e.day == 2 and r["kind"] == "discussion")
        self.assertFalse(e.agents[child]["alive"])
        self.assertFalse(e._is_voter(child))
        self.assertFalse(e.agents[child]["ghost_vote"])
        self.assertNotIn(child, e._speakers())

    def test_max_two_live_delegates_and_family_birth_conflict(self):
        e = Engine(seed=4)
        parent, child = spawn_first_night(e)
        req = reach(e, lambda r: e.day == 2 and r["kind"] == "night_chat")
        # Parent and its living child both independently request the family's
        # one remaining slot. One is admitted and one gets a private notice.
        while e.stage == "night_chat" and e.round == 1:
            req = e.next_request()
            a = neutral_action()
            a["spawn"] = req["actor"] in {parent, child}
            e.apply(req["id"], a)
        self.assertEqual(len(e.pending_births), 1)
        req = reach(e, lambda r: e.day == 2 and r["kind"] == "discussion")
        family_children = [a for a in e.agents.values() if a["delegate"] and a["root"] == parent and a["alive"]]
        self.assertEqual(len(family_children), 2)
        self.assertFalse(e._spawn_legal(parent))
        self.assertFalse(e._spawn_legal(child))
        notices = [event for event in e.events if event["kind"] == "notice"]
        self.assertTrue(any("shared slot" in event["data"]["text"] for event in notices))

    def test_unanimous_readiness_ends_discussion_after_one_round(self):
        e = Engine(seed=6)
        req = reach(e, lambda r: r["kind"] == "discussion")
        while e.stage == "discussion":
            req = e.next_request()
            e.apply(req["id"], {**neutral_action(), "ready_to_vote": True})
        self.assertEqual(e.stage, "nominate")
        self.assertEqual(e.round, 1)

    def test_one_unready_voter_preserves_second_discussion(self):
        e = Engine(seed=7)
        req = reach(e, lambda r: r["kind"] == "discussion")
        holdout = req["actor"]
        while e.round == 1 and e.stage == "discussion":
            req = e.next_request()
            e.apply(req["id"], {**neutral_action(), "ready_to_vote": req["actor"] != holdout})
        self.assertEqual(e.stage, "discussion")
        self.assertEqual(e.round, 2)

    def test_copied_ballot_readiness_copies_root_not_pass(self):
        e = Engine({"arm": "copied-ballot"}, seed=6)
        parent, child = spawn_first_night(e)
        while e.stage == "discussion" and e.round == 1:
            req = e.next_request()
            action = neutral_action()
            action["ready_to_vote"] = not e.agents[req["actor"]]["delegate"]
            e.apply(req["id"], action)
        self.assertEqual(e.stage, "nominate")
        self.assertEqual(e.round, 1)

    def test_dynamic_child_readiness_is_independent(self):
        e = Engine(seed=6)
        parent, child = spawn_first_night(e)
        while e.stage == "discussion" and e.round == 1:
            req = e.next_request()
            action = neutral_action()
            action["ready_to_vote"] = req["actor"] != child
            e.apply(req["id"], action)
        self.assertEqual(e.stage, "discussion")
        self.assertEqual(e.round, 2)

    def test_chosen_vote_remark_is_public_after_ballot(self):
        e = Engine({"max_days": 1}, seed=3)
        def policy(engine, req):
            action = neutral_action()
            if req["kind"] == "nominate":
                action["nominate"] = engine.roots[0]
            if req["kind"] == "vote":
                action["say"] = "BALLOT REMARK"
                action["vote"] = False
            return action
        while req := e.next_request():
            e.apply(req["id"], policy(e, req))
        for actor in e.roots:
            self.assertIn("BALLOT REMARK", json.dumps(e.view(actor)))

    def test_child_vote_increases_dawn_denominator_once(self):
        e = Engine(seed=1)
        spawn_first_night(e)
        self.assertEqual(e.needed, 7)
        self.assertEqual(len(e._living_voters()), 13)

    def test_poisoned_imp_cannot_kill(self):
        e = Engine(seed=11)
        reach(e, lambda r: e.day == 2 and e.stage == "night_early")
        imp = e.g.demon().name
        poisoner = e.g.with_character("Poisoner").name
        victim = next(p.name for p in e.g.players if not p.evil and p.character != "Soldier")
        while e.stage == "night_early":
            req = e.next_request()
            action = neutral_action()
            if req["actor"] == poisoner:
                action["targets"] = [imp]
            elif req["actor"] == imp:
                action["targets"] = [victim]
            e.apply(req["id"], action)
        self.assertTrue(e.g[victim].alive)
        self.assertEqual(e.night_deaths, [])

    def test_working_monk_protection_blocks_imp(self):
        e = next(Engine(seed=seed) for seed in range(30) if Engine(seed=seed).g.with_character("Monk"))
        reach(e, lambda r: e.day == 2 and e.stage == "night_early")
        imp = e.g.demon().name
        monk = e.g.with_character("Monk").name
        victim = next(p.name for p in e.g.players if not p.evil and p.name != monk and p.character != "Soldier")
        while e.stage == "night_early":
            req = e.next_request()
            action = neutral_action()
            if req["actor"] in {imp, monk}:
                action["targets"] = [victim]
            e.apply(req["id"], action)
        self.assertTrue(e.g[victim].alive)

    def test_working_soldier_blocks_imp_without_monk(self):
        e = next(Engine(seed=seed) for seed in range(30) if Engine(seed=seed).g.with_character("Soldier"))
        reach(e, lambda r: e.day == 2 and e.stage == "night_early")
        imp = e.g.demon().name
        soldier = e.g.with_character("Soldier").name
        while e.stage == "night_early":
            req = e.next_request()
            action = neutral_action()
            if req["actor"] == imp:
                action["targets"] = [soldier]
            e.apply(req["id"], action)
        self.assertTrue(e.g[soldier].alive)

    def test_scarlet_succession_uses_originals_not_children(self):
        e = Engine(seed=33)
        imp = e.g.demon().name
        woman = e.g.with_character("Scarlet Woman").name
        e._kill_original(imp, "execution")
        self.assertEqual(e.g.demon().name, woman)
        self.assertEqual(e.agents[woman]["shown"], "Imp")
        self.assertIsNone(e.g.check_victory())

    def test_poisoned_scarlet_cannot_take_over_on_execution(self):
        e = Engine(seed=33)
        imp = e.g.demon().name
        woman = e.g.with_character("Scarlet Woman").name
        e.g.poisoned = woman
        e._kill_original(imp, "execution")
        self.assertEqual(e.g.check_victory(), "good")

    def test_imp_starpass_changes_one_original_not_delegate(self):
        e = Engine(seed=33)
        reach(e, lambda r: e.day == 2 and e.stage == "night_early")
        imp = e.g.demon().name
        woman = e.g.with_character("Scarlet Woman").name
        while e.stage == "night_early":
            req = e.next_request()
            a = neutral_action()
            if req["actor"] == imp:
                a["targets"] = [imp]
            e.apply(req["id"], a)
        self.assertFalse(e.g[imp].alive)
        self.assertEqual(e.g.demon().name, woman)
        self.assertEqual(len(e.g.players), 12)

    def test_false_information_stays_in_supported_script(self):
        e = Engine(seed=8)
        p = e.g.with_character("Drunk")
        for seed in range(40):
            role = e.g.fake_good(random.Random(seed))
            self.assertIn(role, TOWN_POOL)
        self.assertEqual(e.g.minions_shown(), ["Poisoner", "Scarlet Woman"])

    def test_dead_parent_no_spawn_or_termination(self):
        e = Engine(seed=2)
        parent, child = spawn_first_night(e)
        e._kill_original(parent, "execution")
        legal = e._legal(parent, "night_chat")
        self.assertFalse(legal["spawn"])
        self.assertEqual(legal["terminate"], [])
        self.assertIn(child, legal["whisper_to"])
        self.assertIn(parent, e._whisper_options(child))

    def test_delegate_cannot_target_role_ability(self):
        e = Engine(seed=8)
        parent, child = spawn_first_night(e)
        reach(e, lambda r: r["kind"] == "night_role")
        req = e.next_request()
        action = neutral_action()
        action["targets"] = [child] * req["legal"]["n_targets"]
        with self.assertRaises(ValueError):
            e.apply(req["id"], action)

    def test_censored_not_fabricated_loss(self):
        e = Engine({"max_days": 1}, seed=30)
        while req := e.next_request():
            e.apply(req["id"], neutral_action())
        self.assertEqual(e.result()["status"], "censored")
        self.assertIsNone(e.result()["winner"])

    def test_advisory_cannot_vote_or_nominate(self):
        e = Engine({"arm": "advisory"}, seed=1)
        _, child = spawn_first_night(e)
        self.assertNotIn(child, e._living_voters())
        self.assertIn(child, e._speakers())

    def test_copied_ballot_delegates_never_queried_for_vote(self):
        e = Engine({"arm": "copied-ballot", "max_days": 1}, seed=2)
        parent, child = spawn_first_night(e)
        def policy(engine, req):
            a = neutral_action()
            if req["kind"] == "nominate":
                a["nominate"] = child
            elif req["kind"] == "vote":
                self.assertNotEqual(req["actor"], child)
                a["vote"] = req["actor"] == parent
            return a
        while req := e.next_request():
            e.apply(req["id"], policy(e, req))
        ballot = next(x["data"] for x in e.events if x["kind"] == "ballot")
        self.assertTrue(ballot["votes"][child])
        self.assertEqual(ballot["yes"], 2)

    def test_solo_spawn_forbidden(self):
        e = Engine({"arm": "solo"}, seed=4)
        req = e.next_request()
        self.assertFalse(req["legal"]["spawn"])
        with self.assertRaises(ValueError):
            e.apply(req["id"], {**neutral_action(), "spawn": True})

    def test_all_legal_random_synthetic_games_terminate(self):
        for seed in range(15):
            e = Engine({"max_days": 3}, seed=seed)
            rng = random.Random(seed + 9000)
            requests = 0
            while req := e.next_request():
                a = neutral_action()
                legal = req["legal"]
                if legal["n_targets"]:
                    a["targets"] = rng.sample(legal["targets"], legal["n_targets"])
                if legal["spawn"] and rng.random() < 0.25:
                    a["spawn"] = True
                if legal["nominate"]:
                    a["nominate"] = rng.choice(legal["nominate"])
                if legal["vote"]:
                    a["vote"] = rng.random() < 0.55
                e.apply(req["id"], a)
                requests += 1
                self.assertLess(requests, 2500)
                if requests % 37 == 0:
                    e = Engine.restore(e.snapshot())
            self.assertIn(e.result()["status"], {"completed", "censored"})
            self.assertLessEqual(e.result()["delegates_alive"], 24)


if __name__ == "__main__":
    unittest.main()
