"""Generate the August 2026 store-profit backfill reconciliation PDF."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from db import db


OUTPUT = Path(__file__).resolve().parent / "store_profit_backfill_report_2026-08.pdf"
BACKFILL_SOURCE = "shared_checkout_backfill_2026_08"


def money(value) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def page_footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#64748b"))
    canvas.drawString(16 * mm, 10 * mm, "Sir Hans Store Profit Reconciliation")
    canvas.drawRightString(
        landscape(A4)[0] - 16 * mm,
        10 * mm,
        f"Page {doc.page}",
    )
    canvas.restoreState()


def main() -> None:
    grouped = defaultdict(list)
    query = {"store_profit_credit_source": BACKFILL_SOURCE}
    projection = {
        "order_id": 1,
        "store_slug": 1,
        "created_at": 1,
        "store_profit_credited_at": 1,
        "items.store_profit_amount": 1,
    }
    for order in db.orders.find(query, projection).sort(
        [("store_slug", 1), ("created_at", 1)]
    ):
        profit = round(
            sum(money(item.get("store_profit_amount")) for item in order.get("items", [])),
            2,
        )
        grouped[str(order.get("store_slug") or "Unknown")].append(
            {
                "order_id": str(order.get("order_id") or "-"),
                "created_at": order.get("created_at"),
                "credited_at": order.get("store_profit_credited_at"),
                "profit": profit,
            }
        )

    all_orders = [order for orders in grouped.values() for order in orders]
    if not all_orders:
        raise SystemExit("No backfilled orders found.")

    grand_total = round(sum(order["profit"] for order in all_orders), 2)
    affected_dates = [order["created_at"] for order in all_orders if order["created_at"]]
    first_affected = min(affected_dates)
    last_affected = max(affected_dates)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        textColor=colors.HexColor("#1e3a8a"),
        alignment=TA_CENTER,
        spaceAfter=5 * mm,
    )
    subtitle_style = ParagraphStyle(
        "Subtitle",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#475569"),
        alignment=TA_CENTER,
    )
    store_style = ParagraphStyle(
        "Store",
        parent=styles["Heading2"],
        fontSize=13,
        leading=16,
        textColor=colors.HexColor("#1e3a8a"),
        spaceBefore=3 * mm,
        spaceAfter=2 * mm,
    )
    right_style = ParagraphStyle("Right", parent=styles["Normal"], alignment=TA_RIGHT)

    document = SimpleDocTemplate(
        str(OUTPUT),
        pagesize=landscape(A4),
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=15 * mm,
        bottomMargin=17 * mm,
        title="Store Profit Backfill Reconciliation Report",
        author="Sir Hans",
    )

    story = [
        Paragraph("Store Profit Backfill Reconciliation Report", title_style),
        Paragraph(
            "Affected period: "
            f"{first_affected:%d %B %Y, %H:%M} to {last_affected:%d %B %Y, %H:%M} UTC/Ghana time",
            subtitle_style,
        ),
        Paragraph(
            f"Generated: {datetime.utcnow():%d %B %Y, %H:%M} UTC/Ghana time",
            subtitle_style,
        ),
        Spacer(1, 7 * mm),
    ]

    summary_data = [
        ["Affected stores", "Affected orders", "Total amount credited"],
        [str(len(grouped)), str(len(all_orders)), f"GHS {grand_total:,.2f}"],
    ]
    summary = Table(summary_data, colWidths=[70 * mm, 70 * mm, 80 * mm])
    summary.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 9),
                ("FONTSIZE", (0, 1), (-1, 1), 15),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#eff6ff")),
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#93c5fd")),
                ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#bfdbfe")),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.extend([summary, Spacer(1, 7 * mm)])

    overview_data = [["Store", "Orders affected", "Amount credited (GHS)"]]
    for slug in sorted(grouped):
        subtotal = round(sum(order["profit"] for order in grouped[slug]), 2)
        overview_data.append([slug, str(len(grouped[slug])), f"{subtotal:,.2f}"])
    overview_data.append(["GRAND TOTAL", str(len(all_orders)), f"{grand_total:,.2f}"])
    overview = Table(overview_data, colWidths=[130 * mm, 45 * mm, 55 * mm], repeatRows=1)
    overview.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#334155")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#dbeafe")),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f8fafc")]),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.extend([Paragraph("Store Summary", store_style), overview, PageBreak()])

    for position, slug in enumerate(sorted(grouped)):
        orders = grouped[slug]
        subtotal = round(sum(order["profit"] for order in orders), 2)
        story.append(Paragraph(f"Store: {slug}", store_style))
        rows = [["#", "Order ID", "Order date/time", "Credited date/time", "Amount credited (GHS)"]]
        for index, order in enumerate(orders, 1):
            created = order["created_at"].strftime("%d %b %Y %H:%M:%S") if order["created_at"] else "-"
            credited = order["credited_at"].strftime("%d %b %Y %H:%M:%S") if order["credited_at"] else "-"
            rows.append([str(index), order["order_id"], created, credited, f"{order['profit']:,.2f}"])
        rows.append(["", "STORE TOTAL", "", f"{len(orders)} order(s)", f"{subtotal:,.2f}"])
        table = Table(rows, colWidths=[12 * mm, 43 * mm, 58 * mm, 58 * mm, 50 * mm], repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                    ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#dbeafe")),
                    ("ALIGN", (0, 1), (0, -1), "CENTER"),
                    ("ALIGN", (-1, 1), (-1, -1), "RIGHT"),
                    ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f8fafc")]),
                    ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.append(table)
        if position < len(grouped) - 1:
            story.append(Spacer(1, 5 * mm))

    story.extend(
        [
            Spacer(1, 8 * mm),
            Paragraph(
                f"Reconciliation confirmed: {len(all_orders)} orders across {len(grouped)} stores; "
                f"GHS {grand_total:,.2f} credited.",
                ParagraphStyle(
                    "Confirmation",
                    parent=styles["Normal"],
                    fontName="Helvetica-Bold",
                    fontSize=10,
                    alignment=TA_RIGHT,
                    textColor=colors.HexColor("#166534"),
                ),
            ),
        ]
    )
    document.build(story, onFirstPage=page_footer, onLaterPages=page_footer)
    print(OUTPUT)


if __name__ == "__main__":
    main()
