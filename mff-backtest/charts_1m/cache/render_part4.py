
TV = mpf.make_mpf_style(
    base_mpf_style="nightclouds",
    marketcolors=mpf.make_marketcolors(
        up="#26a69a", down="#ef5350", edge="inherit", wick="inherit", volume="in",
        ohlc="inherit"
    ),
    facecolor="#131722", edgecolor="#131722", figcolor="#131722",
    gridcolor="#2a2e39", gridstyle="--",
    rc={"axes.labelcolor": "#d1d4dc", "xtick.color": "#d1d4dc", "ytick.color": "#d1d4dc",
        "axes.edgecolor": "#2a2e39", "font.size": 9}
)

def render_chart(trade, iid, bars, entry, exit_info, png_path):
    if not entry or not bars:
        return False, "no_entry_or_bars"
    entry_t = entry["time"].astimezone(ET).replace(tzinfo=None)
    # window
    win_start = entry_t - timedelta(minutes=10)
    win_end = entry_t + timedelta(minutes=110)
    outside = False
    exit_t_naive = None
    if exit_info and exit_info.get("status") in ("hit", "miss") and exit_info.get("time"):
        exit_t_naive = exit_info["time"].astimezone(ET).replace(tzinfo=None)
        if exit_t_naive > win_end:
            # extend up to 4h if same calendar day
            if exit_t_naive.date() == entry_t.date() and (exit_t_naive - entry_t) <= timedelta(hours=4):
                win_end = exit_t_naive + timedelta(minutes=5)
            else:
                outside = True
    df_all = to_df(bars)
    df = df_all[(df_all.index >= win_start) & (df_all.index <= win_end)]
    if df.empty:
        # fallback: first 24 bars of day
        df = df_all.iloc[:24]
        if df.empty:
            return False, "empty_window"
    addplots = []
    # entry hline via alines later
    title = f"{trade['ticker']} ${trade['strike']} {trade['side'].upper()} exp {trade['expiry']} | {trade['channel']} | {trade['date']}"
    notes = (trade.get("notes") or "")[:80]
    subtitle = f"Discord BID ${trade['entry_bid']} · posted {trade.get('time_et')} ET (label only) · outcome {trade.get('outcome')} · {notes}"

    fig, axes = mpf.plot(
        df, type="candle", style=TV, returnfig=True, figsize=(14, 7),
        title=title, datetime_format="%H:%M", xrotation=0,
        tight_layout=True, warn_too_much_data=10000,
    )
    ax = axes[0]
    # entry marker
    ax.axvline(df.index.get_indexer([entry_t], method="nearest")[0], color="#2196f3", ls="--", lw=1.2, alpha=0.9)
    # use price line
    ax.axhline(entry["fill"], color="#2196f3", ls=":", lw=1, alpha=0.7)
    # annotate entry at last matching index
    try:
        xi = df.index.get_indexer([entry_t], method="nearest")[0]
        ax.scatter([xi], [entry["fill"]], color="#2196f3", s=60, zorder=5, marker="^")
        ax.annotate(f"ENTRY @ ${entry['fill']:.2f}", xy=(xi, entry["fill"]),
                    xytext=(8, 18), textcoords="offset points", color="#64b5f6", fontsize=9,
                    arrowprops=dict(arrowstyle="->", color="#64b5f6"))
    except Exception:
        pass

    if exit_info and exit_info.get("status") == "no_discord_exit_pct":
        ax.text(0.01, 0.02, "no Discord exit %", transform=ax.transAxes, color="#ffb74d", fontsize=9)
    elif exit_info and exit_info.get("px") is not None:
        tgt = exit_info["px"]
        ax.axhline(tgt, color="#ff9800", ls="--", lw=1, alpha=0.8)
        if exit_info.get("status") == "hit" and exit_t_naive is not None and not outside:
            try:
                xi = df.index.get_indexer([exit_t_naive], method="nearest")[0]
                ax.scatter([xi], [tgt], color="#ff9800", s=60, zorder=5, marker="v")
                ax.annotate(f"EXIT {exit_info['pct']:+.0f}% @ ${tgt:.2f}", xy=(xi, tgt),
                            xytext=(8, -28), textcoords="offset points", color="#ffb74d", fontsize=9,
                            arrowprops=dict(arrowstyle="->", color="#ffb74d"))
            except Exception:
                pass
        elif outside or exit_info.get("status") in ("hit", "miss"):
            et_str = exit_t_naive.strftime("%H:%M") if exit_t_naive else "?"
            label = f"EXIT TARGET {exit_info['pct']:+.0f}% @ ${tgt:.2f} · {et_str} ET"
            if outside:
                label += " (outside 2h window)"
            if exit_info.get("status") == "miss":
                label += " · NOT REACHED"
            ax.text(0.99, 0.98, label, transform=ax.transAxes, ha="right", va="top",
                    color="#ffb74d", fontsize=8, bbox=dict(boxstyle="round", facecolor="#1e222d", edgecolor="#ff9800", alpha=0.9))

    # subtitle box
    ax.text(0.01, 0.98, subtitle, transform=ax.transAxes, ha="left", va="top",
            color="#d1d4dc", fontsize=8,
            bbox=dict(boxstyle="round", facecolor="#1e222d", edgecolor="#2a2e39", alpha=0.9))
    ax.text(0.99, 0.02, "RH 5m RTH (1m unavailable)", transform=ax.transAxes, ha="right", va="bottom",
            color="#787b86", fontsize=7)

    fig.savefig(png_path, dpi=140, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    return True, "ok"
