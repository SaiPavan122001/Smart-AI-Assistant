# F1 — strict planner output-schema validation

## What was wrong

About 40 % of planner-driven tasks in Step 7 failed because the plan referenced fields no tool
produces (`steps.s2.data.summary`, `steps.s2.data.entries`, `…candidates`, `…text`).

Root cause:

* Only 9 tools described their output (a hand-written `returns` string); browser, desktop,
  terminal, vision, verification tools and adaptive steps had none, so the model guessed.
* `validate_plan` checked that a referenced *step* was earlier, not that the *field* existed.
  The error surfaced during execution, as a failed verification, and each re-plan could repeat it.
* Verification parameters were not validated at all (`browser_text` without `contains`,
  `element_state` with unknown keys).

## Single source of truth

Every tool declares its output as a pydantic model:

```python
class BrowserOpen(Tool[OpenInput]):
    name = "browser_open"
    Output = PageState          # shape of ToolOutput.data
    # Artifacts = ...           # shape of ToolOutput.artifacts, where a tool emits any
```

`Tool.spec()` derives `output_schema`, `artifacts_schema` and the compact `returns` text from that
model (`app/tools/schema.py`). The hand-written `returns` strings are gone. Three consumers use the
same schema:

| Consumer | Where | Use |
|---|---|---|
| Planner / re-planner context | `planner/prompts.py: tool_catalog` | each tool line ends with `-> data: {field:type, …}; artifacts: {…}; e.g. {{steps.sN.data.<field>}}` |
| Plan validation | `planner/references.py`, `planner/planner.py` | every reference is resolved against the schema before anything runs |
| Router contract check | `tools/router.py: _post_process` | what a tool actually returned must match its model; a mismatch is logged, and is an error when `env == "test"` |

Adaptive steps have a declared output too (`AdaptiveOutput`: `iterations`, `tool_calls`,
`summary`, `answer`), so `{{steps.s3.data.answer}}` is a valid way to use an adaptive result.

`tests/unit/test_plan_schema.py::test_every_registered_tool_declares_an_output_contract` fails if a
tool is added without `Output`.

## Field-path validation

`reference_type(path, …)` accepts `steps.<id>.<section>.<path>` where section is `data`,
`artifacts`, `summary` or `ok`:

* the step must be an earlier step (or, for a step's own `verify`, the step itself);
* in a re-plan, a prior step must have `SUCCEEDED` — failed, skipped and still-pending prior steps
  have no output to reference;
* the path is walked through the JSON schema: object properties, `list[0]` indexes, `.length` on
  lists and strings, mappings with free keys, nullable/optional fields.

A bad path produces a structured `PlanProblem` with the fields that do exist:

```
answer_template: {{steps.s1.data.summary}} does not exist: in the data of browser_open,
'the output' has no field 'summary'. Available fields: navigation, tab, tabs, title, url.
```

Optional fields (marked `?` in the catalogue, e.g. `browser_read.text` which depends on `mode`)
pass static validation; if one is absent at run time the answer is `answer_unavailable` (see F2)
and an argument/verification reference fails the step as `plan_invalid`, which goes straight to
one re-plan.

## Type compatibility

Minimal, deliberate rules (`references.compatible` / `embeddable`):

* a reference that is the whole argument must fit the parameter's declared type
  (`string` expected, `array` given → rejected; `integer` → `number` is fine; numeric text → number
  is allowed because input validation coerces it);
* a reference inside text (an argument string or `answer_template`) must be a scalar — a list or
  object is rejected with "reference one of its fields (or .length)";
* `value_check` with `gt/gte/lt` needs a numeric `actual`.

## Verification contracts

Each checker registers a parameter model (`@checker("browser_text", BrowserTextParams)` in
`verification/checks.py`). These models are the source for the planner's "Verification kinds"
list and for validation, at plan time and again at run time:

* unknown kind, unknown parameter (`extra="forbid"`), missing required parameter;
* enum values (`op`, `kind`), numeric operators with non-numeric operands, invalid regex;
* `wait_s` outside 0–30;
* placeholder expected text (`contains: " "`);
* `success_criteria` that can never hold together (several different `browser_url`/`browser_title`
  `equals` values — the criteria are all checked once against the final state), including against
  criteria already in force during a re-plan.

## Bounded repair

`Planner._plan`:

```
LLM plan -> validate_plan_detailed -> structured problems (JSON, with available fields)
         -> ONE repair call (purpose "repair") -> validate again -> PLAN_INVALID
```

`MAX_REPAIRS = 1`. A plan that is still invalid raises `AgentError(PLAN_INVALID)` with the
problems in `details`; nothing executes and no further model call is made. The same applies to a
re-plan. `usage.repair_calls` counts the repair rounds.

## Tests

`backend/tests/unit/test_plan_schema.py` — nonexistent and nested fields, type mismatches, valid
fields in every reference position, adaptive fields, re-plan references, invalid verification
contracts, catalogue = schema for every tool, real tool outputs validate against their models,
contract drift is caught, one repair works, a second invalid plan is `PLAN_INVALID`.

## Limits

* The check is static: it proves a field is *declared*, not that it will be non-empty.
* A recovered tool step (adaptive fallback) merges the last successful output of the planned tool;
  if no call of that tool succeeded, the referenced field is missing at run time.
