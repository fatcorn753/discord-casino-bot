"""/会社 の Cog と画面: 設立の告知・状況パネルのボタン・倒産の告知。"""

import asyncio
import datetime
from types import SimpleNamespace

import pytest

from cogs.company import cog as company_cog
from cogs.company import views
from domain import company as co
from domain.ports.clock import IClock
from services.company import CompanyService
from tests.fakes import as_bot, as_interaction, invoke, must
from tests.support import make_test_bot

U = 1
DAY = 86400
NOON = co.day_start("2026-11-17", 9.0) + DAY / 2


class _Resp:
    def __init__(self, owner):
        self.owner = owner

    async def send_message(self, *a, **kw):
        self.owner.sent.append({**kw, "content": a[0]} if a else kw)

    async def edit_message(self, **kw):
        self.owner.edited.append(kw)

    async def send_modal(self, modal):
        self.owner.modals.append(modal)


class _It:
    def __init__(self, uid, guild_id=0, values=None):
        self.user = SimpleNamespace(id=uid, mention=f"<@{uid}>")
        self.guild_id = guild_id
        self.guild = SimpleNamespace(id=guild_id)
        self.data = {"values": values or []}
        self.sent, self.edited, self.modals = [], [], []
        self.response = _Resp(self)


class Chan:
    def __init__(self):
        self.sent = []

    def permissions_for(self, _):
        return SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)

    async def send(self, *a, **kw):
        self.sent.append(kw)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    bot = make_test_bot(str(tmp_path / "t.db"))
    chan = Chan()
    guild = SimpleNamespace(id=0, me=object(), system_channel=chan, get_channel=lambda cid: chan, name="g")
    bot.guilds = [guild]
    monkeypatch.setattr("domain.approval.is_allowed", lambda *a: True)
    now = [NOON]
    svc = CompanyService(bot.repos.company, _Clock(now))
    cog = company_cog.CompanyCog(as_bot(bot), bot.config, svc, bot.repos.guild)
    bot.repos.wallet.credit(U, 5_000_000, "テスト")
    return bot.repos, chan, guild, cog, now


class _Clock(IClock):
    """テストが進める時刻（now[0]）を返す。"""

    def __init__(self, now):
        self._now = now

    def now(self):
        return datetime.datetime.fromtimestamp(self._now[0], datetime.timezone(datetime.timedelta(hours=9)))

    def now_ts(self):
        return self._now[0]


def button(panel, label):
    return next(c for c in panel.children if getattr(c, "label", "") and label in c.label)


def test_found_command_creates_the_company_and_announces_it(setup):
    repos, chan, guild, cog, now = setup
    it = _It(U)
    asyncio.run(invoke(cog.found, cog, it, "帝愛商事", SimpleNamespace(value="farm")))
    assert "設立しました" in it.sent[0]["embed"].title and "帝愛商事" in it.sent[0]["embed"].description
    assert repos.company.company_get(U, now[0])["kind"] == "farm"
    again = _It(U)
    asyncio.run(invoke(cog.found, cog, again, "二社目", SimpleNamespace(value="mine")))
    assert "設立できません" in again.sent[0]["embed"].title and again.sent[0]["ephemeral"]


def test_status_shows_a_guide_without_buttons_when_there_is_no_company(setup):
    repos, chan, guild, cog, now = setup
    it = _It(U)
    asyncio.run(invoke(cog.status, cog, it))
    assert "設立" in it.sent[0]["embed"].description and it.sent[0]["view"].children == []


def test_panel_buttons_ship_invest_upgrade_and_change_kind(setup):
    repos, chan, guild, cog, now = setup
    repos.company.company_found(U, "帝愛商事", "mine", now[0])
    now[0] += DAY / 4
    panel = views.CompanyPanel(cog._company, U, 0)
    assert "帝愛商事" in must(panel.embed().title)

    it = _It(U)
    asyncio.run(button(panel, "出荷").callback(as_interaction(it)))
    assert "出荷しました" in it.edited[0]["embed"].description
    assert repos.company.company_get(U, now[0])["funds"] > 0

    it = _It(U)
    asyncio.run(button(panel, "出資").callback(as_interaction(it)))
    modal = it.modals[0]
    modal.amount._value = "1,000,000"
    it2 = _It(U)
    asyncio.run(modal.on_submit(it2))
    assert "出資しました" in it2.edited[0]["embed"].description
    modal.amount._value = "abc"
    it3 = _It(U)
    asyncio.run(modal.on_submit(it3))
    assert "金額が正しくありません" in it3.sent[0]["embed"].title

    it = _It(U)
    asyncio.run(button(panel, "増築").callback(as_interaction(it)))
    assert "Lv2" in it.edited[0]["embed"].description and repos.company.company_get(U, now[0])["level"] == 2

    it = _It(U)
    asyncio.run(button(panel, "種類を変える").callback(as_interaction(it)))
    kind_view = it.sent[0]["view"]
    it2 = _It(U, values=["farm"])
    asyncio.run(kind_view.children[0].callback(it2))
    assert "建て直しました" in it2.edited[0]["embed"].title
    assert repos.company.company_get(U, now[0])["kind"] == "farm"


def test_panel_rejects_other_users_and_reports_failures(setup):
    repos, chan, guild, cog, now = setup
    repos.company.company_found(U, "帝愛商事", "mine", now[0])
    panel = views.CompanyPanel(cog._company, U, 0)
    stranger = _It(2)
    assert asyncio.run(panel.interaction_check(as_interaction(stranger))) is False
    assert "他の人の会社" in stranger.sent[0]["content"]
    it = _It(U)
    asyncio.run(button(panel, "増築").callback(as_interaction(it)))  # 会社資金が無い
    assert "できません" in it.sent[0]["embed"].title and "会社資金が足りません" in it.sent[0]["embed"].description


def test_run_once_announces_a_bankruptcy(setup):
    repos, chan, guild, cog, now = setup
    repos.company.company_found(U, "帝愛商事", "mine", now[0])
    now[0] += 3 * DAY
    asyncio.run(cog.run_once())
    assert "倒産" in chan.sent[0]["embed"].title and "帝愛商事" in chan.sent[0]["embed"].description
    assert repos.company.company_get(U, now[0]) is None
    asyncio.run(cog.run_once())
    assert len(chan.sent) == 1  # 同じ倒産を、二度は告知しない


def test_stake_dividend_and_holdings_commands(setup):
    """#71: /会社 出資・配当率・持分。"""
    repos, chan, guild, cog, now = setup
    asyncio.run(cog.found.callback(cog, _It(U), "帝愛商事", SimpleNamespace(value="farm")))
    investor = 2
    repos.wallet.credit(investor, 500_000, "テスト")
    owner = SimpleNamespace(id=U, mention=f"<@{U}>")
    it = _It(investor)
    asyncio.run(cog.stake.callback(cog, it, owner, 50_000))
    assert "出資しました" in it.sent[0]["embed"].title and "50,000" in it.sent[0]["embed"].description
    bad = _It(investor)
    asyncio.run(cog.stake.callback(cog, bad, owner, 10))
    assert "出資できません" in bad.sent[0]["embed"].title and bad.sent[0]["ephemeral"]
    rate = _It(U)
    asyncio.run(cog.dividend.callback(cog, rate, 30))
    assert "30%" in rate.sent[0]["embed"].description
    too_high = _It(U)
    asyncio.run(cog.dividend.callback(cog, too_high, 90))
    assert "配当率を変えられません" in too_high.sent[0]["embed"].title
    mine = _It(investor)
    asyncio.run(cog.holdings.callback(cog, mine))
    text = mine.sent[0]["embed"].description
    assert "帝愛商事" in text and "持分 100%" in text and "配当率 30%" in text
    nothing = _It(3)
    asyncio.run(cog.holdings.callback(cog, nothing))
    assert "まだどの会社にも出資していません" in nothing.sent[0]["embed"].description
    panel = views.CompanyPanel(cog._company, U, 0)
    fields = {f.name: f.value for f in panel.embed().fields}
    assert "🤝 出資" in fields and "出資者 1人" in must(fields["🤝 出資"])


def test_company_ui_and_help_use_teiai_bussan_pool_wording(setup, monkeypatch):
    from cogs.help import texts as help_texts
    from cogs.shared.messages import format_failure
    from domain.errors import fail

    repos, _chan, _guild, cog, now = setup
    monkeypatch.setattr(co, "DAILY_GRANT", 0)
    repos.company.company_found(U, "帝愛商事", "mine", now[0])
    with repos.db.tx():
        repos.bank.house_pay_for(0, repos.bank.house_balance_for(0) - 100)
    now[0] += DAY / 2
    panel = views.CompanyPanel(cog._company, U, 0)
    assert any(f.name == "📦 帝愛物産の買取予算" for f in panel.embed().fields)

    it = _It(U)
    asyncio.run(button(panel, "出荷").callback(as_interaction(it)))
    assert "帝愛物産の買取予算が足りず" in it.edited[0]["embed"].description
    assert "帝愛物産の買取予算（買取プールと、今日の買取枠）" in format_failure(fail("company_no_budget"))
    company_help = "\n".join(help_texts.PAGES["company"][1])
    assert "帝愛物産の買取プール" in company_help and "胴元の口座" not in company_help
