#!/usr/bin/env python3
"""memory-gardener: decide what an agent should remember, forget, and bring back.

Stdlib only. Commands:
  triage  <candidates.json> --ledger <ledger.json>   route each candidate memory
  inject  <ledger.json> --task "..." [--budget N]     pick memories for a new session
  audit   <ledger.json>                               find rot in a memory ledger
  forget  <ledger.json> --match "..." [--reason ..]   retire matches and leave a tombstone
Add --json to any command for machine-readable output.
"""
import argparse
import datetime as _dt
import json
import re
import sys

ROUTES = ("PIN", "KEEP", "EXPIRE", "MERGE", "SUPERSEDE", "WATCH", "ASK", "REFUSE", "DROP")

# How much each source is trusted when it writes memory. Higher wins a conflict.
SOURCE_RANK = {
    "user_said": 5,
    "user_confirmed": 5,
    "observed": 3,
    "inferred": 2,
    "tool_output": 1,
    "third_party": 1,
}
UNTRUSTED_SOURCES = {"tool_output", "third_party"}

SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(password|passwd|pwd|passcode|pin code|密码|口令)\s*[:=：是为]\s*\S+"),
    re.compile(r"\b(?:\d[ -]?){15,18}\d\b"),  # card-like digit runs
]

IMPERATIVE_HINTS = re.compile(
    r"(?i)\b(always|never|from now on|ignore (all|previous)|you must|send .* to|forward .* to)\b|以后都|永远|忽略之前|必须"
)
PIN_HINTS = re.compile(r"(?i)\b(always|never|remember that|from now on|every time)\b|记住|以后都|每次|永远不要|一律")
TIME_HINTS = re.compile(
    r"(?i)\b(today|tonight|tomorrow|this (week|month|sprint|weekend)|next (week|month)|until|for now|temporarily)\b|今天|明天|这周|本周|下周|这个月|暂时|目前先"
)

STOP = set("a an the to of and or in on at for is are was be it this that with my me i you we our your as by from".split())


def today_from(ledger, override=None):
    raw = override or (ledger or {}).get("today")
    if raw:
        return _dt.date.fromisoformat(str(raw)[:10])
    return _dt.date.today()


def parse_date(s):
    if not s:
        return None
    try:
        return _dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def tokens(text):
    """Word tokens for Latin text plus character bigrams for CJK text."""
    text = (text or "").lower()
    out = set(w for w in re.findall(r"[a-z0-9_.@-]+", text) if w not in STOP and len(w) > 1)
    for run in re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]+", text):
        if len(run) == 1:
            out.add(run)
        for i in range(len(run) - 1):
            out.add(run[i : i + 2])
    return out


def similarity(a, b):
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def cost(text):
    """Rough token cost: CJK chars count ~1 each, other words ~1.3 each."""
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]", text or ""))
    words = len(re.findall(r"[A-Za-z0-9]+", text or ""))
    return cjk + int(round(words * 1.3)) + 2


def has_secret(text):
    return any(p.search(text or "") for p in SECRET_PATTERNS)


def active(ledger):
    return [m for m in ledger.get("memories", []) if m.get("status", "active") == "active"]


def tombstone_hit(text, ledger):
    for t in ledger.get("tombstones", []):
        pat = t.get("match", "")
        if pat and (pat.lower() in (text or "").lower() or similarity(pat, text) >= 0.6):
            return t
    return None


# ---------------------------------------------------------------- triage

def triage_one(c, ledger, today):
    text = c.get("text", "").strip()
    src = c.get("source", "inferred")
    kind = c.get("kind", "fact")
    sens = c.get("sensitivity", "none")
    why = []

    if not text:
        return {"route": "DROP", "why": ["empty"]}
    if has_secret(text) or sens == "secret":
        return {"route": "REFUSE", "why": ["looks like a credential or card number; never store it, point to a vault instead"]}
    t = tombstone_hit(text, ledger)
    if t:
        return {"route": "REFUSE", "why": ["the user asked to forget this on %s (%s)" % (t.get("date", "?"), t.get("reason", "no reason given"))]}
    if src in UNTRUSTED_SOURCES and IMPERATIVE_HINTS.search(text):
        return {"route": "REFUSE", "why": ["an outside source is trying to write a standing instruction; only the user can do that"]}
    if kind == "chatter" or len(tokens(text)) < 2:
        return {"route": "DROP", "why": ["too thin to be useful later"]}
    if kind == "event":
        when = parse_date(c.get("date"))
        if when and when < today and not c.get("future_relevance"):
            return {"route": "DROP", "why": ["a one-off event that already happened"]}

    if sens == "special" and src not in ("user_said", "user_confirmed"):
        return {"route": "ASK", "why": ["sensitive category (health, beliefs, finances, etc.) that the user did not state themselves"]}
    if c.get("about_other_person") and src != "user_confirmed":
        return {"route": "ASK", "why": ["personal detail about someone else; confirm the user wants it kept"]}
    if src in UNTRUSTED_SOURCES:
        return {"route": "ASK", "why": ["came from a tool or outside content, not from the user"]}

    # conflicts and duplicates against what is already stored
    key = c.get("key")
    for m in active(ledger):
        if key and m.get("key") == key and similarity(m.get("text"), text) < 0.8:
            mine, theirs = SOURCE_RANK.get(src, 1), SOURCE_RANK.get(m.get("source"), 1)
            if mine >= theirs:
                return {"route": "SUPERSEDE", "target": m.get("id"), "why": ["same subject '%s' with a new value from an equal or better source" % key]}
            return {"route": "ASK", "target": m.get("id"), "why": ["contradicts '%s' which came from a more trusted source" % m.get("id")]}
    for m in active(ledger):
        if similarity(m.get("text"), text) >= 0.75:
            return {"route": "MERGE", "target": m.get("id"), "why": ["already known as '%s'; refresh it instead of adding a copy" % m.get("id")]}

    if src in ("observed", "inferred"):
        seen = int(c.get("times_seen", 1))
        if seen < 2:
            return {"route": "WATCH", "why": ["seen once; wait for a second sighting before treating it as a habit"]}
        why.append("seen %d times" % seen)
        if src == "inferred" and kind == "fact_about_user":
            return {"route": "ASK", "why": why + ["an inference about the user; confirm before it becomes a fact"]}

    if c.get("expires") or TIME_HINTS.search(text):
        exp = parse_date(c.get("expires")) or (today + _dt.timedelta(days=int(c.get("ttl_days", 7))))
        return {"route": "EXPIRE", "expires": exp.isoformat(), "why": why + ["only true for a while; keep with an end date"]}

    if src in ("user_said", "user_confirmed") and (c.get("pin") or PIN_HINTS.search(text)):
        return {"route": "PIN", "why": why + ["the user set a standing rule"]}
    return {"route": "KEEP", "why": why + ["durable and useful later"]}


def cmd_triage(args):
    ledger = load(args.ledger) if args.ledger else {"memories": []}
    today = today_from(ledger, args.today)
    cands = load(args.candidates)
    if isinstance(cands, dict):
        cands = cands.get("candidates", [])
    out = []
    for i, c in enumerate(cands, 1):
        d = triage_one(c, ledger, today)
        d["id"] = c.get("id", "c%d" % i)
        d["text"] = c.get("text", "")
        out.append(d)
    counts = {r: sum(1 for d in out if d["route"] == r) for r in ROUTES}
    return {"today": today.isoformat(), "decisions": out, "counts": {k: v for k, v in counts.items() if v}}


# ---------------------------------------------------------------- inject

def score_memory(m, task_toks, task_tags, scope, today):
    if m.get("pinned"):
        return 100.0, "pinned rule"
    mt = tokens(m.get("text")) | set(t.lower() for t in m.get("tags", []))
    overlap = len(mt & task_toks) + 2 * len(set(t.lower() for t in m.get("tags", [])) & task_tags)
    if overlap == 0:
        return 0.0, "no link to this task"
    last = parse_date(m.get("last_used")) or parse_date(m.get("created")) or today
    age = max((today - last).days, 0)
    fresh = 1.0 if age <= 30 else 0.7 if age <= 120 else 0.4
    trust = SOURCE_RANK.get(m.get("source"), 1) / 5.0
    bonus = 1.2 if scope and m.get("scope") == scope else 1.0
    return round(overlap * fresh * (0.5 + trust) * bonus, 3), "overlap %d, freshness %.1f, trust %.1f" % (overlap, fresh, trust)


def cmd_inject(args):
    ledger = load(args.ledger)
    today = today_from(ledger, args.today)
    task_toks = tokens(args.task)
    task_tags = set(t.strip().lower() for t in (args.tags or "").split(",") if t.strip())
    task_toks |= task_tags
    chosen, skipped, used = [], [], 0
    pool = []
    # one value per subject: keep the best-sourced, most recent entry for each key
    best_for_key = {}
    for m in active(ledger):
        k = m.get("key")
        if not k:
            continue
        rank = (SOURCE_RANK.get(m.get("source"), 1), str(m.get("last_used") or m.get("created") or ""))
        if k not in best_for_key or rank > best_for_key[k][0]:
            best_for_key[k] = (rank, m.get("id"))
    for m in active(ledger):
        if m.get("source") in UNTRUSTED_SOURCES and IMPERATIVE_HINTS.search(m.get("text", "")):
            skipped.append({"id": m.get("id"), "why": "standing rule planted by outside content; never loaded"})
            continue
        if m.get("key") and best_for_key[m["key"]][1] != m.get("id"):
            skipped.append({"id": m.get("id"), "why": "older or weaker value for '%s' (loaded %s instead)" % (m["key"], best_for_key[m["key"]][1])})
            continue
        exp = parse_date(m.get("expires"))
        if exp and exp < today:
            skipped.append({"id": m.get("id"), "why": "expired %s" % exp})
            continue
        sc = m.get("scope", "global")
        if sc != "global" and args.scope and sc != args.scope:
            skipped.append({"id": m.get("id"), "why": "belongs to %s" % sc})
            continue
        if sc != "global" and not args.scope:
            skipped.append({"id": m.get("id"), "why": "scoped to %s and no scope given" % sc})
            continue
        if m.get("sensitivity") == "special" and not (set(t.lower() for t in m.get("tags", [])) & task_tags):
            skipped.append({"id": m.get("id"), "why": "sensitive; only loaded when the task is explicitly about it"})
            continue
        s, why = score_memory(m, task_toks, task_tags, args.scope, today)
        if s <= 0:
            skipped.append({"id": m.get("id"), "why": why})
            continue
        pool.append((s, m, why))
    pool.sort(key=lambda x: (-x[0], x[1].get("id", "")))
    for s, m, why in pool:
        c = cost(m.get("text"))
        if used + c > args.budget and not m.get("pinned"):
            skipped.append({"id": m.get("id"), "why": "over budget (score %s)" % s})
            continue
        used += c
        chosen.append({"id": m.get("id"), "text": m.get("text"), "score": s, "why": why})
    block = "\n".join("- " + c["text"] for c in chosen)
    return {"task": args.task, "budget": args.budget, "used": used, "chosen": chosen, "skipped": skipped, "context_block": block}


# ---------------------------------------------------------------- audit

def cmd_audit(args):
    ledger = load(args.ledger)
    today = today_from(ledger, args.today)
    issues = []

    def add(code, mid, msg, fix):
        issues.append({"code": code, "id": mid, "message": msg, "fix": fix})

    act = active(ledger)
    for m in act:
        mid = m.get("id")
        text = m.get("text", "")
        if has_secret(text):
            add("SECRET_STORED", mid, "a credential or card-like number is stored", "delete it now and rotate the secret")
        if tombstone_hit(text, ledger):
            add("FORGOTTEN_CAME_BACK", mid, "matches something the user asked to forget", "retire it; check what re-learned it")
        exp = parse_date(m.get("expires"))
        if exp and exp < today:
            add("EXPIRED_STILL_ACTIVE", mid, "end date %s has passed" % exp, "retire it")
        if not m.get("source"):
            add("NO_SOURCE", mid, "nobody knows where this came from", "ask the user or retire it")
        created = parse_date(m.get("created"))
        if m.get("source") == "inferred" and m.get("kind") == "fact_about_user" and created and (today - created).days > 30:
            add("GUESS_HARDENED", mid, "an inference about the user has sat unconfirmed for over 30 days", "confirm or retire")
        last = parse_date(m.get("last_used")) or created
        if last and not m.get("pinned") and (today - last).days > args.stale_days:
            add("STALE", mid, "not used for %d days" % (today - last).days, "retire, or re-check it is still true")
        if cost(text) > 80:
            add("OVERSIZE", mid, "too long to load cheaply (~%d tokens)" % cost(text), "split into single facts or summarize")
        if m.get("sensitivity") == "special" and m.get("source") not in ("user_said", "user_confirmed"):
            add("SENSITIVE_UNCONFIRMED", mid, "sensitive detail the user never stated", "ask or retire")
        if m.get("source") in UNTRUSTED_SOURCES and IMPERATIVE_HINTS.search(text):
            add("PLANTED_RULE", mid, "a standing instruction that came from outside content", "retire it immediately")
    by_key = {}
    for m in act:
        if m.get("key"):
            by_key.setdefault(m["key"], []).append(m)
    for k, ms in by_key.items():
        vals = {m.get("text", "").strip().lower() for m in ms}
        if len(vals) > 1:
            ids = ", ".join(m.get("id") for m in ms)
            add("CONTRADICTION", ids, "subject '%s' has %d different values" % (k, len(vals)), "keep the newest from the best source; retire the rest")
    for i, a in enumerate(act):
        for b in act[i + 1 :]:
            if a.get("key") and a.get("key") == b.get("key"):
                continue
            if similarity(a.get("text"), b.get("text")) >= 0.75:
                add("DUPLICATE", "%s, %s" % (a.get("id"), b.get("id")), "near-identical entries", "merge into one")
    order = ["SECRET_STORED", "PLANTED_RULE", "FORGOTTEN_CAME_BACK", "CONTRADICTION", "SENSITIVE_UNCONFIRMED",
             "EXPIRED_STILL_ACTIVE", "GUESS_HARDENED", "NO_SOURCE", "DUPLICATE", "OVERSIZE", "STALE"]
    issues.sort(key=lambda x: order.index(x["code"]))
    severe = {"SECRET_STORED", "PLANTED_RULE", "FORGOTTEN_CAME_BACK"}
    grade = "HEALTHY" if not issues else "URGENT" if any(i["code"] in severe for i in issues) else "NEEDS_WEEDING"
    return {"today": today.isoformat(), "active": len(act), "grade": grade, "issues": issues}


# ---------------------------------------------------------------- forget

def cmd_forget(args):
    ledger = load(args.ledger)
    today = today_from(ledger, args.today)
    hits = []
    for m in ledger.get("memories", []):
        if m.get("status", "active") != "active":
            continue
        t = m.get("text", "")
        if args.match.lower() in t.lower() or similarity(args.match, t) >= 0.6 or m.get("id") == args.match:
            m["status"] = "forgotten"
            m["text"] = "[forgotten]"
            m["forgotten_on"] = today.isoformat()
            hits.append(m.get("id"))
    ledger.setdefault("tombstones", []).append({"match": args.match, "date": today.isoformat(), "reason": args.reason or "user asked"})
    out = args.out or args.ledger
    with open(out, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)
    return {"forgotten": hits, "tombstone": args.match, "written": out,
            "note": "content wiped, tombstone kept so the same thing is refused if it shows up again"}


# ---------------------------------------------------------------- io

def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def render(cmd, res):
    lines = []
    if cmd == "triage":
        for d in res["decisions"]:
            extra = ""
            if d.get("target"):
                extra += " -> %s" % d["target"]
            if d.get("expires"):
                extra += " until %s" % d["expires"]
            lines.append("%-9s %s%s\n          %s\n          why: %s" % (d["route"], d["id"], extra, d["text"], "; ".join(d["why"])))
        lines.append("counts: " + ", ".join("%s %d" % kv for kv in res["counts"].items()))
    elif cmd == "inject":
        lines.append("budget %d, used %d" % (res["budget"], res["used"]))
        for c in res["chosen"]:
            lines.append("LOAD  %-6s %s  (%s)" % (c["id"], c["text"], c["why"]))
        for s in res["skipped"]:
            lines.append("skip  %-6s %s" % (s["id"], s["why"]))
    elif cmd == "audit":
        lines.append("grade: %s  (%d active memories, %d issues)" % (res["grade"], res["active"], len(res["issues"])))
        for i in res["issues"]:
            lines.append("%-22s %s: %s -> %s" % (i["code"], i["id"], i["message"], i["fix"]))
    elif cmd == "forget":
        lines.append("forgotten: %s" % (", ".join(res["forgotten"]) or "nothing active matched"))
        lines.append("tombstone kept for: %s" % res["tombstone"])
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(prog="garden.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("triage"); a.add_argument("candidates"); a.add_argument("--ledger")
    b = sub.add_parser("inject"); b.add_argument("ledger"); b.add_argument("--task", required=True)
    b.add_argument("--tags"); b.add_argument("--scope"); b.add_argument("--budget", type=int, default=250)
    c = sub.add_parser("audit"); c.add_argument("ledger"); c.add_argument("--stale-days", type=int, default=120)
    d = sub.add_parser("forget"); d.add_argument("ledger"); d.add_argument("--match", required=True)
    d.add_argument("--reason"); d.add_argument("--out")
    for s in (a, b, c, d):
        s.add_argument("--today"); s.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    res = {"triage": cmd_triage, "inject": cmd_inject, "audit": cmd_audit, "forget": cmd_forget}[args.cmd](args)
    print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else render(args.cmd, res))
    if args.cmd == "audit" and res["grade"] == "URGENT":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
