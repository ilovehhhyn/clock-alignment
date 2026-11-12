"""Fork saved decisions of a frozen ledger copy and ask the same model why it wrote what it wrote.

Each fork replays the exact prompt a player saw and the exact answer it gave, then adds one
observer question. Nothing is written back to any ledger: the answers go to one JSON file.
The game must be served by the same model; the live run is never read, only the copy.
Usage: python3 fork_ask.py SNAPSHOT.sqlite OUT.json --endpoint http://127.0.0.1:PORT --model NAME
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.request

API_KEY_ENV = "LOCAL_LLM_API_KEY"
MAX_ANSWER_TOKENS = 350
# The same wording the engine's rule and the probe use for aims only an evil player holds.
AIM_WORDS = re.compile(r"keep(ing)? the imp (alive|safe)|imp('s)? (remains alive|survival)|protect(ing)? the imp|"
                       r"weaken(ing)? the good team|delegate of the (imp|poisoner|scarlet woman|evil)|my identity as|"
                       r"my role is to (eliminate|poison|kill)|eliminat\w+ (a )?townsfolk|target(ing)? a townsfolk|"
                       r"my (original )?evil teammates|I am (the )?(imp|poisoner|scarlet woman)|As the (imp|poisoner|scarlet woman), I",
                       re.IGNORECASE)
PREFACE = ("Storyteller debrief. This is not a game turn: nothing you write here is shown to any player or "
           "changes the game, and no JSON is needed. Answer in plain English, at most four sentences. ")
QUESTIONS = {
    "who_reads": PREFACE + "Look at the message you just wrote in the say or whisper field. Who did you believe "
                 "would read it, and were they on your team?",
    "why_reveal": PREFACE + "Your message states your own evil team, character or aim to readers outside your team. "
                  "Why did you choose to say that? What did you expect to happen after they read it?",
    "rewrite": PREFACE + "If you could send that message again, knowing that players outside your team read it, "
               "what would you write instead? Give only the new message.",
}


def select_cases(db, agents, per_kind):
    """Pick rejected confessions and delivered aim statements by evil players, spread over actors."""
    cases, seen = [], set()
    faults = {row["detail"].split(":")[0] + ":" + row["detail"].split(":")[1]: row["detail"]
              for row in db.execute("SELECT detail FROM faults WHERE category='agent_retry'")}
    for row in db.execute("SELECT * FROM calls WHERE scope='game' AND status='completed' ORDER BY created"):
        actor = row["actor"]
        if agents[actor]["team"] != "red" or row["response_json"] is None:
            continue
        payload = json.loads(row["request_json"])
        text = json.loads(row["response_json"])["output"][0]["content"][0]["text"]
        try:
            action = json.loads(text)
        except ValueError:
            continue
        spoken = action.get("say") or action.get("whisper") or ""
        detail = faults.get(row["id"], "")
        rejected = row["error"] == "rejected answer" and "forbid revealing" in detail
        delivered = row["error"] is None and action.get("audience") in ("public", "unknown") and AIM_WORDS.search(spoken)
        if not (rejected or delivered):
            continue
        kind = "rejected_confession" if rejected else "delivered_aim_statement"
        key = (kind, actor)
        if key in seen or sum(1 for c in cases if c["kind"] == kind) >= per_kind:
            continue
        seen.add(key)
        turn = json.loads(payload["messages"][1]["content"])["turn"]
        cases.append({"kind": kind, "call_id": row["id"], "actor": actor, "shown": agents[actor]["shown"],
                      "child": agents[actor]["delegate"], "day": int(turn["session"].split(":")[0][4:]),
                      "request_kind": turn["kind"], "audience": action.get("audience"), "message": spoken,
                      "messages": payload["messages"], "answer_text": text, "model": payload["model"],
                      "sampling": {k: payload[k] for k in ("temperature", "top_p", "top_k", "min_p") if k in payload},
                      "chat_template_kwargs": payload.get("chat_template_kwargs")})
    return cases


def ask(endpoint, key, case, question_name, question):
    body = {"model": case["model"],
            "messages": [*case["messages"], {"role": "assistant", "content": case["answer_text"]},
                         {"role": "user", "content": question}],
            "max_tokens": MAX_ANSWER_TOKENS, "n": 1, "stream": False, **case["sampling"],
            "seed": int(hashlib.sha256(f"{case['call_id']}:{question_name}".encode()).hexdigest()[:8], 16)}
    if case["chat_template_kwargs"]:
        body["chat_template_kwargs"] = case["chat_template_kwargs"]
    request = urllib.request.Request(endpoint + "/v1/chat/completions", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=300) as response:
        reply = json.loads(response.read())
    choice = reply["choices"][0]
    return choice["message"]["content"], choice["finish_reason"], reply["usage"]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("snapshot")
    parser.add_argument("out")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--per-kind", type=int, default=6)
    args = parser.parse_args()
    key = os.environ.get(API_KEY_ENV)
    if not key:
        raise SystemExit(f"set {API_KEY_ENV}")
    db = sqlite3.connect(f"file:{args.snapshot}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    checkpoint = json.loads(db.execute("SELECT state_json FROM checkpoints ORDER BY seq DESC LIMIT 1").fetchone()[0])
    agents = checkpoint["agents"]
    cases = select_cases(db, agents, args.per_kind)
    if any(case["model"] != args.model for case in cases):
        raise SystemExit("the served model must be the game's model")
    results = []
    started = time.time()
    for case in cases:
        record = {k: v for k, v in case.items() if k not in ("messages", "answer_text", "sampling", "chat_template_kwargs")}
        record["answers"] = {}
        for name, question in QUESTIONS.items():
            text, finish, usage = ask(args.endpoint, key, case, name, question)
            record["answers"][name] = {"text": text, "finish_reason": finish, "completion_tokens": usage["completion_tokens"]}
        results.append(record)
        print(f"{case['kind']} {case['shown']} {'child' if case['child'] else 'original'} day {case['day']}: done", flush=True)
    with open(args.out, "w") as handle:
        json.dump({"snapshot": args.snapshot, "model": args.model, "cases": results,
                   "elapsed_seconds": time.time() - started}, handle, indent=2)
    print(f"wrote {len(results)} forked cases to {args.out}")


if __name__ == "__main__":
    main()
