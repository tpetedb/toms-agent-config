---
name: data-platform
description: "Optional, for data-platform repositories only: medallion (bronze, silver, gold) ETL with the two-file task pattern, a .py and its .yaml sidecar, and {env}_{layer} table names. Use when adding an ingestion job or a gold ref, dim or fact table, wiring orchestration or scaffolding a pipeline. Not for ad-hoc SQL."
metadata:
  source: "the owner's dotfiles toolbox, config/claude/skills/data-platform/SKILL.md, with reference/architecture.md and reference/adapters.md"
  author: "the owner"
  licence: "MIT, the owner's own work"
  changes: "marked optional in the description, which was shortened for the listing budget; em dashes replaced by colons and commas; the toolbox scaffold command and a few domain example names made generic; otherwise as it was"
---

# Data platform: the way of working

A field-tested pattern for a maintainable analytics platform, distilled from a
production medallion ETL platform. The **architecture, conventions, and file
layout here are vendor-neutral and authoritative**: follow them exactly. The
**code samples are the proven PySpark/lakehouse reference implementation**; to
target Snowflake+dbt, S3+Iceberg, or a different CI, translate them via
[`reference/adapters.md`](reference/adapters.md) (each adapter is marked with how
well official docs back it, don't assume, verify).

## Golden rules (anti-hallucination)
1. **Copy structure, don't invent it.** A gold table is *always* exactly two
   files. A job *always* has the `src/{landing,bronze,silver}/` shape. Mirror an
   existing neighbour in the repo (or the project's own scaffold).
2. **Never hardcode an environment or a catalog.** Thread `env` (target) and
   `read_from` (source; defaults to prod data) through every function. No literal
   `dev_`/`prd_` anywhere.
3. **Always join on the tenant key.** Multi-tenant data (here `country_code`)
   must be in every join condition, or you silently fan out rows across tenants.
4. **Config is data.** Keys, FKs, validations, and column docs live in the YAML
   sidecar, not buried in code.
5. **Verify the framework API** (`GoldTask`, `SCD2Processor`, the `Task` base) in
   the shared library before calling it; don't guess method names.

---

## Architecture (the repo layout)

Split by **concern**, one repo (or one top-level dir in a monorepo) each. Detail
+ the per-repo uniform shape + polyrepo-vs-monorepo notes are in
[`reference/architecture.md`](reference/architecture.md).

| Concern | Holds | Medallion role |
|---|---|---|
| **ingestion / jobs** | one *bundle* per source system | landing → bronze → silver |
| **transformation / gold** | dimensional model: `tasks/{ref,dim,fact,agg}/` | gold |
| **library** | shared framework package (`Task` base, `GoldTask`, processors, utils) | all |
| **infrastructure** | Terraform (`services/terraform` + `services/variables`) | none |
| **metastore / monitoring** | catalog/grants, data-quality + cost monitoring | none |
| **cicd** | reusable pipeline templates shared by every repo | none |
| **bi** | reports + semantic models as code | consumption |

**Medallion layers** (data quality increases left→right):
- **landing**: raw payloads land as-is (files/API responses); nothing reshaped.
- **bronze** (`bnz`): raw kept verbatim + ingestion metadata; the audit archive.
- **silver** (`slv`): deduplicated, schema-enforced, merged "enterprise view";
  one table per source-system table; ready for ad-hoc analysis/ML.
- **gold** (`gld`): consumption-ready, business rules applied, modelled as
  `ref` (reusable derived logic) / `dim` (entities) / `fact` (events) / `agg`.

---

## Naming conventions (memorise these)

**Tables:** `{env}_{layer}.{schema}.{table}`
- `env` ∈ {`dev`, `uat`, `prd`} (or `dev`/`staging`/`prd`)
- `layer`: source catalog like `app`, `crm`, `static` (bronze/silver) or
  `gold_analytics` (gold)
- `schema`: `silver` for sources; `ref`/`dim`/`fact`/`agg` for gold
- e.g. `dev_app.silver.client`, `prd_gold_analytics.dim.client`

**Columns:** `<object>_<attribute>_<argument>_<descriptor>`

| part | meaning | examples |
|---|---|---|
| object | entity it belongs to | `client`, `supplier`, `deal`, `job` |
| attribute | core attribute | `status`, `name`, `revenue`, `hours` |
| argument | *(optional)* condition | `accepted`, `completed`, `invoiced` |
| descriptor | *(optional)* type/agg | `first`, `last`, `sum`, `avg`, `count`, `date`, `datetime`, `flag` |

e.g. `client_status_active_flag`, `supplier_hours_invoiced_sum`,
`deal_match_first_date`. Surrogate keys are `{table}_id` and are **auto-added by
the framework**: never create them by hand.

---

## Canonical templates: copy these

> Reference implementation = PySpark on a lakehouse. The **shape** (functional
> `etl(...) -> DataFrame` + thin `main()`, the 2-file pairing, the YAML keys) is
> the part to preserve on any stack. `dataplatform` = your shared-library package.

### Gold task: standard (`tasks/{schema}/<table>.py`)
```python
from pyspark.sql import DataFrame, SparkSession
import pyspark.sql.functions as F
from dataplatform.gold.gold_task import GoldTask

DESTINATION_TABLE = "client"
DESTINATION_SCHEMA = "dim"  # ref | dim | fact | agg


# fmt: off
def etl(spark: SparkSession, read_from: str, env: str) -> DataFrame:
    # read_from → source silver (defaults to prod data); env → gold deps + target
    source  = spark.table(f"{read_from}_app.silver.client")
    ref_dep = spark.table(f"{env}_gold_analytics.ref.client_dates")
    df = (source
        .join(ref_dep,
              [source.id == ref_dep.client_id,
               source.country_code == ref_dep.country_code],   # ALWAYS the tenant key
              "left")
        .select(
            F.col("country_code"),
            F.col("id").alias("client_id"),
            F.col("name").alias("client_name"),
        ))
    return df
# fmt: on


def main() -> None:
    gold_task = GoldTask()
    gold_task.write_data(
        df=etl(gold_task.spark, gold_task.read_from, gold_task.env),
        table_name=DESTINATION_TABLE,
        schema=DESTINATION_SCHEMA,
    )
```
Use `# fmt: off`/`# fmt: on` to keep `select(...)` alignment readable.

### Gold metadata sidecar (`tasks/{schema}/<table>.yaml`): same basename
```yaml
table_metadata:
  table_description: One-line description of what this table is.
  primary_keys: [dim_client_id]              # the auto-generated surrogate key
  business_key: [client_id, country_code]    # optional: errors on duplicates
  foreign_keys:
    ref.client_dates: [client_id, country_code]
  validate_rows:                             # optional: row count vs source
    source_table: "{env}_app.silver.client"
    tolerance: 0.01                          # 1%
  comments:
    country_code: Country code (NL, GB, DE, …)
    client_id: Original ID from the source system
    client_name: Display name of the client
```

### Gold task: SCD2 (history): `<table>_hist`
Use `SCD2Processor` (adds `effective_datetime`, `end_datetime`, `is_current_flag`)
when you need point-in-time history. Loop one snapshot per day from the last
processed date. Full template + the incremental date-range helper are in the
scaffold's `transformation/tasks/ref/_example_status_scd2.py`.

### Ingestion job (bronze/silver): the `src/<layer>/` shape
`src/<layer>/main.py` is a thin entrypoint; `src/<layer>/<layer>.py` is a class
extending the library `Task` base:
```python
# src/silver/main.py
from silver.silver import SilverClientProcessor


def main() -> None:
    SilverClientProcessor().launch()


if __name__ == "__main__":
    main()
```
```python
# src/silver/silver.py
from pyspark.sql import DataFrame, SparkSession
from dataplatform.core.task import Task
from dataplatform.utils.metadata_utils import add_metadata_columns


class SilverClientProcessor(Task):
    def __init__(self, spark: SparkSession | None = None):
        super().__init__(spark=spark)  # gives self.spark, self.logger, self.env, args
        self.source_table_fqn = f"{self.env}_app.bronze.client"
        self.target_table_fqn = f"{self.env}_app.silver.client"

    def _transform_data(self, df: DataFrame) -> DataFrame:
        df = df.dropDuplicates()  # silver = dedup + schema-enforce
        return add_metadata_columns(df, run_datetime=self.task_info.task_start_datetime)

    def launch(self) -> None:  # the one abstract method
        self.logger.info("Silver client → %s", self.target_table_fqn)
        df = self.spark.read.table(self.source_table_fqn)
        self._transform_data(df).writeTo(self.target_table_fqn).createOrReplace()
```
Incremental silver reads only new source versions (Change Data Feed) and stores a
checkpoint, see an existing silver job in the repository.

### Orchestration (`transformation/orchestration/tasks.yaml`)
A DAG of `schema → table: [deps]`, ordered alphabetically inside each schema:
```yaml
tasks:
  ref:
    - client_dates: []
    - client_status: [client_dates]
  dim:
    - client: [client_dates]
  fact:
    - order: []
```

---

## Gold layer guidelines (where does logic go?)
- **`ref/`**: centralised reusable/derived logic (first/last dates, status
  calcs, object-level aggregations, SCD2 history `*_hist`/`*_scd2`). Add here when
  logic is **reused across ≥2 tables** or needs a **consistent platform-wide
  definition**. Check for an existing `ref` before adding a new calculation,
  prioritise reuse and clarity over case-specific exceptions.
- **`dim/`**: descriptive attributes of business entities (`client`, `supplier`,
  `company`, `deal`, `job`).
- **`fact/`**: measurable events/transactions (`order`, `shipment`, `invoices`).
- **`agg/`**: pre-aggregated marts for reporting.

**Adding a gold table:** create the 2 files in `tasks/{schema}/`, add the entry to
`orchestration/tasks.yaml` (alphabetical, with deps), and add a row-count
`validate_rows` test if it should match a source's row count.

---

## Deployment & quality

**Config-as-deploy:** each bundle has a manifest that `include`s shared
`_common/` config (targets, clusters/warehouses, libraries, permissions,
notifications, schedule) + its own `resources/*.yaml`. Folders starting with `_`
are shared, not deployable.

**Diff-based ("smart") deploy:** only bundles with a change are deployed, on a PR,
diff the branch vs `origin/main`; after merge, diff `HEAD` vs `HEAD^`. A manual
run can target specific bundles. Promotion is `dev → uat → prd`, with uat/prd
gated on `main`. (CI mechanics per system in [`reference/adapters.md`](reference/adapters.md).)

**Quality gates (CI):** format · lint · type-check · security-scan, all required.
The reference platform used **black (line length 120) · pylint · mypy · bandit**
with type hints mandatory and Google-style docstrings on library functions. Match
whatever the repo already configures; keep all four gate categories.

**Local dev:** filter early to one tenant while developing
(`.filter((F.col("client_id") == 123) & (F.col("country_code") == "NL"))`); set
`sys.argv` to pass run args when testing a task in a notebook; remove both before
the PR.

---

## Common pitfalls
1. Hardcoding `dev_`/`prd_` instead of `env`/`read_from`.
2. Forgetting the tenant key in a join (silent row fan-out).
3. Hand-creating a `{table}_id` (the framework adds the surrogate key).
4. SCD2 table/name not ending in `_hist`.
5. Missing the YAML sidecar, or letting `.py`/`.yaml` basenames drift apart.
6. Adding a gold table but forgetting to register it in `orchestration/tasks.yaml`.

---

## Other stacks
The above is the lakehouse/PySpark reference. For **Snowflake + dbt**, **S3 +
Apache Iceberg**, or **GitLab/GitHub CI**, the layering, the 2-file (model + YAML)
discipline, the naming, and diff-deploy all carry over, but the constructs have
different names and some analogies break. Read
[`reference/adapters.md`](reference/adapters.md) before porting; it labels each
mapping as confirmed / partial / unverified against official docs.
