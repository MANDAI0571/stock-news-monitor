from __future__ import annotations

from datetime import datetime

import streamlit as st

from run_screening import run_screening


st.set_page_config(page_title="300万円運用 日本株スクリーナー", page_icon="JP", layout="wide")
st.title("300万円運用 日本株スクリーナー")
st.caption("プライム・スタンダード・グロースの個別株を対象に、流動性・トレンド・決算回避・CWHを確認します。")

market_options = {
    "プライム": "prime",
    "スタンダード": "standard",
    "グロース": "growth",
}

with st.sidebar:
    st.header("条件")
    selected = st.multiselect("対象市場", list(market_options), default=list(market_options))
    limit = st.number_input("動作確認用の上限銘柄数", min_value=0, value=50, step=10)
    include_rejected = st.checkbox("見送りも表示", value=False)

if st.button("スクリーニング開始", type="primary"):
    if not selected:
        st.warning("対象市場を選んでください。")
        st.stop()

    with st.spinner("スクリーニング中..."):
        result = run_screening(
            markets=tuple(market_options[name] for name in selected),
            limit=int(limit) or None,
            output_dir="outputs",
            include_rejected=include_rejected,
        )

    if result.empty:
        st.warning("条件に合う銘柄はありませんでした。")
        st.stop()

    st.dataframe(result, use_container_width=True, height=560)
    csv = result.to_csv(index=False, encoding="utf-8-sig")
    st.download_button(
        "CSVダウンロード",
        data=csv,
        file_name=f"screening_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
        use_container_width=True,
    )

st.caption("投資判断の参考情報です。売買を推奨するものではありません。")
