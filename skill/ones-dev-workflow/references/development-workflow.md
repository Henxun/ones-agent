# Defect repair and requirement implementation

Use this workflow when the user wants the current Agent to work in the current project.

1. Fetch the exact normalized defect or requirement with the bridge.
2. Read repository instructions and locate relevant code and tests.
3. For defects, establish reproduction or concrete code evidence before claiming a root cause. For requirements, translate acceptance criteria and linked evidence into an implementation plan.
4. Make only in-scope changes in the current workspace.
5. Run proportionate tests, lint, and build checks.
6. Report changed files, validation, risks, and unresolved evidence. Do not mutate ONES or Git remotes implicitly.

Treat defect prose alone as insufficient for a high-confidence root cause. Distinguish verified facts, inferences, and unknowns.

The standalone client is intentionally read-only. It has no commit, push, PR, comment, or status-transition API. Any later Git or ONES side effect requires a separate user request and the tools available in the target project.
