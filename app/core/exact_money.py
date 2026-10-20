"""Bounded decimal sale facts. No binary floats or display rounding in storage.

Nullable text companions preserve new facts; untouched legacy rows retain their
original Numeric interpretation. SQL arithmetic on these facts uses decimal
functions too, so grouped totals cannot quietly fall back to SQLite REAL SUM.
"""
from decimal import Decimal, InvalidOperation, localcontext
import sqlite3

from sqlalchemy import Text, event, func, literal, type_coerce
from sqlalchemy.engine import Engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import operators
from sqlalchemy.sql.functions import sum as sql_sum
from sqlalchemy.sql.elements import UnaryExpression
from sqlalchemy.types import TypeDecorator


def decimal_value(value) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('金额必须是有效十进制数') from exc
    if not number.is_finite():
        raise ValueError('金额必须是有限数值')
    with localcontext() as ctx:
        ctx.prec = 50
        normalized = number.normalize()
    if (len(normalized.as_tuple().digits) > 30 or normalized.adjusted() >= 20
            or normalized.as_tuple().exponent < -10):
        raise ValueError('金额超出支持范围（最多20位整数、10位小数、30位有效数字），未保存或舍入')
    return number


def money_text(value) -> str:
    number = decimal_value(value)
    if not number:
        return '0'
    return format(number, 'f').rstrip('0').rstrip('.') if '.' in format(number, 'f') else format(number, 'f')


def price_value(value) -> Decimal:
    number = decimal_value(value)
    with localcontext() as ctx:
        ctx.prec = 50
        normalized = number.normalize()
    if number < 0 or normalized.as_tuple().exponent < -6 or number >= Decimal('1e12'):
        raise ValueError('单价须为非负数、小于一万亿元且最多6位小数；请核对原始报价，系统不会自动舍入')
    return number


def multiply_money(*values) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 50
        result = Decimal(1)
        for value in values:
            result *= decimal_value(value)
        return decimal_value(result)


def sum_money(values) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 50
        return decimal_value(sum((decimal_value(value) for value in values), Decimal(0)))


def exact_json(value):
    if isinstance(value, Decimal):
        return money_text(value)
    if isinstance(value, dict):
        return {key: exact_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [exact_json(item) for item in value]
    return value


class ExactDecimalText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else money_text(value)

    def process_result_value(self, value, dialect):
        return None if value is None else decimal_value(value)

    class comparator_factory(TypeDecorator.Comparator):
        def asc(self):
            return UnaryExpression(self.expr.collate('TM_DECIMAL'), modifier=operators.asc_op)

        def desc(self):
            return UnaryExpression(self.expr.collate('TM_DECIMAL'), modifier=operators.desc_op)

        def operate(self, op, *other, **kw):
            names = {operators.add: 'add', operators.sub: 'sub', operators.mul: 'mul'}
            comparisons = {operators.eq: 0, operators.ne: 0, operators.lt: -1,
                           operators.le: -1, operators.gt: 1, operators.ge: 1}
            if len(other) == 1 and other[0] is not None and (op in names or op in comparisons):
                right = other[0]
                if not hasattr(right, '__clause_element__'):
                    right = literal(right, type_=ExactDecimalText())
                if op in names:
                    return type_coerce(getattr(func, 'tm_decimal_' + names[op])(self.expr, right), ExactDecimalText())
                comparison = func.tm_decimal_compare(self.expr, right)
                return op(comparison, 0)
            return super().operate(op, *other, **kw)


def read_exact(instance, legacy_name, exact_name):
    value = getattr(instance, exact_name)
    return value if value is not None else getattr(instance, legacy_name)


def set_exact(instance, number, legacy_name, exact_name, *, price=False):
    number = price_value(number) if price else decimal_value(number)
    setattr(instance, exact_name, number)
    setattr(instance, legacy_name, number)


def exact_expression(cls, legacy_name, exact_name):
    legacy = getattr(cls, legacy_name)
    scale = legacy.property.columns[0].type.scale
    return type_coerce(func.coalesce(getattr(cls, exact_name),
        func.tm_decimal_legacy(legacy, scale)).collate('TM_DECIMAL'), ExactDecimalText()).label(
            legacy.property.columns[0].name)


class _DecimalSum:
    def __init__(self):
        self.total = Decimal(0)

    def step(self, value):
        if value is not None:
            self.total = sum_money((self.total, value))

    def finalize(self):
        return money_text(self.total)


@event.listens_for(Engine, 'connect')
def _register_decimal_sql(connection, _record):
    if not isinstance(connection, sqlite3.Connection):
        return
    connection.create_aggregate('tm_decimal_sum', 1, _DecimalSum)
    # Match the existing SQLAlchemy Numeric result processor for untouched
    # history. Reading a raw REAL via CAST(TEXT) could disagree with the old
    # two/three/six-place ORM read, changing a total when another line is edited.
    connection.create_function('tm_decimal_legacy', 2,
        lambda value, scale: None if value is None else
            money_text(Decimal(('%%.%df' % int(scale)) % value)), deterministic=True)
    connection.create_collation('TM_DECIMAL', lambda a, b:
        (decimal_value(a) > decimal_value(b)) - (decimal_value(a) < decimal_value(b)))
    def binary(operation, left, right):
        if left is None or right is None:
            return None
        with localcontext() as ctx:
            ctx.prec = 50
            a, b = decimal_value(left), decimal_value(right)
            result = {'add': lambda: a + b, 'sub': lambda: a - b, 'mul': lambda: a * b}[operation]()
            return money_text(result)
    for name in ('add', 'sub', 'mul'):
        connection.create_function('tm_decimal_' + name, 2,
            lambda a, b, operation=name: binary(operation, a, b), deterministic=True)
    connection.create_function('tm_decimal_compare', 2,
        lambda a, b: None if a is None or b is None else
            (decimal_value(a) > decimal_value(b)) - (decimal_value(a) < decimal_value(b)), deterministic=True)


@compiles(sql_sum, 'sqlite')
def _compile_decimal_sum(element, compiler, **kw):
    argument = next(iter(element.clauses))
    if isinstance(argument.type, ExactDecimalText):
        return 'tm_decimal_sum(' + compiler.process(argument, **kw) + ')'
    return compiler.visit_function(element, **kw)


class ExcelExactText(str):
    """A marked exact value, distinct from customer/product text codes."""


def excel_decimal(value):
    """Excel numerics support 15 significant digits; longer facts stay exact text."""
    text = money_text(value)
    digits = text.lstrip('-').replace('.', '').lstrip('0').rstrip('0')
    return ExcelExactText(text) if len(digits) > 15 else Decimal(text)


def write_excel_decimal(sheet, row, column, value):
    exact = excel_decimal(value)
    cell = sheet.cell(row, column, exact)
    if isinstance(exact, ExcelExactText):
        from openpyxl.comments import Comment
        cell.number_format = '@'
        cell.comment = Comment('超过Excel可精确计算的15位有效数字，按文本保留完整原值，未舍入。', '天明ERP')
    return cell
