# Cross-stack adapters

> **What's proven vs what to verify.** The reference implementation is **lakehouse
> / PySpark on Databricks**: that code is tested. The **vendor-neutral core**
> (medallion layering, the repo split, 2-file gold tasks, `env`/`read_from`
> parameterization, the naming conventions, diff-based deploy, the quality gates)
> carries over to *every* stack unchanged. The **per-stack mappings below are
> candidates**: they reflect standard practice but you must **confirm each
> against the official docs for your stack before relying on it**. A `/deep-research`
> pass checks these specifically; citations are folded in as they land. The
> `Status` column flags how settled each mapping is.

## Master mapping

| Concept (reference) | Databricks (proven) | Snowflake + dbt | S3 + Apache Iceberg | Status |
|---|---|---|---|---|
| Compute / engine | PySpark | dbt (SQL) / Snowpark | Spark · DuckDB · Trino | core |
| Table identifier | `{env}_{layer}.{schema}.{table}` (Unity Catalog, 3-level) | `{ENV}_{LAYER}.{SCHEMA}.{TABLE}` (db.schema.table) | `catalog.namespace.table` (Glue/REST/Nessie) | verify |
| Storage / format | managed Delta | Snowflake native (or Iceberg tables) | Iceberg (or Delta) on object storage | verify |
| Layering | bronze→silver→gold dirs | dbt `staging → intermediate → marts` | same dir layering | candidate |
| Gold dim model | `tasks/dim`,`tasks/fact` | dbt `models/marts/` (dim_/fact_) | same | candidate |
| Reusable `ref/` logic | `tasks/ref/` | dbt `intermediate/` models (+ macros / semantic layer) | `tasks/ref/` | candidate |
| Incremental silver | Delta CDF + checkpoint table | **Streams + Tasks** (or dbt incremental) | snapshot id / watermark | verify |
| SCD2 history | `SCD2Processor` | **dbt snapshots** (`dbt_valid_from/to`) | MERGE-based snapshot | candidate |
| Surrogate key | `GoldTask` adds `{table}_id` | `dbt_utils.generate_surrogate_key()` | hash of business key | candidate |
| Per-table metadata | YAML sidecar | `schema.yml` (tests + descriptions + constraints) | table props + a tests file | candidate |
| Row-count validation | `validate_rows` | dbt test (`dbt_utils.equal_rowcount` / custom) | custom test | candidate |
| Orchestration DAG | `tasks.yaml` → Databricks Jobs | dbt `ref()` DAG / Airflow / Dagster | Airflow / Dagster | core |
| Bundle / deploy unit | Databricks Asset Bundle | dbt project (+ profiles) | Terraform-deployed job | verify |
| IaC | Terraform | Terraform | Terraform | core |
| Diff-based deploy | Azure dynamic matrix | GitLab `rules:changes` / child pipeline | same | verify |

## Snowflake + dbt
- **Layering ↔ dbt structure.** dbt's recommended project structure is
  `staging → intermediate → marts`; treat `staging` ≈ silver (clean, 1:1 with
  sources), `marts` ≈ gold dimensional model (`dim_`/`fact_`), and `intermediate`
  ≈ your `ref/` (reusable derived logic between staging and marts). *(Confirm
  against dbt's "How we structure our dbt projects".)*
- **Incremental ↔ Streams + Tasks.** A Snowflake **Stream** tracks change (CDC
  offset) on a table; a **Task** runs SQL on a schedule/trigger to consume it,
  together the native analogue of Delta CDF + a checkpoint table. dbt's own
  `materialized='incremental'` is the in-dbt alternative. *(Verify Stream offset
  semantics + Task scheduling.)*
- **SCD2 ↔ dbt snapshots.** A dbt **snapshot** implements SCD2 (strategies
  `timestamp` and `check`), adding `dbt_valid_from`/`dbt_valid_to`: use instead
  of a hand-rolled processor where you can.
- **Metadata sidecar ↔ `schema.yml`.** Map sidecar keys to dbt:
  `primary_keys`/`business_key` → `unique` + `not_null` tests (or a model
  `constraints` block); `foreign_keys` → the `relationships` test; `validate_rows`
  → `dbt_utils.equal_rowcount`/a custom test; `comments` → column `description` +
  `persist_docs`.
- **Surrogate key** → `dbt_utils.generate_surrogate_key([...])`.

## S3 + Apache Iceberg (or Delta)
- An **open table format on object storage** is the analogue of managed Delta:
  ACID, **schema evolution**, **hidden partitioning**, **time travel**. The table
  is addressed as **`catalog.namespace.table`**, where a **catalog** (AWS Glue, a
  REST catalog, or Nessie) resolves names to metadata. *(Verify the catalog you
  pick, capabilities differ.)*
- Engines that read/write Iceberg: **Spark**, **Trino/Presto**, **DuckDB**,
  Flink. The task code shape (`etl(...) -> DataFrame`, the 2-file pairing) is
  unchanged; the read/write calls and the FQN change.
- Incremental: use Iceberg **snapshots / incremental reads** (changelog scan) as
  the CDF analogue; store the last snapshot id as your checkpoint.

## CI/CD
- **GitLab.** `rules:changes:` scopes a job to paths (per-bundle validation);
  `needs:` builds the DAG; `parallel:matrix:` fans out. A *fully data-driven*
  "deploy exactly the bundles that changed" matrix usually needs a **generated
  child pipeline** (emit a `.gitlab-ci.yml` artifact, then `trigger: include:`).
  Promotion: `environment:` + `rules:if: $CI_COMMIT_BRANCH == "main"` +
  `when: manual`. *(Verify child-pipeline + rules:changes interaction.)*
- **GitHub Actions.** `on: push: paths:` (or `dorny/paths-filter`) → a job
  `matrix`; `environment:` protection rules for approvals; gate prod on
  `github.ref == 'refs/heads/main'`.
- **Azure (reference).** A diff step computes changed bundles → a dynamic
  `strategy: matrix`; stage `condition`s gate uat/prd on `main`; environment
  approvals before prd. (See `templates/data-platform/cicd/azure-pipeline.yaml`.)

## How to verify a mapping
Before porting a pattern, read the **official doc** for the exact construct
(dbt docs, Snowflake docs, Apache Iceberg docs, GitLab CI docs): a wrong
assumption here type-checks and fails only at runtime, which is exactly what this
skill exists to prevent. Update the `Status` column to `confirmed` (with the doc
link) once you've checked it for your platform.
