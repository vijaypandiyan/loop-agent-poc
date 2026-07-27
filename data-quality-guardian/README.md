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

### Using DeepSeek / Qwen instead of Gemini (build.nvidia.com)

Nothing in the agents is Gemini-specific. ADK reaches non-Google models through
[LiteLLM](https://docs.litellm.ai/), and `https://build.nvidia.com` exposes an OpenAI-compatible
endpoint, so switching providers is purely environment variables (see `config.py`):

```bash
export MODEL_PROVIDER=nvidia
export NVIDIA_API_KEY="nvapi-..."                 # from https://build.nvidia.com
export ADK_MODEL="qwen/qwen3-next-80b-a3b-instruct"   # or meta/llama-3.3-70b-instruct
python check_model.py    # verifies the endpoint + that the model id really exists
python main.py
```

Prefer a file over exports? Copy `.env.example` to `.env` next to `config.py` — it is loaded
automatically:

```ini
MODEL_PROVIDER=nvidia
NVIDIA_API_KEY=nvapi-your-key-here
ADK_MODEL=qwen/qwen3-next-80b-a3b-instruct
```

If you see `GEMINI_API_KEY is not set`, `MODEL_PROVIDER` simply never reached the process — it is
read from the environment / `.env`, not hard-coded. (You can also change `DEFAULT_PROVIDER` in
`config.py` to `"nvidia"` to make NVIDIA the default with no env var at all.)

Any other OpenAI-compatible server (vLLM, Ollama, Together, …) works too:

```bash
export MODEL_PROVIDER=openai_compatible
export OPENAI_API_BASE=http://localhost:8000/v1
export OPENAI_API_KEY=sk-...
export ADK_MODEL=qwen2.5-72b-instruct
```

#### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ServiceUnavailableError ... 503 ResourceExhausted: Worker local total request limit reached` | The hosted model is out of capacity right now — nothing wrong with your key or code. The client already retries (`LLM_RETRIES`, default 5); if it persists switch models or set `ADK_FALLBACK_MODELS="meta/llama-3.3-70b-instruct,nvidia/llama-3.3-nemotron-super-49b-v1.5"` to fail over automatically. |
| `NotFoundError: 404 page not found` (plain text, not JSON) | The **base URL** is wrong — it must end with `/v1` (`https://integrate.api.nvidia.com/v1`). Run `python check_model.py`. |
| `404 {"detail": "model not found"}` | The **model id** doesn't exist on that endpoint. Model ids change often (e.g. there is no `deepseek-ai/deepseek-v3.1` on build.nvidia.com — it lists `deepseek-ai/deepseek-v4-flash` / `-v4-pro`). `python check_model.py` prints the live list. |
| `GEMINI_API_KEY is not set` while using NVIDIA | `MODEL_PROVIDER` didn't reach the process — put it in `.env` or `export` it. |
| `401 / invalid api key` | Key belongs to a different provider, or was pasted with a trailing space. |
| Agents talk about fixes but nothing changes | The chosen model does not support tool calling — switch models. |

> **Pick a model that supports tool / function calling.** All four agents call tools; a model
> without function-calling support will describe what it *would* do instead of doing it.
> Known-good on NVIDIA: `qwen/qwen3-next-80b-a3b-instruct`, `meta/llama-3.3-70b-instruct`,
> `nvidia/llama-3.3-nemotron-super-49b-v1.5`. Under Gemini use `gemini-2.0-flash` or newer.

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

  --- BEFORE this iteration: 4 issue(s) ---
      orphan_order:103             Order 103 references a customer that does not exist ...
      orphan_order_item:1004       order_item 1004 references missing product 77 ...
      negative_stock:13            Product 13 has negative stock (-4).
      price_mismatch:1005          order_item 1005 unit_price 12.0 != product price 45.5.
  [detector_agent] -> tool detect_issues({})
  [planner_agent] [{"step": 1, "issue_type": "orphan_order", "primary_key": 103, ...}, ...]
  [fixer_agent] -> tool apply_fix({'issue_type': 'negative_stock', 'primary_key': 13})
  [fixer_agent] <- apply_fix negative_stock:13  (1 row(s) changed)
                 sql   : UPDATE products SET stock = 0 WHERE id = 13
                 before: [{'id': 13, 'name': 'Laptop Stand', 'price': 39.0, 'stock': -4}]
                 after : [{'id': 13, 'name': 'Laptop Stand', 'price': 39.0, 'stock': 0}]
                 issues: 2 -> 1
  [validator_agent] -> tool count_issues({})
  [validator_agent] <- count_issues returned {'remaining': 0, 'clean': True}

  *** escalate raised by validator_agent - breaking the loop ***

  --- AFTER this iteration: 0 issue(s) ---
      (none)
  --- iteration delta: 4 resolved, 0 newly exposed ---
      RESOLVED  negative_stock:13
      RESOLVED  orphan_order:103
      RESOLVED  orphan_order_item:1004
      RESOLVED  price_mismatch:1005

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
| `.env.example` | template for `MODEL_PROVIDER` / API keys (copy to `.env`) |
| `check_model.py` | preflight: is the endpoint reachable and the model id real? |
| `config.py` | shared paths + pluggable model provider (Gemini / NVIDIA / OpenAI-compatible) |
| `db/seed_sqlite.py` | idempotent seed with 4 intentional defects |
| `graph/build_kuzu.py` | graph modelling + `sync_from_sqlite()` |
| `tools/quality_tools.py` | `detect_issues` / `count_issues` / `apply_fix` as ADK `FunctionTool`s |
| `agents/agent.py` | the four agents, `SequentialAgent`, `LoopAgent`, `escalate` |
| `main.py` | ADK `Runner` + in-memory session + per-iteration logging |
