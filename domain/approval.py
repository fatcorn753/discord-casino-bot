"""Bot を追加できるサーバーの管理（許可リストと承認状態の純粋判定）。

方式:
- config の allowed_guild_ids … あらかじめ許可しておくサーバー
- require_approval が true … 上に無いサーバーは、Bot の持ち主（owner_ids）が承認するまで使えない
- require_approval が false … 従来どおり。allowed_guild_ids が空ならどのサーバーでも使え、空でなければその中だけ
持ち主（owner_ids）は、どのサーバーでもコマンドを使える（承認の操作を、承認待ちのサーバーの中からもできるように）。
"""

from domain.ports import IGuildApprovalStore

PENDING, APPROVED, DENIED = "pending", "approved", "denied"
DM_GROUP = "運営"  # Bot とのDMでだけ使える、持ち主向けのコマンドの名前


def requires_approval(config: dict) -> bool:
    return bool(config.get("require_approval"))


def owner_ids(config: dict) -> set:
    return set(config.get("owner_ids") or [])


def preapproved(config: dict) -> set:
    return set(config.get("allowed_guild_ids") or [])


def is_allowed(eco: IGuildApprovalStore, config: dict, guild_id: int, user_id: int) -> bool:
    """このサーバーで、このユーザーのコマンドを受け付けてよいか。"""
    if user_id in owner_ids(config) or guild_id in preapproved(config):
        return True
    if requires_approval(config):
        row = eco.get_guild_status(guild_id)
        return bool(row) and row["status"] == APPROVED
    return not preapproved(config)  # 承認制でなければ、リストが空のときだけ全サーバー可
