"""
PDF Export for RIA M&A Calculator.
Generates a summary PDF of the full acquisition analysis.
"""

from fpdf import FPDF
import io
from datetime import date


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
                 loan_amort, note_amort, earnout_scenarios, dscr, inputs):
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
        irr = returns.get(f"irr_yr{yr}", 0)
        coc = returns.get(f"coc_yr{yr}", 0)
        pdf.kv_row(f"Year {yr} IRR:", f"{irr*100:.1f}%")
        pdf.kv_row(f"Year {yr} Cash-on-Cash:", f"{coc:.2f}x")
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

    # Output
    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return buf.getvalue()
