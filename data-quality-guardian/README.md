# Data Quality Guardian

A small, heavily commented **learning project**: an autonomous multi-agent system that
**detects → plans → fixes → validates** data-quality and referential-integrity problems in a
tiny e-commerce database, looping until the data is clean (or a safety limit is hit).

It exists to teach three ideas at once:

| Concept | Where you see it |
|---|---|
| **Multi-agent orchestration** | `agents/agent.py` — four role-specialised `LlmAgent`s sharing session state |
| **Graph engineering** | `graph/build_kuzu.py` + the Cypher in `tools/quality_tools.py` |
| **Autonomous loop engineering** | `LoopAgent` + `escalate` + `max_iterations` in `agents/agent.py` |

Stack: [Google ADK](https://google.github.io/adk-docs/) (agents), [Kuzu](https://kuzudb.com/)
(embedded graph DB), stdlib `sqlite3` (system of record). No servers, no cloud resources —
everything runs from two local files.

---

## The use case

`db/seed_sqlite.py` builds a clean little shop (`customers`, `products`, `orders`,
`order_items`) and then **deliberately breaks it** in four ways:

| # | Defect | Seeded as |
|---|---|---|
| D1 | Orphan order — references a deleted customer | `orders.id = 103`, `customer_id = 99` |
| D2 | Orphan order_item — references a missing product | `order_items.id = 1004`, `product_id = 77` |
| D3 | Negative stock | `products.id = 13`, `stock = -4` |
| D4 | Price mismatch (`unit_price` ≠ `products.price`) | `order_items.id = 1005`, `12.00` vs `45.50` |

The agents have to find all four, repair them in a sensible order, and *prove* the database is
clean afterwards.

---

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Gemini key — get one free at https://aistudio.google.com/apikey
export GEMINI_API_KEY="your-key-here"

python main.py
```

Optional: `export ADK_MODEL=gemini-2.0-flash` to pick a different Gemini model.

Each module also runs standalone, which is the easiest way to explore it:

```bash
python db/seed_sqlite.py      # rebuild shop.db with the 4 defects
python graph/build_kuzu.py    # rebuild the graph, print orphan orders
python tools/quality_tools.py # print the detected issues as JSON
```

---

## Concept 1 — Multi-agent: role-specialised agents sharing state

Rather than one giant prompt, the work is split into four narrow agents. Each has a single job,
its own instruction, and only the tools it is allowed to use:

```
root_agent = LoopAgent(max_iterations=5)
  └── fix_cycle = SequentialAgent
        ├── detector_agent   tools=[detect_issues]  output_key="issues"
        ├── planner_agent    (no tools)             output_key="plan"
        ├── fixer_agent      tools=[apply_fix]      output_key="fix_report"
        └── validator_agent  tools=[count_issues]   output_key="validation"
```

**How they hand off work:** ADK writes an agent's final text into session state under its
`output_key`, and the next agent pulls it into its prompt with a `{placeholder}`:

```python
detector_agent = LlmAgent(..., output_key="issues")            # writes state["issues"]
planner_agent  = LlmAgent(instruction="...{issues?}...",       # reads  state["issues"]
                          output_key="plan")                   # writes state["plan"]
fixer_agent    = LlmAgent(instruction="...{plan?}...", tools=[apply_fix_tool])
```

That is the whole hand-off mechanism — no queues, no custom glue. The `?` suffix just means
"tolerate the key being absent on the first pass".

**Why it is better than one agent:** each prompt stays short and testable; only the fixer can
write to the database (least privilege); and you can swap one role (e.g. a smarter planner)
without touching the others.

---

## Concept 2 — Graph engineering: why Kuzu/Cypher for integrity checks

`graph/build_kuzu.py` mirrors SQLite into a property graph:

```
(Customer)-[:PLACED]->(Order)-[:CONTAINS {item_id, quantity, unit_price}]->(Product)
```

The key design decision: **an edge is only created when both endpoints exist.** A broken foreign
key therefore becomes a *missing edge*, and missing edges are exactly what graph queries are good
at finding.

```cypher
-- Orphan orders: an order nobody placed
MATCH (o:`Order`)
WHERE NOT EXISTS { MATCH (:Customer)-[:PLACED]->(o) }
RETURN o.id;

-- Orders hiding an item whose product row vanished
-- (item_count is copied from SQLite; fewer edges than items == dangling rows)
MATCH (o:`Order`)
OPTIONAL MATCH (o)-[c:CONTAINS]->(:Product)
WITH o, count(c) AS linked
WHERE linked < o.item_count
RETURN o.id, o.item_count, linked;

-- Impossible inventory
MATCH (p:Product) WHERE p.stock < 0 RETURN p.id, p.stock;

-- Frozen price on the edge disagrees with the catalogue price on the node
MATCH (:`Order`)-[c:CONTAINS]->(p:Product)
WHERE abs(c.unit_price - p.price) > 0.001
RETURN c.item_id, c.unit_price, p.price;
```

*(`Order` needs backticks — it is a reserved Cypher word.)*

**Impact analysis** is the second reason to keep a graph. Before deleting anything you can ask
"what else does this touch?" in one hop, instead of writing recursive joins:

```cypher
MATCH (c:Customer {id: 1})-[:PLACED]->(o:`Order`)-[:CONTAINS]->(p:Product)
RETURN c.name, collect(DISTINCT p.name) AS products_at_risk;
```

The graph is a **derived view**, never the source of truth: `apply_fix()` writes to SQLite and
then calls `sync_from_sqlite()` so the next detection pass reasons about repaired data.

---

## Concept 3 — Loop engineering: "run until fixed"

A single detect→fix pass is rarely enough: fixes can reveal or resolve other issues (deleting
orphan order 103 also removes its child `order_item` 1003). So the pass is wrapped in a loop with
two exits:

```python
fix_cycle  = SequentialAgent(sub_agents=[detector, planner, fixer, validator])
root_agent = LoopAgent(sub_agents=[fix_cycle], max_iterations=5)
```

* **Success exit — `escalate`.** The `count_issues` tool receives ADK's `tool_context` and, when
  nothing is left, sets:

  ```python
  tool_context.actions.escalate = True   # -> EventActions.escalate -> LoopAgent stops
  ```

  This is the ADK-idiomatic way to break a loop from *inside* a sub-agent. The decision is made
  by re-measuring the database, not by asking the model whether it feels done.

* **Safety exit — `max_iterations=5`.** If something is genuinely unfixable, the loop still
  terminates. An autonomous system without a bound is a hang waiting to happen.

The general pattern is worth remembering: **act → re-measure with a deterministic tool →
escalate on a verified condition → bound the whole thing.**

---

## Expected output

Abridged, from `python main.py`:

```
========================================================================
STEP 3  issues before the agents run
========================================================================
  - orphan_order       pk=103    Order 103 references a customer that does not exist ...
  - orphan_order_item  pk=1004   order_item 1004 references missing product 77 ...
  - negative_stock     pk=13     Product 13 has negative stock (-4).
  - price_mismatch     pk=1005   order_item 1005 unit_price 12.0 != product price 45.5.

========================================================================
ITERATION 1
========================================================================
  [detector_agent] -> tool detect_issues({})
  [detector_agent] <- detect_issues returned {'count': 4, ...}
  [planner_agent] [{"step": 1, "issue_type": "orphan_order", "primary_key": 103, ...}, ...]
  [fixer_agent] -> tool apply_fix({'issue_type': 'orphan_order', 'primary_key': 103})
  [fixer_agent] -> tool apply_fix({'issue_type': 'orphan_order_item', 'primary_key': 1004})
  [fixer_agent] -> tool apply_fix({'issue_type': 'negative_stock', 'primary_key': 13})
  [fixer_agent] -> tool apply_fix({'issue_type': 'price_mismatch', 'primary_key': 1005})
  [validator_agent] -> tool count_issues({})
  [validator_agent] <- count_issues returned {'remaining': 0, 'clean': True}

  *** escalate raised by validator_agent - breaking the loop ***

========================================================================
LOOP FINISHED
========================================================================
  iterations run : 1
  stopped because: validator escalated (data is clean)

========================================================================
STEP 5  final SQLite verification
========================================================================
  orphan orders        0
  orphan order_items   0
  negative stock       0
  price mismatches     0

  TOTAL REMAINING DEFECTS: 0
  RESULT: DATABASE IS CLEAN
```

Because an LLM drives the plan, the exact wording and the number of iterations can vary — a run
that needs two passes is normal and is precisely what the loop is for. What is *not* allowed to
vary is the final verification: it is plain SQL, and it must print zero.

---

## Files

| Path | What it teaches |
|---|---|
| `config.py` | shared paths + model name |
| `db/seed_sqlite.py` | idempotent seed with 4 intentional defects |
| `graph/build_kuzu.py` | graph modelling + `sync_from_sqlite()` |
| `tools/quality_tools.py` | `detect_issues` / `count_issues` / `apply_fix` as ADK `FunctionTool`s |
| `agents/agent.py` | the four agents, `SequentialAgent`, `LoopAgent`, `escalate` |
| `main.py` | ADK `Runner` + in-memory session + per-iteration logging |
