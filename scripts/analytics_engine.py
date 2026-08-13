import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pandas as pd


@dataclass(frozen=True)
class RunMeta:
    run_id: int
    run_at: str


SNAPSHOT_COLUMNS = [
    "平台",
    "平台作品ID",
    "作品ID",
    "标题",
    "发布日期",
    "曝光量",
    "播放量",
    "点赞量",
    "收藏量",
    "评论量",
    "分享量",
    "更新时间",
]


def _now_run_meta() -> RunMeta:
    run_id = int(time.time() * 1000)
    run_at = time.strftime("%Y-%m-%d %H:%M:%S")
    return RunMeta(run_id=run_id, run_at=run_at)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).strip()
    if text.lower() in ("nan", "none", "null"):
        return ""
    return text


_UNIT_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*([万亿])\s*$")


def parse_int_like(value: Any) -> Optional[int]:
    """Best-effort numeric parsing for platform exported fields."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if pd.isna(value):
            return None
        return int(value)

    text = str(value).strip()
    if not text:
        return None
    if text == "-":
        return None
    if text.lower() in ("nan", "none", "null"):
        return None

    # Percentages are not counts.
    if text.endswith("%"):
        return None

    # Remove separators.
    text = text.replace(",", "")

    m = _UNIT_RE.match(text)
    if m:
        n = float(m.group(1))
        unit = m.group(2)
        if unit == "万":
            return int(n * 10_000)
        if unit == "亿":
            return int(n * 100_000_000)

    try:
        return int(float(text))
    except Exception:
        return None


def normalize_date(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    try:
        dt = pd.to_datetime(text, errors="coerce")
        if pd.isna(dt):
            return text
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return text


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=8)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    return conn


def ensure_db(db_path: str) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
              run_id INTEGER PRIMARY KEY,
              run_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
              run_id INTEGER NOT NULL,
              platform_work_id TEXT NOT NULL,
              platform TEXT NOT NULL,
              work_id TEXT NOT NULL,
              title TEXT NOT NULL,
              publish_date TEXT NOT NULL,
              exposure INTEGER,
              plays INTEGER,
              likes INTEGER,
              favorites INTEGER,
              comments INTEGER,
              shares INTEGER,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (run_id, platform_work_id)
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_platform_work ON snapshots(platform_work_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_run_platform ON snapshots(run_id, platform);")
        conn.commit()
    finally:
        conn.close()


def list_recent_runs(db_path: str, limit: int = 30) -> List[RunMeta]:
    ensure_db(db_path)
    conn = _connect(db_path)
    try:
        cur = conn.execute("SELECT run_id, run_at FROM runs ORDER BY run_id DESC LIMIT ?;", (int(limit),))
        return [RunMeta(run_id=int(r[0]), run_at=str(r[1])) for r in cur.fetchall()]
    finally:
        conn.close()


def archive_snapshot_from_excel(excel_path: str, db_path: str) -> Optional[RunMeta]:
    """Archive the current ALL_DATA_FILE into SQLite as a run snapshot.

    Best-effort, never raises: caller should not fail the pipeline if this fails.
    """
    try:
        df = pd.read_excel(excel_path, dtype=str)
    except Exception:
        return None

    if df.empty:
        return None

    for col in SNAPSHOT_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    df = df[SNAPSHOT_COLUMNS].where(pd.notna(df), None)

    meta = _now_run_meta()
    ensure_db(db_path)
    conn = _connect(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO runs(run_id, run_at) VALUES (?, ?);", (meta.run_id, meta.run_at))

        rows = []
        for _, r in df.iterrows():
            platform = _clean_text(r.get("平台"))
            platform_work_id = _clean_text(r.get("平台作品ID"))
            work_id = _clean_text(r.get("作品ID"))
            if not platform or not platform_work_id or not work_id:
                continue

            rows.append(
                (
                    meta.run_id,
                    platform_work_id,
                    platform,
                    work_id,
                    _clean_text(r.get("标题")),
                    normalize_date(r.get("发布日期")),
                    parse_int_like(r.get("曝光量")),
                    parse_int_like(r.get("播放量")),
                    parse_int_like(r.get("点赞量")),
                    parse_int_like(r.get("收藏量")),
                    parse_int_like(r.get("评论量")),
                    parse_int_like(r.get("分享量")),
                    _clean_text(r.get("更新时间")) or meta.run_at,
                )
            )

        conn.executemany(
            """
            INSERT OR REPLACE INTO snapshots(
              run_id, platform_work_id, platform, work_id, title, publish_date,
              exposure, plays, likes, favorites, comments, shares, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            rows,
        )
        conn.commit()
        return meta
    except Exception:
        return None
    finally:
        conn.close()


def _run_df(conn: sqlite3.Connection, run_id: int) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT
          platform_work_id,
          platform,
          work_id,
          title,
          publish_date,
          exposure,
          plays,
          likes,
          favorites,
          comments,
          shares,
          updated_at
        FROM snapshots
        WHERE run_id = ?;
        """,
        conn,
        params=(int(run_id),),
    )
    for c in ("exposure", "plays", "likes", "favorites", "comments", "shares"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)
    return df


def _compute_engagement(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in ("plays", "likes", "favorites", "comments", "shares"):
        if c not in out.columns:
            out[c] = 0
    out["engagement"] = out["likes"] + out["favorites"] + out["comments"] + out["shares"]
    out["engagement_rate"] = out.apply(
        lambda r: (float(r["engagement"]) / float(r["plays"])) if r["plays"] > 0 else 0.0,
        axis=1,
    )
    return out


def _extract_tags(title: str) -> List[str]:
    text = _clean_text(title)
    if not text:
        return []
    tags = re.findall(r"#([0-9A-Za-z_\\u4e00-\\u9fff]{1,32})", text)
    seen = set()
    out: List[str] = []
    for t in tags:
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def compute_dashboard(
    db_path: str,
    *,
    top_n: int = 12,
    tag_top_n: int = 16,
    cadence_weeks: int = 12,
    timeseries_runs: int = 30,
) -> Dict[str, Any]:
    """Compute quantifiable dashboard analytics.

    The output is fully fact-based (aggregations/deltas) and suitable for "运营建议" generation.
    """
    ensure_db(db_path)
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "SELECT run_id, run_at FROM runs ORDER BY run_id DESC LIMIT ?;",
            (int(max(2, timeseries_runs)),),
        )
        runs = [RunMeta(run_id=int(r[0]), run_at=str(r[1])) for r in cur.fetchall()]
        if not runs:
            return {"ok": True, "empty": True, "message": "no_runs"}

        latest = runs[0]
        prev = runs[1] if len(runs) > 1 else None

        latest_df = _compute_engagement(_run_df(conn, latest.run_id))
        prev_df = _compute_engagement(_run_df(conn, prev.run_id)) if prev else pd.DataFrame()

        prev_small = (
            prev_df[["platform_work_id", "plays", "engagement", "engagement_rate"]].rename(
                columns={
                    "plays": "plays_prev",
                    "engagement": "engagement_prev",
                    "engagement_rate": "engagement_rate_prev",
                }
            )
            if not prev_df.empty
            else pd.DataFrame(columns=["platform_work_id", "plays_prev", "engagement_prev", "engagement_rate_prev"])
        )

        merged = latest_df.merge(prev_small, on="platform_work_id", how="left")
        merged["is_new"] = merged["plays_prev"].isna()
        merged["plays_prev"] = merged["plays_prev"].fillna(0).astype(int)
        merged["engagement_prev"] = merged["engagement_prev"].fillna(0).astype(int)
        merged["engagement_rate_prev"] = merged["engagement_rate_prev"].fillna(0.0).astype(float)
        merged["plays_delta"] = merged["plays"] - merged["plays_prev"]
        merged["engagement_delta"] = merged["engagement"] - merged["engagement_prev"]
        merged["engagement_rate_delta"] = merged["engagement_rate"] - merged["engagement_rate_prev"]

        totals = {
            "works": int(len(latest_df)),
            "plays": int(latest_df["plays"].sum()) if not latest_df.empty else 0,
            "engagement": int(latest_df["engagement"].sum()) if not latest_df.empty else 0,
        }

        deltas: Optional[Dict[str, int]] = None
        if prev is not None and not prev_df.empty:
            deltas = {
                "plays": int(totals["plays"] - int(prev_df["plays"].sum())),
                "engagement": int(totals["engagement"] - int(prev_df["engagement"].sum())),
            }

        gp_latest = latest_df.groupby("platform", as_index=False).agg(
            works=("platform_work_id", "count"),
            plays=("plays", "sum"),
            engagement=("engagement", "sum"),
        )
        gp_prev = (
            prev_df.groupby("platform", as_index=False).agg(
                plays_prev=("plays", "sum"),
                engagement_prev=("engagement", "sum"),
            )
            if prev is not None and not prev_df.empty
            else pd.DataFrame(columns=["platform", "plays_prev", "engagement_prev"])
        )
        gp = gp_latest.merge(gp_prev, on="platform", how="left")
        gp["plays_prev"] = gp["plays_prev"].fillna(0).astype(int)
        gp["engagement_prev"] = gp["engagement_prev"].fillna(0).astype(int)
        if prev is not None and not prev_df.empty:
            gp["plays_delta"] = gp["plays"] - gp["plays_prev"]
            gp["engagement_delta"] = gp["engagement"] - gp["engagement_prev"]
        else:
            gp["plays_delta"] = None
            gp["engagement_delta"] = None
        gp = gp.sort_values(by=["plays"], ascending=False, kind="stable")

        by_platform: List[Dict[str, Any]] = []
        for _, r in gp.iterrows():
            plays_delta = r.get("plays_delta", None)
            engagement_delta = r.get("engagement_delta", None)
            plays_delta_out = int(plays_delta) if plays_delta is not None and not pd.isna(plays_delta) else None
            engagement_delta_out = (
                int(engagement_delta) if engagement_delta is not None and not pd.isna(engagement_delta) else None
            )
            by_platform.append(
                {
                    "platform": str(r["platform"]),
                    "works": int(r["works"]),
                    "plays": int(r["plays"]),
                    "engagement": int(r["engagement"]),
                    "plays_delta": plays_delta_out,
                    "engagement_delta": engagement_delta_out,
                }
            )

        # Top gainers (existing works) + Top new
        top_existing = (
            merged[merged["is_new"] == False]
            .sort_values(by=["plays_delta", "plays"], ascending=False, kind="stable")
            .head(int(top_n))
        )
        top_new = (
            merged[merged["is_new"] == True]
            .sort_values(by=["plays", "engagement"], ascending=False, kind="stable")
            .head(int(top_n))
        )
        top_works = merged.sort_values(by=["plays", "engagement"], ascending=False, kind="stable").head(int(top_n))

        def _rows_to_dict(df: pd.DataFrame) -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            if df.empty:
                return out
            for _, r in df.iterrows():
                out.append(
                    {
                        "platform": str(r.get("platform", "")),
                        "work_id": str(r.get("work_id", "")),
                        "platform_work_id": str(r.get("platform_work_id", "")),
                        "title": str(r.get("title", "")),
                        "publish_date": str(r.get("publish_date", "")),
                        "plays": int(r.get("plays", 0)),
                        "plays_prev": int(r.get("plays_prev", 0)),
                        "plays_delta": int(r.get("plays_delta", 0)),
                        "engagement": int(r.get("engagement", 0)),
                        "engagement_prev": int(r.get("engagement_prev", 0)),
                        "engagement_delta": int(r.get("engagement_delta", 0)),
                        "engagement_rate": float(r.get("engagement_rate", 0.0)),
                        "engagement_rate_prev": float(r.get("engagement_rate_prev", 0.0)),
                        "engagement_rate_delta": float(r.get("engagement_rate_delta", 0.0)),
                        "is_new": bool(r.get("is_new", False)),
                    }
                )
            return out

        # Hashtag stats (from titles)
        tag_stats: List[Dict[str, Any]] = []
        tags = []
        for _, r in latest_df.iterrows():
            for t in _extract_tags(str(r.get("title", ""))):
                tags.append(
                    {
                        "tag": t,
                        "plays": int(r.get("plays", 0)),
                        "engagement_rate": float(r.get("engagement_rate", 0.0)),
                    }
                )
        if tags:
            tag_df = pd.DataFrame(tags)
            agg = tag_df.groupby("tag", as_index=False).agg(
                works=("tag", "count"),
                avg_plays=("plays", "mean"),
                med_plays=("plays", "median"),
                avg_engagement_rate=("engagement_rate", "mean"),
            )
            agg = agg.sort_values(by=["works", "avg_plays"], ascending=False, kind="stable").head(int(tag_top_n))
            for _, r in agg.iterrows():
                tag_stats.append(
                    {
                        "tag": str(r["tag"]),
                        "works": int(r["works"]),
                        "avg_plays": int(round(float(r["avg_plays"]))),
                        "med_plays": int(round(float(r["med_plays"]))),
                        "avg_engagement_rate": float(r["avg_engagement_rate"]),
                    }
                )

        # Cadence (works per week by publish_date)
        cadence: List[Dict[str, Any]] = []
        publish_dt = pd.to_datetime(latest_df["publish_date"], errors="coerce")
        if publish_dt.notna().any():
            cad = pd.DataFrame({"publish_dt": publish_dt.dropna()})
            cad["week"] = cad["publish_dt"].dt.to_period("W").astype(str)
            cad_agg = cad.groupby("week", as_index=False).size().rename(columns={"size": "works"})
            cad_agg = cad_agg.sort_values(by=["week"], ascending=True, kind="stable").tail(int(cadence_weeks))
            for _, r in cad_agg.iterrows():
                cadence.append({"week": str(r["week"]), "works": int(r["works"])})

        # Timeseries over recent runs (total plays)
        run_ids = [int(r.run_id) for r in reversed(runs[: int(timeseries_runs)])]
        timeseries: List[Dict[str, Any]] = []
        if run_ids:
            q_marks = ",".join("?" for _ in run_ids)
            ts_df = pd.read_sql_query(
                f"""
                SELECT
                  run_id,
                  SUM(COALESCE(plays, 0)) AS plays,
                  SUM(COALESCE(likes, 0) + COALESCE(favorites, 0) + COALESCE(comments, 0) + COALESCE(shares, 0)) AS engagement
                FROM snapshots
                WHERE run_id IN ({q_marks})
                GROUP BY run_id
                ORDER BY run_id ASC;
                """,
                conn,
                params=run_ids,
            )
            by_run = {int(r.run_id): r for r in runs}
            for _, r in ts_df.iterrows():
                rid = int(r["run_id"])
                timeseries.append(
                    {
                        "run_id": rid,
                        "run_at": by_run[rid].run_at if rid in by_run else "",
                        "plays": int(r["plays"] or 0),
                        "engagement": int(r["engagement"] or 0),
                    }
                )

        # Recommendations: rule-based, quantifiable.
        recommendations: List[Dict[str, Any]] = []
        if not latest_df.empty:
            # Many creators hide works (plays=0). Quantiles become misleading if zeros dominate.
            # We compute recommendation thresholds on non-zero plays when possible.
            df_nonzero = latest_df[latest_df["plays"] > 0].copy()
            if df_nonzero.empty:
                df_nonzero = latest_df.copy()

            plays_series = df_nonzero["plays"]
            er_series = df_nonzero["engagement_rate"]
            med_plays = float(plays_series.median()) if len(plays_series) else 0.0
            p90_plays = float(plays_series.quantile(0.9)) if len(plays_series) else 0.0
            p25_er = float(er_series.quantile(0.25)) if len(er_series) else 0.0
            p75_er = float(er_series.quantile(0.75)) if len(er_series) else 0.0

            high_eng_low_play = merged[(merged["plays"] <= med_plays) & (merged["engagement_rate"] >= p75_er)].sort_values(
                by=["engagement_rate", "plays"], ascending=False, kind="stable"
            )
            if not high_eng_low_play.empty:
                recommendations.append(
                    {
                        "title": "高互动但低播放：适合二次分发/复刻",
                        "why": "互动率高于样本 75 分位，但播放量低于中位数，说明内容共鸣不错，但分发或标题包装可能限制了曝光。",
                        "actions": [
                            "复刻同主题/同结构做 2-3 条变体（不同开头/标题/封面）。",
                            "把高互动作品剪成 15-30 秒短版，再投放到其他平台（或同平台二次发布）。",
                            "在评论区置顶更明确的 CTA（关注/收藏/私信关键词）。",
                        ],
                        "examples": _rows_to_dict(high_eng_low_play.head(3)),
                    }
                )

            low_eng_high_play = merged[(merged["plays"] >= p90_plays) & (merged["engagement_rate"] <= p25_er)].sort_values(
                by=["plays"], ascending=False, kind="stable"
            )
            if not low_eng_high_play.empty:
                recommendations.append(
                    {
                        "title": "高播放但低互动：需要加强转化",
                        "why": "播放量进入前 10%，但互动率落在后 25%，说明有流量但转化弱。",
                        "actions": [
                            "在前 3 秒增加明确收益点（观众看完能得到什么）。",
                            "中段设置一个“评论触发点”（提问/投票/反差/争议点）。",
                            "结尾给可执行的下一步（关注后领取/置顶评论/合集）。",
                        ],
                        "examples": _rows_to_dict(low_eng_high_play.head(3)),
                    }
                )

            if prev is not None and not top_existing.empty:
                recommendations.append(
                    {
                        "title": "本次增长最快的旧作品：优先拆解并复用",
                        "why": "这些旧作品在两次采集之间播放增量最高，说明它们正在被平台再次推荐或被外部流量带动。",
                        "actions": [
                            "逐条拆解：标题结构/开头 3 秒/节奏点/结尾 CTA。",
                            "把同主题作品做成系列（合集页 + 固定栏目名）。",
                        ],
                        "examples": _rows_to_dict(top_existing.head(5)),
                    }
                )

        return {
            "ok": True,
            "latest_run": {"run_id": latest.run_id, "run_at": latest.run_at},
            "prev_run": {"run_id": prev.run_id, "run_at": prev.run_at} if prev else None,
            "totals": totals,
            "deltas": deltas,
            "by_platform": by_platform,
            "top_existing_gainers": _rows_to_dict(top_existing),
            "top_new": _rows_to_dict(top_new),
            "top_works": _rows_to_dict(top_works),
            "tag_stats": tag_stats,
            "cadence": cadence,
            "timeseries": timeseries,
            "recommendations": recommendations,
            "thresholds": {
                "notes": "分位数阈值用于‘高互动但低播放’等规则建议（默认尽量在 plays>0 的样本上计算）。",
                "median_plays": int(round(med_plays)) if "med_plays" in locals() else 0,
                "p90_plays": int(round(p90_plays)) if "p90_plays" in locals() else 0,
                "p25_engagement_rate": float(p25_er) if "p25_er" in locals() else 0.0,
                "p75_engagement_rate": float(p75_er) if "p75_er" in locals() else 0.0,
                "zero_plays_works": int((latest_df["plays"] == 0).sum()) if "latest_df" in locals() else 0,
            },
            "formulas": {
                "engagement": "点赞量+收藏量+评论量+分享量",
                "engagement_rate": "互动率=互动量/播放量（播放量为 0 时记 0）",
            },
        }
    finally:
        conn.close()
