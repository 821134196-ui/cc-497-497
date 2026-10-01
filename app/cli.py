"""命令行入口。

  python3 -m app.cli init [--db data/app.db --photos data/photos]   建库并导入演示数据
  python3 -m app.cli serve [--db data/app.db --port 8000]           启动 Web 服务
"""
import argparse
import json
import os
import sys

from .db import init_db
from .seed import seed

DEFAULT_DB = os.path.join("data", "app.db")
DEFAULT_PHOTOS = os.path.join("data", "photos")


def cmd_init(args):
    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    info = seed(args.db, args.photos)
    print("✓ 数据库已初始化并导入演示数据（正常 + 异常串码）：", args.db)
    print("  照片目录：", os.path.abspath(args.photos))
    print("  演示账号：")
    for role, cred in info["accounts"]:
        print(f"    - {role}: {cred}")
    print("  演示串码：")
    for k, v in info["codes"].items():
        print(f"    · {k}: {', '.join(v)}")
    print("  配送单：", info["note"])


def cmd_serve(args):
    if not os.path.exists(args.db):
        print("数据库不存在，先执行 init ...", file=sys.stderr)
        init_db(args.db)
    from .server import run
    run(args.db, args.photos, args.port)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="app.cli",
                                     description="卷烟条包流转追溯演示系统")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="建库并导入正常/异常演示串码")
    p_init.add_argument("--db", default=DEFAULT_DB)
    p_init.add_argument("--photos", default=DEFAULT_PHOTOS)
    p_init.set_defaults(func=cmd_init)

    p_serve = sub.add_parser("serve", help="启动 Web 服务")
    p_serve.add_argument("--db", default=DEFAULT_DB)
    p_serve.add_argument("--photos", default=DEFAULT_PHOTOS)
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
