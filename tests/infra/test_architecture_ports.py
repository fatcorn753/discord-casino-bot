"""水平ポート契約（ISP / DIP）とレイヤ境界の自動ガードレールテスト。"""

import ast
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from cogs.shared.cog_ports import guild_port, port_of
from domain import ports as domain_ports
from domain.ports import IEconomy, IHorsePort, IShopPort
from infra.db.economy import Economy, _verify_ieconomy

ROOT = Path(__file__).resolve().parents[2]


def _iter_py(rel: str):
    return sorted((ROOT / rel).rglob("*.py"))


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
    return mods


def test_domain_ports_import_does_not_require_catalogs():
    """domain.ports はカタログバインド前でも MRO 衝突なく単独 import できる。"""
    proc = subprocess.run(
        [sys.executable, "-c", "import domain.ports"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_economy_satisfies_ieconomy_and_all_ports():
    """Economy が IEconomy および全ドメインポートに定義された公開メソッドを備える。"""
    eco = Economy(":memory:")
    try:
        verified = _verify_ieconomy(eco)
        assert verified is eco

        guild_eco = eco.for_guild(1)
        assert guild_eco.balance(1) > 0

        bot = SimpleNamespace(economy=eco)
        assert port_of(bot, IEconomy) is eco
        assert guild_port(bot, 1, IShopPort).inventory(1) == {}
        assert guild_port(bot, 1, IHorsePort).horse_list(1) == []

        for name in domain_ports.__all__:
            obj = getattr(domain_ports, name)
            if isinstance(obj, type) and getattr(obj, "_is_protocol", False) and name != "ICatalogs":
                assert hasattr(obj, "__mro__")
    finally:
        eco.close()


def test_domain_has_no_outward_dependencies():
    """domain/ は infra・services・cogs・discord に依存しない。"""
    forbidden = ("infra", "services", "cogs", "discord")
    violations: list[str] = []
    for path in _iter_py("domain"):
        for mod in _imported_modules(path):
            top = mod.split(".")[0]
            if top in forbidden:
                violations.append(f"{path.relative_to(ROOT)} imports {mod}")
    assert not violations, "\n".join(violations)


def test_services_no_infra_db_cogs_or_any_economy():
    """services/ は infra.db・cogs・discord に依存せず、economy を Any 型にしない。"""
    violations: list[str] = []
    for path in _iter_py("services"):
        for mod in _imported_modules(path):
            if mod == "infra.db" or mod.startswith("infra.db.") or mod.split(".")[0] in ("cogs", "discord"):
                violations.append(f"{path.relative_to(ROOT)} imports {mod}")

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                    if arg.arg in ("economy", "_economy", "eco") and isinstance(arg.annotation, ast.Name):
                        if arg.annotation.id == "Any":
                            violations.append(f"{path.relative_to(ROOT)}:{node.lineno} param {arg.arg}: Any")
            elif isinstance(node, ast.AnnAssign) and isinstance(node.annotation, ast.Name):
                target = node.target
                name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                if name in ("economy", "_economy", "eco") and node.annotation.id == "Any":
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno} attr {name}: Any")
    assert not violations, "\n".join(violations)


def test_cogs_no_infra_db_imports_and_no_raw_bot_economy():
    """cogs/ は infra.db を import せず、cogs/shared/(bot|cog_ports).py 以外で bot.economy に直接触れない。"""
    allowed_bot_economy = {
        Path("cogs/shared/bot.py"),
        Path("cogs/shared/cog_ports.py"),
    }
    violations: list[str] = []
    for path in _iter_py("cogs"):
        rel = path.relative_to(ROOT)
        for mod in _imported_modules(path):
            if mod == "infra.db" or mod.startswith("infra.db."):
                violations.append(f"{rel} imports {mod}")

        if rel in allowed_bot_economy:
            continue

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "economy":
                val = node.value
                if (isinstance(val, ast.Name) and val.id == "bot") or (
                    isinstance(val, ast.Attribute) and val.attr == "bot"
                ):
                    violations.append(f"{rel}:{node.lineno} accesses *.economy directly")
    assert not violations, "\n".join(violations)
