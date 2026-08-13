import argparse
from pathlib import Path
import pandas as pd


def csvish(value):
    if value is None:
        return ""
    return str(value)


def format_value(value):
    if pd.isna(value):
        return ""
    return value


def merge_sources(df):
    merged = {}
    for _, row in df.iterrows():
        source = csvish(row.get('来源', '')).strip() or '未知来源'
        for col in df.columns:
            if col == '来源':
                continue
            key = f"{col}_{source}"
            merged[key] = format_value(row.get(col))
    return merged


def merge_files(files):
    merged = {}
    conflicts = {}

    for file_path in files:
        df = pd.read_excel(file_path)
        if df.empty:
            continue

        if '来源' in df.columns and '来源占比' in df.columns:
            merged.update(merge_sources(df))
            continue

        if len(df) == 1:
            row = df.iloc[0].to_dict()
            for col, val in row.items():
                val = format_value(val)
                if col in merged:
                    existing = csvish(merged[col])
                    incoming = csvish(val)
                    if existing == incoming or incoming == "":
                        continue
                    if existing == "":
                        merged[col] = val
                        continue
                    conflicts.setdefault(col, []).append(incoming)
                    continue
                merged[col] = val
            continue

        # 多行但不是“来源”表：按行展开
        for idx, row in df.iterrows():
            for col, val in row.items():
                key = f"{col}_row{idx + 1}"
                if key not in merged:
                    merged[key] = format_value(val)

    return merged, conflicts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--date', required=True)
    parser.add_argument('--work-id', required=True)
    parser.add_argument('--files', nargs='+', required=True)
    args = parser.parse_args()

    files = [Path(p) for p in args.files]
    merged, conflicts = merge_files(files)

    # 元信息列
    ordered = {
        '作品ID': args.work_id,
        '标题': args.title,
        '发布日期': args.date,
    }
    ordered.update(merged)

    df_out = pd.DataFrame([ordered])
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df_out.to_excel(output_path, index=False)

    if conflicts:
        conflict_path = output_path.with_suffix('.conflicts.txt')
        with conflict_path.open('w', encoding='utf-8') as f:
            for col, vals in conflicts.items():
                f.write(f"{col}: {', '.join(vals)}\n")


if __name__ == '__main__':
    main()
