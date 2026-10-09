# Workbench UI tests

From this directory run `npm ci`, then `npm run test:workbench`.
The pinned DOM dependency is used only in isolated Node tests and is never
loaded by the ERP browser or included in the formal runtime.
