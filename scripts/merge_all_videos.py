import argparse
from pathlib import Path
import pandas as pd


def parse_date_value(value):
    if value is None or value == "":
        return None
    text = str(value).strip()
    for sep in ("-", ".", "/"):
        if sep in text:
            parts = text.split(sep)
            if len(parts) == 3 and all(part.isdigit() for part in parts):
                year, month, day = (int(parts[0]), int(parts[1]), int(parts[2]))
                try:
                    return pd.Timestamp(year=year, month=month, day=day)
                except ValueError:
                    return None
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--files", nargs="+", required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    files = [Path(p) for p in args.files]
    rows = []
    columns_order = []
    base_cols = ["作品ID", "标题", "发布日期"]

    def add_columns(cols):
        for col in cols:
            if col not in columns_order:
                columns_order.append(col)

    for file_path in files:
        if not file_path.exists():
            continue
        df = pd.read_excel(file_path, dtype={"作品ID": str})
        if df.empty:
            continue
        row = df.iloc[0].to_dict()
        row["__source_mtime"] = file_path.stat().st_mtime
        rows.append(row)
        if not columns_order:
            add_columns(base_cols)
        add_columns(df.columns.tolist())

    if not rows:
        return

    # Ensure base columns always first
    for col in reversed(base_cols):
        if col in columns_order:
            columns_order.remove(col)
        columns_order.insert(0, col)

    df_all = pd.DataFrame(rows)
    if "作品ID" in df_all.columns:
        df_all["作品ID"] = (
            df_all["作品ID"].astype(str).str.replace(r"\.0$", "", regex=True)
        )

    df_all["__publish_ts"] = df_all.get("发布日期", "").apply(parse_date_value)

    # Deduplicate by 作品ID, keep newest merged file
    if "作品ID" in df_all.columns:
        df_all = df_all.sort_values("__source_mtime", ascending=False)
        df_all = df_all.drop_duplicates(subset=["作品ID"], keep="first")

    # Apply limit by newest publish date
    if args.limit and args.limit > 0:
        df_all = df_all.sort_values("__publish_ts", ascending=False).head(args.limit)

    df_all = df_all.sort_values("__publish_ts", ascending=True)

    df_all = df_all.drop(columns=["__source_mtime", "__publish_ts"], errors="ignore")
    df_all = df_all.fillna("")

    # Fill missing numeric-like values to avoid nulls in downstream tables
    for col in df_all.columns:
        if col in base_cols:
            continue
        series = df_all[col].astype(str)
        has_percent = series.str.contains("%", na=False).any()
        fill_value = "0%" if has_percent else "0"
        df_all[col] = series.replace("", fill_value)

    normalized = []
    for _, row in df_all.iterrows():
        normalized.append({col: row.get(col, "") for col in columns_order})

    out_df = pd.DataFrame(normalized, columns=columns_order)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_excel(output_path, index=False)


if __name__ == "__main__":
    main()
