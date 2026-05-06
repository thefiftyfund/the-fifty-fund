import json
from collections import defaultdict, Counter
from datetime import datetime, timezone

LEDGER = "data/ledger.jsonl"

today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
events = []

with open(LEDGER, encoding="utf-8") as fh:
for i, line in enumerate(fh, 1):
line = line.strip()
if not line:
continue
try:
e = json.loads(line)
except Exception as exc:
print(f"[BAD JSON] line {i}: {exc}")
continue
ts = e.get("timestamp", "")
if ts.startswith(today):
e["_line"] = i
events.append(e)

print(f"\nLoaded {len(events)} ledger events for {today}\n")

by_cycle = defaultdict(list)
for e in events:
by_cycle[e.get("cycle_id", "<missing>")].append(e)

expected_order = [
"CYCLE_START",
"RECONCILIATION",
"DECISION_PROPOSED",
"DECISION_VALIDATED",
]

def summarize_cycle(cid, evs):
evs = sorted(evs, key=lambda x: x.get("timestamp", ""))
types = [e.get("event_type", "<missing>") for e in evs]
counts = Counter(types)
problems = []

```
if counts["CYCLE_START"] != 1:
    problems.append(f"CYCLE_START count={counts['CYCLE_START']}")
if counts["CYCLE_END"] != 1:
    problems.append(f"CYCLE_END count={counts['CYCLE_END']}")

for must in ["RECONCILIATION", "DECISION_PROPOSED", "DECISION_VALIDATED"]:
    if counts[must] != 1:
        problems.append(f"{must} count={counts[must]}")

if counts["ORDER_FILLED"] > counts["ORDER_SUBMITTED"]:
    problems.append("more ORDER_FILLED than ORDER_SUBMITTED")
if counts["ORDER_REJECTED"] > counts["ORDER_SUBMITTED"]:
    problems.append("more ORDER_REJECTED than ORDER_SUBMITTED")
if counts["ORDER_SUBMITTED"] > 0 and (counts["ORDER_FILLED"] + counts["ORDER_REJECTED"] == 0):
    problems.append("ORDER_SUBMITTED with no ORDER_FILLED/ORDER_REJECTED")
if counts["ORDER_SUBMITTED"] == 0 and (counts["ORDER_FILLED"] + counts["ORDER_REJECTED"] > 0):
    problems.append("fill/reject exists without ORDER_SUBMITTED")

first_index = {}
for idx, t in enumerate(types):
    if t not in first_index:
        first_index[t] = idx

for a, b in zip(expected_order, expected_order[1:]):
    if a in first_index and b in first_index and first_index[a] > first_index[b]:
        problems.append(f"event order wrong: {a} occurs after {b}")

if "ORDER_SUBMITTED" in first_index:
    for later in ["ORDER_FILLED", "ORDER_REJECTED"]:
        if later in first_index and first_index["ORDER_SUBMITTED"] > first_index[later]:
            problems.append(f"event order wrong: ORDER_SUBMITTED occurs after {later}")

if "CYCLE_END" in first_index and first_index["CYCLE_END"] != len(types) - 1:
    problems.append("CYCLE_END is not the final event")

submitted = [
    (
        e.get("payload", {}).get("ticker"),
        e.get("payload", {}).get("action"),
        e.get("_line"),
    )
    for e in evs if e.get("event_type") == "ORDER_SUBMITTED"
]
seen = set()
for ticker, action, line in submitted:
    key = (ticker, action)
    if key in seen:
        problems.append(f"duplicate ORDER_SUBMITTED in cycle for {action} {ticker}")
    seen.add(key)

rec = next((e for e in evs if e.get("event_type") == "RECONCILIATION"), None)
if rec:
    payload = rec.get("payload", {})
    current = payload.get("current", {})
    if not isinstance(current, dict):
        problems.append("RECONCILIATION.current missing/damaged")
    else:
        if "cash" not in current or "portfolio_value" not in current or "positions" not in current:
            problems.append("RECONCILIATION.current missing required fields")

return types, counts, problems
```

bad_cycles = []
for cid, evs in sorted(by_cycle.items(), key=lambda kv: min(e.get("timestamp", "") for e in kv[1])):
types, counts, problems = summarize_cycle(cid, evs)
print(f"Cycle {cid}")
print(f"  Events: {' -> '.join(types)}")
if counts["ORDER_SUBMITTED"]:
orders = [
f"{e.get('payload', {}).get('action','?')} {e.get('payload', {}).get('ticker','?')}"
for e in evs if e.get("event_type") == "ORDER_SUBMITTED"
]
print(f"  Orders: {', '.join(orders)}")
if problems:
print(f"  PROBLEMS: {'; '.join(problems)}")
bad_cycles.append((cid, problems))
else:
print("  OK")
print()

recent_orders = defaultdict(list)
for e in sorted(events, key=lambda x: x.get("timestamp", "")):
if e.get("event_type") == "ORDER_SUBMITTED":
p = e.get("payload", {})
key = (p.get("ticker"), p.get("action"))
recent_orders[key].append((e.get("cycle_id"), e.get("timestamp"), e.get("_line")))

print("Cross-cycle ORDER_SUBMITTED summary:")
found_dup = False
for key, vals in recent_orders.items():
if len(vals) > 1:
found_dup = True
print(f"  DUPLICATE {key}: {vals}")
if not found_dup:
print("  No cross-cycle duplicate submitted orders found")
print()

print("Summary:")
print(f"  Total cycles today: {len(by_cycle)}")
print(f"  Problem cycles: {len(bad_cycles)}")
if bad_cycles:
print("  Problem details:")
for cid, probs in bad_cycles:
print(f"   - {cid}: {'; '.join(probs)}")
else:
print("  No structural problems detected in today's cycles.")
