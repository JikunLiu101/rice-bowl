/* rice-bowl dashboard. */
(function () {
  "use strict";
  const K = window.PanelKit;
  const { el, clear, card, badge, empty, lineChart, barChart, legend, time, day } = K;
  const $ = (id) => document.getElementById(id);
  const api = K.makeApi("饭碗");
  const SIGN = { CNY: "¥", USD: "$" };
  let cur = "CNY";

  const money = (v, digits) => (SIGN[cur] || cur + " ") + Number(v || 0).toFixed(digits === undefined ? 2 : digits);
  const tokens = (n) => (n >= 10000 ? (n / 10000).toFixed(1) + "万" : String(n));

  function render(d) {
    cur = d.currency || "CNY";
    const notes = [];
    if (!d.enabled) notes.push(el("div", { class: "note" }, "插件设置里关掉了“让她看自己的饭量”：不记账，她也看不了。"));
    if (!d.key_ok) notes.push(el("div", { class: "note" }, `没找到 DeepSeek 的 key：在 AstrBot 里配好 DeepSeek 提供商，或者填进这个插件的设置。`));
    if (d.last_error) notes.push(el("div", { class: "note" }, "上次没查到：" + d.last_error));
    if (d.problem === "" && d.checked) notes.push(el("div", { class: "ok-note" }, "刚查过，已经记下。"));
    if (d.low) notes.push(el("div", { class: "note" }, `快吃完了（还能吃不到 ${d.warn_days} 天，或者余额低于 ${money(d.warn_balance, 0)}）：她会在${d.owner}私聊里每天最多提一次。`));
    clear($("notes"), notes);

    if (d.empty) {
      clear($("cards"));
      clear($("series"), empty("还没记下余额。点“现在查一次”，或者等插件下次定时去查。"));
      clear($("daily"));
      clear($("topups"));
      renderTokens(d.tokens || []);
      clear($("foot"));
      return;
    }

    const perDay = d.per_day === null ? "还算不出" : money(d.per_day);
    const left = d.days_left === null ? "还算不出" : `约 ${Math.round(d.days_left)} 天`;
    const today = d.today_since && d.today < 0.005 ? "刚开始记" : money(d.today);
    const last = d.topups.length ? d.topups[d.topups.length - 1] : null;
    clear($("cards"),
      card("现在还剩", money(d.balance), "上次查：" + time(d.last_check), d.low ? "warn" : ""),
      card("今天吃了", today, d.today_since ? "从 " + K.clock(d.today_since) + " 开始记" : ""),
      card("平均每天", perDay, d.per_day === null ? "记满半天才算得出" : ""),
      card("照这样还能吃", left, "", d.low ? "warn" : ""),
      card("上次充饭", last ? day(last.at) : "—", last ? "约 " + money(last.amount) : "还没见到充值"));

    clear($("series"), lineChart(d.series.map((s) => ({ t: s.t, v: s.v, mark: s.mark })),
      { height: 200, fmt: (v) => money(v, Number.isInteger(v) ? 0 : 1), label: "余额变化" }));

    clear($("daily"), barChart(d.daily.map((r) => ({
      label: day(r.date), parts: [{ v: r.eaten, cls: r.partial ? "s2" : "s1", name: r.partial ? "（这天没记全）" : "" }],
    })), { width: 380, every: 2, fmt: (v) => money(v, Number.isInteger(v) ? 0 : 1), label: "每天吃了多少" }),
    legend([["c1", "吃掉的"], ["c2", "这天没记全（第一天或今天）"]]));

    renderTokens(d.tokens);

    clear($("topups"), d.topups.length
      ? el("table", {}, el("tr", {}, el("th", {}, "时间"), el("th", { class: "num" }, "至少充了")),
        d.topups.slice().reverse().map((t) => el("tr", {}, el("td", {}, time(t.at)), el("td", { class: "num" }, money(t.amount)))))
      : empty("还没见到充值。余额变多的那次会记成一次充值（同一段时间里吃掉的看不出来，所以是“至少”）。"));

    clear($("foot"), `每 ${d.check_hours} 小时记一次余额（查余额不收费），从 ${time(d.since)} 开始，共记了 ${d.checks} 次。`);
  }

  function renderTokens(rows) {
    clear($("tokens"), barChart(rows.map((r) => ({
      label: day(r.date), hit: r.hit,
      parts: [{ v: r.cached, cls: "s3", name: "命中缓存" }, { v: r.input - r.cached, cls: "s4", name: "没命中" }],
    })), { width: 380, every: 2, fmt: tokens, note: (r) => (r.hit === null || r.hit === undefined ? "" : r.hit + "%"), label: "聊天用的 token",
      emptyText: "还没有统计（AstrBot 的 provider_stats）" }),
    rows.length ? legend([["c3", "命中缓存"], ["c4", "没命中"]]) : null);
  }

  async function load() {
    render(await api("GET", "overview"));
  }

  const check = $("check");
  check.addEventListener("click", K.busy(check, async () => render(await api("POST", "check"))));
  $("refresh").addEventListener("click", () => load().catch(() => {}));
  K.onReady("饭碗", () => load().catch(() => {}));
})();
