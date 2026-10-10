"""Guard the native migration child before importing the migration framework."""
import os
from pathlib import Path
import runpy


def main():
    from app.core.home_rehearsal import install_network_guard
    install_network_guard()
    database = Path(os.environ['ERP_DATABASE_PATH'])
    if not database.is_absolute() or not database.is_file() or database.resolve() != database or database.is_symlink():
        raise ValueError('迁移要求已存在的本机数据库，拒绝空库或链接')
    runpy.run_module('alembic', run_name='__main__', alter_sys=True)


if __name__ == '__main__':
    main()
