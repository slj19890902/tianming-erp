"""Browser-DOM safety guards for the inline Vue application template."""

from pathlib import Path
import re
import subprocess


INDEX = (
    Path(__file__).resolve().parents[1] / "static" / "index.html"
).read_text(encoding="utf-8")


def test_complete_vue_template_compiles_with_shipped_runtime() -> None:
    root = Path(__file__).resolve().parents[1]
    subprocess.run(["node", str(root / "tests/check_index_vue_template.cjs")], cwd=root, check=True, capture_output=True, text=True)


def test_visible_vue_expressions_do_not_contain_raw_less_than_signs() -> None:
    """A raw ``<`` in mustache text is parsed as markup before Vue compiles it."""

    expressions = re.findall(r"\{\{(.*?)\}\}", INDEX, flags=re.DOTALL)
    unsafe = [expression.strip() for expression in expressions if "<" in expression]

    assert unsafe == [], (
        "Raw '<' inside visible Vue expressions can make the browser repair the DOM "
        f"and blank the whole application: {unsafe}"
    )


def test_acceptance_balance_comparison_uses_browser_safe_direction() -> None:
    safe = "Number(row.amount)>Number(item.available_payment_amount)"
    unsafe = "Number(item.available_payment_amount)<Number(row.amount)"

    assert unsafe not in INDEX
    assert INDEX.count(safe) == 2
