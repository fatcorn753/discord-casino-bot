"""持ち主向けの管理ツール（Discord のコマンドではなく、サーバーの端末で実行する）。

使い方（サーバーで、Bot のフォルダの中で）:
  .venv/bin/python manage.py grant <サーバーID> <ユーザーID> <金額> [理由]   ペリカを付与する
  .venv/bin/python manage.py servers                                    サーバーの許可状況を見る

Bot の起動中に実行しても安全（SQLite の同じ DB に、トランザクションで書き込む）。
"""

import argparse
import os
import sys

from core import approval
from core.economy import Economy

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "casino.db")
LABEL = {approval.APPROVED: "許可", approval.PENDING: "承認待ち", approval.DENIED: "断った"}


def cmd_grant(eco: Economy, args) -> int:
    balance = eco.for_guild(args.guild_id).credit(args.user_id, args.amount, f"付与（管理）{args.reason}".strip())
    print(f"付与しました: サーバー {args.guild_id} / ユーザー {args.user_id} に {args.amount:,}ペリカ（残高 {balance:,}）")
    return 0


def cmd_servers(eco: Economy, _args) -> int:
    rows = eco.list_guild_status()
    if not rows:
        print("記録されているサーバーはありません。")
    for r in rows:
        print(f"{LABEL.get(r['status'], r['status']):6s} {r['guild_id']}  {r['name'] or '?'}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="カジノBot の管理ツール")
    sub = p.add_subparsers(dest="command", required=True)
    g = sub.add_parser("grant", help="ペリカを付与する")
    g.add_argument("guild_id", type=int)
    g.add_argument("user_id", type=int)
    g.add_argument("amount", type=int)
    g.add_argument("reason", nargs="?", default="")
    sub.add_parser("servers", help="サーバーの許可状況")
    args = p.parse_args(argv)
    if args.command == "grant" and not (0 < args.amount <= 10_000_000 and args.guild_id > 0 and args.user_id > 0):
        print("エラー: サーバーID・ユーザーIDは正の数、金額は 1〜10,000,000 にしてください。", file=sys.stderr)
        return 2
    if not os.path.exists(DB_PATH):
        print(f"エラー: {DB_PATH} が見つかりません。Bot のフォルダの中で実行してください。", file=sys.stderr)
        return 2
    eco = Economy(DB_PATH, strict=True)
    return {"grant": cmd_grant, "servers": cmd_servers}[args.command](eco, args)


if __name__ == "__main__":
    sys.exit(main())
