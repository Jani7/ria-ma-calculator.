"""
PDF Export for RIA M&A Calculator.
Generates a summary PDF of the full acquisition analysis.
"""

from fpdf import FPDF
import io
import math
from datetime import date

# The one gate every IRR display routes through — including the legacy export
# below, which quoted a 1.1e21 "IRR" into a downloadable PDF for two rounds.
from insights import irr_is_meaningful


class AnalysisPDF(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 14)
        self.cell(0, 10, "RIA M&A Acquisition Analysis", align="C", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "", 9)
        self.cell(0, 5, f"Generated: {date.today().strftime('%B %d, %Y')}", align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(5)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.cell(0, 10, f"RIA M&A Calculator | Page {self.page_no()}/{{nb}}", align="C")

    def section_title(self, title):
        self.set_font("Helvetica", "B", 11)
        self.set_fill_color(30, 40, 60)
        self.set_text_color(255, 255, 255)
        self.cell(0, 8, f"  {title}", fill=True, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.ln(3)

    def kv_row(self, key, value):
        self.set_font("Helvetica", "", 9)
        self.cell(80, 6, key, new_x="RIGHT")
        self.set_font("Helvetica", "B", 9)
        self.cell(0, 6, str(value), new_x="LMARGIN", new_y="NEXT")

    def table(self, headers, rows, col_widths=None):
        if col_widths is None:
            w = (self.w - 20) / len(headers)
            col_widths = [w] * len(headers)

        self.set_font("Helvetica", "B", 8)
        self.set_fill_color(40, 50, 70)
        self.set_text_color(255, 255, 255)
        for i, h in enumerate(headers):
            self.cell(col_widths[i], 7, h, border=1, fill=True, align="C")
        self.ln()
        self.set_text_color(0, 0, 0)
        self.set_font("Helvetica", "", 8)
        for row in rows:
            for i, val in enumerate(row):
                self.cell(col_widths[i], 6, str(val), border=1, align="R")
            self.ln()
        self.ln(3)


def fmt_d(val):
    if abs(val) >= 1e6:
        return f"${val/1e6:,.1f}M"
    return f"${val:,.0f}"


def generate_pdf(purchase_price, multiples, eboc, pro_forma, returns,
                 loan_amort, note_amort, earnout_scenarios, dscr, inputs,
                 synergy_schedule=None):
    pdf = AnalysisPDF()
    pdf.alias_nb_pages()
    pdf.add_page()

    # Deal Summary
    pdf.section_title("Deal Summary")
    pdf.kv_row("Purchase Price:", fmt_d(purchase_price))
    pdf.kv_row("Revenue Multiple:", f"{multiples['revenue_multiple']:.2f}x")
    pdf.kv_row("AUM Multiple:", f"{multiples['aum_multiple']:.2f}%")
    pdf.kv_row("EBOC Multiple:", f"{multiples['eboc_multiple']:.2f}x")
    pdf.kv_row("EBOC:", fmt_d(eboc))
    pdf.ln(3)

    # Target Firm
    pdf.section_title("Target Firm")
    pdf.kv_row("AUM:", fmt_d(inputs["aum"]))
    pdf.kv_row("Annual Revenue:", fmt_d(inputs["revenue"]))
    pdf.kv_row("EBITDA:", fmt_d(inputs["ebitda"]))
    pdf.kv_row("Owner's Compensation:", fmt_d(inputs["owner_comp"]))
    pdf.kv_row("Number of Clients:", str(inputs["num_clients"]))
    pdf.kv_row("Revenue Growth Rate:", f"{inputs['growth_rate']*100:.1f}%")
    pdf.kv_row("Client Attrition Rate:", f"{inputs['attrition_rate']*100:.1f}%")
    pdf.ln(3)

    # Key Returns
    pdf.section_title("Buyer Return Metrics")
    for yr in [3, 5, 7]:
        # Same gate as every other IRR surface. This one printed
        # "Year 5 IRR: 1131149108065523728384.0%" into a downloadable PDF —
        # and it survived two rounds of fixes because the key is COMPUTED
        # (f"irr_yr{yr}"), so grepping for "irr_yr5" could never find it.
        irr = returns.get(f"irr_yr{yr}")
        coc = returns.get(f"coc_yr{yr}")
        irr_txt = f"{irr*100:.1f}%" if irr_is_meaningful(irr) else "n/a"
        coc_txt = f"{coc:.2f}x" if isinstance(coc, (int, float)) and math.isfinite(coc) else "n/a"
        pdf.kv_row(f"Year {yr} IRR:", irr_txt)
        pdf.kv_row(f"Year {yr} Cash-on-Cash:", coc_txt)
    pdf.kv_row("Breakeven Year:", str(returns["breakeven_year"]))
    pdf.kv_row("Total Cash Invested:", fmt_d(returns["total_cash_invested"]))
    pdf.ln(3)

    # Pro Forma P&L
    pdf.add_page()
    pdf.section_title("5-Year Pro Forma P&L")
    pf5 = pro_forma[pro_forma["year"] <= 5]
    headers = ["Year", "Revenue", "Expenses", "EBITDA", "Debt Svc", "Net CF"]
    rows = []
    for _, r in pf5.iterrows():
        rows.append([
            str(int(r["year"])), fmt_d(r["revenue"]), fmt_d(r["expenses"]),
            fmt_d(r["ebitda"]), fmt_d(r["debt_service"]), fmt_d(r["net_cash_flow"]),
        ])
    pdf.table(headers, rows)

    # DSCR
    pdf.section_title("Debt Service Coverage Ratio")
    dscr_vals = pro_forma[["year", "dscr"]].copy()
    dscr_rows = [[str(int(r["year"])), f"{r['dscr']:.2f}x"] for _, r in dscr_vals.iterrows() if r["dscr"] > 0]
    if dscr_rows:
        pdf.table(["Year", "DSCR"], dscr_rows, [40, 40])

    # Earnout Scenarios
    pdf.section_title("Earnout Scenarios")
    for sc in earnout_scenarios:
        pdf.kv_row(f"{sc['scenario']}:", f"{fmt_d(sc['total_payout'])} ({sc['pct_of_max']:.0f}% of max)")
    pdf.ln(3)

    # Loan Amortization
    if len(loan_amort) > 0:
        pdf.add_page()
        pdf.section_title("Loan Amortization Schedule")
        headers = ["Year", "Beg Bal", "Payment", "Interest", "Principal", "End Bal"]
        rows = []
        for _, r in loan_amort.iterrows():
            rows.append([
                str(int(r["year"])), fmt_d(r["beg_balance"]), fmt_d(r["payment"]),
                fmt_d(r["interest"]), fmt_d(r["principal_paid"]), fmt_d(r["end_balance"]),
            ])
        pdf.table(headers, rows)

    # Seller Note
    if len(note_amort) > 0:
        pdf.section_title("Seller Note Amortization")
        rows = []
        for _, r in note_amort.iterrows():
            rows.append([
                str(int(r["year"])), fmt_d(r["beg_balance"]), fmt_d(r["payment"]),
                fmt_d(r["interest"]), fmt_d(r["principal_paid"]), fmt_d(r["end_balance"]),
            ])
        pdf.table(headers, rows)

    # Integration Strategy
    pdf.add_page()
    pdf.section_title("Integration Strategy")
    if synergy_schedule is not None and len(synergy_schedule) > 0:
        attrs = synergy_schedule.attrs
        pdf.kv_row("Cost Takeout (Run-Rate):", fmt_d(attrs["runrate_cost_takeout"]))
        pdf.kv_row("Revenue Uplift (Run-Rate):", fmt_d(attrs["runrate_revenue_uplift"]))
        pdf.kv_row("Total Run-Rate Synergy:", fmt_d(attrs["runrate_total_synergy"]))
        pdf.kv_row("Total Capture Cost:", fmt_d(attrs["total_capture_cost"]))
        pdf.kv_row("Buyer Capability Factor:", f"{attrs['capability_factor']*100:.0f}%")
        pdf.kv_row("Synergy NPV @ 10%:", fmt_d(attrs["synergy_npv"]))
        pdf.ln(3)
        headers = ["Year", "Cost Takeout", "Revenue Uplift", "Capture Cost", "Net Synergy"]
        rows = []
        for _, r in synergy_schedule.iterrows():
            rows.append([
                str(int(r["year"])), fmt_d(r["cost_takeout"]), fmt_d(r["revenue_uplift"]),
                fmt_d(r["capture_cost"]), fmt_d(r["net_synergy"]),
            ])
        pdf.table(headers, rows)
    else:
        pdf.kv_row("Y1 Integration Cost:", fmt_d(inputs["integration_costs"]))
        pdf.kv_row(
            "Consulting (annual x years):",
            f"{fmt_d(inputs['consulting_annual'])} x {inputs['consulting_years']} yr",
        )
        pdf.kv_row(
            "Non-compete (total / years):",
            f"{fmt_d(inputs['noncompete_total'])} over {inputs['noncompete_years']} yr",
        )

    # Output
    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return buf.getvalue()


# ==============================================================================
# Deal Memo PDF -- five light-themed pages mirroring memo_mode.render_memo_tab.
#
# The memo is a structured document, not a dashboard export: white page, black
# body text, Helvetica core fonts (no runtime font fetching), palette hexes
# only on graphic elements (dots, bars, rules) plus the two sanctioned color-
# text exceptions (verdict word, band bid-label) and the spec'd red negatives
# in the pro forma table. All copy passes through memo_mode.pdf_safe() to
# stay ASCII-only for fpdf2's latin-1 core fonts.
#
# generate_pdf() above is untouched; MemoPDF is a sibling, not a replacement.
# ==============================================================================

from memo_mode import (  # noqa: E402  (kept with the memo section it serves)
    build_memo_data, is_zero_state, pdf_safe,
    fmt_money, fmt_money_table, fmt_mult, fmt_dscr, fmt_irr,
    _sev, _plain, _breakeven_parts,
    APP_URL as MEMO_APP_URL, MEMO_DISCLAIMER, _comps_table_caption,
)

_M_ACCENT = (124, 140, 255)
_M_POS = (74, 222, 128)
_M_CAUTION = (251, 191, 36)
_M_NEG = (248, 113, 113)
_M_MUTED = (107, 115, 133)
_M_LIGHT = (139, 148, 168)
_M_DARK = (47, 54, 70)
_M_BLACK = (0, 0, 0)

_SEV_RGB = {
    "positive": _M_POS, "caution": _M_CAUTION, "negative": _M_NEG,
    "info": _M_ACCENT, "muted": _M_MUTED,
}
_VERDICT_RGB = {"go": _M_POS, "caution": _M_CAUTION, "reconsider": _M_NEG,
                "incomplete": _M_MUTED}
_SEG_RGB = {"upfront": _M_ACCENT, "note": _M_LIGHT,
            "earnout": _M_MUTED, "rollover": _M_DARK}


class MemoPDF(FPDF):
    """Light-themed 5-page deal memo (spec pages 1-5, PDF variants)."""

    def __init__(self, firm, date_str):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.memo_firm = pdf_safe(firm)
        self.memo_date = pdf_safe(date_str)
        self.set_margins(20, 16, 20)
        self.set_auto_page_break(False)

    def header(self):
        # Page 1 carries the document header in-body; pages 2-5 get the
        # slim pagehead: firm left, date right, thin rule beneath.
        if self.page_no() == 1:
            return
        self.set_y(10)
        self.set_font("Helvetica", "", 8.5)
        self.set_text_color(*_M_MUTED)
        self.cell(85, 5, self.memo_firm, align="L")
        self.cell(85, 5, self.memo_date, align="R", new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*_M_LIGHT)
        self.set_line_width(0.2)
        self.line(20, 16.5, 190, 16.5)
        self.set_y(24)

    def footer(self):
        self.set_draw_color(*_M_LIGHT)
        self.set_line_width(0.2)
        self.line(20, 281, 190, 281)
        self.set_y(-13)
        self.set_font("Helvetica", "", 7.5)
        self.set_text_color(*_M_MUTED)
        # The URL is the only way back to the funnel from a printed page.
        self.cell(120, 5, f"Prepared with RIA M&A Calculator -- {MEMO_APP_URL}",
                  align="L")
        self.cell(50, 5, f"Page {self.page_no()} of 5", align="R")


def _m_dot(pdf, x, y, diameter, rgb):
    pdf.set_fill_color(*rgb)
    pdf.ellipse(x, y, diameter, diameter, "F")


def _m_head(pdf, title):
    """Section head: 1.2 x 4mm accent bar + 9pt bold uppercase black."""
    y = pdf.get_y() + 2
    pdf.set_fill_color(*_M_ACCENT)
    pdf.rect(20, y + 0.6, 1.2, 4, "F")
    pdf.set_xy(24, y)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*_M_BLACK)
    pdf.cell(0, 5, pdf_safe(title).upper(), align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)


def _m_para(pdf, text):
    pdf.set_x(20)
    pdf.set_font("Helvetica", "", 10.5)
    pdf.set_text_color(*_M_BLACK)
    pdf.multi_cell(170, 5.7, pdf_safe(text), align="L")


def _m_caption(pdf, text):
    pdf.set_x(20)
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*_M_MUTED)
    pdf.multi_cell(170, 4.4, pdf_safe(text), align="L")
    pdf.ln(1)


def _m_disclaimer(pdf, text):
    """Boxed small print, last page only. Deliberately quiet but unmissable:
    a forwarded memo has to carry its own scope limits."""
    pdf.set_x(20)
    y = pdf.get_y()
    pdf.set_draw_color(*_M_LIGHT)
    pdf.set_line_width(0.2)
    pdf.set_font("Helvetica", "", 7.2)
    pdf.set_text_color(*_M_MUTED)
    pdf.multi_cell(170, 3.4, pdf_safe(text), align="L", border=0)
    pdf.rect(20, y - 1.5, 170, pdf.get_y() - y + 2.5)
    pdf.ln(1)


def _m_insight(pdf, ins, drop_sub=False):
    """One insight strip: thin box, 1mm severity left bar, dot, plain text.
    Severity and copy come from insights.py via the digest -- never re-derived.

    drop_sub suppresses the hedge sub-label where the page states the same
    reference in a caption (page 3's returns strip)."""
    if ins is None:
        return
    sev = _sev(ins)
    rgb = _SEV_RGB.get(sev, _M_MUTED)
    x, w, h = 20, 170, 11
    y = pdf.get_y() + 1.5
    pdf.set_draw_color(*_M_LIGHT)
    pdf.set_line_width(0.2)
    pdf.rect(x, y, w, h)
    pdf.set_fill_color(*rgb)
    pdf.rect(x, y, 1.0, h, "F")
    _m_dot(pdf, x + 4.5, y + h / 2 - 0.8, 1.6, rgb)
    pdf.set_xy(x + 8, y + 3)
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(*_M_BLACK)
    text = pdf_safe(_plain(ins))
    data = getattr(ins, "data", None)
    sub = None if drop_sub else (data.get("sub") if isinstance(data, dict) else None)
    if sub:
        pdf.cell(108, 5, text, align="L")
        pdf.set_xy(x + 118, y + 3.4)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*_M_MUTED)
        pdf.cell(48, 4.5, pdf_safe(str(sub)), align="R")
    else:
        pdf.cell(158, 5, text, align="L")
    pdf.set_y(y + h + 2)


def _m_stat_row(pdf, stats, col_w, dots=None):
    """Row of borderless stat tiles: 7.5pt muted label over 13pt bold value,
    optional 2.2mm severity dot before the value."""
    y = pdf.get_y()
    for i, (label, value) in enumerate(stats):
        x = 20 + i * col_w
        pdf.set_xy(x, y)
        pdf.set_font("Helvetica", "B", 7.5)
        pdf.set_text_color(*_M_MUTED)
        pdf.cell(col_w - 2, 4, pdf_safe(label).upper(), align="L")
        vx = x
        if dots and dots[i]:
            _m_dot(pdf, x + 0.3, y + 6.4, 2.2, _SEV_RGB.get(dots[i], _M_MUTED))
            vx = x + 4
        pdf.set_xy(vx, y + 4.6)
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_text_color(*_M_BLACK)
        pdf.cell(col_w - (vx - x) - 2, 6, pdf_safe(value), align="L")
    pdf.set_y(y + 13)


def _m_verdict(pdf, d):
    """Verdict bar: 1 x 10mm severity bar + 3mm dot + colored verdict word
    (one of the two sanctioned color-text spots) + muted counts."""
    y = pdf.get_y() + 2
    rgb = _VERDICT_RGB.get(d["verdict"], _M_MUTED)
    pdf.set_fill_color(*rgb)
    pdf.rect(20, y, 1.0, 10, "F")
    _m_dot(pdf, 24.5, y + 3.5, 3, rgb)
    pdf.set_xy(30, y + 1.5)
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(*rgb)
    pdf.cell(60, 7, pdf_safe(d["verdict_word"]), align="L")
    pdf.set_xy(110, y + 3)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*_M_MUTED)
    pdf.cell(80, 5, pdf_safe(d["counts_line"]), align="R")
    pdf.set_y(y + 13)


def _m_band(pdf, positions, axis_labels, marker_label, sev,
            track_only=False, track_only_label=None):
    """Band number-line, 170mm: 0.6mm track, 4mm band rect, 0.3 x 6mm ticks,
    1.2 x 8mm severity marker with its label above (the second sanctioned
    color-text spot). Percent positions come from memo_mode._band_positions."""
    x0, w = 20, 170
    top = pdf.get_y() + 8
    ty = top + 6

    def X(pct):
        return x0 + pct / 100.0 * w

    bid_pos = positions.get("bid") if positions else None
    rgb = _SEV_RGB.get(sev, _M_MUTED)
    if bid_pos is not None and marker_label:
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*rgb)
        lx = min(max(X(bid_pos) - 25, x0), x0 + w - 50)
        pdf.set_xy(lx, top - 5.5)
        pdf.cell(50, 4, pdf_safe(marker_label), align="C")

    pdf.set_draw_color(*_M_MUTED)
    pdf.set_line_width(0.6)
    pdf.line(x0, ty, x0 + w, ty)

    if not track_only and positions:
        bx = X(positions["low"])
        bw = max(0.0, X(positions["high"]) - bx)
        pdf.set_fill_color(*_M_LIGHT)
        pdf.rect(bx, ty - 2, bw, 4, "F")
        pdf.set_fill_color(*_M_MUTED)
        for key in ("low", "mid", "high"):
            pdf.rect(X(positions[key]) - 0.15, ty - 3, 0.3, 6, "F")
    if bid_pos is not None:
        pdf.set_fill_color(*rgb)
        pdf.rect(X(bid_pos) - 0.6, ty - 4, 1.2, 8, "F")

    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*_M_MUTED)
    if not track_only and positions:
        for key, label in axis_labels:
            lx = min(max(X(positions[key]) - 20, x0 - 2), x0 + w - 38)
            pdf.set_xy(lx, ty + 5)
            pdf.cell(40, 4, pdf_safe(label), align="C")
    elif track_only_label:
        pdf.set_xy(x0 + w / 2 - 25, ty + 5)
        pdf.cell(50, 4, pdf_safe(track_only_label), align="C")
    pdf.set_y(ty + 11)


def _m_structure(pdf, d):
    """Two 170 x 8mm stacked bars (Suggested / Yours) with 0.8mm white gaps.
    Fills are the certainty-ramp palette; % labels sit inside segments >= 8%."""
    x0, w, bar_h = 20, 170, 8
    for label, segs in d["structure_rows"]:
        pdf.set_x(x0)
        pdf.set_font("Helvetica", "B", 7.5)
        pdf.set_text_color(*_M_MUTED)
        pdf.cell(0, 4, pdf_safe(label).upper(), new_x="LMARGIN", new_y="NEXT")
        y = pdf.get_y() + 0.5
        vis = [(seg, frac) for seg, frac in segs if frac > 0]
        total = sum(frac for _, frac in vis)
        if not vis or total <= 0:
            continue
        gaps = 0.8 * (len(vis) - 1)
        avail = w - gaps
        x = x0
        for seg, frac in vis:
            sw = frac / total * avail
            pdf.set_fill_color(*_SEG_RGB[seg])
            pdf.rect(x, y, sw, bar_h, "F")
            if frac * 100 >= 8:
                pdf.set_font("Helvetica", "", 7.5)
                if seg in ("upfront", "note"):
                    pdf.set_text_color(11, 13, 18)
                else:
                    pdf.set_text_color(229, 233, 240)
                pdf.set_xy(x, y + 2)
                pdf.cell(sw, 4, f"{frac*100:.0f}", align="C")
            x += sw + 0.8
        pdf.set_y(y + bar_h + 3)
    x = x0
    y = pdf.get_y() + 1
    pdf.set_font("Helvetica", "", 8)
    for seg in ("upfront", "note", "earnout", "rollover"):
        pdf.set_fill_color(*_SEG_RGB[seg])
        pdf.rect(x, y + 0.7, 3, 3, "F")
        pdf.set_xy(x + 4.5, y)
        pdf.set_text_color(*_M_MUTED)
        pdf.cell(22, 4.5, seg.capitalize(), align="L")
        x += 32
    pdf.set_y(y + 7)


def _m_table(pdf, headers, widths, rows, body_size=9.5):
    """Rules-only table: muted uppercase header, 0.2mm light rules under the
    header and each row, no fills, no vertical rules.

    headers: [(text, align)]; rows: [[(text, align, rgb, dot_rgb), ...]] --
    rgb None means black text; dot_rgb draws a 1.5mm dot at the cell's left
    edge (used to flag estimated comps without coloring body text)."""
    x0 = 20
    total_w = sum(widths)
    pdf.set_x(x0)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*_M_MUTED)
    for (htxt, halign), wdt in zip(headers, widths):
        pdf.cell(wdt, 6, pdf_safe(htxt).upper(), align=halign)
    pdf.ln(6)
    pdf.set_draw_color(*_M_LIGHT)
    pdf.set_line_width(0.2)
    pdf.line(x0, pdf.get_y(), x0 + total_w, pdf.get_y())
    for row in rows:
        ry = pdf.get_y() + 1.0
        pdf.set_font("Helvetica", "", body_size)
        x = x0
        for (txt, align, rgb, dot_rgb), wdt in zip(row, widths):
            if dot_rgb is not None:
                _m_dot(pdf, x + 1.2, ry + 1.9, 1.5, dot_rgb)
            pdf.set_text_color(*(rgb if rgb else _M_BLACK))
            pdf.set_xy(x, ry)
            pdf.cell(wdt, 5.4, pdf_safe(txt), align=align)
            x += wdt
        yline = ry + 6.0
        pdf.line(x0, yline, x0 + total_w, yline)
        pdf.set_y(yline)
    pdf.ln(3)


def _m_fit(pdf, text, width):
    """Truncate text with an ellipsis to fit a column (font must be set)."""
    t = pdf_safe(text)
    if pdf.get_string_width(t) <= width - 2:
        return t
    while t and pdf.get_string_width(t + "...") > width - 2:
        t = t[:-1]
    return t + "..."


def _m_synergy_bars(pdf, years):
    """Per-year net-synergy bars on a 40mm plot: 18mm bars, 8mm gaps, 0.3mm
    black baseline at the zero line; accent above, red below."""
    if not years:
        return
    bar_w, gap = 18.0, 8.0
    n = len(years)
    total_w = n * bar_w + (n - 1) * gap
    x0 = 20 + (170 - total_w) / 2
    max_abs = max(abs(v) for _, v in years) or 1.0
    scale = 26.0 / max_abs
    max_pos_h = max((v for _, v in years if v > 0), default=0.0) * scale
    max_neg_h = max((-v for _, v in years if v < 0), default=0.0) * scale
    top = pdf.get_y() + 6
    zero_y = top + max_pos_h + 5

    pdf.set_draw_color(*_M_BLACK)
    pdf.set_line_width(0.3)
    pdf.line(x0 - 2, zero_y, x0 + total_w + 2, zero_y)

    pdf.set_font("Helvetica", "", 8)
    for i, (yr, v) in enumerate(years):
        x = x0 + i * (bar_w + gap)
        h = abs(v) * scale
        sign = "-" if v < 0 else ""
        label = f"{sign}${abs(v)/1e6:.1f}M"
        if v >= 0:
            pdf.set_fill_color(*_M_ACCENT)
            pdf.rect(x, zero_y - h, bar_w, h, "F")
            pdf.set_xy(x - 2, zero_y - h - 4.6)
        else:
            pdf.set_fill_color(*_M_NEG)
            pdf.rect(x, zero_y, bar_w, h, "F")
            pdf.set_xy(x - 2, zero_y + h + 0.6)
        pdf.set_text_color(*_M_MUTED)
        pdf.cell(bar_w + 4, 4, label, align="C")

    ylab = zero_y + max_neg_h + 6
    pdf.set_text_color(*_M_MUTED)
    for i, (yr, _v) in enumerate(years):
        pdf.set_xy(x0 + i * (bar_w + gap) - 2, ylab)
        pdf.cell(bar_w + 4, 4, f"Y{yr}", align="C")
    pdf.set_y(ylab + 7)


def _m_bullets(pdf, bullets):
    """Open-question bullets: 1.2mm gray dots, 10pt text, 4mm hanging indent."""
    for b in bullets:
        y = pdf.get_y() + 1
        _m_dot(pdf, 21.2, y + 1.9, 1.2, _M_LIGHT)
        pdf.set_xy(26, y)
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(*_M_BLACK)
        pdf.multi_cell(164, 5.3, pdf_safe(b), align="L")
        pdf.set_y(pdf.get_y() + 0.8)


def _m_empty_box(pdf, line1, line2):
    """Honest comps empty state: bordered box, bold black line, muted line."""
    x, w = 30, 150
    y = pdf.get_y() + 2
    pdf.set_draw_color(*_M_LIGHT)
    pdf.set_line_width(0.2)
    pdf.rect(x, y, w, 24)
    pdf.set_xy(x + 5, y + 4)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(*_M_BLACK)
    pdf.cell(w - 10, 6, pdf_safe(line1), align="C", new_x="LEFT", new_y="NEXT")
    pdf.set_x(x + 5)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*_M_MUTED)
    pdf.multi_cell(w - 10, 4.6, pdf_safe(line2), align="C")
    pdf.set_y(y + 27)


def generate_memo_pdf(ctx, extras):
    """Render the 5-page deal memo as PDF bytes.

    Mirrors memo_mode.render_memo_tab page for page; both consume the same
    memo_mode.build_memo_data digest so screen and PDF cannot disagree.

    Zero-state guard: raises ValueError. The tab hides the download button
    when no firm data is entered, so reaching this with an empty deal is a
    caller bug we want loud -- not a silent memo full of $0s.
    """
    if is_zero_state(ctx):
        raise ValueError(
            "No deal data entered -- the memo PDF is unavailable in the zero state."
        )
    d = build_memo_data(ctx, extras)
    pdf = MemoPDF(d["firm"], d["date_long"])

    # ---- Page 1: executive summary ------------------------------------------
    pdf.add_page()
    pdf.set_y(24)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*_M_MUTED)
    if hasattr(pdf, "set_char_spacing"):
        pdf.set_char_spacing(0.35)
    pdf.cell(140, 4, "ACQUISITION MEMO", align="L")
    if hasattr(pdf, "set_char_spacing"):
        pdf.set_char_spacing(0)
    pdf.set_font("Helvetica", "", 8)
    pdf.cell(30, 4, "1 / 5", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(*_M_BLACK)
    pdf.cell(0, 9, pdf_safe(d["firm"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*_M_MUTED)
    pdf.cell(0, 5, pdf_safe(d["meta_line"]), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    _m_stat_row(pdf, d["stats_target"], 56.7)
    pdf.ln(2)
    _m_stat_row(pdf, d["stats_bid"], 56.7)
    pdf.ln(2)
    _m_verdict(pdf, d)
    pdf.ln(2)
    _m_para(pdf, d["paragraph"])

    # ---- Page 2: valuation & structure ---------------------------------------
    pdf.add_page()
    _m_head(pdf, "Valuation band")
    if d["band"]:
        axis = [("low", f"Low {fmt_money(d['val']['low'])}"),
                ("mid", f"Mid {fmt_money(d['val']['mid'])}"),
                ("high", f"High {fmt_money(d['val']['high'])}")]
        marker = f"your bid {fmt_money(d['bid'])}" if d["bid"] > 0 else None
        _m_band(pdf, d["band"], axis, marker, _sev(d["ins"]["valuation"]))
    else:
        _m_caption(pdf, "Valuation band unavailable — adjusted EBITDA is zero "
                        "at these inputs.")
    _m_insight(pdf, d["ins"]["valuation"])
    pdf.ln(3)
    _m_head(pdf, "Deal structure")
    _m_structure(pdf, d)
    if d["rationale"]:
        _m_caption(pdf, d["rationale"])

    # ---- Page 3: pro forma & returns -----------------------------------------
    pdf.add_page()
    _m_head(pdf, "Pro forma, five years")
    if d["pf_rows"]:
        headers = [("Year", "L"), ("Revenue", "R"), ("EBITDA", "R"),
                   ("Debt service", "R"), ("Net cash flow", "R"), ("DSCR", "R")]
        widths = [16, 33, 33, 34, 34, 20]
        rows = []
        for r in d["pf_rows"]:
            row = [(f"Y{r['year']}", "L", None, None)]
            for v in (r["revenue"], r["ebitda"], r["debt"], r["ncf"]):
                row.append((fmt_money_table(v), "R", _M_NEG if v < 0 else None, None))
            row.append((fmt_dscr(r["dscr"]), "R", None, None))
            rows.append(row)
        _m_table(pdf, headers, widths, rows)
    else:
        _m_caption(pdf, "Pro forma unavailable at these inputs.")
    pdf.ln(2)
    _m_head(pdf, "Returns")
    ret, dots = d["ret"], d["stat_dots"]
    dscr_val = "n/a"
    if ret["min_dscr"] is not None:
        dscr_val = f"{fmt_dscr(ret['min_dscr'])} (Y{ret['min_dscr_year']})"
    _m_stat_row(
        pdf,
        [("5Y IRR", fmt_irr(ret["irr5"])),
         ("5Y cash-on-cash", fmt_mult(ret["coc5"])),
         ("Breakeven", _breakeven_parts(ret["breakeven"])[0]),
         ("Lowest DSCR", dscr_val)],
        42.5,
        dots=[dots["irr"], dots["coc"], dots["be"], dots["dscr"]],
    )
    pdf.ln(1)
    # drop_sub: the hurdle caption below states the ~18-22% reference already.
    _m_insight(pdf, d["ins"]["returns"], drop_sub=True)
    _m_insight(pdf, d["ins"]["dscr"])
    pdf.ln(1)
    _m_caption(pdf, d["hurdle_caption"])

    # ---- Page 4: integration & risks -----------------------------------------
    pdf.add_page()
    if d["syn"]["mode"] == "schedule":
        _m_head(pdf, "Synergy ramp")
        _m_synergy_bars(pdf, d["syn"]["years"])
        _m_caption(pdf, d["syn"]["note"])
    else:
        _m_head(pdf, "Integration plan")
        _m_para(pdf, d["syn"]["note"])
        pdf.ln(1)
    _m_insight(pdf, d["ins"]["synergy"])
    pdf.ln(3)
    _m_head(pdf, "Open questions")
    _m_bullets(pdf, d["bullets"])

    # ---- Page 5: comps benchmark ----------------------------------------------
    pdf.add_page()
    _m_head(pdf, "Comps benchmark")
    c = d["comps"]
    marker = f"your deal {fmt_mult(c['deal_mult'])}" if c["deal_mult"] else None
    if c["band_ok"] and c["strip"]:
        axis = [("low", f"P25 {fmt_mult(c['p25'])}"),
                ("mid", f"Median {fmt_mult(c['median'])}"),
                ("high", f"P75 {fmt_mult(c['p75'])}")]
        _m_band(pdf, c["strip"], axis, marker, c["sev"])
        _m_insight(pdf, d["ins"]["comps"])
    else:
        positions = {"bid": 50.0} if c["deal_mult"] else None
        _m_band(pdf, positions, [], marker, "muted",
                track_only=True, track_only_label="no reliable band")
        _m_empty_box(
            pdf,
            f"Insufficient confirmed comparables -- N={c['n_confirmed']}.",
            "Multiples for most RIA deals are never disclosed. The band above "
            "is withheld rather than built on estimates.",
        )
    if c["rows"]:
        pdf.ln(2)
        headers = [("Date", "L"), ("Buyer", "L"), ("Seller", "L"),
                   ("AUM", "R"), ("EV/EBITDA", "R"), ("Source", "R")]
        widths = [22, 34, 34, 24, 26, 30]
        pdf.set_font("Helvetica", "", 8.5)
        rows = []
        for r in c["rows"]:
            # Estimated rows flag with a caution dot + muted text: severity
            # hues stay off body text in the PDF (spec implementation note 8).
            rows.append([
                (r["date"], "L", None, None),
                (_m_fit(pdf, r["buyer"], 34), "L", None, None),
                (_m_fit(pdf, r["seller"], 34), "L", None, None),
                (fmt_money(r["aum"]), "R", None, None),
                (fmt_mult(r["mult"]), "R", None, None),
                ("Est." if r["est"] else "Confirmed", "R", _M_MUTED,
                 _M_CAUTION if r["est"] else None),
            ])
        _m_table(pdf, headers, widths, rows, body_size=8.5)
        # A printed page 5 circulates without anyone here to explain it: real
        # buyer/seller names sitting beside a modeled multiple need the legend
        # spelled out, not compressed into a four-character tag.
        _m_caption(pdf, _comps_table_caption(c))
    elif not c["available"]:
        _m_caption(pdf, "No comparable-transactions database is available in "
                        "this deployment.")

    pdf.ln(3)
    _m_disclaimer(pdf, MEMO_DISCLAIMER)
    return bytes(pdf.output())
