"""
RIA M&A Calculator — Streamlit Application
Buyer-side economics for acquiring a Registered Investment Advisor.
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from calculations import (
    compute_eboc, implied_multiples, build_pro_forma,
    compute_irr_and_returns, build_loan_amortization,
    build_seller_note_amortization, compute_dscr,
    compute_earnout_scenarios, compute_seller_total_proceeds,
    sensitivity_irr, sensitivity_breakeven,
)

# -- Page config ---------------------------------------------------------------
st.set_page_config(page_title="RIA M&A Calculator", page_icon="📊", layout="wide")

# -- Dark theme CSS ------------------------------------------------------------
st.markdown("""
<style>
    .stApp { background-color: #0e1117; }
    .metric-card {
        background: linear-gradient(135deg, #1a1f2e 0%, #16192b 100%);
        border: 1px solid #2d3748;
        border-radius: 12px;
        padding: 16px 12px;
        text-align: center;
        margin: 5px;
        min-height: 110px;
        display: flex;
        flex-direction: column;
        justify-content: center;
    }
    .metric-card h3 {
        color: #8b95a5;
        font-size: 0.7rem;
        font-weight: 600;
        margin-bottom: 8px;
        text-transform: uppercase;
        letter-spacing: 0.03em;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .metric-card h2 {
        color: #e2e8f0;
        font-size: 1.5rem;
        font-weight: 700;
        margin: 0;
        white-space: nowrap;
    }
    .metric-card .positive { color: #48bb78; }
    .metric-card .negative { color: #fc8181; }
    .metric-card .neutral { color: #63b3ed; }
    .section-header {
        color: #e2e8f0;
        border-bottom: 2px solid #4a90d9;
        padding-bottom: 8px;
        margin: 25px 0 15px 0;
        font-size: 1.1rem;
        font-weight: 600;
    }
    div[data-testid="stSidebar"] {
        background-color: #131722;
    }
    .stTabs [data-baseweb="tab-list"] { gap: 8px; }
    .stTabs [data-baseweb="tab"] {
        background-color: #1a1f2e;
        border-radius: 8px 8px 0 0;
        padding: 10px 20px;
        color: #8b95a5;
    }
    .stTabs [aria-selected="true"] {
        background-color: #2d3748;
        color: #e2e8f0;
    }
    /* Compact sidebar inputs */
    div[data-testid="stSidebar"] .stTextInput > div > div > input {
        font-family: 'SF Mono', 'Consolas', monospace;
        font-size: 0.95rem;
    }
</style>
""", unsafe_allow_html=True)

# -- Session state -------------------------------------------------------------
if "show_calculator" not in st.session_state:
    st.session_state.show_calculator = False

PLOTLY_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(26,31,46,0.8)",
    font=dict(color="#e2e8f0", size=12),
    xaxis=dict(gridcolor="#2d3748", zerolinecolor="#2d3748"),
    yaxis=dict(gridcolor="#2d3748", zerolinecolor="#2d3748"),
    margin=dict(l=40, r=40, t=50, b=40),
    legend=dict(bgcolor="rgba(0,0,0,0)"),
)

COLORS = ["#4a90d9", "#48bb78", "#ed8936", "#fc8181", "#9f7aea", "#63b3ed"]


# -- Helpers -------------------------------------------------------------------
def fmt_dollar(val):
    if abs(val) >= 1e9:
        return f"${val/1e9:,.1f}B"
    if abs(val) >= 1e6:
        return f"${val/1e6:,.1f}M"
    if abs(val) >= 1e3:
        return f"${val/1e3:,.0f}K"
    return f"${val:,.0f}"


def fmt_pct(val):
    return f"{val*100:.1f}%"


def metric_card(label, value, css_class="neutral"):
    return f"""
    <div class="metric-card">
        <h3>{label}</h3>
        <h2 class="{css_class}">{value}</h2>
    </div>
    """


def render_welcome_page():
    """Render the welcome/landing page with sidebar hidden."""
    st.markdown("""
    <style>
        [data-testid="stSidebar"] { display: none; }
        [data-testid="stSidebarCollapsedControl"] { display: none; }
        .welcome-container {
            max-width: 880px;
            margin: 0 auto;
            padding: 60px 20px 40px 20px;
        }
        .welcome-header {
            text-align: center;
            margin-bottom: 48px;
        }
        .welcome-header h1 {
            color: #e2e8f0;
            font-size: 2.4rem;
            font-weight: 700;
            letter-spacing: -0.02em;
            margin-bottom: 12px;
            line-height: 1.2;
        }
        .welcome-header .subtitle {
            color: #8b95a5;
            font-size: 1.05rem;
            font-weight: 400;
            line-height: 1.6;
            max-width: 560px;
            margin: 0 auto;
        }
        .welcome-divider {
            width: 48px;
            height: 2px;
            background-color: #4a90d9;
            margin: 20px auto;
        }
        .feature-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 14px;
            margin-bottom: 48px;
        }
        @media (max-width: 768px) {
            .feature-grid { grid-template-columns: repeat(2, 1fr); }
        }
        @keyframes cardFadeUp {
            from { opacity: 0; transform: translateY(18px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .feature-card {
            background: #1a1f2e;
            border: 1px solid #2d3748;
            border-radius: 8px;
            padding: 22px 18px;
            transition: transform 0.25s ease, border-color 0.25s ease, box-shadow 0.25s ease;
            animation: cardFadeUp 0.5s ease both;
        }
        .feature-card:nth-child(1) { animation-delay: 0.05s; }
        .feature-card:nth-child(2) { animation-delay: 0.12s; }
        .feature-card:nth-child(3) { animation-delay: 0.19s; }
        .feature-card:nth-child(4) { animation-delay: 0.26s; }
        .feature-card:nth-child(5) { animation-delay: 0.33s; }
        .feature-card:nth-child(6) { animation-delay: 0.40s; }
        .feature-card:hover {
            transform: translateY(-3px);
            box-shadow: 0 8px 24px rgba(0,0,0,0.3);
        }
        .feature-card.fc-blue:hover   { border-color: #4a90d9; }
        .feature-card.fc-green:hover  { border-color: #48bb78; }
        .feature-card.fc-amber:hover  { border-color: #ed8936; }
        .feature-card.fc-purple:hover { border-color: #9f7aea; }
        .feature-card.fc-cyan:hover   { border-color: #63b3ed; }
        .feature-card.fc-rose:hover   { border-color: #f687b3; }
        .feature-icon {
            width: 32px;
            height: 32px;
            border-radius: 6px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.9rem;
            margin-bottom: 12px;
        }
        .fi-blue   { background: rgba(74,144,217,0.15); color: #4a90d9; }
        .fi-green  { background: rgba(72,187,120,0.15); color: #48bb78; }
        .fi-amber  { background: rgba(237,137,54,0.15); color: #ed8936; }
        .fi-purple { background: rgba(159,122,234,0.15); color: #9f7aea; }
        .fi-cyan   { background: rgba(99,179,237,0.15); color: #63b3ed; }
        .fi-rose   { background: rgba(246,135,179,0.15); color: #f687b3; }
        .feature-card h3 {
            color: #e2e8f0;
            font-size: 0.88rem;
            font-weight: 600;
            margin-bottom: 6px;
        }
        .feature-card p {
            color: #6b7280;
            font-size: 0.78rem;
            line-height: 1.5;
            margin: 0;
        }
    </style>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div class="welcome-container">
        <div class="welcome-header">
            <h1>RIA M&A Calculator</h1>
            <div class="welcome-divider"></div>
            <p class="subtitle">
                Buyer-side acquisition modeling for Registered Investment Advisors.
                Structure deals, model financing, and stress-test returns.
            </p>
        </div>
        <div class="feature-grid">
            <div class="feature-card fc-blue">
                <div class="feature-icon fi-blue">&#9670;</div>
                <h3>Deal Structuring</h3>
                <p>Model upfront cash, seller notes, earnouts, and equity rollover splits</p>
            </div>
            <div class="feature-card fc-green">
                <div class="feature-icon fi-green">&#9638;</div>
                <h3>Pro Forma Analysis</h3>
                <p>5-year P&L with revenue growth, attrition, and cost synergies</p>
            </div>
            <div class="feature-card fc-amber">
                <div class="feature-icon fi-amber">&#9686;</div>
                <h3>Return Metrics</h3>
                <p>IRR, cash-on-cash, breakeven analysis, and DSCR tracking</p>
            </div>
            <div class="feature-card fc-purple">
                <div class="feature-icon fi-purple">&#9649;</div>
                <h3>Sensitivity Tables</h3>
                <p>Two-way heatmaps across multiples, growth, and attrition scenarios</p>
            </div>
            <div class="feature-card fc-cyan">
                <div class="feature-icon fi-cyan">&#9655;</div>
                <h3>Earnout Modeling</h3>
                <p>Floor, cap, cliff vesting, and multi-metric performance thresholds</p>
            </div>
            <div class="feature-card fc-rose">
                <div class="feature-icon fi-rose">&#9744;</div>
                <h3>Debt Analysis</h3>
                <p>Amortization schedules with I/O periods, balloon payments, and standstill terms</p>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    _col1, col_btn, _col3 = st.columns([1, 1, 1])
    with col_btn:
        if st.button("Open Calculator", type="primary", use_container_width=True):
            st.session_state.show_calculator = True
            st.rerun()


def render_instructions_tab():
    """Render the instructions/help tab with navigation guide and scenarios."""
    st.markdown("""
    <style>
        .guide-card {
            background: #1a1f2e;
            border: 1px solid #2d3748;
            border-radius: 8px;
            padding: 24px;
            margin-bottom: 16px;
        }
        .guide-card h3 {
            color: #e2e8f0;
            font-size: 1rem;
            font-weight: 600;
            margin-bottom: 14px;
            padding-bottom: 8px;
            border-bottom: 1px solid #2d3748;
        }
        .guide-card p, .guide-card li {
            color: #a0aec0;
            font-size: 0.86rem;
            line-height: 1.7;
        }
        .guide-card strong { color: #e2e8f0; }
        .guide-card code {
            background: #0e1117;
            color: #4a90d9;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 0.8rem;
        }
        .step-row {
            display: flex;
            align-items: flex-start;
            gap: 14px;
            margin-bottom: 16px;
        }
        .step-num {
            flex-shrink: 0;
            width: 28px;
            height: 28px;
            background: #4a90d9;
            color: #0e1117;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            font-size: 0.8rem;
            margin-top: 2px;
        }
        .step-content { flex: 1; }
        .step-content strong { color: #e2e8f0; }
        .step-content p {
            color: #a0aec0;
            font-size: 0.86rem;
            line-height: 1.6;
            margin: 0;
        }
        .scenario-box {
            background: linear-gradient(135deg, #1a1f2e 0%, #16192b 100%);
            border-radius: 8px;
            padding: 24px;
            margin-bottom: 16px;
        }
        .scenario-box h4 {
            font-size: 0.95rem;
            font-weight: 600;
            margin-bottom: 16px;
        }
        .scenario-blue h4 { color: #4a90d9; border-left: 3px solid #4a90d9; padding-left: 12px; }
        .scenario-green h4 { color: #48bb78; border-left: 3px solid #48bb78; padding-left: 12px; }
        .scenario-box table {
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 14px;
        }
        .scenario-box th {
            text-align: left;
            color: #8b95a5;
            font-size: 0.72rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            padding: 6px 10px;
            border-bottom: 1px solid #2d3748;
        }
        .scenario-box td {
            color: #e2e8f0;
            font-size: 0.84rem;
            padding: 6px 10px;
            border-bottom: 1px solid rgba(45,55,72,0.5);
        }
        .scenario-box .note {
            color: #8b95a5;
            font-size: 0.78rem;
            font-style: italic;
            line-height: 1.6;
            margin-top: 8px;
        }
        .nav-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 12px;
            margin-top: 12px;
        }
        @media (max-width: 768px) {
            .nav-grid { grid-template-columns: 1fr; }
        }
        .nav-item {
            background: #0e1117;
            border: 1px solid #2d3748;
            border-radius: 6px;
            padding: 14px 16px;
        }
        .nav-item strong {
            color: #e2e8f0;
            font-size: 0.84rem;
            display: block;
            margin-bottom: 4px;
        }
        .nav-item span {
            color: #6b7280;
            font-size: 0.76rem;
            line-height: 1.5;
        }
    </style>
    """, unsafe_allow_html=True)

    st.markdown('<div class="section-header">Getting Started</div>', unsafe_allow_html=True)

    st.markdown("""
    <div class="guide-card">
        <h3>How to Navigate the Calculator</h3>
        <div class="step-row">
            <div class="step-num">1</div>
            <div class="step-content">
                <p><strong>Sidebar (left panel)</strong> is where all inputs live. Start with
                <strong>Target Firm</strong> at the top &mdash; enter the AUM, revenue, EBITDA,
                and owner comp of the RIA you&rsquo;re evaluating. Scroll down through
                Deal Terms, Financing, and other sections.</p>
            </div>
        </div>
        <div class="step-row">
            <div class="step-num">2</div>
            <div class="step-content">
                <p><strong>Deal Terms</strong> controls structure. You can either enter
                a purchase price directly or pick a revenue multiple and let the calculator
                derive the price. Then allocate the consideration: upfront cash, seller note,
                earnout, and equity rollover should add up to 100%.</p>
            </div>
        </div>
        <div class="step-row">
            <div class="step-num">3</div>
            <div class="step-content">
                <p><strong>Everything updates live.</strong> As you change any input in the sidebar,
                the five analysis tabs on the right recalculate instantly. There&rsquo;s no
                submit button &mdash; just adjust the inputs and watch the outputs change.</p>
            </div>
        </div>
        <div class="step-row">
            <div class="step-num">4</div>
            <div class="step-content">
                <p><strong>Work through the tabs left to right.</strong> Start with
                Deal Summary for headline numbers, then dig into Pro Forma for the P&amp;L,
                Sensitivity for stress testing, Earnout for seller economics,
                and Debt for amortization and DSCR.</p>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div class="guide-card">
        <h3>What Each Tab Shows You</h3>
        <div class="nav-grid">
            <div class="nav-item">
                <strong>Deal Summary</strong>
                <span>Purchase price multiples (revenue, AUM, EBOC), deal structure
                waterfall chart, IRR, cash-on-cash, breakeven year, DSCR</span>
            </div>
            <div class="nav-item">
                <strong>Pro Forma Financials</strong>
                <span>5-year P&amp;L table with revenue, expenses, debt service, net cash flow.
                Combined entity view if buyer profile is enabled.</span>
            </div>
            <div class="nav-item">
                <strong>Sensitivity Analysis</strong>
                <span>Two heatmaps &mdash; IRR by multiple vs. attrition, and breakeven
                by growth vs. multiple. Use these to find where the deal breaks.</span>
            </div>
            <div class="nav-item">
                <strong>Earnout &amp; Seller Economics</strong>
                <span>Earnout payouts under 3 scenarios, cumulative seller proceeds timeline,
                and seller note amortization schedule.</span>
            </div>
            <div class="nav-item">
                <strong>Debt Analysis</strong>
                <span>Full loan amortization, principal vs. interest breakdown, and DSCR
                by year with 1.25x lender threshold marked.</span>
            </div>
            <div class="nav-item">
                <strong>PDF Export (bottom of page)</strong>
                <span>Scroll past the tabs to the footer. Click &ldquo;Export Full Analysis
                to PDF&rdquo; to download a complete report.</span>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # -- Scenario walkthroughs ---
    st.markdown('<div class="section-header">Example Scenarios</div>', unsafe_allow_html=True)

    st.markdown("""
    <div class="scenario-box scenario-blue">
        <h4>Scenario A &mdash; Large Platform Acquiring a $1B RIA</h4>
        <table>
            <tr><th>Input</th><th>Value</th><th>Rationale</th></tr>
            <tr><td>Target AUM</td><td>$1,000,000,000</td><td>Mid-size RIA with institutional book</td></tr>
            <tr><td>Revenue</td><td>$7,500,000</td><td>~75 bps on AUM (fee-based)</td></tr>
            <tr><td>EBITDA</td><td>$3,000,000</td><td>40% margin, well-run practice</td></tr>
            <tr><td>Owner Comp</td><td>$750,000</td><td>Founder takes above-market salary</td></tr>
            <tr><td>Clients</td><td>350</td><td>HNW-focused, avg $2.9M per household</td></tr>
            <tr><td>Growth Rate</td><td>6%</td><td>Organic + market appreciation</td></tr>
            <tr><td>Attrition</td><td>4%</td><td>Low &mdash; sticky client base</td></tr>
            <tr><td>Purchase Price</td><td>$18,000,000</td><td>~2.4x revenue, in-line for quality book</td></tr>
            <tr><td>Structure</td><td>60 / 20 / 15 / 5</td><td>Cash / Note / Earnout / Rollover</td></tr>
            <tr><td>Financing</td><td>50% self-funded</td><td>$5.4M loan at 6.5% over 7 years</td></tr>
            <tr><td>Integration</td><td>$200K one-time</td><td>Tech migration, compliance transfer</td></tr>
        </table>
        <p class="note">
            <strong>What to look for:</strong> Check Deal Summary for the implied EBOC multiple
            (should be under 5x for a quality deal). Go to Sensitivity Analysis and see how
            IRR holds up if attrition spikes to 8-10% in the first two years &mdash; this is the
            key risk in platform acquisitions where the founder is departing. The Earnout tab
            shows what the seller actually receives under different retention outcomes.
        </p>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div class="scenario-box scenario-green">
        <h4>Scenario B &mdash; $10B Wealth Platform Acquiring a $500M RIA</h4>
        <table>
            <tr><th>Input</th><th>Value</th><th>Rationale</th></tr>
            <tr><td>Target AUM</td><td>$500,000,000</td><td>Smaller practice, lifestyle RIA</td></tr>
            <tr><td>Revenue</td><td>$4,000,000</td><td>~80 bps blended fee schedule</td></tr>
            <tr><td>EBITDA</td><td>$1,400,000</td><td>35% margin, lean team</td></tr>
            <tr><td>Owner Comp</td><td>$500,000</td><td>Close to market rate already</td></tr>
            <tr><td>Clients</td><td>150</td><td>Avg $3.3M per client, concentrated book</td></tr>
            <tr><td>Growth Rate</td><td>4%</td><td>Modest &mdash; mature practice</td></tr>
            <tr><td>Attrition</td><td>7%</td><td>Higher risk &mdash; personal relationship-driven</td></tr>
            <tr><td>Purchase Price</td><td>$8,000,000</td><td>2.0x revenue (standard tuck-in)</td></tr>
            <tr><td>Structure</td><td>50 / 25 / 20 / 5</td><td>More deferred &mdash; heavier note + earnout</td></tr>
            <tr><td>Financing</td><td>40% self-funded</td><td>$2.4M loan at 7% over 5 years</td></tr>
            <tr><td>Seller Note</td><td>5% rate, 5-yr term</td><td>1-year standstill with interest-only</td></tr>
            <tr><td>Earnout</td><td>Revenue retention, 0% floor, 125% cap</td><td>3-year earnout, annual vesting</td></tr>
            <tr><td>Consulting</td><td>$150K/yr for 2 years</td><td>Transition advisory</td></tr>
            <tr><td>Non-Compete</td><td>$100K over 3 years</td><td>Standard geographic restriction</td></tr>
        </table>
        <p class="note">
            <strong>What to look for:</strong> This is a riskier deal with higher attrition and
            a concentrated book. In the Pro Forma tab, watch Year 1-2 net cash flow &mdash;
            it may go negative with consulting costs and integration drag. The Debt tab
            will show whether DSCR stays above 1.25x. Check if the seller note standstill
            helps early-year cash flow. In Sensitivity, stress test the 7% attrition up
            to 12% &mdash; with only 150 clients, losing a few large households can materially
            change the outcome. Enable the combined entity view in Buyer Profile to see the
            post-acquisition $10.5B platform picture.
        </p>
    </div>
    """, unsafe_allow_html=True)

    # Key assumptions reference
    st.markdown('<div class="section-header">Key Assumptions</div>', unsafe_allow_html=True)

    st.markdown("""
    <div class="guide-card">
        <h3>How the Model Works</h3>
        <div class="nav-grid">
            <div class="nav-item">
                <strong>EBOC = EBITDA + (Owner Comp &minus; $200K)</strong>
                <span>Normalizes for the seller&rsquo;s excess comp above market replacement.
                Used as the primary earnings metric for pricing.</span>
            </div>
            <div class="nav-item">
                <strong>IRR uses 6x EBITDA terminal value</strong>
                <span>Computed at year 3, 5, and 7 horizons. Assumes a future sale
                at a 6x multiple of that year&rsquo;s EBITDA.</span>
            </div>
            <div class="nav-item">
                <strong>Client attrition in years 1&ndash;2 only</strong>
                <span>Post-acquisition churn typically peaks in the first two years,
                then stabilizes as clients settle with the new team.</span>
            </div>
            <div class="nav-item">
                <strong>DSCR = EBITDA / Total Debt Service</strong>
                <span>Lenders typically require 1.25x minimum. The calculator
                tracks this each year against both loan and note payments.</span>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)


def currency_input(label, default, key, help_text=None):
    """Text input that displays and accepts comma-formatted dollar amounts."""
    formatted_default = f"{default:,.0f}"
    raw = st.sidebar.text_input(label, value=formatted_default, key=key, help=help_text)
    try:
        cleaned = raw.replace(",", "").replace("$", "").replace(" ", "").strip()
        val = int(float(cleaned))
        # Show formatted feedback if user typed raw digits
        if raw != f"{val:,.0f}" and raw == cleaned:
            st.sidebar.caption(f"= ${val:,.0f}")
        return val
    except (ValueError, TypeError):
        st.sidebar.error(f"Invalid number: {raw}")
        return default


# ==============================================================================
# WELCOME PAGE GATE
# ==============================================================================
if not st.session_state.show_calculator:
    render_welcome_page()
    st.stop()

# ==============================================================================
# SIDEBAR -- Inputs
# ==============================================================================
st.sidebar.markdown("## RIA M&A Calculator")
st.sidebar.markdown("---")

# -- Target Firm ---------------------------------------------------------------
st.sidebar.markdown("### Target Firm")
aum = currency_input("AUM ($)", 500_000_000, "aum")
annual_revenue = currency_input("Annual Revenue ($)", 4_000_000, "revenue")
ebitda = currency_input("EBITDA ($)", 1_600_000, "ebitda")
owner_comp = currency_input("Owner's Compensation ($)", 500_000, "owner_comp")
num_clients = st.sidebar.number_input("Number of Clients", value=200, step=10)
rev_growth = st.sidebar.slider("Revenue Growth Rate (%)", 0.0, 15.0, 5.0, 0.5) / 100
pct_recurring = st.sidebar.slider("% Recurring Revenue", 50, 100, 90, 5)
attrition_rate = st.sidebar.slider("Client Attrition Rate (%)", 0.0, 20.0, 5.0, 0.5) / 100

st.sidebar.markdown("---")

# -- Deal Terms ----------------------------------------------------------------
st.sidebar.markdown("### Deal Terms")
price_method = st.sidebar.radio("Purchase Price Method", ["Enter Price", "Select Multiple"])
if price_method == "Enter Price":
    purchase_price = currency_input("Purchase Price ($)", 8_000_000, "purchase_price")
else:
    rev_multiple = st.sidebar.slider("Revenue Multiple", 1.0, 5.0, 2.0, 0.1)
    purchase_price = int(annual_revenue * rev_multiple)
    st.sidebar.info(f"Implied Price: {fmt_dollar(purchase_price)}")

pct_upfront_cash = st.sidebar.slider("% Upfront Cash", 0, 100, 60, 5) / 100
pct_seller_note = st.sidebar.slider("% Seller Note", 0, 100, 20, 5) / 100
pct_earnout = st.sidebar.slider("% Earnout", 0, 100, 15, 5) / 100
pct_equity_rollover = st.sidebar.slider("% Equity Rollover", 0, 100, 5, 5) / 100

deal_total = pct_upfront_cash + pct_seller_note + pct_earnout + pct_equity_rollover
if abs(deal_total - 1.0) > 0.01:
    st.sidebar.warning(f"Deal structure sums to {deal_total*100:.0f}% (should be 100%)")

st.sidebar.markdown("---")

# -- Seller Note Terms ---------------------------------------------------------
st.sidebar.markdown("### Seller Note Terms")
note_rate = st.sidebar.slider("Interest Rate (%)", 0.0, 10.0, 5.0, 0.25) / 100
note_term = st.sidebar.number_input("Amortization Term (years)", value=5, min_value=1, max_value=10, key="note_term")
note_standstill = st.sidebar.number_input(
    "Standstill Period (years)", value=0, min_value=0, max_value=5, key="note_standstill",
    help="Deferred start -- no principal payments during this period",
)
note_io_standstill = False
if note_standstill > 0:
    note_io_standstill = st.sidebar.checkbox(
        "Pay interest during standstill",
        value=True, key="note_io",
        help="If unchecked, interest accrues (PIK) and is added to the balance",
    )

st.sidebar.markdown("---")

# -- Earnout Terms -------------------------------------------------------------
st.sidebar.markdown("### Earnout Terms")
earnout_period = st.sidebar.number_input("Earnout Period (years)", value=3, min_value=1, max_value=5)
earnout_metric = st.sidebar.selectbox(
    "Performance Metric",
    ["Revenue Retention", "AUM Retention", "Client Retention"],
    help="The KPI used to measure earnout achievement each year",
)
earnout_floor = st.sidebar.slider(
    "Earnout Floor (%)", 0, 100, 0, 5,
    help="Minimum payout % per year regardless of performance",
)
earnout_cap = st.sidebar.slider(
    "Earnout Cap (%)", 100, 150, 125, 5,
    help="Maximum payout % per year (allows upside above target)",
)
earnout_cliff = st.sidebar.checkbox(
    "Cliff Vesting",
    value=False,
    help="If checked, entire earnout pays out at end of period based on avg performance (vs. annual payouts)",
)

st.sidebar.markdown("---")

# -- Financing -----------------------------------------------------------------
st.sidebar.markdown("### Financing")
pct_self_funded = st.sidebar.slider("% Self-Funded (of upfront cash)", 0, 100, 50, 5) / 100
loan_rate = st.sidebar.slider("Loan Interest Rate (%)", 0.0, 12.0, 6.5, 0.25) / 100
loan_term = st.sidebar.number_input("Loan Term (years)", value=7, min_value=1, max_value=15, key="loan_term")

with st.sidebar.expander("Advanced Loan Options"):
    loan_io_years = st.number_input(
        "Interest-Only Period (years)", value=0, min_value=0, max_value=5, key="loan_io",
        help="Pay only interest for this many years before amortization begins",
    )
    loan_balloon = st.checkbox(
        "Balloon Payment at Maturity",
        value=False, key="loan_balloon",
        help="Amortize over a longer schedule with remaining balance due at term end",
    )
    loan_amort_years = loan_term
    if loan_balloon:
        loan_amort_years = st.number_input(
            "Amortization Schedule (years)", value=15, min_value=loan_term + 1, max_value=30, key="loan_amort_yrs",
            help="Payments calculated on this schedule; remaining balance due at loan term",
        )

st.sidebar.markdown("---")

# -- Transition Compensation ---------------------------------------------------
st.sidebar.markdown("### Seller Transition Terms")
consulting_annual = currency_input(
    "Annual Consulting Fee ($)", 150_000, "consulting_fee",
    help_text="Post-close consulting/advisory agreement with seller",
)
consulting_years = st.sidebar.number_input(
    "Consulting Duration (years)", value=2, min_value=0, max_value=5, key="consult_yrs",
)
noncompete_total = currency_input(
    "Non-Compete Payment Total ($)", 100_000, "noncompete",
    help_text="Total non-compete consideration paid to seller",
)
noncompete_years = st.sidebar.number_input(
    "Non-Compete Period (years)", value=3, min_value=0, max_value=7, key="nc_yrs",
)

st.sidebar.markdown("---")

# -- Integration ---------------------------------------------------------------
st.sidebar.markdown("### Integration Assumptions")
integration_costs = currency_input("One-Time Integration Costs ($)", 150_000, "integration")
annual_synergies = currency_input("Expected Annual Cost Synergies ($)", 100_000, "synergies")
additional_staff = st.sidebar.number_input("Additional Staff Needed", value=1, min_value=0, max_value=10)
integration_months = st.sidebar.number_input("Integration Timeline (months)", value=12, min_value=3, max_value=36)

st.sidebar.markdown("---")

# -- Tax -----------------------------------------------------------------------
st.sidebar.markdown("### Tax Impact")
tax_rate = st.sidebar.slider(
    "Buyer's Marginal Tax Rate (%)", 0, 50, 0, 1,
    help="Applied to interest deductions for after-tax cash flow view",
) / 100

st.sidebar.markdown("---")

# -- Buyer Profile -------------------------------------------------------------
st.sidebar.markdown("### Buyer Profile (Optional)")
show_combined = st.sidebar.checkbox("Show Combined Entity View", value=False)
buyer_aum = currency_input("Buyer's AUM ($)", 1_000_000_000, "buyer_aum") if show_combined else 0
buyer_revenue = currency_input("Buyer's Revenue ($)", 8_000_000, "buyer_rev") if show_combined else 0
buyer_margin = st.sidebar.slider("Buyer's EBITDA Margin (%)", 0, 60, 35, 5) / 100 if show_combined else 0


# ==============================================================================
# CALCULATIONS
# ==============================================================================
eboc = compute_eboc(ebitda, owner_comp)
multiples = implied_multiples(purchase_price, annual_revenue, aum, eboc)

pro_forma = build_pro_forma(
    revenue=annual_revenue, ebitda=ebitda, owner_comp=owner_comp,
    growth_rate=rev_growth, attrition_rate=attrition_rate, aum=aum,
    purchase_price=purchase_price, pct_upfront=pct_upfront_cash,
    pct_seller_note=pct_seller_note, note_rate=note_rate, note_term=note_term,
    pct_earnout=pct_earnout, earnout_period=earnout_period,
    pct_equity_rollover=pct_equity_rollover, pct_self_funded=pct_self_funded,
    loan_rate=loan_rate, loan_term=loan_term,
    integration_costs=integration_costs, annual_synergies=annual_synergies,
    additional_staff=additional_staff, years=7,
    loan_io_years=loan_io_years, loan_balloon=loan_balloon,
    loan_amort_years=loan_amort_years,
    note_standstill_years=note_standstill,
    note_io_during_standstill=note_io_standstill,
    consulting_annual=consulting_annual, consulting_years=consulting_years,
    noncompete_total=noncompete_total, noncompete_years=noncompete_years,
    tax_rate=tax_rate,
)

returns = compute_irr_and_returns(
    pro_forma, purchase_price, pct_upfront_cash, pct_self_funded, pct_equity_rollover,
)

dscr = compute_dscr(pro_forma)
pro_forma["dscr"] = dscr

debt_amount = purchase_price * pct_upfront_cash * (1 - pct_self_funded)
loan_amort = build_loan_amortization(
    debt_amount, loan_rate, loan_term,
    io_years=loan_io_years, balloon=loan_balloon,
    amort_years=loan_amort_years,
)

seller_note_principal = purchase_price * pct_seller_note
note_amort = build_seller_note_amortization(
    seller_note_principal, note_rate, note_term,
    standstill_years=note_standstill,
    io_during_standstill=note_io_standstill,
)

earnout_scenarios = compute_earnout_scenarios(
    purchase_price, pct_earnout, earnout_period, annual_revenue,
    aum, num_clients, rev_growth, attrition_rate,
    earnout_metric=earnout_metric,
    earnout_floor_pct=earnout_floor,
    earnout_cap_pct=earnout_cap,
    earnout_cliff=earnout_cliff,
)

seller_proceeds = compute_seller_total_proceeds(
    purchase_price, pct_upfront_cash, pct_seller_note, note_rate, note_term,
    earnout_scenarios,
    note_standstill_years=note_standstill,
    note_io_during_standstill=note_io_standstill,
    consulting_annual=consulting_annual, consulting_years=consulting_years,
    noncompete_total=noncompete_total, noncompete_years=noncompete_years,
)

# ==============================================================================
# MAIN PANEL
# ==============================================================================
_hdr_left, _hdr_right = st.columns([5, 1])
with _hdr_left:
    st.markdown("# RIA M&A Calculator")
    st.markdown("*Buyer-side acquisition economics for Registered Investment Advisors*")
with _hdr_right:
    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("Home", key="back_home", use_container_width=True):
        st.session_state.show_calculator = False
        st.rerun()

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "Deal Summary", "Pro Forma Financials", "Sensitivity Analysis",
    "Earnout & Seller Economics", "Debt Analysis", "Instructions",
])

# ==============================================================================
# TAB 1 -- Deal Summary
# ==============================================================================
with tab1:
    st.markdown('<div class="section-header">Purchase Price & Implied Multiples</div>', unsafe_allow_html=True)

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.markdown(metric_card("Price", fmt_dollar(purchase_price), "neutral"), unsafe_allow_html=True)
    with c2:
        st.markdown(metric_card("Rev Multiple", f"{multiples['revenue_multiple']:.2f}x", "neutral"), unsafe_allow_html=True)
    with c3:
        st.markdown(metric_card("% of AUM", f"{multiples['aum_multiple']:.2f}%", "neutral"), unsafe_allow_html=True)
    with c4:
        st.markdown(metric_card("EBOC Multiple", f"{multiples['eboc_multiple']:.2f}x", "neutral"), unsafe_allow_html=True)
    with c5:
        price_per_client = purchase_price / num_clients if num_clients else 0
        st.markdown(metric_card("Price / Client", fmt_dollar(price_per_client), "neutral"), unsafe_allow_html=True)

    st.markdown('<div class="section-header">EBOC Calculation</div>', unsafe_allow_html=True)
    ec1, ec2, ec3, ec4 = st.columns(4)
    with ec1:
        st.markdown(metric_card("EBITDA", fmt_dollar(ebitda), "neutral"), unsafe_allow_html=True)
    with ec2:
        st.markdown(metric_card("Owner Comp Adj.", fmt_dollar(owner_comp - 200_000), "neutral"), unsafe_allow_html=True)
    with ec3:
        st.markdown(metric_card("EBOC", fmt_dollar(eboc), "positive"), unsafe_allow_html=True)
    with ec4:
        margin = ebitda / annual_revenue if annual_revenue else 0
        st.markdown(metric_card("EBITDA Margin", fmt_pct(margin), "positive" if margin > 0.3 else "neutral"), unsafe_allow_html=True)

    st.markdown('<div class="section-header">Deal Structure</div>', unsafe_allow_html=True)

    components = ["Upfront Cash", "Seller Note", "Earnout", "Equity Rollover"]
    values = [
        purchase_price * pct_upfront_cash,
        purchase_price * pct_seller_note,
        purchase_price * pct_earnout,
        purchase_price * pct_equity_rollover,
    ]

    fig_waterfall = go.Figure(go.Waterfall(
        name="Deal Structure",
        orientation="v",
        x=components + ["Total"],
        y=values + [0],
        measure=["relative"] * len(components) + ["total"],
        connector=dict(line=dict(color="#2d3748")),
        increasing=dict(marker=dict(color=COLORS[0])),
        totals=dict(marker=dict(color=COLORS[1])),
        text=[fmt_dollar(v) for v in values] + [fmt_dollar(purchase_price)],
        textposition="outside",
    ))
    fig_waterfall.update_layout(**PLOTLY_LAYOUT, title="Deal Structure Waterfall", height=400)
    st.plotly_chart(fig_waterfall, use_container_width=True)

    st.markdown('<div class="section-header">Key Return Metrics</div>', unsafe_allow_html=True)
    k1, k2, k3, k4 = st.columns(4)

    irr5 = returns.get("irr_yr5", 0)
    irr_class = "positive" if irr5 > 0.15 else ("neutral" if irr5 > 0 else "negative")
    with k1:
        st.markdown(metric_card("5-Year IRR", fmt_pct(irr5), irr_class), unsafe_allow_html=True)
    with k2:
        coc5 = returns.get("coc_yr5", 0)
        st.markdown(metric_card("5-Yr Cash/Cash", f"{coc5:.2f}x", "positive" if coc5 > 1 else "negative"), unsafe_allow_html=True)
    with k3:
        st.markdown(metric_card("Breakeven Year", str(returns["breakeven_year"]), "neutral"), unsafe_allow_html=True)
    with k4:
        yr1_dscr = dscr.iloc[0] if len(dscr) > 0 else 0
        st.markdown(metric_card("Year 1 DSCR", f"{yr1_dscr:.2f}x", "positive" if yr1_dscr > 1.25 else "negative"), unsafe_allow_html=True)

    # Transition comp summary if applicable
    if consulting_annual > 0 or noncompete_total > 0:
        st.markdown('<div class="section-header">Seller Transition Costs (Buyer Expense)</div>', unsafe_allow_html=True)
        t1, t2, t3 = st.columns(3)
        total_consulting = consulting_annual * consulting_years
        with t1:
            st.markdown(metric_card("Consulting Total", fmt_dollar(total_consulting), "neutral"), unsafe_allow_html=True)
        with t2:
            st.markdown(metric_card("Non-Compete Total", fmt_dollar(noncompete_total), "neutral"), unsafe_allow_html=True)
        with t3:
            st.markdown(metric_card("Total Transition", fmt_dollar(total_consulting + noncompete_total), "negative"), unsafe_allow_html=True)


# ==============================================================================
# TAB 2 -- Pro Forma Financials
# ==============================================================================
with tab2:
    st.markdown('<div class="section-header">5-Year Pro Forma P&L</div>', unsafe_allow_html=True)

    display_pf = pro_forma[pro_forma["year"] <= 5].copy()
    pf_cols = ["year", "revenue", "expenses", "transition_comp", "integration_costs",
               "ebitda", "debt_service", "seller_note_payment"]

    # Conditionally show tax benefit column
    if tax_rate > 0:
        pf_cols += ["tax_benefit", "net_cash_flow"]
        col_names = ["Year", "Revenue", "Expenses", "Transition", "Integration",
                     "EBITDA", "Debt Svc", "Note Pmt", "Tax Benefit", "Net Cash Flow"]
    else:
        pf_cols += ["net_cash_flow"]
        col_names = ["Year", "Revenue", "Expenses", "Transition", "Integration",
                     "EBITDA", "Debt Svc", "Note Pmt", "Net Cash Flow"]

    display_pf_fmt = display_pf[pf_cols].copy()
    for col in display_pf_fmt.columns:
        if col != "year":
            display_pf_fmt[col] = display_pf_fmt[col].apply(lambda x: f"${x:,.0f}")
    display_pf_fmt.columns = col_names
    st.dataframe(display_pf_fmt, use_container_width=True, hide_index=True)

    # Combined entity view
    if show_combined and buyer_revenue > 0:
        st.markdown('<div class="section-header">Combined Entity Pro Forma</div>', unsafe_allow_html=True)
        combined = display_pf[["year", "revenue", "ebitda"]].copy()
        combined["buyer_revenue"] = buyer_revenue * ((1 + 0.03) ** combined["year"])
        combined["buyer_ebitda"] = combined["buyer_revenue"] * buyer_margin
        combined["combined_revenue"] = combined["revenue"] + combined["buyer_revenue"]
        combined["combined_ebitda"] = combined["ebitda"] + combined["buyer_ebitda"]
        combined["combined_aum"] = [
            aum * ((1 + rev_growth - (attrition_rate if yr <= 2 else 0)) ** yr)
            + buyer_aum * ((1 + 0.03) ** yr)
            for yr in combined["year"]
        ]

        comb_fmt = combined[["year", "combined_revenue", "combined_ebitda", "combined_aum"]].copy()
        for col in comb_fmt.columns:
            if col != "year":
                comb_fmt[col] = comb_fmt[col].apply(lambda x: f"${x:,.0f}")
        comb_fmt.columns = ["Year", "Combined Revenue", "Combined EBITDA", "Combined AUM"]
        st.dataframe(comb_fmt, use_container_width=True, hide_index=True)

    # Revenue and cash flow chart
    st.markdown('<div class="section-header">Revenue & Cash Flow Trajectory</div>', unsafe_allow_html=True)
    fig_pf = go.Figure()
    pf5 = pro_forma[pro_forma["year"] <= 5]
    fig_pf.add_trace(go.Scatter(
        x=pf5["year"], y=pf5["revenue"], name="Revenue",
        line=dict(color=COLORS[0], width=3), mode="lines+markers",
    ))
    fig_pf.add_trace(go.Scatter(
        x=pf5["year"], y=pf5["net_cash_flow"], name="Net Cash Flow",
        line=dict(color=COLORS[1], width=3), mode="lines+markers",
    ))
    fig_pf.add_trace(go.Scatter(
        x=pf5["year"], y=pf5["ebitda"], name="EBITDA",
        line=dict(color=COLORS[2], width=2, dash="dash"), mode="lines+markers",
    ))
    if tax_rate > 0:
        fig_pf.add_trace(go.Scatter(
            x=pf5["year"], y=pf5["pretax_cash_flow"], name="Pre-Tax Cash Flow",
            line=dict(color=COLORS[4], width=2, dash="dot"), mode="lines+markers",
        ))
    fig_pf.update_layout(
        **PLOTLY_LAYOUT,
        title="5-Year Financial Trajectory",
        yaxis_title="Dollars ($)",
        xaxis_title="Year",
        height=450,
    )
    st.plotly_chart(fig_pf, use_container_width=True)


# ==============================================================================
# TAB 3 -- Sensitivity Analysis
# ==============================================================================
with tab3:
    base_params = dict(
        revenue=annual_revenue, ebitda=ebitda, owner_comp=owner_comp,
        growth_rate=rev_growth, attrition_rate=attrition_rate, aum=aum,
        purchase_price=purchase_price, pct_upfront=pct_upfront_cash,
        pct_seller_note=pct_seller_note, note_rate=note_rate, note_term=note_term,
        pct_earnout=pct_earnout, earnout_period=earnout_period,
        pct_equity_rollover=pct_equity_rollover, pct_self_funded=pct_self_funded,
        loan_rate=loan_rate, loan_term=loan_term,
        integration_costs=integration_costs, annual_synergies=annual_synergies,
        additional_staff=additional_staff, years=7,
        loan_io_years=loan_io_years, loan_balloon=loan_balloon,
        loan_amort_years=loan_amort_years,
        note_standstill_years=note_standstill,
        note_io_during_standstill=note_io_standstill,
        consulting_annual=consulting_annual, consulting_years=consulting_years,
        noncompete_total=noncompete_total, noncompete_years=noncompete_years,
        tax_rate=tax_rate,
    )

    st.markdown('<div class="section-header">IRR Sensitivity: Revenue Multiple vs. Client Attrition</div>', unsafe_allow_html=True)

    multiples_range = [round(x, 1) for x in np.arange(1.0, 4.1, 0.5)]
    attrition_range = [round(x, 2) for x in np.arange(0.0, 0.16, 0.03)]

    irr_table = sensitivity_irr(base_params, multiples_range, attrition_range)

    fig_hm1 = go.Figure(go.Heatmap(
        z=irr_table.values * 100,
        x=[f"{a*100:.0f}%" for a in irr_table.columns],
        y=[f"{m:.1f}x" for m in irr_table.index],
        colorscale=[[0, "#fc8181"], [0.5, "#fefcbf"], [1, "#48bb78"]],
        text=[[f"{v*100:.1f}%" for v in row] for row in irr_table.values],
        texttemplate="%{text}",
        textfont=dict(size=11),
        colorbar=dict(title="IRR %"),
    ))
    fig_hm1.update_layout(
        **PLOTLY_LAYOUT,
        title="5-Year IRR by Multiple & Attrition Rate",
        xaxis_title="Client Attrition Rate",
        yaxis_title="Revenue Multiple",
        height=400,
    )
    st.plotly_chart(fig_hm1, use_container_width=True)

    st.markdown('<div class="section-header">Breakeven Sensitivity: Growth Rate vs. Revenue Multiple</div>', unsafe_allow_html=True)

    growth_range = [round(x, 2) for x in np.arange(0.0, 0.11, 0.02)]
    multiples_range2 = [round(x, 1) for x in np.arange(1.0, 4.1, 0.5)]

    be_table = sensitivity_breakeven(base_params, growth_range, multiples_range2)

    fig_hm2 = go.Figure(go.Heatmap(
        z=be_table.values,
        x=[f"{m:.1f}x" for m in be_table.columns],
        y=[f"{g*100:.0f}%" for g in be_table.index],
        colorscale=[[0, "#48bb78"], [0.5, "#fefcbf"], [1, "#fc8181"]],
        text=[[f"Yr {int(v)}" if v < 8 else ">7" for v in row] for row in be_table.values],
        texttemplate="%{text}",
        textfont=dict(size=11),
        colorbar=dict(title="Years"),
    ))
    fig_hm2.update_layout(
        **PLOTLY_LAYOUT,
        title="Breakeven Year by Growth Rate & Multiple",
        xaxis_title="Revenue Multiple",
        yaxis_title="Revenue Growth Rate",
        height=400,
    )
    st.plotly_chart(fig_hm2, use_container_width=True)


# ==============================================================================
# TAB 4 -- Earnout & Seller Economics
# ==============================================================================
with tab4:
    total_earnout_amount = purchase_price * pct_earnout

    st.markdown('<div class="section-header">Earnout Payout Scenarios</div>', unsafe_allow_html=True)

    # Show earnout config
    cfg1, cfg2, cfg3, cfg4 = st.columns(4)
    with cfg1:
        st.markdown(metric_card("Metric", earnout_metric.split()[0], "neutral"), unsafe_allow_html=True)
    with cfg2:
        st.markdown(metric_card("Floor", f"{earnout_floor}%", "neutral"), unsafe_allow_html=True)
    with cfg3:
        st.markdown(metric_card("Cap", f"{earnout_cap}%", "neutral"), unsafe_allow_html=True)
    with cfg4:
        st.markdown(metric_card("Vesting", "Cliff" if earnout_cliff else "Annual", "neutral"), unsafe_allow_html=True)

    e1, e2, e3 = st.columns(3)
    for idx, (col, sc) in enumerate(zip([e1, e2, e3], earnout_scenarios)):
        with col:
            color = ["neutral", "positive", "negative"][idx]
            st.markdown(metric_card(
                sc["scenario"],
                f"{fmt_dollar(sc['total_payout'])} ({sc['pct_of_max']:.0f}%)",
                color,
            ), unsafe_allow_html=True)

    # Earnout bar chart
    fig_earnout = go.Figure()
    for idx, sc in enumerate(earnout_scenarios):
        fig_earnout.add_trace(go.Bar(
            x=list(range(1, len(sc["yearly_payouts"]) + 1)),
            y=sc["yearly_payouts"],
            name=sc["scenario"],
            marker_color=COLORS[idx],
        ))
    fig_earnout.update_layout(
        **PLOTLY_LAYOUT,
        title="Earnout Payouts by Year & Scenario",
        xaxis_title="Year", yaxis_title="Earnout Payment ($)",
        barmode="group", height=400,
    )
    st.plotly_chart(fig_earnout, use_container_width=True)

    # Seller total proceeds timeline
    st.markdown('<div class="section-header">Total Seller Proceeds Timeline</div>', unsafe_allow_html=True)
    st.caption("Includes: upfront cash + seller note payments + earnout + consulting fees + non-compete payments")

    fig_seller = go.Figure()
    for sc_name in seller_proceeds["scenario"].unique():
        sc_data = seller_proceeds[seller_proceeds["scenario"] == sc_name]
        fig_seller.add_trace(go.Scatter(
            x=sc_data["year"], y=sc_data["cumulative_proceeds"],
            name=sc_name, mode="lines+markers",
        ))
    fig_seller.add_hline(y=purchase_price, line_dash="dash", line_color="#fc8181",
                         annotation_text="Purchase Price")
    fig_seller.update_layout(
        **PLOTLY_LAYOUT,
        title="Cumulative Seller Proceeds (All Sources)",
        xaxis_title="Year", yaxis_title="Cumulative Proceeds ($)",
        height=400,
    )
    st.plotly_chart(fig_seller, use_container_width=True)

    # Seller note amortization
    st.markdown('<div class="section-header">Seller Note Amortization Schedule</div>', unsafe_allow_html=True)
    if len(note_amort) > 0:
        if note_standstill > 0:
            st.caption(f"Standstill period: {note_standstill} years ({'interest-only' if note_io_standstill else 'PIK accrual'})")
        note_fmt = note_amort.copy()
        for col in note_fmt.columns:
            if col not in ("year",):
                note_fmt[col] = note_fmt[col].apply(lambda x: f"${x:,.0f}")
        note_fmt.columns = ["Year", "Beginning Balance", "Payment", "Interest", "Principal", "Ending Balance", "Balloon"]
        st.dataframe(note_fmt, use_container_width=True, hide_index=True)
    else:
        st.info("No seller note in deal structure.")


# ==============================================================================
# TAB 5 -- Debt Analysis
# ==============================================================================
with tab5:
    st.markdown('<div class="section-header">Loan Amortization Schedule</div>', unsafe_allow_html=True)

    # Show loan config summary
    loan_features = []
    if loan_io_years > 0:
        loan_features.append(f"{loan_io_years}-year IO period")
    if loan_balloon:
        loan_features.append(f"Balloon at maturity (amortized over {loan_amort_years} yrs)")
    if loan_features:
        st.caption("Loan features: " + " | ".join(loan_features))

    if len(loan_amort) > 0:
        loan_fmt = loan_amort.copy()
        has_balloon = "balloon_payment" in loan_fmt.columns and loan_fmt["balloon_payment"].sum() > 0
        fmt_cols = [c for c in loan_fmt.columns if c != "year"]
        for col in fmt_cols:
            loan_fmt[col] = loan_fmt[col].apply(lambda x: f"${x:,.0f}")

        if has_balloon:
            loan_fmt.columns = ["Year", "Beg Balance", "Payment", "Interest", "Principal", "End Balance", "Balloon"]
        else:
            loan_fmt = loan_fmt.drop(columns=["balloon_payment"], errors="ignore")
            loan_fmt.columns = ["Year", "Beg Balance", "Payment", "Interest", "Principal", "End Balance"]
        st.dataframe(loan_fmt, use_container_width=True, hide_index=True)

        # Principal vs Interest chart
        st.markdown('<div class="section-header">Principal vs. Interest Over Time</div>', unsafe_allow_html=True)
        fig_debt = go.Figure()
        fig_debt.add_trace(go.Bar(
            x=loan_amort["year"], y=loan_amort["principal_paid"],
            name="Principal", marker_color=COLORS[0],
        ))
        fig_debt.add_trace(go.Bar(
            x=loan_amort["year"], y=loan_amort["interest"],
            name="Interest", marker_color=COLORS[3],
        ))
        if "balloon_payment" in loan_amort.columns and loan_amort["balloon_payment"].sum() > 0:
            fig_debt.add_trace(go.Bar(
                x=loan_amort["year"], y=loan_amort["balloon_payment"],
                name="Balloon", marker_color=COLORS[2],
            ))
        fig_debt.update_layout(
            **PLOTLY_LAYOUT,
            title="Loan Amortization: Principal vs Interest",
            xaxis_title="Year", yaxis_title="Dollars ($)",
            barmode="stack", height=400,
        )
        st.plotly_chart(fig_debt, use_container_width=True)
    else:
        st.info("No debt financing in deal structure (100% self-funded).")

    # DSCR by year
    st.markdown('<div class="section-header">Debt Service Coverage Ratio (DSCR)</div>', unsafe_allow_html=True)

    max_debt_yr = max(loan_term, note_term + note_standstill)
    dscr_data = pro_forma[["year", "dscr"]].copy()
    dscr_data = dscr_data[dscr_data["year"] <= max_debt_yr]

    if len(dscr_data) > 0 and dscr_data["dscr"].sum() > 0:
        fig_dscr = go.Figure()
        fig_dscr.add_trace(go.Bar(
            x=dscr_data["year"], y=dscr_data["dscr"],
            marker_color=[COLORS[1] if d >= 1.25 else COLORS[3] for d in dscr_data["dscr"]],
            text=[f"{d:.2f}x" for d in dscr_data["dscr"]],
            textposition="outside",
        ))
        fig_dscr.add_hline(y=1.25, line_dash="dash", line_color="#ed8936",
                           annotation_text="1.25x Target")
        fig_dscr.update_layout(
            **PLOTLY_LAYOUT,
            title="DSCR by Year",
            xaxis_title="Year", yaxis_title="DSCR",
            height=400, showlegend=False,
        )
        st.plotly_chart(fig_dscr, use_container_width=True)
    else:
        st.info("No debt service to analyze.")


# ==============================================================================
# TAB 6 -- Instructions
# ==============================================================================
with tab6:
    render_instructions_tab()


# ==============================================================================
# FOOTER -- PDF Export
# ==============================================================================
st.markdown("---")
st.markdown("### Export Analysis")

if st.button("Export Full Analysis to PDF", type="primary"):
    try:
        from pdf_export import generate_pdf
        pdf_bytes = generate_pdf(
            purchase_price=purchase_price,
            multiples=multiples,
            eboc=eboc,
            pro_forma=pro_forma,
            returns=returns,
            loan_amort=loan_amort,
            note_amort=note_amort,
            earnout_scenarios=earnout_scenarios,
            dscr=dscr,
            inputs=dict(
                aum=aum, revenue=annual_revenue, ebitda=ebitda,
                owner_comp=owner_comp, num_clients=num_clients,
                growth_rate=rev_growth, pct_recurring=pct_recurring,
                attrition_rate=attrition_rate, pct_upfront=pct_upfront_cash,
                pct_seller_note=pct_seller_note, pct_earnout=pct_earnout,
                pct_equity_rollover=pct_equity_rollover,
                pct_self_funded=pct_self_funded, loan_rate=loan_rate,
                loan_term=loan_term, integration_costs=integration_costs,
                consulting_annual=consulting_annual, consulting_years=consulting_years,
                noncompete_total=noncompete_total, noncompete_years=noncompete_years,
                tax_rate=tax_rate,
            ),
        )
        st.download_button(
            label="Download PDF",
            data=pdf_bytes,
            file_name="ria_ma_analysis.pdf",
            mime="application/pdf",
        )
    except ImportError:
        st.warning("PDF export requires the `fpdf2` package. Install with: pip install fpdf2")
    except Exception as e:
        st.error(f"PDF generation failed: {e}")

st.caption("RIA M&A Calculator | Built for buyer-side acquisition modeling")
