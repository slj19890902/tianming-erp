"""Required database structure derived from authenticated release model sources.

Release code is untrusted input here: parse source bytes with ``ast`` and never
import or execute them. Extracted release directories are mutable caches and
are deliberately not consulted.
"""
from __future__ import annotations

import ast
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat
from typing import Mapping
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from desktop_assistant.storage import safe_name


CONTRACT_VERSION = 1
CONTRACT_SOURCE = "sqlalchemy_models_ast_v1"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MODEL_PREFIX = "app/models/"
_MODEL_LIMIT = 32 * 1024 * 1024
_STRUCTURAL_NAMES = {"Base", "DeclarativeBase", "Column", "Mapped", "mapped_column", "relationship"}


def _parse(path: str, raw: bytes) -> ast.Module:
    try:
        source = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"签名程序模型源码不是UTF-8：{path}") from error
    try:
        return ast.parse(source, filename=path)
    except SyntaxError as error:
        raise ValueError(f"签名程序模型源码无法静态解析：{path}") from error


def _module_path(module: str) -> str | None:
    if not module.startswith("app.models."):
        return None
    suffix = module.removeprefix("app.models.")
    if not suffix or any(not _IDENTIFIER.fullmatch(part) for part in suffix.split(".")):
        raise ValueError("签名程序包含无法确认的模型模块")
    return "app/models/" + suffix.replace(".", "/") + ".py"


def _inside_type_checking(tree: ast.Module, target: ast.AST) -> bool:
    for node in ast.walk(tree):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                and node.test.id == "TYPE_CHECKING" and target in set(ast.walk(node))):
            return True
    return False


def _registered_modules(sources: Mapping[str, bytes]) -> tuple[set[str], dict[str, ast.Module]]:
    entry = "app/models/__init__.py"
    if entry not in sources:
        raise ValueError("签名程序缺少模型注册入口，无法确认必要结构")
    trees = {path: _parse(path, raw) for path, raw in sources.items()}
    reachable = {entry}
    pending = [entry]
    while pending:
        path = pending.pop()
        tree = trees[path]
        top_level_imports = {id(node) for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                if name in {"__import__", "import_module"}:
                    raise ValueError(f"签名程序模型使用动态导入，无法确认必要结构：{path}")
            if not isinstance(node, (ast.Import, ast.ImportFrom)) or id(node) in top_level_imports:
                continue
            modules = ([item.name for item in node.names] if isinstance(node, ast.Import)
                       else ([node.module] if node.module else []))
            if (any(module.startswith("app.models.") for module in modules)
                    and not _inside_type_checking(tree, node)):
                raise ValueError(f"签名程序模型使用条件导入，无法确认必要结构：{path}")
        for node in tree.body:
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                if any(item.name == "*" for item in node.names):
                    raise ValueError(f"签名程序模型使用星号导入，无法确认必要结构：{path}")
                if node.level:
                    if node.level != 1 or not node.module:
                        raise ValueError(f"签名程序模型使用无法确认的相对导入：{path}")
                    modules = ["app.models." + node.module]
                elif node.module:
                    modules = [node.module]
            for module in modules:
                target = _module_path(module)
                if target is None:
                    continue
                if target not in sources:
                    raise ValueError(f"签名程序缺少已注册模型模块：{target}")
                if target not in reachable:
                    reachable.add(target)
                    pending.append(target)
    extra = set(sources) - reachable
    if extra:
        raise ValueError("签名程序含未注册模型模块，无法确认必要结构：" + ", ".join(sorted(extra)[:5]))
    return reachable, trees


def _assigned(statement: ast.stmt) -> tuple[str | None, ast.expr | None, ast.expr | None]:
    if isinstance(statement, ast.Assign):
        if len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
            return statement.targets[0].id, statement.value, None
        return None, None, None
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        return statement.target.id, statement.value, statement.annotation
    return None, None, None


def _call_name(value: ast.expr | None) -> str | None:
    return value.func.id if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) else None


def _mapped_annotation(annotation: ast.expr | None) -> bool:
    return (isinstance(annotation, ast.Subscript)
            and isinstance(annotation.value, ast.Name)
            and annotation.value.id == "Mapped")


def _assigned_name(statement: ast.stmt) -> str | None:
    name, _value, _annotation = _assigned(statement)
    return name


def _reject_structural_aliases(path: str, tree: ast.Module) -> None:
    """Reject aliases that could make a mapped table or column invisible to the grammar."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for item in node.names:
                if item.name in _STRUCTURAL_NAMES and item.asname not in {None, item.name}:
                    raise ValueError(f"模型结构符号使用别名，无法静态确认：{path}:{item.name}")
        elif isinstance(node, ast.Import):
            for item in node.names:
                if item.name in {"sqlalchemy", "sqlalchemy.orm", "app.models"}:
                    raise ValueError(f"模型结构模块导入无法静态确认：{path}:{item.name}")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            _name, value, _annotation = _assigned(node)
            if _name in _STRUCTURAL_NAMES:
                raise ValueError(f"模型结构符号被重新定义，无法静态确认：{path}:{_name}")
            if isinstance(value, ast.Name) and value.id in _STRUCTURAL_NAMES:
                raise ValueError(f"模型结构符号使用运行时别名，无法静态确认：{path}:{value.id}")


def _validate_base(trees: Mapping[str, ast.Module]) -> None:
    """The supported ERP grammar has exactly one empty DeclarativeBase.

    Inherited columns/factories require a new reviewed contract version, not a
    best-effort projection that quietly forgets their database requirements.
    """
    definitions = [(path, node) for path, tree in trees.items() for node in ast.walk(tree)
                   if isinstance(node, ast.ClassDef) and node.name == "Base"]
    if len(definitions) != 1:
        raise ValueError("模型Base必须具有唯一静态定义")
    path, base = definitions[0]
    if (path != "app/models/__init__.py" or base not in trees[path].body
            or base.decorator_list or base.keywords or len(base.bases) != 1
            or not isinstance(base.bases[0], ast.Name) or base.bases[0].id != "DeclarativeBase"
            or any(not isinstance(node, ast.Pass) for node in base.body)):
        raise ValueError("模型Base继承或字段无法静态确认")
    imports = [node for node in trees[path].body if isinstance(node, ast.ImportFrom)
               and node.module == "sqlalchemy.orm" and not node.level]
    if not any(any(item.name == "DeclarativeBase" and not item.asname for item in node.names)
               for node in imports):
        raise ValueError("模型Base来源无法静态确认")


def _column_name(call: ast.Call, attribute_name: str, table_name: str) -> str:
    """Resolve SQLAlchemy's static positional/keyword database column name."""
    if any(keyword.arg is None for keyword in call.keywords):
        raise ValueError(f"模型列参数无法静态确认：{table_name}.{attribute_name}")
    keyword_names = [keyword.value for keyword in call.keywords if keyword.arg == "name"]
    if len(keyword_names) > 1:
        raise ValueError(f"模型列名无法静态确认：{table_name}.{attribute_name}")
    explicit_names: list[str] = []
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        explicit_names.append(call.args[0].value)
    if keyword_names:
        value = keyword_names[0]
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            raise ValueError(f"模型列名无法静态确认：{table_name}.{attribute_name}")
        explicit_names.append(value.value)
    if len(explicit_names) > 1:
        raise ValueError(f"模型列名重复指定，无法静态确认：{table_name}.{attribute_name}")
    column_name = explicit_names[0] if explicit_names else attribute_name
    if not _IDENTIFIER.fullmatch(column_name):
        raise ValueError(f"模型列名无法静态确认：{table_name}.{attribute_name}")
    return column_name


def _reject_hidden_table_classes(path: str, tree: ast.Module) -> None:
    top_level_classes = {id(node) for node in tree.body if isinstance(node, ast.ClassDef)}
    for class_node in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        if id(class_node) in top_level_classes:
            continue
        bases = [base.id if isinstance(base, ast.Name) else None for base in class_node.bases]
        if ("Base" in bases
                or any(_assigned_name(statement) == "__tablename__"
                       for statement in ast.walk(class_node)
                       if isinstance(statement, (ast.Assign, ast.AnnAssign)))):
            raise ValueError(f"模型表定义不在模块顶层，无法静态确认：{path}:{class_node.name}")


def schema_contract_from_sources(sources: Mapping[str, bytes], revision: str) -> dict:
    """Build the deterministic required table/column contract without execution."""
    if not isinstance(revision, str) or not revision.strip():
        raise ValueError("签名程序缺少数据库版本，无法确认必要结构")
    model_sources = {path: raw for path, raw in sources.items()
                     if path == "app/models/__init__.py"
                     or (path.startswith(_MODEL_PREFIX) and path.endswith(".py"))}
    if not model_sources or sum(len(raw) for raw in model_sources.values()) > _MODEL_LIMIT:
        raise ValueError("签名程序模型源码缺失或过大")
    reachable, trees = _registered_modules(model_sources)
    _validate_base(trees)
    tables: dict[str, list[str]] = {}
    for path in sorted(reachable):
        _reject_structural_aliases(path, trees[path])
        _reject_hidden_table_classes(path, trees[path])
        for class_node in (node for node in trees[path].body if isinstance(node, ast.ClassDef)):
            table_name: str | None = None
            table_assignment_seen = False
            direct_table_assignments: set[int] = set()
            for statement in class_node.body:
                name, value, _annotation = _assigned(statement)
                if name != "__tablename__":
                    continue
                direct_table_assignments.add(id(statement))
                table_assignment_seen = True
                if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                    raise ValueError(f"模型表名不是静态字符串：{path}:{class_node.name}")
                table_name = value.value
            for statement in (node for node in ast.walk(class_node)
                              if isinstance(node, (ast.Assign, ast.AnnAssign))):
                if (_assigned_name(statement) == "__tablename__"
                        and id(statement) not in direct_table_assignments):
                    raise ValueError(f"模型表名不在静态类字段中：{path}:{class_node.name}")
            bases = [base.id if isinstance(base, ast.Name) else None for base in class_node.bases]
            if not table_assignment_seen:
                if "Base" in bases:
                    raise ValueError(f"Base模型缺少静态表名：{path}:{class_node.name}")
                continue
            if bases != ["Base"] or not table_name or not _IDENTIFIER.fullmatch(table_name):
                raise ValueError(f"模型继承或表名无法静态确认：{path}:{class_node.name}")
            if class_node.decorator_list or class_node.keywords:
                raise ValueError(f"模型装饰器或元类无法静态确认：{path}:{class_node.name}")
            if table_name in tables:
                raise ValueError(f"模型表名重复：{table_name}")
            columns: set[str] = set()
            parsed_calls: set[int] = set()
            for statement in class_node.body:
                name, value, annotation = _assigned(statement)
                if not name or name.startswith("__"):
                    continue
                call_name = _call_name(value)
                if call_name in {"mapped_column", "Column"}:
                    assert isinstance(value, ast.Call)
                    parsed_calls.add(id(value))
                    columns.add(_column_name(value, name, table_name))
                elif _mapped_annotation(annotation):
                    if value is None:
                        columns.add(name)
                    elif call_name != "relationship":
                        raise ValueError(f"Mapped字段无法静态确认：{table_name}.{name}")
                elif isinstance(value, ast.Call) and call_name != "relationship":
                    raise ValueError(f"模型类字段构造无法静态确认：{table_name}.{name}")
            for call in (node for node in ast.walk(class_node) if isinstance(node, ast.Call)):
                if _call_name(call) in {"mapped_column", "Column"} and id(call) not in parsed_calls:
                    raise ValueError(f"模型列定义不在静态类字段中：{path}:{class_node.name}")
            if not columns:
                raise ValueError(f"模型表没有可确认列：{table_name}")
            tables[table_name] = sorted(columns)
    if not tables:
        raise ValueError("签名程序没有可确认的ERP模型表")
    return {"version": CONTRACT_VERSION, "source": CONTRACT_SOURCE, "revision": revision,
            "tables": {name: tables[name] for name in sorted(tables)}}


def _validate_contract(value: object, revision: str) -> dict:
    if not isinstance(value, dict) or set(value) != {"version", "source", "revision", "tables"}:
        raise ValueError("签名程序结构契约格式未知")
    if value.get("version") != CONTRACT_VERSION or value.get("source") != CONTRACT_SOURCE:
        raise ValueError("签名程序结构契约版本未知")
    if value.get("revision") != revision or not isinstance(value.get("tables"), dict):
        raise ValueError("签名程序结构契约与数据库版本不一致")
    tables: dict[str, list[str]] = {}
    for table, columns in value["tables"].items():
        if (not isinstance(table, str) or not _IDENTIFIER.fullmatch(table)
                or not isinstance(columns, list) or not columns
                or columns != sorted(set(columns))
                or any(not isinstance(column, str) or not _IDENTIFIER.fullmatch(column) for column in columns)):
            raise ValueError("签名程序结构契约表列格式无效")
        tables[table] = columns
    return {"version": CONTRACT_VERSION, "source": CONTRACT_SOURCE, "revision": revision,
            "tables": {name: tables[name] for name in sorted(tables)}}


def schema_contract_from_signed_release(archive: Path, public_key: bytes) -> tuple[dict, dict]:
    """Read the signed archive directly and verify every consumed model byte."""
    with zipfile.ZipFile(archive) as zipped:
        infos = zipped.infolist()
        names = [safe_name(info.filename) for info in infos]
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("签名程序归档含重复路径")
        if any(stat.S_ISLNK(info.external_attr >> 16) for info in infos):
            raise ValueError("签名程序归档不得含链接")
        raw_manifest = zipped.read("manifest.json")
        key = serialization.load_pem_public_key(public_key)
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("发布公钥类型错误")
        key.verify(zipped.read("manifest.sig"), raw_manifest)
        manifest = json.loads(raw_manifest)
        if manifest.get("type") != "tianming.release.v1" or not isinstance(manifest.get("files"), dict):
            raise ValueError("不是签名ERP程序发布包")
        files = manifest["files"]
        if set(names) != set(files) | {"manifest.json", "manifest.sig"}:
            raise ValueError("签名程序文件清单不一致")
        model_names = {name for name in files
                       if name == "app/models/__init__.py"
                       or (name.startswith(_MODEL_PREFIX) and name.endswith(".py"))}
        sizes = {info.filename: info.file_size for info in infos}
        if not model_names or sum(sizes.get(name, 0) for name in model_names) > _MODEL_LIMIT:
            raise ValueError("签名程序模型源码缺失或过大")
        sources: dict[str, bytes] = {}
        for name, expected_hash in files.items():
            if not isinstance(name, str) or safe_name(name) != name:
                raise ValueError("签名程序文件路径无效")
            if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
                raise ValueError("签名程序文件哈希格式无效")
            if name in model_names:
                raw = zipped.read(name)
                if hashlib.sha256(raw).hexdigest() != expected_hash:
                    raise ValueError("签名程序模型文件校验失败：" + name)
                sources[name] = raw
    revision = manifest.get("revision")
    derived = schema_contract_from_sources(sources, revision)
    declared = manifest.get("schema_contract")
    if declared is not None:
        declared = _validate_contract(declared, revision)
        if declared != derived:
            raise ValueError("签名结构契约与已验证模型源码不一致")
        return manifest, declared
    return manifest, derived


def validate_database_schema(database: Path, contract: dict) -> None:
    """Require every signed ORM table and column; extra legacy objects are allowed."""
    revision = contract.get("revision") if isinstance(contract, dict) else ""
    contract = _validate_contract(contract, revision)
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        actual_tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        missing_tables = sorted(set(contract["tables"]) - actual_tables)
        if missing_tables:
            raise ValueError("恢复库缺少签名程序必要结构（表）：" + ", ".join(missing_tables[:10]))
        for table, required_columns in contract["tables"].items():
            quoted = '"' + table.replace('"', '""') + '"'
            actual_columns = {row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")}
            missing_columns = sorted(set(required_columns) - actual_columns)
            if missing_columns:
                raise ValueError("恢复库缺少签名程序必要结构（列）："
                                 + table + "." + ",".join(missing_columns[:10]))
