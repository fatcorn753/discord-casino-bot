"""
ペリカカジノ Bot — エントリポイント。

ゲーム内の仮想通貨「ペリカ」で遊ぶ、お遊び用のカジノ。現実のお金とは一切関係ない。
設定は config.json、ペリカの記録は data/casino.db（SQLite）に保存する。
"""

import json
import logging
import os
import re
import traceback

import discord
from discord import app_commands
from discord.ext import commands

from core import approval, ui
from core.economy import Economy
from core.session import Sessions

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COGS_DIR = os.path.join(BASE_DIR, "cogs")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DB_PATH = os.path.join(BASE_DIR, "data", "casino.db")


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit("config.json が見つかりません。config.example.json をコピーしてトークンを設定してください。")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_token(token: str) -> str:
    """トークンが未設定・雛形のままなら、原因と対処を日本語で示して終了する。"""
    token = (token or "").strip()
    if not re.fullmatch(r"[\w-]{20,}\.[\w-]{4,}\.[\w-]{20,}", token):
        raise SystemExit(
            "config.json の token が未設定、または Bot トークンの形式ではありません。\n"
            "  1. Discord Developer Portal → アプリ → 「Bot」→「トークンをリセット」で表示される値をコピー\n"
            "  2. config.json の \"token\" に貼り付けて保存\n"
            "  （トークンは他人に見せないでください）")
    return token


class BotTree(app_commands.CommandTree):
    """全コマンド共通のガードとエラー応答。"""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Discord は3秒以内に応答しないと「応答しませんでした」と表示する。届くのが遅れていないかを記録しておく
        late = (discord.utils.utcnow() - interaction.created_at).total_seconds()
        if late > 1.5:
            print(f"⚠ コマンドの受信が {late:.1f} 秒遅れました（{getattr(interaction.command, 'qualified_name', '?')}）。"
                  f"Bot のネットワークや、PC のスリープ・負荷を確認してください。ゲートウェイ遅延: {interaction.client.latency * 1000:.0f}ms")
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=ui.error_embed("利用できません", "このコマンドはサーバー内でのみ使えます。"), ephemeral=True)
            return False
        if not await approval.guard(interaction):          # 許可リスト・持ち主の承認
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        cause = getattr(error, "original", error)
        if isinstance(error, app_commands.MissingPermissions):
            text = "このコマンドを使うには、サーバーの「サーバー管理」権限が必要です。"
        elif isinstance(error, app_commands.CheckFailure):
            text = "このコマンドは、いまは使えません。"
        elif isinstance(cause, discord.Forbidden):
            text = "Bot の権限が足りないため実行できませんでした。チャンネルの権限を確認してください。"
        elif isinstance(cause, discord.HTTPException):
            text = f"Discord 側のエラーで失敗しました（コード {cause.status}）。時間をおいてもう一度お試しください。"
        else:
            traceback.print_exception(type(error), error, error.__traceback__)
            text = "予期しないエラーが発生しました。精算の前なら賭けたペリカは返しています（`/ペリカ 履歴` で確認できます）。"
        embed = ui.error_embed("実行できませんでした", text)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass
        print(f"コマンドエラー: {getattr(interaction.command, 'qualified_name', '?')}: {error!r}")


config = load_config()

intents = discord.Intents.default()       # 特権インテントは不要（メッセージの内容もメンバー一覧も読まない）

bot = commands.Bot(
    command_prefix=commands.when_mentioned,
    intents=intents,
    help_command=None,
    tree_cls=BotTree,
    activity=discord.Game(name=config.get("status", "/ヘルプ でペリカを稼ごう")),
)
bot.config = config
if config.get("debug_http"):
    # 原因調査用: Discord への通信（応答の送信など）を1件ずつ記録する
    logging.getLogger("discord.http").setLevel(logging.DEBUG)
# ペリカはサーバーごとに別々。古い形式の記録は legacy_guild_id（無ければ許可リストの先頭）のサーバーが引き継ぐ
_legacy = config.get("legacy_guild_id") or next(iter(config.get("allowed_guild_ids") or []), None)
bot.economy = Economy(DB_PATH, legacy_guild_id=_legacy, strict=True)
bot.sessions = Sessions()


async def load_all_cogs() -> list:
    failed = []
    for filename in sorted(os.listdir(COGS_DIR)):
        if not filename.endswith(".py") or filename.startswith("_"):
            continue
        module = f"cogs.{filename[:-3]}"
        try:
            await bot.load_extension(module)
            print(f"✓ ロード: {module}")
        except Exception:
            print(f"✗ 失敗: {module}")
            traceback.print_exc()
            failed.append(module)
    return failed


async def sync_test_guild(guild_id: int) -> bool:
    """開発用サーバーへコマンドを即時登録する。招待前などで失敗しても、Bot は止めない。"""
    guild_obj = discord.Object(id=guild_id)
    bot.tree.copy_global_to(guild=guild_obj)
    try:
        await bot.tree.sync(guild=guild_obj)
    except discord.Forbidden:
        print(f"⚠ サーバー {guild_id} にコマンドを登録できませんでした。Bot がそのサーバーに招待されていないか、"
              "招待URLに applications.commands が含まれていません。招待すると、自動で登録されます。")
        return False
    print(f"✓ テストギルド {guild_id} にコマンド同期")
    return True


@bot.event
async def on_guild_join(guild: discord.Guild):
    # 開発用サーバーに招待されたら、すぐコマンドを登録する（再起動不要）
    test_guild = bot.config.get("test_guild_id")
    if test_guild and guild.id == int(test_guild):
        await sync_test_guild(guild.id)


@bot.event
async def setup_hook():
    # 前回の起動中に落ちて、預かったままになっている賭け金を返す
    refunded = bot.economy.refund_all_escrows()
    if refunded:
        print(f"中断していたゲーム {refunded} 件の賭け金を返金しました")
    failed = await load_all_cogs()
    if failed:
        # 同期は Discord 側の登録を丸ごと上書きするため、欠けた状態で行うとそのコマンドが消える
        print(f"⚠ 読み込みに失敗した cog があるためコマンド同期をスキップします: {', '.join(failed)}")
        return
    test_guild = bot.config.get("test_guild_id")
    if test_guild:
        await sync_test_guild(int(test_guild))
        print("⚠ test_guild_id は開発用です。グローバルのコマンドも登録されていると、同じコマンドが2つ表示されます。本番では null にしてください。")
    else:
        await bot.tree.sync()
        print("✓ グローバルにコマンド同期（反映まで最大1時間）")


@bot.event
async def on_ready():
    print("=" * 50)
    print(f"ログイン: {bot.user} (ID: {bot.user.id})")
    print(f"参加サーバー数: {len(bot.guilds)}")
    allowed = bot.config.get("allowed_guild_ids") or []
    print(f"許可サーバー: {allowed if allowed else '制限なし（どのサーバーでも遊べます）'}")
    print("=" * 50)


_lock_file = None      # 起動中ずっと保持する（閉じるとロックが外れる）


def acquire_single_instance_lock() -> None:
    """同じフォルダで Bot を二重に起動できないようにする。

    二重に動くと、同じコマンドを2つのプロセスが取り合って、エラーや二重の処理が起きる。
    （別のコンピューターでの同時起動までは検知できない。同じトークンでは1か所だけで動かすこと。）
    """
    global _lock_file
    try:
        import fcntl
    except ImportError:          # Windows など: ロックなしで続ける
        return
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    _lock_file = open(os.path.join(os.path.dirname(DB_PATH), "bot.lock"), "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("すでにこの Bot が起動しています。二重に起動すると、コマンドを取り合ってエラーになります。\n"
                         "動いているものを止めてから、起動し直してください。")


def main():
    acquire_single_instance_lock()
    token = validate_token(config.get("token"))
    test_guild = config.get("test_guild_id")
    if test_guild is not None and not str(test_guild).isdigit():
        raise SystemExit("config.json の test_guild_id は数字のサーバーIDか null にしてください。")
    try:
        bot.run(token)
    except discord.LoginFailure:
        raise SystemExit("ログインに失敗しました。config.json の token が正しいか、再発行されていないか確認してください。")


if __name__ == "__main__":
    main()
