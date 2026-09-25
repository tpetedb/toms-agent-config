---
name: gitlab-ci
description: "Optional, for GitLab projects: GitLab CI concepts and conventions translated from GitHub Actions and Azure DevOps. Use when writing or reasoning about .gitlab-ci.yml, pipelines, jobs, stages, environments, review apps, JUnit reports, parallel matrix jobs, runners or CI/CD variables."
metadata:
  source: "the owner's dotfiles toolbox, config/claude/skills/gitlab-ci/SKILL.md"
  author: "the owner"
  licence: "MIT, the owner's own work"
  changes: "marked optional in the description; three em dashes became commas; otherwise as it was"
---

# GitLab CI, translated

Reference for building GitLab CI pipelines when your mental model comes from
GitHub Actions or Azure DevOps. It covers the features that carry most
pipelines, and the gotchas that cost an afternoon each.

## Pipeline building blocks (with GH / Azure equivalents)

- A pipeline is the whole run. Like a GitHub Actions workflow run or an Azure
  pipeline run.
- A stage is an ordered phase (`validate`, `deploy`, `cleanup`). Jobs in the
  same stage run in parallel; the next stage waits. Azure has stages too;
  GitHub approximates this with `needs`.
- A job is a unit of work that runs in a container (`image:`) on a runner.
  Like a GitHub job or an Azure job. The `script:` block is the commands.
- A runner is the machine/executor that runs the job. Equivalent to a GitHub
  runner or Azure agent. If a job must reach a network-restricted backend,
  the runner's egress IP must be allowlisted or the connection hangs.

## The features most pipelines rely on

### rules: run only on merge requests

```yaml
rules:
  - if: $CI_MERGE_REQUEST_IID
```

Gates the job so it only runs in MR pipelines. `CI_MERGE_REQUEST_IID` is the
per-MR number, useful for naming ephemeral resources.

### environment + on_stop + auto_stop_in (the "review app" pattern)

```yaml
environment:
  name: review/$CI_COMMIT_REF_SLUG
  on_stop: stop-review
  auto_stop_in: 1 day
```

A dynamic environment is created by the pipeline and torn down later. The
environment does not have to be a website: the same machinery manages any
throwaway resource per merge request (a database schema, a namespace, a
stack); the `url` is decorative for those. `on_stop` names the cleanup job
(`action: stop`, usually `when: manual`, and it runs when the MR closes).
`auto_stop_in` is the final safety net. Add
`resource_group: review/$CI_COMMIT_REF_SLUG` to serialize runs for the same
branch so two pipelines cannot collide on the same resource.

### artifacts:reports:junit (the visible result)

```yaml
artifacts:
  when: always
  reports:
    junit: ci/report.xml
```

GitLab parses the JUnit XML and shows per-test pass/fail in the MR widget.
`when: always` ensures the report uploads even when tests fail, exactly when
you want it. pytest emits JUnit with `--junitxml`.

### parallel:matrix (fan-out)

```yaml
parallel:
  matrix:
    - GROUP: [sql, python, notebooks]
```

Spawns one job per value; give each its own resource suffix so concurrent
jobs do not collide. GitLab aggregates JUnit across all of them
automatically. Comparable to a GitHub or Azure matrix strategy.

### CI/CD variables and scoping

- Variables are set in project (or group) settings, not in code.
- They can be environment-scoped (different value for `test` vs `prod`).
- Masked hides the value in job logs; Protected restricts it to protected
  branches/tags.
- A File-type variable writes its content to a temp file and exposes the
  path, the way to deliver a private key to a job.
- Group-level variables are inherited by projects in that group, NOT by a
  project in a personal namespace.

### Container registry

`$CI_REGISTRY_IMAGE` resolves to the current project's own registry path. A
sandbox project's registry is isolated from the real project's images, so
building/pulling in a sandbox cannot affect production images.

## Gotchas to respect

- Browser-based SSO auth cannot work in CI (no human browser). Use keypair or
  token auth for any backend a job logs into.
- A job that cannot reach a network-restricted backend hangs rather than
  failing fast; suspect the IP allowlist/network policy first.
- Keep MR pipeline wall-clock low. If it grows past a few minutes, lean on
  parallel groups or gate heavy work behind a manual/nightly job.
