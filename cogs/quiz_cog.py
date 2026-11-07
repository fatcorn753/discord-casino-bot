"""/クイズ — 持ち主が作った4択に答えて、正解でペリカをもらう（時事ネタ・豆知識）。

- 問題は全サーバー共通。持ち主が `/運営 クイズ 追加`（Bot とのDMで使う）の入力フォームで登録する（選択肢の1行目が正解。並びは登録時に1回だけシャッフルして固定する）。
- 1問につき1人1回。`/クイズ 挑戦` で、まだ答えていない一番古い問題が自分だけに見える形で出る。
- 正解で、問題ごとの報酬を受け取る。正解と解説は、正誤にかかわらず答えたあとに見せる。
- 誰でも `/クイズ 出題` で自分の問題を出せる（そのサーバーの中だけ。正解者への報酬は出題者のペリカから払い、出題者は答えられない。
  出題者への報酬（基本・正答率ボーナス・参加報酬）は、回答のたびにその時点の合計との差を払う（減るときは戻す）。数字は core/quizpay.py）。
- 公開された問題に誰かが答えると、その場で「〇〇が正解！／不正解…」がみんなに見える形で出る（選んだ答えと正解は出さない）。
- 登録時に「告知」すると、全サーバーの告知チャンネルに問題と4択ボタンが出て、その場で答えられる。
- 期限を過ぎた問題は出なくなる。ミッション・連勝・週間ランキング・突発イベントには数えない。
"""

import re
import time
from dataclasses import dataclass
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.weekly_cog import announce_channel
from core import approval, logthrottle, quizpay, ui
from core.duration import format_minutes, parse_duration

MAX_CHOICE_LEN = 80
MAX_VERDICT_LEN = 20  # 正解・不正解の見出しの文言の最大文字数
DEFAULT_RIGHT, DEFAULT_WRONG = "正解！", "不正解…"
DEFAULT_REWARD = 100
LETTERS = "ABCD"


def parse_choices(text: str) -> list:
    """入力欄から選択肢を取り出す。4行・空行なし・重複なし。問題があれば ValueError。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) != 4:
        raise ValueError(f"選択肢は4行で入力してください（いま {len(lines)} 行）。1行目が正解です。")
    if len(set(lines)) != 4:
        raise ValueError("同じ選択肢が2つあります。")
    if any(len(ln) > MAX_CHOICE_LEN for ln in lines):
        raise ValueError(f"選択肢は1つ{MAX_CHOICE_LEN}文字までです。")
    return lines


def parse_int(text: str, label: str) -> int:
    try:
        return int(text.strip().replace(",", ""))
    except ValueError:
        raise ValueError(f"{label}は数字で入力してください。") from None


def creator_label(q: dict) -> str:
    """問題に出す制作者の名前。登録時の名前、なければ（古い問題）出題者のメンション、持ち主の問題なら「運営」。"""
    if q.get("creator_name"):
        return q["creator_name"]
    return f"<@{q['author_id']}>" if q.get("author_id") else "運営"


def parse_terms(text: str) -> tuple:
    """「報酬 / 期間」の入力欄を、(報酬, 期間の文字列) に分ける。期間は省略できる（空文字を返す）。"""
    parts = re.split(r"[/／]", text, maxsplit=1)
    return parse_int(parts[0], "報酬"), (parts[1].strip() if len(parts) > 1 else "")


def parse_verdicts(text: str) -> tuple:
    """「正解・不正解の文言」の入力欄を、(正解のとき, 不正解のとき) にする。1行目が正解、2行目が不正解。省略した方は空文字（既定になる）。"""
    lines = [ln.strip() for ln in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    if len(lines) > 2:
        raise ValueError("文言は2行まで（1行目: 正解のとき、2行目: 不正解のとき）です。")
    right, wrong = (lines + ["", ""])[:2]
    if len(right) > MAX_VERDICT_LEN or len(wrong) > MAX_VERDICT_LEN:
        raise ValueError(f"文言は1つ{MAX_VERDICT_LEN}文字までです。")
    return right, wrong


def verdict(q: dict, correct: bool) -> str:
    """正解・不正解の見出しの言葉。出題者が決めていなければ既定。"""
    if q:
        text = (q.get("msg_right") if correct else q.get("msg_wrong")) or ""
        if text:
            return text
    return DEFAULT_RIGHT if correct else DEFAULT_WRONG


def question_embed(q: dict, order: list, remaining: int) -> discord.Embed:
    body = (
        q["question"]
        + "\n\n"
        + "\n".join(f"**{LETTERS[i]}.** {c}" for i, c in enumerate(order))
        + f"\n\n出題: **{creator_label(q)}**"
    )
    e = ui.embed(f"📰 クイズ #{q['id']}", body, ui.INFO)
    e.set_footer(text=f"正解で {q['reward']:,}ペリカ ｜ 1問1回だけ ｜ あと{remaining}問")
    return e


def result_embed(result: dict, more: int, quiz: dict = None) -> discord.Embed:
    if result["correct"] and result["reward_blocked"]:
        e = ui.embed(
            f"⭕ {verdict(quiz, True)}",
            f"**報酬はありません**（ゲームを{quizpay.MIN_GAMES_FOR_REWARD}回以上遊ぶと、みんなの問題の報酬をもらえます）",
            ui.OK,
        )
    elif result["correct"]:
        e = ui.embed(f"⭕ {verdict(quiz, True)}", f"**+{ui.pelica(result['reward'])}**", ui.OK)
    else:
        e = ui.embed(f"❌ {verdict(quiz, False)}", f"正解は **{result['answer']}** でした。", ui.ERROR)
    if result["explanation"]:
        e.add_field(name="解説", value=result["explanation"][:1000], inline=False)
    if more:
        e.set_footer(text=f"まだ{more}問あります。/クイズ 挑戦")
    return e


def public_embed(q: dict) -> discord.Embed:
    body = q["question"] + "\n\n" + "\n".join(f"**{LETTERS[i]}.** {c}" for i, c in enumerate(q["order"]))
    body += f"\n\n出題: **{creator_label(q)}**" + (
        "（出題者は答えられません。正解の報酬は出題者が払います）" if q["author_id"] else ""
    )
    if q["expires"] is not None:
        body += f"\n⏳ 期限 <t:{int(q['expires'])}:R>"
    e = ui.embed(f"📰 {'みんなのクイズ' if q['author_id'] else '新しいクイズ'} #{q['id']}", body, ui.GOLD)
    e.set_footer(text=f"下のボタンで答える。正解で {q['reward']:,}ペリカ ｜ 1人1回だけ")
    return e


def names_field(user_ids: list, limit: int = 900) -> str:
    """メンションを並べる（埋め込みの中なので通知は飛ばない）。長すぎるときは、残りを人数で示す。"""
    if not user_ids:
        return "なし"
    out, used = [], 0
    for i, uid in enumerate(user_ids):
        part = f"<@{uid}>"
        if used + len(part) + 1 > limit:
            return " ".join(out) + f" ほか{len(user_ids) - i}人"
        out.append(part)
        used += len(part) + 1
    return " ".join(out)


def result_with_names(q: dict) -> discord.Embed:
    e = ui.embed(f"📰 クイズ #{q['id']} 終了", settle_text(q), ui.INFO)
    e.add_field(name=f"⭕ 正解者（{len(q['right_users'])}人）", value=names_field(q["right_users"]), inline=False)
    e.add_field(name=f"❌ 不正解者（{len(q['wrong_users'])}人）", value=names_field(q["wrong_users"]), inline=False)
    return e


def settle_text(q: dict) -> str:
    n, c = q["participants"], q["correct_count"]
    total = q["base"] + q["bonus"] + q["part"]
    if total == 0:
        return f"回答者が{quizpay.MIN_PARTICIPANTS}人に満たなかった（{n}人）ので、出題報酬はありません。"
    return (
        f"参加 **{n}人** ／ 正解 **{c}人**（正答率 **{c / n:.0%}**）\n"
        f"出題報酬: 基本 {ui.pelica(q['base'])} ＋ 正答率ボーナス {ui.pelica(q['bonus'])} ＋ 参加報酬 {ui.pelica(q['part'])}"
        f" ＝ **{ui.pelica(total)}**（回答のたびに受け取り済み）\n（正答率{quizpay.TARGET_RATE:.0%}に近いほど、ボーナスと参加報酬が大きくなります）"
    )


async def record_answer(bot, interaction: discord.Interaction, quiz_id: int, pos: int) -> None:
    """公開された問題のボタン。押した人が答える（結果は本人だけに見える）。"""
    eco = bot.economy.for_guild(interaction.guild_id)
    quiz = bot.economy.quiz_get(quiz_id)
    if quiz is None:
        return await interaction.response.send_message(embed=ui.error_embed("この問題は終了しました"), ephemeral=True)
    if not approval.is_allowed(bot.economy, bot.config, interaction.guild_id, interaction.user.id):
        return await interaction.response.send_message(embed=ui.error_embed("利用できません"), ephemeral=True)
    ok, result = eco.quiz_answer(interaction.user.id, quiz_id, quiz["order"][pos], time.time())
    if not ok:
        return await interaction.response.send_message(embed=ui.error_embed("答えられません", result), ephemeral=True)
    await interaction.response.send_message(
        embed=result_embed(result, eco.quiz_next(interaction.user.id, time.time())[1], quiz), ephemeral=True
    )
    # 結果（正解か不正解か）だけを、みんなに見える形で出す。選んだ答えと正解は出さない（まだ答えていない人のため）
    if result["correct"]:
        paid = "報酬なし" if result["reward_blocked"] else f"+{ui.pelica(result['reward'])}"
        line = f"⭕ <@{interaction.user.id}> が #{quiz_id} に **{verdict(quiz, True)}**（{paid}）"
    else:
        line = f"❌ <@{interaction.user.id}> が #{quiz_id} に **{verdict(quiz, False)}**"
    if result["author_debt_added"]:  # 出題者の残高で払いきれなかった分は、出題者の借金になる
        line += (
            f"\n⚠️ 出題者 <@{result['author_id']}> の残高が足りないため、不足 "
            f"{ui.pelica(result['author_debt_added'])} は出題者の借金になりました。"
        )
    if result["author_delta"]:  # 出題者の受け取りも、その場で見せる（正答率が40%から離れると減ることがある）
        sign = "+" if result["author_delta"] > 0 else "−"
        line += f"\n💰 出題者 <@{result['author_id']}> {sign}{ui.pelica(abs(result['author_delta']))}（累計 {ui.pelica(result['author_total'])}）"
    try:
        await interaction.followup.send(line, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as exc:
        logthrottle.warn(
            ("quiz-public", interaction.guild_id), f"回答の結果を投稿できません: guild={interaction.guild_id}: {exc!r}"
        )


class PublicButton(discord.ui.DynamicItem[discord.ui.Button], template=r"quiz:(?P<qid>\d+):(?P<pos>[0-3])"):
    """公開された問題の選択肢ボタン。再起動しても動くように、問題の番号と位置を custom_id に入れている。"""

    def __init__(self, qid: int, pos: int):
        super().__init__(
            discord.ui.Button(label=LETTERS[pos], style=discord.ButtonStyle.primary, custom_id=f"quiz:{qid}:{pos}")
        )
        self.qid, self.pos = qid, pos

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match):
        return cls(int(match["qid"]), int(match["pos"]))

    async def callback(self, interaction: discord.Interaction):
        await record_answer(interaction.client, interaction, self.qid, self.pos)


def public_view(qid: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for pos in range(4):
        view.add_item(PublicButton(qid, pos))
    return view


class AnswerView(discord.ui.View):
    """4択のボタン。押した人が挑戦者本人のときだけ受け付け、答えたらボタンを外す。"""

    def __init__(self, cog: "QuizCog", user_id: int, quiz: dict, order: list):
        super().__init__(timeout=600)
        self.cog, self.user_id, self.quiz, self.order = cog, user_id, quiz, order
        for i in range(4):
            btn = discord.ui.Button(label=LETTERS[i], style=discord.ButtonStyle.primary)
            btn.callback = self._make_callback(i)
            self.add_item(btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=ui.error_embed("挑戦者本人だけが答えられます"), ephemeral=True
            )
            return False
        return True

    def _make_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            eco = self.cog.bot.economy.for_guild(interaction.guild_id)
            ok, result = eco.quiz_answer(self.user_id, self.quiz["id"], self.order[index], time.time())
            if not ok:
                self.stop()
                return await interaction.response.edit_message(
                    embed=ui.error_embed("答えられません", result), view=None
                )
            _, more = eco.quiz_next(self.user_id, time.time())
            self.stop()
            await interaction.response.edit_message(embed=result_embed(result, more, self.quiz), view=None)

        return callback


@dataclass
class FormValues:
    """出題フォームの入力を、検証したあとの値。minutes が None なら無期限。"""

    choices: list
    reward: int
    minutes: Optional[int]
    msg_right: str
    msg_wrong: str


def verdicts_input() -> discord.ui.TextInput:
    return discord.ui.TextInput(
        label="正解・不正解の文言（2行。省略可）",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=45,
        placeholder="1行目: 正解のとき（例: お見事！）\n2行目: 不正解のとき（例: おしい！）",
    )


class QuizForm(discord.ui.Modal):
    """持ち主の登録フォームと、みんなの出題フォームに共通の入力欄と検証。「報酬 / 期間」と「文言」は、子が足す（欄は5つまで）。"""

    question = discord.ui.TextInput(label="問題文", style=discord.TextStyle.paragraph, max_length=500)
    choices = discord.ui.TextInput(
        label="選択肢（4行。1行目が正解）", style=discord.TextStyle.paragraph, max_length=400
    )
    explanation = discord.ui.TextInput(
        label="解説（なくてもよい）", style=discord.TextStyle.paragraph, required=False, max_length=800
    )

    def read(
        self, max_reward: Optional[int], default_minutes: Optional[int], min_minutes: int, max_minutes: int
    ) -> FormValues:
        """入力を検証して返す。問題があれば ValueError（文面はそのまま利用者に見せる）。"""
        choices = parse_choices(self.choices.value)
        reward, duration = parse_terms(self.terms.value)
        if reward < 1 or (max_reward is not None and reward > max_reward):
            raise ValueError("報酬は 1 以上で入力してください。")
        msg_right, msg_wrong = parse_verdicts(self.verdicts.value)
        minutes = parse_duration(duration) if duration else default_minutes
        if minutes is not None and not min_minutes <= minutes <= max_minutes:
            raise ValueError(
                f"有効な期間は {format_minutes(min_minutes)}〜{format_minutes(max_minutes)} の範囲で入力してください。"
            )
        return FormValues(choices, reward, minutes, msg_right, msg_wrong)


class AddModal(QuizForm, title="クイズを追加"):
    terms = discord.ui.TextInput(
        label="報酬 / 期間（例: 100 / 3日。期間は省略可）",
        default=str(DEFAULT_REWARD),
        max_length=30,
        placeholder="報酬は 1 以上（上限なし）。期間は 90分・2時間・3日 など（省略で無期限）",
    )
    verdicts = verdicts_input()

    def __init__(self, cog: "QuizCog", announce: bool = True, guild=None, channel=None):
        super().__init__()
        self.cog, self.announce, self.guild, self.channel = cog, announce, guild, channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            form = self.read(None, None, 1, 365 * 1440)
        except ValueError as exc:
            return await interaction.response.send_message(embed=ui.error_embed("入力エラー", str(exc)), ephemeral=True)
        now = time.time()
        expires = now + form.minutes * 60 if form.minutes else None
        qid = self.cog.bot.economy.quiz_add(
            self.question.value.strip(),
            form.choices,
            self.explanation.value.strip(),
            form.reward,
            expires,
            now,
            interaction.user.display_name,
            form.msg_right,
            form.msg_wrong,
        )
        await interaction.response.defer(ephemeral=True)
        posted = await self.cog.broadcast(qid, self.guild, self.channel) if self.announce else None
        note = (
            "告知はしていません。`/クイズ 挑戦` で出ます。" if posted is None else f"{posted}サーバーに告知しました。"
        )
        period = f"期間: {format_minutes(form.minutes)}" if form.minutes else "期間: 無期限"
        await interaction.followup.send(
            embed=ui.ok_embed(
                f"クイズ #{qid} を登録しました",
                f"正解: **{form.choices[0]}**　報酬: {ui.pelica(form.reward)}　{period}\n{note}",
            ),
            ephemeral=True,
        )


class UserModal(QuizForm, title="クイズを出題"):
    terms = discord.ui.TextInput(
        label="報酬 / 期間（例: 100 / 2日。期間は省略可）",
        default="100",
        max_length=30,
        placeholder=f"報酬は正解1人あたり 1 以上（上限なし。あなたのペリカ）。期間は最長7日、省略で{format_minutes(quizpay.USER_DEFAULT_MINUTES)}",
    )
    verdicts = verdicts_input()

    def __init__(self, cog: "QuizCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        try:
            form = self.read(
                None,
                quizpay.USER_DEFAULT_MINUTES,
                quizpay.USER_MIN_MINUTES,
                quizpay.USER_MAX_MINUTES,
            )
        except ValueError as exc:
            return await interaction.response.send_message(embed=ui.error_embed("入力エラー", str(exc)), ephemeral=True)
        eco = self.cog.bot.economy.for_guild(interaction.guild_id)
        ok, result = eco.quiz_post(
            interaction.user.id,
            self.question.value.strip(),
            form.choices,
            self.explanation.value.strip(),
            form.reward,
            form.minutes,
            interaction.channel_id,
            time.time(),
            interaction.user.display_name,
            form.msg_right,
            form.msg_wrong,
        )
        if not ok:
            return await interaction.response.send_message(
                embed=ui.error_embed("出題できません", result), ephemeral=True
            )
        await interaction.response.send_message(
            embed=public_embed(eco.quiz_get(result)),
            view=public_view(result),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        eco.quiz_set_message(result, (await interaction.original_response()).id)


class QuizCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_dynamic_items(PublicButton)
        self.tick.start()

    def cog_unload(self):
        self.tick.cancel()

    # ------------------------------ 出題者への報酬（期限が来たら） ------------------------------ #
    @tasks.loop(minutes=1)
    async def tick(self):
        try:
            await self.run_once()
        except Exception as exc:
            print(f"クイズの精算に失敗: {exc!r}")

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def run_once(self) -> None:
        now = time.time()
        for guild in list(self.bot.guilds):
            if not approval.is_allowed(self.bot.economy, self.bot.config, guild.id, 0):
                continue
            try:
                for q in self.bot.economy.for_guild(guild.id).quiz_settle(now):
                    await self.announce_settle(guild, q)
            except Exception as exc:  # 1サーバーの失敗で、ほかを止めない
                print(f"クイズを精算できません: guild={guild.id}: {exc!r}")

    async def announce_settle(self, guild: discord.Guild, q: dict) -> None:
        """出題の投稿からボタンを外し、結果と出題報酬を同じチャンネルに出す（出題者にはメンションする）。"""
        channel = guild.get_channel(q["channel_id"]) if q["channel_id"] else None
        if channel is None:
            return
        closed = public_embed(q)
        closed.title = closed.title.replace("📰 ", "📰 【終了】")
        result = result_with_names(q)
        try:
            if q["message_id"]:
                await channel.get_partial_message(q["message_id"]).edit(embed=closed, view=None)
            await channel.send(
                content=f"<@{q['author_id']}>",
                embed=result,
                allowed_mentions=discord.AllowedMentions(
                    users=[discord.Object(id=q["author_id"])], roles=False, everyone=False
                ),
            )
        except discord.HTTPException as exc:
            logthrottle.warn(("quiz-settle", guild.id), f"クイズの結果を投稿できません: guild={guild.id}: {exc!r}")

    async def broadcast(self, quiz_id: int, only_guild=None, only_channel=None) -> int:
        """問題を、許可されている全サーバーの告知チャンネルに出す。出せた投稿の数を返す。

        only_guild を渡すとそのサーバーだけ、only_channel も渡すとそのチャンネルに出す
        （持ち主が指定した場所には、許可の有無にかかわらず出す）。
        """
        quiz = self.bot.economy.quiz_get(quiz_id)
        count = 0
        for guild in [only_guild] if only_guild is not None else list(self.bot.guilds):
            if only_guild is None and not approval.is_allowed(self.bot.economy, self.bot.config, guild.id, 0):
                continue
            channel = only_channel or announce_channel(guild, self.bot.economy.for_guild(guild.id))
            if channel is None:
                continue
            try:
                await channel.send(
                    embed=public_embed(quiz), view=public_view(quiz_id), allowed_mentions=discord.AllowedMentions.none()
                )
                count += 1
            except discord.HTTPException as exc:
                logthrottle.warn(("quiz", guild.id), f"クイズを投稿できません: guild={guild.id}: {exc!r}")
        return count

    group = app_commands.Group(name="クイズ", description="📰 時事ネタ・豆知識の4択に答えて、正解でペリカをもらう")

    @group.command(name="挑戦", description="クイズに挑戦する（正解でペリカがもらえる）")
    async def play(self, interaction: discord.Interaction):
        eco = self.bot.economy.for_guild(interaction.guild_id)
        quiz, remaining = eco.quiz_next(interaction.user.id, time.time())
        if quiz is None:
            return await interaction.response.send_message(
                embed=ui.embed("📰 クイズ", "いま挑戦できる問題はありません。新しい問題を待ってください。", ui.INFO),
                ephemeral=True,
            )
        await interaction.response.send_message(
            embed=question_embed(quiz, quiz["order"], remaining),
            view=AnswerView(self, interaction.user.id, quiz, quiz["order"]),
            ephemeral=True,
        )

    @group.command(name="出題", description="自分のクイズを出す（報酬は自分のペリカから払う）")
    async def post(self, interaction: discord.Interaction):
        await interaction.response.send_modal(UserModal(self))

    @group.command(name="取り下げ", description="自分が出したクイズを終わらせる")
    @app_commands.rename(quiz_id="番号")
    @app_commands.describe(quiz_id="取り下げる問題（自分の問題だけ候補に出ます）")
    async def withdraw(self, interaction: discord.Interaction, quiz_id: int):
        done = self.bot.economy.for_guild(interaction.guild_id).quiz_withdraw(interaction.user.id, quiz_id, time.time())
        if done is None:
            return await interaction.response.send_message(
                embed=ui.error_embed(
                    "取り下げられません", "あなたがこのサーバーで出した、終了前の問題の番号を指定してください。"
                ),
                ephemeral=True,
            )
        await interaction.response.send_message(
            embed=ui.ok_embed(f"クイズ #{quiz_id} を取り下げました", settle_text(done)), ephemeral=True
        )
        await self.announce_settle(interaction.guild, done)

    @withdraw.autocomplete("quiz_id")
    async def withdraw_autocomplete(self, interaction: discord.Interaction, current: str):
        """候補は、自分がこのサーバーで出した、終了前の問題だけ。"""
        if interaction.guild_id is None:
            return []
        now = time.time()
        cur = current.strip().lower()
        out = []
        for q in self.bot.economy.for_guild(interaction.guild_id).quiz_own_open(interaction.user.id, now):
            text = q["question"].replace("\n", " ")
            label = f"#{q['id']} {text[:50]}（あと{format_minutes(max(1, int((q['expires'] - now) // 60)))}）"
            if not cur or cur in label.lower():
                out.append(app_commands.Choice(name=label[:100], value=q["id"]))
        return out[:25]

    # ------------------------------ 運営の操作（`/運営 クイズ …` から呼ぶ） ------------------------------ #
    def add_modal(self, announce: bool = True, guild=None, channel=None) -> "AddModal":
        return AddModal(self, announce, guild, channel)

    def listing_embed(self) -> discord.Embed:
        now, lines = time.time(), []
        for q in self.bot.economy.quiz_list()[:15]:
            state = "⌛" if q["expires"] is not None and now >= q["expires"] else "🟢"
            until = "" if q["expires"] is None else f" 〜<t:{int(q['expires'])}:d>"
            lines.append(
                f"{state}{'👤' if q['author_id'] else ''} `#{q['id']}` {q['question'][:40].replace(chr(10), ' ')}　正解「{q['choices'][0][:20]}」　{q['reward']:,}ペリカ{until}　{q['right_count']}/{q['answered']}人正解"
            )
        return ui.embed(
            "📰 クイズ一覧（新しい順・15件まで）",
            "\n".join(lines) or "まだありません。`/運営 クイズ 追加` で登録できます。",
            ui.INFO,
        )

    def remove(self, quiz_id: int) -> bool:
        return self.bot.economy.quiz_delete(quiz_id)


async def setup(bot):
    await bot.add_cog(QuizCog(bot))
