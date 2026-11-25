"""持ち主向けの管理ツール（Discord のコマンドではなく、サーバーの端末で実行する）。

使い方（サーバーで、Bot のフォルダの中で）:
  .venv/bin/python manage.py grant <サーバーID> <ユーザーID> <金額> [理由]   ペリカを付与する
  .venv/bin/python manage.py servers                                    サーバーの許可状況を見る
  .venv/bin/python manage.py migrate [--db PATH] [--check] [--legacy-guild-id ID]  スキーマ移行・検証

Bot の起動中に実行しても安全（SQLite の同じ DB に、トランザクションで書き込む）。
"""

import argparse
import os
import sqlite3
import sys
import tempfile

from infra.catalogs import bind_catalogs

bind_catalogs()

from domain import approval  # noqa: E402
from domain.ports.repos import IGuildRepo, IWalletRepo  # noqa: E402
from infra.db.repositories import open_repositories  # noqa: E402
from infra.db.schema import CURRENT_SCHEMA_VERSION, MigrationReport, initialize_db  # noqa: E402
from infra.paths import DB_PATH as _DB_PATH  # noqa: E402

DB_PATH = str(_DB_PATH)
LABEL = {approval.APPROVED: "許可", approval.PENDING: "承認待ち", approval.DENIED: "断った"}


def cmd_grant(wallet: IWalletRepo, args) -> int:
    balance = wallet.for_guild(args.guild_id).credit(args.user_id, args.amount, f"付与（管理）{args.reason}".strip())
    print(
        f"付与しました: サーバー {args.guild_id} / ユーザー {args.user_id} に {args.amount:,}ペリカ（残高 {balance:,}）"
    )
    return 0


def cmd_servers(guild: IGuildRepo, _args) -> int:
    rows = guild.list_guild_status()
    if not rows:
        print("記録されているサーバーはありません。")
    for r in rows:
        print(f"{LABEL.get(r['status'], r['status']):6s} {r['guild_id']}  {r['name'] or '?'}")
    return 0


def _open_migrate_conn(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _assert_integrity(conn: sqlite3.Connection) -> None:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    status = str(row[0]) if row is not None else "unknown"
    if status != "ok":
        raise RuntimeError(f"PRAGMA integrity_check に失敗しました: {status}")


def _verify_copy(target_db: str, legacy_guild_id: int | None) -> tuple[MigrationReport, MigrationReport]:
    with tempfile.TemporaryDirectory() as tmpdir:
        copy_path = os.path.join(tmpdir, os.path.basename(target_db) or "copy.db")
        src = sqlite3.connect(f"file:{os.path.abspath(target_db)}?mode=ro", uri=True)
        dst = sqlite3.connect(copy_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        conn = _open_migrate_conn(copy_path)
        try:
            first = initialize_db(conn, copy_path, legacy_guild_id)
            _assert_integrity(conn)
            second = initialize_db(conn, copy_path, legacy_guild_id)
            _assert_integrity(conn)
        finally:
            conn.close()
        if second.applied:
            raise RuntimeError(f"2回目の初期化が冪等ではありません: {second.applied}")
        return first, second


def _migrate_direct(target_db: str, legacy_guild_id: int | None) -> MigrationReport:
    conn = _open_migrate_conn(target_db)
    try:
        report = initialize_db(conn, target_db, legacy_guild_id)
        _assert_integrity(conn)
        return report
    finally:
        conn.close()


def cmd_migrate(args: argparse.Namespace, target_db: str) -> int:
    """DB スキーマの移行（または一時コピーでの事前検証）を実行する。"""
    try:
        if args.check:
            report, second = _verify_copy(target_db, args.legacy_guild_id)
            print(
                f"検証OK (check): version={report.start_version}->{second.end_version}/{CURRENT_SCHEMA_VERSION} "
                f"applied={list(report.applied)} invariants={'ok' if report.invariants_checked else 'skipped'} "
                "integrity=ok idempotent=ok"
            )
            return 0
        report = _migrate_direct(target_db, args.legacy_guild_id)
        print(
            f"移行完了: version={report.start_version}->{report.end_version}/{CURRENT_SCHEMA_VERSION} "
            f"applied={list(report.applied)} backups={list(report.backup_paths)} integrity=ok"
        )
        return 0
    except (RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"エラー: マイグレーションに失敗しました: {exc}", file=sys.stderr)
        return 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="カジノBot の管理ツール")
    sub = p.add_subparsers(dest="command", required=True)
    g = sub.add_parser("grant", help="ペリカを付与する")
    g.add_argument("guild_id", type=int)
    g.add_argument("user_id", type=int)
    g.add_argument("amount", type=int)
    g.add_argument("reason", nargs="?", default="")
    sub.add_parser("servers", help="サーバーの許可状況")
    m = sub.add_parser("migrate", help="DB スキーマを移行・検証する")
    m.add_argument("--db", default=None, help="対象の SQLite DB ファイルパス")
    m.add_argument("--check", action="store_true", help="元 DB を変更せず一時コピーで移行・整合性を検証する")
    m.add_argument("--legacy-guild-id", type=int, default=None, help="旧DBの引き継ぎ先サーバーID")
    args = p.parse_args(argv)
    if args.command == "migrate":
        target_db = args.db or DB_PATH
        if not os.path.exists(target_db):
            print(f"エラー: {target_db} が見つかりません。", file=sys.stderr)
            return 2
        return cmd_migrate(args, target_db)
    if args.command == "grant" and not (0 < args.amount <= 10_000_000 and args.guild_id > 0 and args.user_id > 0):
        print("エラー: サーバーID・ユーザーIDは正の数、金額は 1〜10,000,000 にしてください。", file=sys.stderr)
        return 2
    if not os.path.exists(DB_PATH):
        print(f"エラー: {DB_PATH} が見つかりません。Bot のフォルダの中で実行してください。", file=sys.stderr)
        return 2
    repos = open_repositories(DB_PATH, strict=True)
    try:
        if args.command == "grant":
            return cmd_grant(repos.wallet, args)
        return cmd_servers(repos.guild, args)
    finally:
        repos.close()


if __name__ == "__main__":
    sys.exit(main())
