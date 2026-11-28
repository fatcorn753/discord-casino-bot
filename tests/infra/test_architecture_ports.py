"""水平ポート契約（名目的基底クラス・Bridge・Constructor DI）とレイヤ境界の自動ガードレールテスト。"""

import ast
import dataclasses
import re
import subprocess
import sys
import typing
from pathlib import Path

import pytest

from domain.ports import repos as repo_ports
from infra.db.repositories import RepositoryBundle, open_repositories

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


def test_legacy_facade_and_service_locators_are_removed():
    """旧 Economy ファサード・cog_ports・旧プロトコル群が削除されている。"""
    assert not (ROOT / "infra/db/economy.py").exists()
    assert not (ROOT / "cogs/shared/cog_ports.py").exists()
    for removed in ("core.py", "casino.py", "community.py", "economy.py", "horse.py", "labor.py", "market.py"):
        assert not (ROOT / "domain/ports" / removed).exists()


# 住み分けポリシー（python-coding-conventions §3.3 / §7.2）:
# - 名目的契約層（不変条件・状態・実行時 isinstance 契約あり）では Protocol・abc.ABC を禁止する。
# - 静的型のみで LSP を 100% 保証できる純粋契約層（単一コールバックの bridges・純粋戦略の games）では
#   Protocol / Callable 型エイリアスを許可する（ただし abc モジュールと ABC 基底は引き続き禁止）。
NOMINAL_PORT_DIRS = ("domain/ports/repos", "domain/ports/contracts")
NOMINAL_PORT_FILES = ("domain/ports/catalogs.py", "domain/ports/clock.py", "domain/ports/session.py")
NOMINAL_EXTRA_FILES = ("cogs/shared/bridges.py",)
PURE_CONTRACT_DIRS = ("domain/ports/bridges", "domain/games")


def _protocol_or_abc_violations(path: Path, *, allow_protocol: bool) -> list[str]:
    violations: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                base_id = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                if base_id == "ABC" or (base_id == "Protocol" and not allow_protocol):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno} class {node.name} inherits {base_id}")
        elif isinstance(node, ast.ImportFrom) and node.module in ("abc",):
            violations.append(f"{path.relative_to(ROOT)}:{node.lineno} imports abc")
    return violations


def _in_dirs(path: Path, dirs: tuple[str, ...]) -> bool:
    rel = str(path.relative_to(ROOT))
    return any(rel == d or rel.startswith(d + "/") for d in dirs)


def test_nominal_port_layers_forbid_protocol_and_abc():
    """名目的契約層（domain/ports/repos・contracts・catalogs・clock・session と cogs/shared/bridges.py）は
    Protocol・abc.ABC を使わない名目的基底クラスのままである（不変条件・実行時契約のため）。"""
    targets = [
        p
        for p in _iter_py("domain/ports")
        if _in_dirs(p, NOMINAL_PORT_DIRS)
        or str(p.relative_to(ROOT)) in NOMINAL_PORT_FILES
        or "catalog_types" in p.parts
    ]
    targets += [ROOT / f for f in NOMINAL_EXTRA_FILES]
    violations = [v for path in targets for v in _protocol_or_abc_violations(path, allow_protocol=False)]
    assert not violations, "\n".join(violations)


def test_pure_contract_layers_may_use_protocol_or_callable_but_never_abc():
    """純粋契約層（domain/ports/bridges の単一コールバック契約・domain/games の純粋戦略）では
    Protocol / Callable 型エイリアスを許可するが、abc モジュールと ABC 基底は引き続き禁止する。"""
    targets = [p for d in PURE_CONTRACT_DIRS for p in _iter_py(d)]
    violations = [v for path in targets for v in _protocol_or_abc_violations(path, allow_protocol=True)]
    assert not violations, "\n".join(violations)


def test_bridges_package_reexports_every_submodule_symbol():
    """domain/ports/bridges/ の各サブモジュールが定義する公開シンボル（クラス・関数・型エイリアス）は、
    パッケージから漏れなく再エクスポートされる。"""
    import domain.ports.bridges as bridges_pkg

    pkg_dir = ROOT / "domain" / "ports" / "bridges"
    defined = _package_defined_symbols(pkg_dir)
    assert defined, "bridges サブモジュールが空です"
    not_exported = [f"{name} ({mod})" for name, mod in defined.items() if not hasattr(bridges_pkg, name)]
    assert not not_exported, "パッケージへの再エクスポート漏れ:\\n" + "\\n".join(not_exported)
    assert set(bridges_pkg.__all__) == set(defined), "__all__ とサブモジュール定義が一致しません"


@pytest.mark.real_scope
def test_repository_bundle_satisfies_nominal_repo_ports():
    """open_repositories() が返す全リポジトリは domain.ports.repos の名目的基底クラスを継承する。"""
    repos = open_repositories(":memory:", strict=True)
    try:
        assert isinstance(repos, repo_ports.IRepositoryBundle)
        assert isinstance(repos, RepositoryBundle)
        hints = typing.get_type_hints(repo_ports.IRepositoryBundle)
        names = [f.name for f in dataclasses.fields(repo_ports.IRepositoryBundle)]
        assert len(names) == 33, names  # フィールドを足したら、この数も直す（検証漏れを作らないため）
        wrong = []
        for name in names:
            port = typing.get_origin(hints[name]) or hints[name]
            if not isinstance(getattr(repos, name), port):
                wrong.append(f"{name}: {type(getattr(repos, name)).__name__} は {port.__name__} ではない")
        assert not wrong, "\n".join(wrong)

        scoped = repos.for_guild(1)
        assert scoped.wallet.balance(1) > 0
        assert scoped.shop.inventory(1) == {}
        assert scoped.horses.horse_list(1) == []
    finally:
        repos.close()


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


def test_services_no_infra_cogs_or_any_economy():
    """services/（Composition Root 用 container.py 除く）は infra・cogs・discord・time.time() に依存せず、economy を Any 型にしない。"""
    violations: list[str] = []
    for path in _iter_py("services"):
        rel = path.relative_to(ROOT)
        for mod in _imported_modules(path):
            top = mod.split(".")[0]
            if top in ("cogs", "discord"):
                violations.append(f"{rel} imports {mod}")
            elif top == "infra" and path.name != "container.py":
                violations.append(f"{rel} imports {mod}")

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                    if arg.arg in ("economy", "_economy", "eco") and isinstance(arg.annotation, ast.Name):
                        if arg.annotation.id == "Any":
                            violations.append(f"{rel}:{node.lineno} param {arg.arg}: Any")
            elif isinstance(node, ast.AnnAssign) and isinstance(node.annotation, ast.Name):
                target = node.target
                name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                if name in ("economy", "_economy", "eco") and node.annotation.id == "Any":
                    violations.append(f"{rel}:{node.lineno} attr {name}: Any")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "time"
                and node.func.attr == "time"
            ):
                violations.append(f"{rel}:{node.lineno} calls time.time() instead of IClock")
    assert not violations, "\n".join(violations)


def _wall_clock_calls(path: Path) -> list[str]:
    """time.time()・time.localtime() の直呼び（IClock を通さない時刻の取得）の行。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "time":
            if any(a.name in ("time", "localtime") for a in node.names):
                found.append(f"{path.relative_to(ROOT)}:{node.lineno} imports time.{node.names[0].name}")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "time"
            and node.func.attr in ("time", "localtime")
        ):
            found.append(f"{path.relative_to(ROOT)}:{node.lineno} calls time.{node.func.attr}() instead of IClock")
    return found


def test_cogs_and_repositories_take_time_from_iclock():
    """cogs/ と infra/db/ は time.time() を直接呼ばない（#64）。

    Cog は Service の clock から、リポジトリは引数の now か、注入された IClock（ScopedRepo._now()）から時刻を取る。
    起動時に1回だけ動くマイグレーション（infra/db/schema/）は対象外。
    """
    violations: list[str] = []
    for path in [*_iter_py("cogs"), *_iter_py("infra/db")]:
        if "schema" in path.relative_to(ROOT).parts:
            continue
        violations += _wall_clock_calls(path)
    assert not violations, "\n".join(violations)


def test_cogs_no_infra_imports_and_no_service_locators():
    """cogs/ は infra を一切 import せず、bot.economy や旧 Service Locator を一切使わない。"""
    forbidden_names = {"port_of", "guild_port", "get_cog_port", "require_cog_port", "services_of"}
    forbidden_infra_prefixes = ("infra",)
    violations: list[str] = []
    for path in _iter_py("cogs"):
        rel = path.relative_to(ROOT)
        for mod in _imported_modules(path):
            if (
                any(mod == prefix or mod.startswith(prefix + ".") for prefix in forbidden_infra_prefixes)
                or "cog_ports" in mod
            ):
                violations.append(f"{rel} imports {mod}")

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "economy":
                val = node.value
                if (isinstance(val, ast.Name) and val.id == "bot") or (
                    isinstance(val, ast.Attribute) and val.attr == "bot"
                ):
                    violations.append(f"{rel}:{node.lineno} accesses *.economy directly")
            elif isinstance(node, ast.Name) and node.id in forbidden_names:
                violations.append(f"{rel}:{node.lineno} uses locator {node.id}")
    assert not violations, "\n".join(violations)


def test_production_follows_python_coding_conventions():
    """本番コード全体で abc / abc.ABC / @abstractmethod / @staticmethod / 不要な @classmethod を禁止する。

    Protocol 基底だけは、静的型のみで LSP を 100% 保証できる純粋契約層
    （domain/ports/bridges/・domain/games/）に限り許可する（住み分けポリシー）。それ以外の
    名目的契約層（domain/ports/repos/・cogs/shared/bridges.py・infra/db/ など）では禁止のまま。
    """
    allowed_classmethod_methods = {"from_custom_id"}
    targets = [
        *[p for folder in ("domain", "services", "infra", "cogs") for p in _iter_py(folder)],
        ROOT / "main.py",
        ROOT / "manage.py",
    ]
    violations: list[str] = []
    for path in targets:
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(a.name == "abc" for a in node.names):
                violations.append(f"{rel}:{node.lineno} imports abc")
            elif isinstance(node, ast.ImportFrom) and node.module == "abc":
                violations.append(f"{rel}:{node.lineno} imports from abc")
            elif isinstance(node, ast.ClassDef):
                protocol_allowed = _in_dirs(path, PURE_CONTRACT_DIRS)
                for base in node.bases:
                    base_name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                    if base_name == "ABC" or (base_name == "Protocol" and not protocol_allowed):
                        violations.append(f"{rel}:{node.lineno} class {node.name}({base_name})")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    dec_str = ast.unparse(dec)
                    if "abstractmethod" in dec_str:
                        violations.append(f"{rel}:{node.lineno} @{dec_str} on {node.name}")
                    elif "staticmethod" in dec_str:
                        violations.append(f"{rel}:{node.lineno} @staticmethod on {node.name}")
                    elif "classmethod" in dec_str and node.name not in allowed_classmethod_methods:
                        violations.append(f"{rel}:{node.lineno} @classmethod on {node.name}")
    assert not violations, "\n".join(violations)


def test_production_code_has_no_rng_any_parameter():
    """本番コード（domain/・services/・infra/・cogs/・main.py・manage.py）で rng 引数への Any 型を禁止する。

    乱数は Optional[random.Random] で注入し、テスト決定性を担保する。
    """
    targets = [
        *[p for folder in ("domain", "services", "infra", "cogs") for p in _iter_py(folder)],
        ROOT / "main.py",
        ROOT / "manage.py",
    ]
    violations: list[str] = []
    for path in targets:
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                    if arg.arg == "rng" and arg.annotation is not None and ast.unparse(arg.annotation) == "Any":
                        violations.append(f"{rel}:{node.lineno} {node.name} の引数 rng: Any")
    assert not violations, "\n".join(violations)


def test_production_code_has_no_tuple_any_ellipsis_annotation():
    """本番コード（domain/・services/・infra/・cogs/・main.py・manage.py）で `tuple[Any, ...]` の型注釈を禁止する。

    結果タプルは domain.errors.Outcome[T]（または実態の具体型）で表す。
    """
    targets = [
        *[p for folder in ("domain", "services", "infra", "cogs") for p in _iter_py(folder)],
        ROOT / "main.py",
        ROOT / "manage.py",
    ]
    violations: list[str] = []
    for path in targets:
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        annotations: list[tuple[int, ast.expr]] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.returns is not None:
                    annotations.append((node.lineno, node.returns))
                for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                    if arg.annotation is not None:
                        annotations.append((arg.lineno, arg.annotation))
            elif isinstance(node, ast.AnnAssign) and node.annotation is not None:
                annotations.append((node.lineno, node.annotation))
        for lineno, ann in annotations:
            if "tuple[Any, ...]" in ast.unparse(ann):
                violations.append(f"{rel}:{lineno} tuple[Any, ...]")
    assert not violations, "\n".join(violations)


def test_ports_repos_no_dict_any():
    """domain/ports/repos/ の型注釈に `dict[str, Any]`（大文字 Dict 含む）が 0 件である。

    リポジトリポートの引数・戻り値の辞書契約は domain/ports/contracts/ の TypedDict か
    正確な組み込み型で表す（ネストした list[dict[str, Any]] 等も検出する）。
    """
    violations: list[str] = []
    for path in _iter_py("domain/ports/repos"):
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        annotations: list[tuple[int, ast.expr]] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.returns is not None:
                    annotations.append((node.lineno, node.returns))
                for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                    if arg.annotation is not None:
                        annotations.append((arg.lineno, arg.annotation))
            elif isinstance(node, ast.AnnAssign) and node.annotation is not None:
                annotations.append((node.lineno, node.annotation))
        for lineno, ann in annotations:
            text = ast.unparse(ann)
            if "dict[str, Any]" in text or "Dict[str, Any]" in text:
                violations.append(f"{rel}:{lineno} {text}")
    assert not violations, "dict[str, Any] が残っています:\\n" + "\\n".join(violations)


def test_cogs_have_no_untyped_cog_parameter():
    """`cog` 引数に型が無いと Pyright が Unknown 扱いにし、消えたメソッドの呼び出しを見逃す(#61)。"""
    found = []
    for path in _iter_py("cogs"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                    if arg.arg == "cog" and arg.annotation is None:
                        found.append(f"{path.relative_to(ROOT)}:{node.lineno} {node.name}")
    assert not found, "型の無い cog 引数:\n" + "\n".join(found)


def test_debt_repo_does_not_touch_other_domains_tables():
    """破産・差し押さえは、各リポジトリのポート経由で行う。infra/db/debt/ パッケージは他ドメインの表へ SQL を書かない(#67)。"""
    import re

    pkg_dir = ROOT / "infra" / "db" / "debt"
    assert not (ROOT / "infra/db/debt.py").exists(), "debt.py は debt/ パッケージへ分割済みのはず"
    sources = sorted(pkg_dir.rglob("*.py"))
    assert sources, "infra/db/debt/ パッケージが空です"
    foreign = (
        "options",
        "bonds",
        "inventory",
        "profile",
        "user_equipment",
        "user_buffs",
        "horses",
        "seized_items",
        "crypto",
        "companies",
        "loans",
    )
    found = [
        f"{path.relative_to(ROOT)}:{m.group(0)}"
        for path in sources
        for table in foreign
        for m in re.finditer(
            rf"\b(?:FROM|UPDATE|INTO|JOIN)\s+{table}\b", path.read_text(encoding="utf-8"), flags=re.IGNORECASE
        )
    ]
    assert not found, "infra/db/debt/ が他ドメインの表を直接操作している:\n" + "\n".join(found)


def _package_defined_symbols(pkg_dir: Path) -> dict[str, str]:
    """パッケージのサブモジュール（__init__.py 除く）が定義する公開シンボル {名前: ファイル名}。"""
    defined: dict[str, str] = {}
    for path in sorted(pkg_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        for node in ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith(
                "_"
            ):
                defined[node.name] = path.name
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name.startswith("_") or (
                    isinstance(node.value, ast.Call) and getattr(node.value.func, "id", "") == "TypeVar"
                ):
                    continue
                defined[name] = path.name
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and not node.target.id.startswith("_")
            ):
                defined[node.target.id] = path.name
    return defined


def test_split_packages_reexport_every_submodule_symbol():
    """分割パッケージ（bank/・debt/・kyosai/・orders/・contracts/）はサブモジュールの公開シンボルを漏れなく再エクスポートする。"""
    import importlib

    packages = ["infra.db.bank", "infra.db.debt", "infra.db.kyosai", "cogs.market.orders", "domain.ports.contracts"]
    for dotted in packages:
        pkg = importlib.import_module(dotted)
        pkg_dir = ROOT.joinpath(*dotted.split("."))
        defined = _package_defined_symbols(pkg_dir)
        assert defined, f"{dotted} のサブモジュールが空です"
        not_exported = [f"{name} ({mod})" for name, mod in defined.items() if not hasattr(pkg, name)]
        assert not not_exported, f"{dotted} への再エクスポート漏れ:\n" + "\n".join(not_exported)
        assert set(pkg.__all__) == set(defined), f"{dotted} の __all__ とサブモジュール定義が一致しません"
    for flat in ("infra/db/bank.py", "infra/db/debt.py", "infra/db/kyosai.py"):
        assert not (ROOT / flat).exists(), f"{flat} はパッケージへ分割済みのはず"


def test_cogs_do_not_bypass_reply_error_with_raw_send():
    """cogs/ では生の send/send_message に error_embed を渡さない。エラー応答は ui.reply_error（統一入口）へ。"""
    violations: list[str] = []
    for path in _iter_py("cogs"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("send", "send_message")
            ):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "error_embed":
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno} raw send with error_embed")
                    break
    assert not violations, "ui.reply_error を迂回する生のエラー送信:\n" + "\n".join(violations)


def test_cogs_use_services_instead_of_repositories():
    """Cog は Repository を持たず、状態を変える処理と時刻を伴う処理を services/ に任せる(#65)。

    例外は IGuildRepo（サーバーの許可・告知先・表示の設定。全 Cog が共有する Discord 側の設定）と、
    #67 でそれを用途別に分けた部分契約だけ。
    """
    allowed_repo_ports = {
        "IGuildRepo",
        "IGuildApprovalRepo",
        "ILeaderboardConfigRepo",
        "INickPrefRepo",
        "IProfileDisplayReader",
    }
    violations: list[str] = []
    for path in _iter_py("cogs"):
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("domain.ports.repos"):
                for alias in node.names:
                    if alias.name.endswith("Repo") and alias.name not in allowed_repo_ports:
                        violations.append(f"{rel}:{node.lineno} imports {alias.name}")
            elif (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "repos"
                and node.attr != "guild"
            ):
                violations.append(f"{rel}:{node.lineno} uses *.repos.{node.attr}")
    assert not violations, "\n".join(violations)


def _max_if_for_while_depth(node: ast.AST, depth: int = 0) -> int:
    """関数本体内の if / for / async for / while の最大ネスト深度（elif は同階層、ネストした関数・クラス定義は別カウント）。"""
    best = depth
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While)):
            is_elif = (
                isinstance(node, ast.If)
                and isinstance(child, ast.If)
                and len(node.orelse) == 1
                and child is node.orelse[0]
            )
            next_depth = depth if is_elif else depth + 1
            best = max(best, _max_if_for_while_depth(child, next_depth))
        else:
            best = max(best, _max_if_for_while_depth(child, depth))
    return best


def test_production_if_for_while_nesting_at_most_3():
    """本番コード（domain/・services/・infra/・cogs/・main.py・manage.py）の if/for/while ネストは 3 以内。

    深い分岐が出たときは、単にプライベートヘルパへ逃がすのではなく、条件知識のコンテキスト側からの注入・
    多態化・事前絞り込み・ガード節・match/case で本質的に平坦化する。
    """
    targets = [
        *[p for folder in ("domain", "services", "infra", "cogs") for p in _iter_py(folder)],
        ROOT / "main.py",
        ROOT / "manage.py",
    ]
    violations: list[str] = []
    for path in targets:
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                d = _max_if_for_while_depth(node)
                if d > 3:
                    violations.append(f"{rel}:{node.lineno} {node.name} (depth={d})")
    assert not violations, "if/for/while のネストが 4 以上:\n" + "\n".join(violations)


_DB_DIR = ROOT / "infra" / "db"
_WRITE_TABLE_RE = re.compile(
    r"(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+([a-z_][a-z0-9_]*)", re.IGNORECASE
)
_SHARED_TABLE_REF_RE = re.compile(
    r"\b(?:FROM|JOIN|UPDATE|INTO)\s+(inventory|user_equipment|user_buffs)\b", re.IGNORECASE
)


def _db_source_files() -> list[Path]:
    """infra/db/ 配下の全 .py（schema/ 配下は除外）。"""
    return [p for p in sorted(_DB_DIR.rglob("*.py")) if "schema" not in p.relative_to(_DB_DIR).parts]


def _repo_owner_name(path: Path) -> str:
    """テーブル所有者名。サブパッケージのファイルは f"{親ディレクトリ名}/"、直下のファイルはファイル名。"""
    if len(path.relative_to(_DB_DIR).parts) == 1:
        return path.name
    return f"{path.parent.name}/"


def _write_table_owners() -> dict[str, set[str]]:
    """書き込み先テーブル -> {所有者名} の対応表（ON CONFLICT ... DO UPDATE SET の set は除外）。"""
    owners: dict[str, set[str]] = {}
    for path in _db_source_files():
        owner = _repo_owner_name(path)
        for m in _WRITE_TABLE_RE.finditer(path.read_text(encoding="utf-8")):
            table = m.group(1).lower()
            if table != "set":
                owners.setdefault(table, set()).add(owner)
    return owners


def test_all_repositories_enforce_single_table_write_ownership():
    """infra/db/ 全リポジトリのテーブル書き込み単一所有権ガードレール。

    共有プリミティブが所有するテーブルは単一ファイルのみが書き込み、競馬ドメインの明示ペア
    （horse.py / race.py）を除く全書き込み対象テーブルで所有者が 1 つだけであることを検証する。
    """
    owners = _write_table_owners()
    assert owners, "infra/db/ から書き込み対象テーブルが検出できません"

    primitive_owners = {
        "inventory": {"inventory.py"},
        "user_equipment": {"profile.py"},
        "user_buffs": {"profile.py"},
        "accounts": {"account.py"},
        "ledger": {"account.py"},
        "teiai_debts": {"account.py"},
        "user_play_stats": {"account.py"},
        "discord_posts": {"discord_posts.py"},
    }
    wrong_primitives = [
        f"{table}: {sorted(owners.get(table, set()))} (期待: {sorted(expected)})"
        for table, expected in primitive_owners.items()
        if owners.get(table) != expected
    ]
    assert not wrong_primitives, "共有プリミティブ所有テーブルの単一所有に違反:\n" + "\n".join(wrong_primitives)

    cohesive_pairs = {
        "horses": frozenset({"horse.py", "race.py"}),
        "horse_entries": frozenset({"horse.py", "race.py"}),
        "horse_race_registrations": frozenset({"horse.py", "race.py"}),
    }
    wrong_pairs = [
        f"{table}: {sorted(owners.get(table, set()))} (期待: {sorted(expected)})"
        for table, expected in cohesive_pairs.items()
        if frozenset(owners.get(table, set())) != expected
    ]
    assert not wrong_pairs, "競馬ペアの共有ホワイトリストに違反:\n" + "\n".join(wrong_pairs)

    shared = {
        table: sorted(owners[table]) for table in owners if len(owners[table]) > 1 and table not in cohesive_pairs
    }
    assert not shared, "複数リポジトリが書き込むテーブル:\n" + "\n".join(f"{t}: {o}" for t, o in shared.items())

    allowed_refs = {
        "inventory.py": {"inventory", "user_equipment", "user_buffs"},
        "profile.py": {"inventory", "user_equipment", "user_buffs"},
        "assets.py": {"inventory"},  # inventory の読み取り専用参照のみ許可
    }
    violations = []
    for path in _db_source_files():
        allowed = allowed_refs.get(_repo_owner_name(path), set())
        for m in _SHARED_TABLE_REF_RE.finditer(path.read_text(encoding="utf-8")):
            if m.group(1).lower() not in allowed:
                violations.append(f"{path.relative_to(ROOT)}: {m.group(0)}")
    assert not violations, "共有プリミティブのテーブルへの生 SQL 参照（許可ファイル以外に存在）:\n" + "\n".join(violations)


@pytest.mark.real_scope
def test_repository_bundle_shares_core_primitives():
    """open_repositories() の InventoryCore / ProfileCore が全リポジトリと for_guild ビューで同一インスタンス共有される。"""
    bundle = open_repositories(":memory:", strict=True)
    try:
        inventory_holders = (
            bundle.relics,
            bundle.records,
            bundle.shop,
            bundle.auction,
            bundle.horses,
            bundle.labor,
            bundle.p2p,
            bundle.wallet,
        )
        profile_holders = (bundle.records, bundle.guild, bundle.shop, bundle.escrow, bundle.p2p, bundle.wallet)
        for repo in inventory_holders:
            assert repo._inventory is bundle.inventory, f"{type(repo).__name__} が inventory を共有していない"
        for repo in profile_holders:
            assert repo._profile is bundle.profile, f"{type(repo).__name__} が profile を共有していない"

        scoped = bundle.for_guild(1)
        assert scoped.inventory is bundle.inventory
        assert scoped.profile is bundle.profile
        scoped_inventory_holders = (
            scoped.relics,
            scoped.records,
            scoped.shop,
            scoped.auction,
            scoped.horses,
            scoped.labor,
            scoped.p2p,
            scoped.wallet,
        )
        scoped_profile_holders = (scoped.records, scoped.guild, scoped.shop, scoped.escrow, scoped.p2p, scoped.wallet)
        for repo in scoped_inventory_holders:
            assert repo._inventory is bundle.inventory, (
                f"for_guild(1) の {type(repo).__name__} が inventory を共有していない"
            )
        for repo in scoped_profile_holders:
            assert repo._profile is bundle.profile, (
                f"for_guild(1) の {type(repo).__name__} が profile を共有していない"
            )
    finally:
        bundle.close()
