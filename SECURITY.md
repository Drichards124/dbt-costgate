# Security Policy

## Supported versions

`dbt-costgate` is pre-1.0. Security fixes are applied to the latest released
`0.x` minor version. Once `1.0` ships, this policy will name a support window.

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.**

Report privately using **GitHub's private vulnerability reporting**:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability**.
3. Provide a description, affected version(s), and reproduction steps.

You will receive an acknowledgement within **5 business days**. We will keep you
informed of progress toward a fix and public disclosure, and will credit you in
the release notes unless you ask otherwise.

## Scope

`dbt-costgate` runs in CI next to warehouse credentials, so we treat its attack
surface seriously. By design:

- The only warehouse interaction is BigQuery **dry-run** jobs (`dryRun=true`) —
  free, nothing executed, no table data read. Any way to make dbt-costgate issue a
  billable or data-reading query is a vulnerability; please report it.
- dbt-costgate never accepts, stores, or logs credentials. Authentication is
  delegated to Google's Application Default Credentials chain. Any code path
  that surfaces a token or key material is a vulnerability.
- Reports (PR comments, terminal output, JSON) intentionally exclude compiled
  SQL, because compiled SQL can embed secrets templated via `env_var()`/vars.
  Any way for secret material to reach a report without explicit opt-in is a
  vulnerability.
- There is no telemetry and no phone-home. Any network call other than the
  BigQuery API is a vulnerability.

Also in scope: workflow patterns in our documentation that would expose
secrets to fork pull requests (`pull_request_target` misuse), and
dependency-introduced vulnerabilities anywhere in the build.

Deliberately out of scope: the accuracy of Google's published pricing (the
bundled table is best-effort and every report discloses the rate used), and
vulnerabilities in dbt or the BigQuery service itself.
