#!/usr/bin/env python3
"""
Daily Spanish electricity generation mix report.

Fetches today's generation-mix data so far (from 00:00 up to the moment
the script runs, ~12:00) from Red Eléctrica de España's public REData API
(apidatos.ree.es — no API key required), builds a pie chart of the
generation mix, and emails a summary (key figures + chart) to the
configured recipient.

Designed to be run once a day (around 12:00 Europe/Madrid time) from a
GitHub Actions workflow, but works fine run locally / manually too.
"""

import os
import sys
import smtplib
import logging
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import requests
import matplotlib
matplotlib.use("Agg")  # no display available in CI
import matplotlib.pyplot as plt

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ree_report")

MADRID_TZ = ZoneInfo("Europe/Madrid")
REE_API_URL = "https://apidatos.ree.es/en/datos/generacion/estructura-generacion"

# Fallback colour palette for technologies that don't have a colour
# provided by the API (shouldn't normally happen, but just in case).
FALLBACK_COLORS = [
    "#4C72B0", "#DD8452", "#55A868", "#C44E52", "#8172B2",
    "#937860", "#DA8BC3", "#8C8C8C", "#CCB974", "#64B5CD",
]

# REE's API mixes in aggregate/rollup rows (e.g. "Total generation") among
# the individual technologies in the "included" list. We identify and
# drop those by name rather than trusting any structural flag, since
# that's the most robust signal across API quirks.
AGGREGATE_ROW_KEYWORDS = ("total generation", "generación total", "generacion total")

# Renewable/non-renewable is determined by an explicit technology list
# rather than trusting the API's own category field — REE's classification
# field is returned in whatever language the endpoint is queried in (here,
# English), and matching against it proved unreliable. This list covers
# REE's standard "estructura-generacion" technologies in both English and
# Spanish, lower-cased, so it works regardless of endpoint language.
RENEWABLE_TECHNOLOGIES = {
    "hydro", "hidráulica", "hidraulica",
    "wind", "eólica", "eolica",
    "solar photovoltaic", "solar fotovoltaica",
    "solar thermal", "solar térmica", "solar termica",
    "other renewables", "otras renovables",
    "renewable waste", "residuos renovables",
    "hydroeolic", "hidroeólica", "hidroeolica",
}
NON_RENEWABLE_TECHNOLOGIES = {
    "nuclear",
    "coal", "carbón", "carbon",
    "fuel + gas", "fuel/gas", "fuel-gas",
    "diesel engines", "motores diésel", "motores diesel",
    "gas turbine", "turbina de gas",
    "steam turbine", "turbina de vapor",
    "combined cycle", "ciclo combinado",
    "cogeneration", "cogeneración", "cogeneracion",
    "non-renewable waste", "residuos no renovables",
    "pumped storage", "turbinación bombeo", "turbinacion bombeo",
}


def should_run_now(force: bool) -> bool:
    """
    GitHub Actions cron always runs in UTC, but we want this to fire at
    ~12:00 *local* Madrid time year-round, including across the CET/CEST
    daylight-saving switch. The workflow schedules TWO cron triggers
    (one for each UTC offset Madrid can have, ~60 minutes apart); this
    function checks the actual local time at run time and only proceeds
    if it's close to noon, skipping the "wrong" one of the two triggers
    on any given day.

    GitHub documents scheduled ("cron") triggers as best-effort: they can
    be delayed by tens of minutes (rarely more) during periods of high
    load, or in rare cases dropped entirely. To absorb typical delays
    without risking a double-send, we accept a window of +/- 30 minutes
    around noon — comfortably inside the ~60 minute gap between the two
    scheduled triggers, so only one of them will ever pass on a given day.
    """
    if force:
        log.info("FORCE_RUN set — skipping local-time check.")
        return True

    now_madrid = datetime.now(MADRID_TZ)
    minutes_from_noon = (now_madrid.hour * 60 + now_madrid.minute) - 12 * 60

    if abs(minutes_from_noon) <= 30:
        return True

    log.info(
        "Current Madrid local time is %s (%+d min from noon) — outside the accepted window, skipping this trigger.",
        now_madrid.strftime("%Y-%m-%d %H:%M %Z"),
        minutes_from_noon,
    )
    return False


def fetch_generation_mix(start_dt: datetime, end_dt: datetime) -> dict:
    """
    Fetch the generation structure between start_dt and end_dt (both
    Europe/Madrid-aware datetimes). Used to get "today so far" data:
    from midnight up to the moment the report runs (~12:00).
    """
    params = {
        "start_date": start_dt.strftime("%Y-%m-%dT%H:%M"),
        "end_date": end_dt.strftime("%Y-%m-%dT%H:%M"),
        "time_trunc": "day",
    }
    log.info("Requesting REE generation mix from %s to %s ...", params["start_date"], params["end_date"])
    resp = requests.get(REE_API_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def classify_technology(name: str) -> str:
    """Classify a technology name as 'renewable', 'non-renewable', or 'unknown'."""
    key = name.strip().lower()
    if key in RENEWABLE_TECHNOLOGIES:
        return "renewable"
    if key in NON_RENEWABLE_TECHNOLOGIES:
        return "non-renewable"
    log.warning("Unrecognized technology %r — not counted in renewable/non-renewable totals.", name)
    return "unknown"


def parse_generation_mix(payload: dict) -> list[dict]:
    """
    Turn the raw REE API response into a clean list of:
      {"name": str, "category": "renewable"|"non-renewable"|"unknown", "mwh": float, "pct": float, "color": str}
    sorted by MWh descending. Aggregate/rollup rows (e.g. "Total generation")
    that REE includes alongside the individual technologies are dropped.
    """
    items = []
    for entry in payload.get("included", []):
        attrs = entry.get("attributes", {})
        name = attrs.get("title") or entry.get("type") or "Unknown"

        if any(kw in name.strip().lower() for kw in AGGREGATE_ROW_KEYWORDS):
            log.info("Skipping aggregate row %r", name)
            continue

        color = attrs.get("color") or None
        total = attrs.get("total")
        if total is None:
            # Fall back to summing the individual time-bucket values
            total = sum(v.get("value", 0) or 0 for v in attrs.get("values", []))

        items.append({
            "name": name,
            "category": classify_technology(name),
            "mwh": float(total or 0),
            "pct": None,  # computed below, after aggregate rows are excluded
            "color": color,
        })

    items.sort(key=lambda x: x["mwh"], reverse=True)

    total_mwh = sum(i["mwh"] for i in items) or 1  # avoid div-by-zero
    for i in items:
        i["pct"] = i["mwh"] / total_mwh * 100

    # Assign fallback colours to anything missing one
    fallback_iter = iter(FALLBACK_COLORS * 3)
    for i in items:
        if not i["color"]:
            i["color"] = next(fallback_iter)

    return items


def compute_summary(items: list[dict]) -> dict:
    total_mwh = sum(i["mwh"] for i in items)
    renewable_mwh = sum(i["mwh"] for i in items if i["category"] == "renewable")
    non_renewable_mwh = sum(i["mwh"] for i in items if i["category"] == "non-renewable")
    other_mwh = total_mwh - renewable_mwh - non_renewable_mwh

    top_source = items[0] if items else None
    top_renewable = next((i for i in items if i["category"] == "renewable"), None)

    return {
        "total_mwh": total_mwh,
        "renewable_mwh": renewable_mwh,
        "non_renewable_mwh": non_renewable_mwh,
        "other_mwh": other_mwh,
        "renewable_pct": (renewable_mwh / total_mwh * 100) if total_mwh else 0,
        "non_renewable_pct": (non_renewable_mwh / total_mwh * 100) if total_mwh else 0,
        "top_source": top_source,
        "top_renewable": top_renewable,
    }


def build_pie_chart(items: list[dict], out_path: str, small_slice_threshold: float = 1.0) -> None:
    """
    Build a pie chart of the generation mix. Slices under
    `small_slice_threshold` percent are grouped into "Other" to keep the
    chart readable; the email's table still shows full detail.
    """
    main_items = [i for i in items if i["pct"] >= small_slice_threshold and i["mwh"] > 0]
    small_items = [i for i in items if i["pct"] < small_slice_threshold and i["mwh"] > 0]

    labels = [i["name"] for i in main_items]
    sizes = [i["pct"] for i in main_items]
    colors = [i["color"] for i in main_items]

    if small_items:
        labels.append("Other")
        sizes.append(sum(i["pct"] for i in small_items))
        colors.append("#B0B0B0")

    fig, ax = plt.subplots(figsize=(7, 7))
    wedges, _texts, autotexts = ax.pie(
        sizes,
        colors=colors,
        autopct=lambda p: f"{p:.1f}%" if p >= 3 else "",
        pctdistance=0.8,
        startangle=90,
        wedgeprops={"linewidth": 1, "edgecolor": "white"},
    )
    for t in autotexts:
        t.set_color("white")
        t.set_fontsize(9)
        t.set_fontweight("bold")

    ax.legend(
        wedges, labels,
        title="Source",
        loc="center left",
        bbox_to_anchor=(1.02, 0.5),
        fontsize=9,
        frameon=False,
    )
    ax.set_title("Spain — Electricity Generation Mix", fontsize=14, fontweight="bold", pad=15)
    ax.axis("equal")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log.info("Pie chart saved to %s", out_path)


def build_email_html(report_dt: datetime, items: list[dict], summary: dict, image_cid: str) -> str:
    def fmt_gwh(mwh: float) -> str:
        return f"{mwh / 1000:,.1f} GWh"

    rows = "\n".join(
        f"""
        <tr>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;">
            <span style="display:inline-block;width:10px;height:10px;border-radius:2px;
                         background:{i['color']};margin-right:6px;"></span>{i['name']}
          </td>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right;">{fmt_gwh(i['mwh'])}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right;">{i['pct']:.1f}%</td>
        </tr>"""
        for i in items if i["mwh"] > 0
    )

    top_source = summary["top_source"]
    top_source_line = (
        f"{top_source['name']} ({top_source['pct']:.1f}%)" if top_source else "n/a"
    )

    return f"""\
<html>
  <body style="font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; color:#222; max-width:640px; margin:auto;">
    <h2 style="margin-bottom:0;">⚡ Spain Electricity Generation Report</h2>
    <p style="color:#666; margin-top:4px;">
      {report_dt.strftime('%A, %d %B %Y')} — cumulative from 00:00 to {report_dt.strftime('%H:%M')} —
      source: Red Eléctrica de España (REData API)
    </p>

    <div style="background:#f6f8fa; border-radius:8px; padding:16px 20px; margin:16px 0;">
      <table style="width:100%; border-collapse:collapse; font-size:14px;">
        <tr><td style="padding:4px 0;">Total generation</td><td style="text-align:right;"><b>{fmt_gwh(summary['total_mwh'])}</b></td></tr>
        <tr><td style="padding:4px 0;">Renewable share</td><td style="text-align:right;"><b style="color:#2e7d32;">{summary['renewable_pct']:.1f}%</b></td></tr>
        <tr><td style="padding:4px 0;">Non-renewable share</td><td style="text-align:right;">{summary['non_renewable_pct']:.1f}%</td></tr>
        <tr><td style="padding:4px 0;">Largest source overall</td><td style="text-align:right;">{top_source_line}</td></tr>
      </table>
    </div>

    <img src="cid:{image_cid}" alt="Generation mix pie chart" style="width:100%; max-width:600px; display:block; margin: 0 auto 20px;">

    <h3 style="margin-bottom:8px;">Breakdown by source</h3>
    <table style="width:100%; border-collapse:collapse; font-size:13px;">
      <thead>
        <tr style="text-align:left; color:#666; border-bottom:2px solid #ddd;">
          <th style="padding:6px 10px;">Source</th>
          <th style="padding:6px 10px; text-align:right;">Energy</th>
          <th style="padding:6px 10px; text-align:right;">Share</th>
        </tr>
      </thead>
      <tbody>{rows}
      </tbody>
    </table>

    <p style="color:#999; font-size:12px; margin-top:24px;">
      Automated daily report generated via GitHub Actions.
      Data: <a href="https://www.ree.es/en/apidatos">apidatos.ree.es</a> (REData API).
    </p>
  </body>
</html>
"""


def send_email(subject: str, html_body: str, image_path: str, image_cid: str) -> None:
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USERNAME"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    email_from = os.environ.get("EMAIL_FROM", smtp_user)
    email_to = os.environ["EMAIL_TO"]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.set_content("This email requires an HTML-capable client to view the report.")

    with open(image_path, "rb") as f:
        img_data = f.read()

    msg.add_alternative(html_body, subtype="html")
    # Attach the image to the HTML alternative part so it can be
    # referenced via cid: in the <img> tag.
    html_part = msg.get_payload()[-1]
    html_part.add_related(img_data, maintype="image", subtype="png", cid=f"<{image_cid}>")

    log.info("Connecting to SMTP server %s:%s ...", smtp_host, smtp_port)
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)
    log.info("Email sent to %s", email_to)


def send_failure_email(error_text: str) -> None:
    """Best-effort notification if something goes wrong, so failures aren't silent."""
    try:
        smtp_host = os.environ["SMTP_HOST"]
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        smtp_user = os.environ["SMTP_USERNAME"]
        smtp_password = os.environ["SMTP_PASSWORD"]
        email_from = os.environ.get("EMAIL_FROM", smtp_user)
        email_to = os.environ["EMAIL_TO"]

        msg = EmailMessage()
        msg["Subject"] = "⚠️ REE daily report failed"
        msg["From"] = email_from
        msg["To"] = email_to
        msg.set_content(
            "The daily Spain electricity generation report failed to run.\n\n"
            f"Error:\n{error_text}\n\n"
            "Check the GitHub Actions run logs for details."
        )
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
    except Exception as e:  # noqa: BLE001
        log.error("Could not send failure notification email either: %s", e)


def main() -> int:
    force = os.environ.get("FORCE_RUN", "").lower() in ("1", "true", "yes")

    if not should_run_now(force):
        return 0

    now_madrid = datetime.now(MADRID_TZ)
    start_of_day = now_madrid.replace(hour=0, minute=0, second=0, microsecond=0)

    try:
        payload = fetch_generation_mix(start_of_day, now_madrid)
        items = parse_generation_mix(payload)
        if not items:
            raise RuntimeError("REE API returned no generation data for the requested window.")

        summary = compute_summary(items)

        chart_path = "/tmp/generation_mix.png"
        build_pie_chart(items, chart_path)

        html = build_email_html(now_madrid, items, summary, image_cid="generation_mix")
        subject = (
            f"⚡ Spain grid report {now_madrid.strftime('%Y-%m-%d %H:%M')} — "
            f"{summary['renewable_pct']:.0f}% renewable"
        )
        send_email(subject, html, chart_path, image_cid="generation_mix")

    except Exception as e:  # noqa: BLE001
        log.exception("Report generation failed")
        send_failure_email(f"{type(e).__name__}: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
