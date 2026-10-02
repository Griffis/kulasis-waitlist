"""courses.yml / config.yml の読み込み。"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DAYS = "月火水木金土"


def norm(s: str) -> str:
    """NFKC正規化 + 空白除去。Ⅱ→II、全角英数字→半角などの表記ゆれを吸収する。"""
    return "".join(unicodedata.normalize("NFKC", s).split())


@dataclass(frozen=True)
class Course:
    name: str
    day: str
    period: int
    match: str = ""  # 空なら name を使う
    lecture_no: str = ""  # 空でなければ、rush で検索を省略して直接申込するときの lectureNo（文字列。先頭の0も保つ）

    @property
    def key(self) -> str:
        return f"{self.day}{self.period}:{self.name}"

    @property
    def day_period(self) -> str:
        """KULASISページの「曜時限」列の表記（例: "月2"）と比較する文字列。"""
        return f"{self.day}{self.period}"

    @property
    def needle(self) -> str:
        return norm(self.match or self.name)


class _StrNumLoader(yaml.SafeLoader):
    """数値っぽい値(064073 など)を数値に変換せず文字列のまま読むローダ。

    YAML 1.1 では先頭が0の数字が8進数と解釈され、lectureNo の先頭の0が消えたり別の値になったりする。
    これを避けるため、int / float の暗黙変換だけを外す。period などは読み込み側で int() に直す。
    """


_StrNumLoader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag not in ("tag:yaml.org,2002:int", "tag:yaml.org,2002:float")]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def load_courses(path: Path = ROOT / "courses.yml") -> list[Course]:
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrNumLoader) or {}
    courses: list[Course] = []
    for i, item in enumerate(data.get("courses") or [], 1):
        try:
            name = str(item["name"]).strip()
            day = str(item["day"]).strip()
            period = int(item["period"])
            match = str(item.get("match") or "").strip()
            lecture_no = str(item.get("lecture_no") or "").strip()
        except (KeyError, ValueError, TypeError) as e:
            raise ValueError(f"courses.yml の{i}件目が不正です: {item!r}") from e
        if not name:
            raise ValueError(f"courses.yml の{i}件目: name が空です")
        if len(day) != 1 or day not in DAYS:
            raise ValueError(f"courses.yml の{i}件目: day は {'/'.join(DAYS)} のどれか: {day!r}")
        if not 1 <= period <= 6:
            raise ValueError(f"courses.yml の{i}件目: period は1〜6: {period}")
        if lecture_no and not lecture_no.isdigit():
            raise ValueError(f"courses.yml の{i}件目: lecture_no は数字のみ: {lecture_no!r}")
        courses.append(Course(name=name, day=day, period=period, match=match, lecture_no=lecture_no))
    if not courses:
        raise ValueError("courses.yml に科目がありません")
    return courses


def load_config(path: Path = ROOT / "config.yml") -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ---------------------------------------------------------------------------
# 動作モード（実行環境ごとの設定）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Profile:
    """動作モード。状況に合わせて、ページ遷移の数・待ち時間・再送の粘り強さを切り替える。"""
    name: str
    description: str
    warmup: bool                      # 検索の前に、履修登録トップ・時間割ページを開くか（ブラウザの遷移の再現）
    timeslot_read_timeout_sec: float  # 履修登録ページの応答を待つ秒数（混雑時は順番待ちで遅い）
    course_gap_sec: float             # 科目ごとの検索の間隔（秒）
    req_max_attempts: int = 5                                  # GET を最大何回送るか（POST は再送しない）
    retry_waits: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)      # GET 再送の待ち秒数（失敗n回目のあと）
    apply_max_attempts: int = 5                                # 申込(candidate_add)を最大何回送るか
    apply_retry_waits: tuple[float, ...] = (0.5, 1.0, 2.0, 3.0)  # 申込の再送の待ち秒数
    retry_budget_sec: float = 360.0   # プロセス開始からこの秒数を超えたら、以後は再試行しない
    run_deadline_sec: float = 480.0   # 1回の実行の上限（各リクエストの待ちもここまでに収める）
    direct_apply: bool = False        # courses.yml の lecture_no がある科目を、検索せず直接申込するか


PROFILES: dict[str, Profile] = {
    # 通常運用: 定期実行でキャンセル待ちを監視する。速さ優先: 事前取得なし・待ちを短く・再送も短い間隔で切り上げ、
    # 回復しなければ次の定期実行に任せる（長く居座って次の実行を詰まらせない）。
    "watch": Profile(
        "watch", "キャンセル待ちの監視（定期実行・通常時。速さ優先で短く切り上げる）",
        warmup=False, timeslot_read_timeout_sec=30.0, course_gap_sec=0.0,
        req_max_attempts=3, retry_waits=(0.5, 1.0, 2.0),
        apply_max_attempts=5, apply_retry_waits=(0.3, 0.5, 1.0, 2.0),
        retry_budget_sec=90.0, run_deadline_sec=120.0,
        direct_apply=False,
    ),
    # 公開直後: 混雑して順番待ちになる。ページ遷移を最小にし、応答を長く待つ。
    # lecture_no がある科目は検索を省略して直接申込する。
    "rush": Profile(
        "rush", "先着順の公開直後（混雑・ページ遷移を最小化）",
        warmup=False, timeslot_read_timeout_sec=240.0, course_gap_sec=0.0,
        req_max_attempts=5, retry_waits=(1.0, 2.0, 4.0, 8.0),
        apply_max_attempts=5, apply_retry_waits=(0.5, 1.0, 2.0, 3.0),
        retry_budget_sec=360.0, run_deadline_sec=480.0,
        direct_apply=True,
    ),
}
DEFAULT_MODE = "watch"
_OVERRIDABLE = {
    "warmup", "timeslot_read_timeout_sec", "course_gap_sec",
    "req_max_attempts", "retry_waits", "apply_max_attempts", "apply_retry_waits",
    "retry_budget_sec", "run_deadline_sec", "direct_apply",
}
_BOOL_KEYS = ("warmup", "direct_apply")
_INT_KEYS = ("req_max_attempts", "apply_max_attempts")
_FLOAT_KEYS = ("timeslot_read_timeout_sec", "course_gap_sec", "retry_budget_sec", "run_deadline_sec")
_WAITS_KEYS = ("retry_waits", "apply_retry_waits")


def resolve_profile(cfg: dict, cli_mode: str | None = None, env_mode: str | None = None) -> Profile:
    """動作モードを決める。優先順位: --mode > 環境変数 KULASIS_MODE > config.yml の mode > watch。

    config.yml の modes.<モード名> で、モードの設定を個別に上書きできる。
    """
    name = str(cli_mode or env_mode or cfg.get("mode") or DEFAULT_MODE).strip().lower()
    if name not in PROFILES:
        raise ValueError(f"mode は {' / '.join(PROFILES)} のどれか: {name!r}")
    overrides = dict((cfg.get("modes") or {}).get(name) or {})
    unknown = set(overrides) - _OVERRIDABLE
    if unknown:
        raise ValueError(f"config.yml の modes.{name} に未知の項目: {sorted(unknown)}（使える項目: {sorted(_OVERRIDABLE)}）")
    for key in _BOOL_KEYS:
        if key in overrides:
            overrides[key] = bool(overrides[key])
    for key in _INT_KEYS:
        if key in overrides:
            overrides[key] = int(overrides[key])
            if overrides[key] < 1:
                raise ValueError(f"config.yml の modes.{name}.{key} は1以上: {overrides[key]}")
    for key in _FLOAT_KEYS:
        if key in overrides:
            overrides[key] = float(overrides[key])
    for key in _WAITS_KEYS:
        if key in overrides:
            waits = overrides[key]
            if not isinstance(waits, (list, tuple)) or not waits:
                raise ValueError(f"config.yml の modes.{name}.{key} は秒数のリスト(例: [0.5, 1, 2]): {waits!r}")
            overrides[key] = tuple(float(w) for w in waits)
    return replace(PROFILES[name], **overrides)