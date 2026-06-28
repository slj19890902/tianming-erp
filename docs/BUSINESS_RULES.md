# Business Rules

## Company Info

- Company info is an admin-maintained system setting.
- Only admins can view or edit it.
- Blank company names are invalid.

## Delivery Print

- Delivery print uses company sender information from the backend.
- If sender info is missing, the UI should fall back to the existing display values.

## Data Safety

- This release does not alter historical orders.
- This release does not modify `legacy_*` tables.
- This release does not run any migration against the formal database.
