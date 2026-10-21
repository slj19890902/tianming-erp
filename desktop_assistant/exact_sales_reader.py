"""Read-only admission for programs that preserve exact decimal sale facts."""
CAPABILITY = 'exact_sales_decimal_v1'
COLUMNS = {
    'sales_orders': ('total_amount_exact',),
    'sales_order_items': ('unit_price_exact', 'subtotal_exact'),
    'finance_statements': ('total_receivable_exact',),
    'finance_statement_items': ('unit_price_snapshot_exact', 'receivable_amount_exact'),
}


def has_facts(db):
    for table, columns in COLUMNS.items():
        existing = {r[1] for r in db.execute('PRAGMA table_info("' + table + '")')}
        present = [name for name in columns if name in existing]
        if present and db.execute('SELECT 1 FROM "' + table + '" WHERE ' +
                ' OR '.join('"' + name + '" IS NOT NULL' for name in present) + ' LIMIT 1').fetchone():
            return True
    return False


def require_reader(db, manifest):
    reader = (manifest.get('reader_capabilities') or {}).get(CAPABILITY)
    if has_facts(db) and not (type(reader) is int and reader == 1):
        raise ValueError('当前数据库已有精确销售金额，目标程序不能保留原始单价与金额；禁止启动或回退')


def recover_empty_migration(manager, candidate, previous, manifest, state, environment=None, *, _shared_catalog=False, _supplier_edges=False):
    """Undo only a fixed empty additive migration after failed first startup.

    The installed previous supervisor cannot recognize a future revision. Return
    to its exact original schema using the signed downgrade, never an old DB.
    Caller holds maintenance and has stopped the candidate process.
    """
    from contextlib import closing
    from datetime import datetime, timezone
    from pathlib import Path
    import sqlite3
    import subprocess
    import uuid
    from desktop_assistant.storage import (database_info, read_json, sha,
        signed_release_manifest, write_json)
    from desktop_assistant import migration
    from desktop_assistant.runtime_platform import process_options

    if _shared_catalog and _supplier_edges:
        raise ValueError('恢复类型不唯一')
    source_revision, target_revision = ('ew1020em','ex1021cp') if _shared_catalog else ('ev1020hr','ew1020em')
    capability = 'shared_product_master_v1' if _shared_catalog else CAPABILITY
    label, audit_key, prefix = ('客户共用','empty_shared_schema_recovery','empty-shared-recovery-') if _shared_catalog else ('精确金额','empty_exact_schema_recovery','empty-exact-recovery-')
    if _supplier_edges:
        from desktop_assistant.supplier_sheet_reader import CAPABILITY as capability
        source_revision, target_revision = 'ex1021cp', 'ey1021se'
        label, audit_key, prefix = '供应商毛净', 'empty_supplier_edges_recovery', 'empty-supplier-edges-recovery-'
    root = manager.root
    shared = root / 'shared'
    database = shared / 'data/carton_erp.sqlite3'
    def require(ok):
        if not ok:
            raise ValueError(label+'首次启动恢复依据不完整，保留数据库及现场')
    require(manager._process() is None and state.get('operation') == 'update'
            and state.get('current') == candidate)
    packages = [root / 'packages' / (identity + '.zip') for identity in (previous, candidate)]
    require(all(path.is_file() and sha(path) == identity
                for path, identity in zip(packages, (previous, candidate))))
    old, new = [signed_release_manifest(path, manager.public_key) for path in packages]
    require(new == manifest and old['revision'] == source_revision and new['revision'] == target_revision
            and new.get('migration') == dict(policy='preserve_existing_facts_v1',
                from_revision=source_revision, rollback_package_sha256=previous)
            and new.get('reader_capabilities', {}).get(capability) == 1)
    report_path = Path(state.get('migration_report', ''))
    require(report_path.name == 'report.json' and report_path.parent.parent == root/'staging'
            and report_path.parent.name.startswith('migration-')
            and report_path.is_file() and sha(report_path) == state.get('migration_report_sha256'))
    report = read_json(report_path)
    require(report.get('status') == 'passed' and report.get('package_sha256') == candidate
            and report.get('source_revision') == source_revision
            and report.get('target_revision') == target_revision
            and isinstance(report.get('source_schema'), str)
            and report.get('schema_fingerprint_version') == 2)
    backup = Path(state.get('update_backup', ''))
    require(backup.is_file() and sha(backup) == state.get('migration_backup_sha256'))
    require(database_info(database)['revision'] == target_revision
            and migration.schema(database) == report['result_schema']
            and migration.facts(database, report['source_facts']['columns']) == report['source_facts']
            and migration.files(shared) == state.get('migration_files'))
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        if _supplier_edges:
            from desktop_assistant.supplier_sheet_reader import COLUMNS as edge_columns, has_facts as edge_facts
            require(not edge_facts(db))
            for table, columns in edge_columns.items():
                info = {r[1]: r for r in db.execute('PRAGMA table_info("'+table+'")')}
                require(all(name in info and info[name][2].upper() == 'VARCHAR(20)'
                    and info[name][3] == 0 and info[name][4] is None for name in columns))
        elif _shared_catalog:
            from desktop_assistant.reader_contract import has_shared_product_master_facts
            require(not has_shared_product_master_facts(db))
            for table in ('customer_sharing_groups','customer_sharing_members','scrap_production_jobs'):
                require(db.execute('SELECT COUNT(*) FROM '+table).fetchone()==(0,))
            info={r[1]:r for r in db.execute('PRAGMA table_info(products)')}
            column=info.get('production_variants')
            require(column and column[2].upper()=='JSON' and column[3]==0 and column[4] is None)
        else:
            require(not has_facts(db))
            for table, columns in COLUMNS.items():
                info = {r[1]: r for r in db.execute('PRAGMA table_info("'+table+'")')}
                require(all(name in info and info[name][2].upper() == 'TEXT'
                            and info[name][3] == 0 and info[name][4] is None for name in columns))
        require(db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
                and not db.execute('PRAGMA foreign_key_check').fetchall())
    release = manager._execution_release(candidate, kind='migration')
    arguments, env, system_service = migration.prepare_migration(
        release, shared, target_revision, environment=environment, release_manifest=manifest)
    # The guarded entry and environment are authenticated normally above. Only
    # the fixed direction/revision change; no arbitrary migration is accepted.
    require(arguments[-2:] == ['upgrade', target_revision])
    arguments[-2:] = ['downgrade', source_revision]
    log = root/'control'/(prefix+uuid.uuid4().hex+'.log')
    audit = dict(status='running', candidate=candidate, previous=previous,
        from_revision=target_revision, to_revision=source_revision,
        source_report_sha256=state['migration_report_sha256'],
        backup_sha256=state['migration_backup_sha256'],
        database_replaced=False, started_at=datetime.now(timezone.utc).isoformat(), log=str(log))
    audit['new_supplier_edge_facts' if _supplier_edges else ('new_shared_facts' if _shared_catalog else 'new_exact_facts')]=0
    state.update(operation='migration_running')
    state[audit_key]=audit
    write_json(root/'state.json', state)
    try:
        with log.open('xb') as output:
            process = subprocess.Popen(arguments, cwd=release, env=env,
                stdin=subprocess.DEVNULL, stdout=output, stderr=output, **process_options())
            if system_service:
                # Never kill an active formal migration because a chat window ends.
                interrupted = False
                while True:
                    try:
                        code = process.wait()
                        break
                    except KeyboardInterrupt:
                        interrupted = True
                require(not interrupted)
            else:
                code = process.wait(timeout=180)
            require(code == 0)
        require(database_info(database)['revision'] == source_revision
                and migration.schema(database) == report['source_schema']
                and migration.facts(database) == report['source_facts']
                and migration.files(shared) == state['migration_files'])
        audit.update(status='completed', finished_at=datetime.now(timezone.utc).isoformat(),
                     restored_source_schema=report['source_schema'])
        state['schema_authority'] = None
        write_json(root/'state.json', state)
        return audit
    except BaseException:
        audit.update(status='failed', finished_at=datetime.now(timezone.utc).isoformat())
        state['operation'] = 'migration_failed'
        write_json(root/'state.json', state)
        raise
