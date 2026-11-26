"""Go-Live Assurance MCP server v3.
Mirrors Go-Live_Velocity_Model.xlsx. All numbers are illustrative: replace with your measured baseline.
v2 adds: customer-vs-Kenko delay attribution, blocker and owner per migration, follow-up/stale-touch detection,
next actions with due dates, source-aware proactive customer updates, and a portfolio view."""
import math
from datetime import date, timedelta
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("go-live-assurance")
TARGET = 10                                                  # two-week go-live target (business days)
LANES = {"A": (8, .5), "B": (15, 1), "C": (25, 1.5)}         # P90 business days, extra days per added location
PAY = {"stripe": 5, "modern": 10, "legacy": 20}              # payment-transfer lead time (business days)
GATES, GNG, WIN, DAY_N, SLACK = (.25, .5, .7), 2, 2, 2, 2    # gate % of window, go/no-go, warn window, Day-N check, absorbable slip
STALE, CLEAN = 3, 3                                          # business days without a customer touch; extra days for data cleanup
KNOWN = {"mindbody", "wellnessliving", "mariana tek"}        # sources with a pre-built schema mapper
SRC_WAIT = {"mindbody": 9, "wellnessliving": 6, "mariana tek": 5, "other": 12}   # ILLUSTRATIVE export wait; learn_source_profile replaces it
OWNER = {"data": "customer", "sandbox": "kenko", "staff": "customer", "cutover": "kenko"}
HOL = {date(2026, 12, 25), date(2027, 1, 1)}                 # make this per-region in production

def D(s): return date.fromisoformat(s) if s else None
def work(d): return d.weekday() < 5 and d not in HOL
def wd(d, n):                                                # Excel WORKDAY
    s = 1 if n >= 0 else -1
    while n:
        d += timedelta(s); n -= s if work(d) else 0
    return d
def nwd(a, b): return sum(work(a + timedelta(i)) for i in range((b - a).days + 1))   # Excel NETWORKDAYS
def rnd(x): return int(x + .5)
def err(m): return {"error": m}

@mcp.tool()
def route_lane(locations: int, source_system: str, data_quality: str = "clean", integrations: bool = False) -> dict:
    """Assign a delivery lane at sales handoff (A templated, B standard, C complex)."""
    if locations < 1: return err("locations must be at least 1")
    known = source_system.strip().lower() in KNOWN
    lane = "C" if integrations or locations > 10 else "A" if locations <= 3 and known else "B"
    why = {"A": "1-3 locations on a source with a pre-built mapper", "B": "4-10 locations, or a source without a pre-built mapper", "C": "integrations or more than 10 locations"}[lane]
    return {"lane": lane, "needs_cleanup": data_quality != "clean", "reasons": [why]}

@mcp.tool()
def quote_commit_date(signed: str, lane: str, locations: int, processor: str, team_load: float, cleanup: bool = False, kickoff: str = "") -> dict:
    """Commit date = lane P90 (+location uplift) vs payment lead time (parallel, longer wins) + capacity buffer (+cleanup); checked against the two-week target. Supply kickoff when it differs from signature."""
    lane, processor = lane.strip().upper(), processor.strip().lower()
    if lane not in LANES or processor not in PAY: return err("unknown lane or processor")
    if locations < 1 or not 0 <= team_load <= 1: return err("locations must be >= 1 and team_load between 0 and 1")
    try: s = D(kickoff or signed)
    except ValueError: return err("signed and kickoff must be YYYY-MM-DD")
    if not s: return err("signed is required")
    p90, up = LANES[lane]; lane_wd = p90 + up * (locations - 1); cap = 0 if team_load < .71 else 2 if team_load < .86 else 5; buf = cap + (CLEAN if cleanup else 0)
    days = math.ceil(max(lane_wd, PAY[processor])) + buf
    g = [wd(s, rnd(days * x)) for x in GATES]; c = wd(s, days); binding = "lane" if lane_wd >= PAY[processor] else "payment"
    levers = [] if days <= TARGET else (["Start payment transfer on Day 0 and escalate the processor request"] if binding == "payment" else []) + \
        (["Phase the launch: members, schedule and payments first; history after"] if lane != "A" else []) + \
        (["Add specialist capacity or rebalance load"] if cap else []) + (["Start data cleanup in parallel with the export"] if cleanup else []) + ["Pre-book kickoff and cutover slots; run mapping in parallel"]
    return {"commit_days": days, "commit_date": c.isoformat(), "binding": binding, "within_target": days <= TARGET, "gap_wd": max(0, days - TARGET), "levers": levers,
            "gates": {"data": g[0].isoformat(), "sandbox": g[1].isoformat(), "staff": g[2].isoformat(), "go_no_go": wd(c, -GNG).isoformat()}}

ENT = ("members", "active_memberships", "class_packs", "future_bookings", "payment_methods")
REQ = ("dry_run_passed", "payment_test_transaction", "rollback_tested", "front_desk_checkin_verified", "owner_signoff")

@mcp.tool()
def audit_cutover_readiness(source: dict, migrated: dict, excluded: dict, tests: dict) -> dict:
    """Zero-tolerance gate: migrated = source - approved exclusions for every entity, and every required test passed."""
    miss = [e for e in ENT if e not in source or e not in migrated]
    if miss: return err(f"missing counts for: {', '.join(miss)}")
    if any(type(n) is not int for e in ENT for n in (source[e], migrated[e], excluded.get(e, 0))):
        return err("all record counts must be integers")
    inv = [e for e in ENT if source[e] < 0 or migrated[e] < 0 or excluded.get(e, 0) < 0 or excluded.get(e, 0) > source[e]]
    if inv: return err(f"invalid counts (negative, or exclusions above source) for: {', '.join(inv)}")
    bad = [{"entity": e, "expected": source[e] - excluded.get(e, 0), "migrated": migrated[e], "delta": migrated[e] - (source[e] - excluded.get(e, 0))}
           for e in ENT if migrated[e] != source[e] - excluded.get(e, 0)]
    failed = [t for t in REQ if tests.get(t) is not True]
    return {"decision": "no_go" if bad or failed else "go", "mismatches": bad, "failed_tests": failed, "checks_run": len(ENT) + len(REQ)}

@mcp.tool()
def learn_source_profile(history: list[dict]) -> dict:
    """From past migrations [{source, export_wait_days, on_time}] compute export wait P50/P90 and late rate per source system, so quotes and customer messages use measured, not assumed, waits."""
    out = {}
    for h in history:
        out.setdefault(h["source"].strip().lower(), []).append(h)
    def pct(v, p): v = sorted(v); return v[max(0, math.ceil(p * len(v)) - 1)]   # nearest rank
    return {s: {"n": len(r), "p50_wait": pct([x["export_wait_days"] for x in r], .5), "p90_wait": pct([x["export_wait_days"] for x in r], .9),
                "late_rate": round(1 - sum(1 for x in r if x["on_time"]) / len(r), 2)} for s, r in out.items()}

@mcp.tool()
def assess_go_live(signed: str, lane: str, locations: int, processor: str, team_load: float, as_of: str, data_received: str = "",
                   sandbox_done: str = "", staff_verified: str = "", payment_started: str = "", customer_slip: int = -1, live: str = "",
                   last_touch: str = "", source_system: str = "other", cleanup: bool = False, kickoff: str = "") -> dict:
    """Status, forecast, who is blocking (customer or kenko), follow-up gaps, next actions with due dates, and whether to notify the customer.
    customer_slip=-1 means attribute delay automatically from gate actuals; pass a number to override."""
    q = quote_commit_date(signed, lane, locations, processor, team_load, cleanup, kickoff)
    if "error" in q: return q
    try: [D(x) for x in (as_of, data_received, sandbox_done, staff_verified, live, last_touch)]
    except ValueError: return err("dates must be YYYY-MM-DD")
    if not as_of: return err("as_of is required")
    s, a, commit = D(kickoff or signed), D(as_of), D(q["commit_date"])
    due = [D(q["gates"][k]) for k in ("data", "sandbox", "staff")]; act = [D(data_received), D(sandbox_done), D(staff_verified)]
    slips = [max(0, nwd(d, x) - 1) if x else (nwd(d, a) - 1 if a > d else 0) for d, x in zip(due, act)]
    total = max(slips)
    cust = max(0, min(total, slips[0] + max(0, slips[2] - max(slips[1], slips[0])))) if customer_slip < 0 else customer_slip   # delay not explained by earlier gates
    rb = wd(commit, cust); net = 0 if live else max(0, total - cust); fc = D(live) or max(wd(a, 1), wd(rb, max(0, net - SLACK)))
    gate = None if live else next((g for g, x in zip(("data", "sandbox", "staff"), act) if not x), "cutover")
    blocked = "none" if live else OWNER[gate]
    late = bool(gate in ("data", "sandbox", "staff") and a > due[("data", "sandbox", "staff").index(gate)])
    idle = nwd(D(last_touch) if last_touch else s, a) - 1; stale = not live and idle >= STALE
    dn = wd(s, DAY_N); warn = ""
    if not live:
        if not act[0] and a >= dn: warn = f"Data file outstanding on Day {DAY_N}"
        elif not payment_started and a >= dn: warn = f"Payment transfer not started by Day {DAY_N}"
        elif not act[0] and a >= wd(due[0], -WIN): warn = "Data gate due soon"
        elif act[0] and not act[1] and a >= wd(due[1], -WIN): warn = "Sandbox gate due soon"
        elif act[1] and not act[2] and a >= wd(due[2], -WIN): warn = "Staff gate due soon"
    cust_late = blocked == "customer" and late
    status = (("Live - on time" if fc <= commit else "Live - customer-delayed" if fc <= rb else "Live - late (vendor)") if live
              else "Red" if fc > rb else "Amber" if net > 0 or warn or cust_late or stale else "Green")
    todo = []
    if not live:
        due_g = {"data": due[0], "sandbox": due[1], "staff": due[2], "cutover": commit}[gate]; when = a if late or a >= wd(due_g, -WIN) else wd(due_g, -WIN)
        todo.append({"action": {"data": f"Chase data export from {source_system}: reminder plus one-step upload link", "sandbox": "Finish schema mapping and sandbox review",
                                "staff": "Book staff training and verify front-desk check-in", "cutover": "Run readiness audit and book the cutover slot"}[gate],
                     "owner": "customer + onboarding specialist" if blocked == "customer" else "kenko onboarding specialist", "due": when.isoformat()})
        if not payment_started: todo.append({"action": "Start the payment transfer request with the processor", "owner": "onboarding specialist", "due": a.isoformat()})
        if stale: todo.append({"action": f"No customer touch for {idle} business days: log a check-in call today", "owner": "CSM", "due": a.isoformat()})
    notify = not live and (cust > 0 or cust_late or status == "Red")
    return {"status": status, "forecast_go_live": fc.isoformat(), "committed": q["commit_date"], "blocked_on": blocked, "blocking_gate": gate,
            "customer_slip_wd": cust, "vendor_slip_wd": net, "idle_wd": idle, "stale": stale, "early_warning": warn, "next_actions": todo,
            "notify_customer": notify, "promise_at_risk": not live and fc > wd(s, TARGET),
            "recovery_play": {"Red": "Swarm a second specialist; extended cutover window; phased launch; exec sponsor call within 24h",
                              "Amber": "Owner call within 1 business day; hold next training slot; re-run pre-flight validator"}.get(status, "")}

@mcp.tool()
def draft_customer_update(account: str, source_system: str, blocking_gate: str, committed: str, forecast: str, expected_wait_days: int = -1) -> dict:
    """Deterministic proactive update for the customer: where we are, the one thing needed, and a source-specific expectation. A model may polish the wording."""
    wait = expected_wait_days if expected_wait_days >= 0 else SRC_WAIT.get(source_system.strip().lower(), SRC_WAIT["other"])
    ask = {"data": f"your member, membership, class pack and future booking exports from {source_system}",
           "sandbox": "a quick review of your test setup (about 20 minutes)", "staff": "a 30-minute session with your front-desk team",
           "cutover": "your sign-off on the cutover slot"}.get(blocking_gate, "one small step")
    note = f" Exports from {source_system} usually take about {wait} business days to arrive, so asking today protects your date." if blocking_gate == "data" else ""
    body = (f"Hi {account} team, a quick update on your move to Kenko. Your go-live is committed for {committed} and currently forecast for {forecast}. "
            f"The one thing we need from you is {ask}.{note} Reply here or pick a slot and we will take care of the rest.")
    return {"subject": f"{account}: one step to keep your go-live on track", "body": body, "channels": ["email", "text"]}

@mcp.tool()
def portfolio_view(accounts: list[dict], as_of: str) -> dict:
    """Every in-flight and recent migration with status, blocker and owner, plus a summary for CS and executives. Each account dict holds a name plus the assess_go_live inputs."""
    rows = []
    for acc in accounts:
        acc = dict(acc); name = acc.pop("name", "?")
        try: r = assess_go_live(as_of=as_of, **acc)
        except TypeError as e: r = err(str(e))
        r["name"] = name; rows.append(r)
    skipped = [{"name": r["name"], "error": r["error"]} for r in rows if "error" in r]; rows = [r for r in rows if "error" not in r]
    rank = {"Red": 0, "Amber": 1, "Green": 2}; rows.sort(key=lambda r: rank.get(r["status"], 3))
    live = [r for r in rows if r["status"].startswith("Live")]; flight = [r for r in rows if not r["status"].startswith("Live")]
    return {"summary": {"in_flight": len(flight), "by_status": {k: sum(r["status"] == k for r in flight) for k in ("Red", "Amber", "Green")},
                        "blocked_on_customer": sum(r["blocked_on"] == "customer" for r in flight), "blocked_on_kenko": sum(r["blocked_on"] == "kenko" for r in flight),
                        "stale_follow_ups": sum(r["stale"] for r in flight), "promise_at_risk": sum(r["promise_at_risk"] for r in flight),
                        "live_on_time_rate": round(sum(r["status"] == "Live - on time" for r in live) / len(live), 2) if live else None},
            "accounts": rows, "skipped": skipped}

@mcp.tool()
def first_invoice_bridge(accounts: list[dict], close_date: str) -> dict:
    """Compare cumulative first invoices through the close for distinct USD contracts.
    Each row needs contract_id, first_invoice_cents, billing_trigger (signature or go_live),
    signed, committed and forecast (ISO dates). For a live contract use its actual launch
    date as forecast. Optional blocker, action_owner, due and next_action pass through.
    Amounts are full first invoices. Collections and revenue recognition are separate.
    """
    try:
        close = date.fromisoformat(close_date)
    except (TypeError, ValueError):
        return err("invalid close_date")
    seen, rows = set(), []
    planned = pushed = pulled = 0
    for row in accounts:
        key = row.get("contract_id")
        if not isinstance(key, str) or not key.strip() or key.strip() in seen:
            return err("each row needs a distinct contract_id")
        seen.add(key.strip())
        fee, trigger = row.get("first_invoice_cents"), row.get("billing_trigger")
        if type(fee) is not int or fee < 0 or trigger not in ("go_live", "signature"):
            return err(f"{key}: invalid invoice amount or billing trigger")
        try:
            signed, committed, forecast = (date.fromisoformat(row[k]) for k in ("signed", "committed", "forecast"))
        except (KeyError, TypeError, ValueError):
            return err(f"{key}: invalid or missing contract date")
        if min(committed, forecast) < signed:
            return err(f"{key}: launch cannot precede signature")
        baseline, revised = (signed, signed) if trigger == "signature" else (committed, forecast)
        before = fee if baseline <= close else 0
        after = fee if revised <= close else 0
        delta = after - before
        planned += before
        pushed += max(0, -delta)
        pulled += max(0, delta)
        rows.append({"contract_id": key, "billing_trigger": trigger, "first_invoice_cents": fee,
                     "planned_invoice": baseline.isoformat(), "revised_invoice": revised.isoformat(),
                     "impact_cents": delta, **{k: row.get(k) for k in ("blocker", "action_owner", "due", "next_action")}})
    return {"currency": "USD", "close_date": close.isoformat(), "planned_cents": planned,
            "pushed_out_cents": pushed, "pulled_in_cents": pulled, "revised_cents": planned - pushed + pulled,
            "contracts": sorted(rows, key=lambda row: row["impact_cents"])}


from import_assistant_tools import register
register(mcp)


if __name__ == "__main__":
    mcp.run()
