import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Dict, Any
from app.config import settings
import logging

logger = logging.getLogger(__name__)

def send_results_email(emails: List[str], data: Dict[str, Any]) -> bool:
    """
    Constructs and sends an HTML email with the analysis results.
    """
    if not settings.smtp_server or not settings.smtp_username or not settings.smtp_password:
        logger.error("SMTP configuration is missing. Cannot send email.")
        raise ValueError("Email functionality is not configured on the server.")

    subject = f"ShelvesFinder Analysis Report: {data.get('product_title', 'Unknown Product')}"

    # Generate HTML content
    keywords = data.get('keywords', [])
    keywords_html = "<div style='display: flex; flex-wrap: wrap; gap: 8px;'>" + "".join([f"<span style='display:inline-block; margin: 4px; padding: 6px 12px; background: rgba(56,189,248,0.1); border: 1px solid rgba(56,189,248,0.2); border-radius: 9999px; font-size: 13px; color: #38bdf8;'>{kw}</span>" for kw in keywords]) + "</div>" if keywords else "<p style='color:#94a3b8;'>None</p>"

    discovered_urls = data.get('discovered_urls', [])
    if discovered_urls:
        urls_html = ""
        for page in discovered_urls:
            title_part = f"<h4 style='margin: 0 0 8px; color: #f1f5f9; font-size: 15px;'>{page.get('title', 'Recommended Page')}</h4>"
            base_url_part = f"<a href='{page.get('url', '')}' style='display:inline-block; color: #38bdf8; word-break: break-all; text-decoration: none; font-size: 13px;'>{page.get('url', '')}</a>"
            
            brand_part = ""
            if page.get('brandUrl') and page.get('brandUrl') != page.get('url'):
                brand_part = f"<div style='margin-top: 12px; padding-top: 12px; border-top: 1px solid rgba(255,255,255,0.05);'><span style='color:#94a3b8; font-size: 11px; text-transform: uppercase;'>Brand URL Variant:</span><br><a href='{page.get('brandUrl')}' style='color: #a78bfa; font-size: 13px; text-decoration: none; word-break: break-all;'>{page.get('brandUrl')}</a></div>"
                
            urls_html += f"<div style='background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.05); padding: 18px; margin-bottom: 12px; border-radius: 8px;'>{title_part}{base_url_part}{brand_part}</div>"
    else:
        browse_pages = data.get('browse_pages', [])
        urls_html = "<ul style='padding-left:20px;'>" + "".join([f'<li><a href="{page}" style="color:#38bdf8;">{page}</a></li>' for page in browse_pages]) + "</ul>" if browse_pages else "<p style='color:#94a3b8;'>No pages found.</p>"

    shelf_stats = data.get('shelf_stats', {})
    found = shelf_stats.get('found', 0)
    total = shelf_stats.get('total', 0)
    score = shelf_stats.get('score', 0)

    # Build UI-like components tailored strictly to the Visibility Dashboard Image layout
    shelf_stats = data.get('shelf_stats', {})
    found = shelf_stats.get('found', 0)
    total = shelf_stats.get('total', 1)
    missing = total - found
    score = shelf_stats.get('score', 0)
    miss_pct = round((missing / total) * 100, 1)
    found_pct = round((found / total) * 100, 1)

    # Keywords list formulation mapping dashboard
    keywords = data.get('keywords', [])
    keywords_list_items = "".join([f"<li><span style='color: #e2e8f0;'>{kw}</span></li>" for kw in keywords])
    keywords_html = f"<ul style='margin: 0 0 30px 0; padding-left: 25px; color: #ef4444; font-weight: 500; font-size: 14px; line-height: 1.8;'>{keywords_list_items}</ul>" if keywords else "<p style='color:#94a3b8;'>None</p>"

    # Category Pages formulation 
    discovered_urls = data.get('discovered_urls', [])
    table_rows = ""
    if discovered_urls:
        for page in discovered_urls:
            url = page.get('url', '')
            # Try to grab detailed stat if present, otherwise default False
            is_found = shelf_stats.get('details', {}).get(url, False)
            icon = "<span style='color: #10b981; font-size: 18px;'>&#10003;</span>" if is_found else "<span style='color: #ef4444; font-size: 18px;'>&#10005;</span>"
            
            title = page.get('title', 'Recommended Page')
            kw_val = page.get('keyword', 'Unknown')
            brand_url = page.get('brandUrl', url)
            pos = page.get('position')
            pos_html = f"<span style='font-size: 11px; color: #94a3b8; font-weight: 600;'>#{pos}</span>" if pos else ""
            table_rows += f'''<tr style="border-bottom: 1px solid rgba(255,255,255,0.03);">
                <td style="padding: 20px 10px; vertical-align: middle;">
                    <table width="100%" cellpadding="0" cellspacing="0" style="border: none;">
                        <tr>
                            <td style="color: #e2e8f0;">{kw_val}</td>
                            <td style="text-align: right;">{pos_html}</td>
                        </tr>
                    </table>
                </td>
                <td style="padding: 20px 10px; vertical-align: middle;">
                    <div style="background: #1f2532; border: 1px solid rgba(255,255,255,0.05); padding: 15px 20px; border-radius: 8px; font-size: 14px; color: #f8fafc;">
                        <a href="{url}" style="color: #38bdf8; text-decoration: none;">{title}</a>
                    </div>
                </td>
                <td style="padding: 20px 10px; vertical-align: middle;">
                    <div style="background: #1f2532; border: 1px solid rgba(255,255,255,0.05); padding: 15px 20px; border-radius: 8px; font-size: 14px; color: #f8fafc;">
                        <a href="{brand_url}" style="color: #a78bfa; text-decoration: none;">{title} (Brand Filter)</a>
                    </div>
                </td>
                <td style="padding: 20px 10px; text-align: center; vertical-align: middle;">{icon}</td>
            </tr>'''
    else:
        table_rows = '''<tr><td colspan="4" style="padding: 20px; text-align: center; color: #64748b;">No pages found.</td></tr>'''

    if score < 30:
        pill_txt = 'CRITICAL'
        bg_col = 'rgba(239, 68, 68, 0.1)'
        brd_col = '#ef4444'
        r_top = 'CRITICAL LEVEL'
        r_huge = 'HIGH RISK'
        r_desc = f"Immediate action required for {missing} items"
        banner = f"<strong>Action Required:</strong> Your items are missing from {missing} out of {total} digital shelves. This represents an {miss_pct}% visibility loss across the network."
    elif score < 70:
        pill_txt = 'WARNING'
        bg_col = 'rgba(245, 158, 11, 0.1)'
        brd_col = '#f59e0b'
        r_top = 'WARNING LEVEL'
        r_huge = 'MODERATE'
        r_desc = f"Visibility lacking on {missing} items"
        banner = f"<strong>Action Required:</strong> Your products have moderate visibility online. Consider optimizing your listings for the missing {missing} shelves."
    else:
        pill_txt = 'OPTIMAL'
        bg_col = 'rgba(16, 185, 129, 0.1)'
        brd_col = '#10b981'
        r_top = 'OPTIMAL LEVEL'
        r_huge = 'LOW RISK'
        r_desc = f"Strong presence across {found} items"
        banner = f"<strong>Action Required:</strong> Excellent work! Your product holds a commanding presence across your digital shelf network."

    html_content = f"""
    <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #0f131a; padding: 20px; }}
                .app-wrapper {{ max-width: 900px; margin: 0 auto; background-color: #12151b; padding: 25px; border-radius: 12px; }}
                /* Grid 4 columns */
                .top-grid {{ margin-bottom: 20px; display: table; width: 100%; border-spacing: 12px 0; }}
                .top-card {{ display: table-cell; width: 25%; background: #1f2532; border: 1px solid rgba(255,255,255,0.05); border-radius: 8px; padding: 20px; vertical-align: top; }}
                .card-title {{ color: #94a3b8; font-size: 11px; font-weight: 700; letter-spacing: 0.5px; margin-bottom: 15px; display: block; }}
                .card-val {{ font-size: 28px; font-weight: 800; color: #fff; line-height: 1.1; margin: 0; }}
                .card-sublbl {{ font-size: 24px; font-weight: 800; color: #fff; margin-bottom: 10px; display: block; }}
                .card-desc {{ color: #64748b; font-size: 12px; }}

                /* Layout */
                .mid-grid {{ display: table; width: 100%; border-spacing: 12px 0; margin-bottom: 20px; }}
                
                .left-panel {{ display: table-cell; width: 66%; background: #1f2532; border: 1px solid rgba(255,255,255,0.05); border-radius: 8px; padding: 25px; }}
                .right-panel {{ display: table-cell; width: 33%; }}

                .side-box {{ background: #1f2532; border: 1px solid rgba(255,255,255,0.05); border-radius: 8px; padding: 25px; text-align: center; margin-bottom: 12px; height: 100%; }}
                
                .progress-lbl {{ display: table; width: 100%; margin-bottom: 6px; }}
                .lbl-left {{ display: table-cell; text-align: left; font-size: 13px; color: #cbd5e1; }}
                .lbl-right {{ display: table-cell; text-align: right; font-size: 12px; font-weight: 700; }}
                
                .bar-bg {{ background: #0f131a; border-radius: 99px; height: 12px; width: 100%; margin-bottom: 25px; overflow: hidden; }}
                .bar-fill-red {{ background: #ef4444; height: 100%; border-radius: 99px; width: {miss_pct}%; }}
                .bar-fill-grn {{ background: #10b981; height: 100%; border-radius: 99px; width: {found_pct}%; }}

                .stat-trinity {{ display: table; width: 100%; border-spacing: 8px 0; margin-top: 10px; }}
                .stat-box {{ display: table-cell; background: #171b24; padding: 15px; border-radius: 6px; text-align: center; width: 33.3%; }}
                .stat-num {{ font-size: 20px; font-weight: 700; margin-bottom: 5px; }}
                .stat-lbl {{ font-size: 10px; color: #94a3b8; font-weight: 600; }}

                .dash-alert {{ background: {bg_col}; border: 1px solid {brd_col}; border-radius: 8px; padding: 20px; color: #cbd5e1; font-size: 13px; line-height: 1.6; margin-top: 10px; }}
            </style>
        </head>
        <body>
            <div class="app-wrapper">
                
                <div style="color: #cbd5e1; margin-bottom: 20px; padding: 0 12px;">
                    <h3 style="margin: 0 0 5px; color: #fff;">Digital Shelf Visibility Report</h3>
                    <p style="margin: 0; font-size: 13px;">Product: <a href="{data.get('product_url', '#')}" style="color: #38bdf8; text-decoration: none;">{data.get('product_title', 'Unknown')}</a></p>
                </div>

                <div class="top-grid">
                    <div class="top-card" style="border-left: 2px solid #ef4444;">
                        <span class="card-title">MISSING ITEMS</span>
                        <div class="card-val">{missing}</div>
                        <span class="card-sublbl">Shelves</span>
                        <span class="card-desc">{miss_pct}% of total shelves</span>
                    </div>
                    <div class="top-card" style="border-left: 2px solid #10b981;">
                        <span class="card-title">ITEMS FOUND</span>
                        <div class="card-val">{found}</div>
                        <span class="card-sublbl">Shelves</span>
                        <span class="card-desc">{found_pct}% of total shelves</span>
                    </div>
                    <div class="top-card" style="border-left: 2px solid #f59e0b;">
                        <span class="card-title">TOTAL SHELVES</span>
                        <div class="card-val">{total}</div>
                        <span class="card-sublbl">Shelves</span>
                        <span class="card-desc">100% coverage tracked</span>
                    </div>
                    <div class="top-card" style="border-left: 2px solid #3b82f6;">
                        <span class="card-title">VISIBILITY RATIO</span>
                        <div class="card-val">{found} / {total}</div>
                        <span style="display: block; height: 10px;"></span>
                        <span class="card-desc">{"Critical visibility gap" if score < 30 else "Tracking visibility"}</span>
                    </div>
                </div>

                <div class="mid-grid">
                    <div class="left-panel">
                        <div style="display: table; width: 100%; margin-bottom: 30px;">
                            <div style="display: table-cell; text-align: left; vertical-align: middle;">
                                <h3 style="margin: 0; color: #fff; font-size: 16px; letter-spacing: 0.5px;">SHELF STATUS DISTRIBUTION</h3>
                            </div>
                            <div style="display: table-cell; text-align: right; vertical-align: middle;">
                                <span style="display: inline-block; border: 1px solid {brd_col}; color: {brd_col}; padding: 4px 12px; border-radius: 99px; font-size: 10px; font-weight: 700; letter-spacing: 0.5px;">{pill_txt}</span>
                            </div>
                        </div>

                        <div class="progress-lbl">
                            <div class="lbl-left">Missing Items</div>
                            <div class="lbl-right" style="color: #ef4444;">{miss_pct}%</div>
                        </div>
                        <div class="bar-bg"><div class="bar-fill-red"></div></div>

                        <div class="progress-lbl">
                            <div class="lbl-left">Found Items</div>
                            <div class="lbl-right" style="color: #10b981;">{found_pct}%</div>
                        </div>
                        <div class="bar-bg"><div class="bar-fill-grn"></div></div>

                        <div class="stat-trinity">
                            <div class="stat-box">
                                <div class="stat-num" style="color: #ef4444;">{missing}</div>
                                <div class="stat-lbl">MISSING</div>
                            </div>
                            <div class="stat-box">
                                <div class="stat-num" style="color: #10b981;">{found}</div>
                                <div class="stat-lbl">FOUND</div>
                            </div>
                            <div class="stat-box">
                                <div class="stat-num" style="color: #f59e0b;">{total}</div>
                                <div class="stat-lbl">TOTAL</div>
                            </div>
                        </div>
                    </div>

                    <div class="right-panel">
                        <div class="side-box">
                            <div class="card-title" style="text-align: center;">VISIBILITY SCORE</div>
                            <div style="font-size: 42px; font-weight: 800; color: #fff; margin: 15px 0;">{score}%</div>
                            <div class="bar-bg" style="height: 6px; margin: 15px 0;"><div class="bar-fill-grn" style="width: {score}%;"></div></div>
                            <div style="color: {brd_col}; font-weight: 700; font-size: 12px; letter-spacing: 0.5px;">{r_top}</div>
                        </div>

                        <div class="side-box" style="margin-bottom: 0;">
                            <div class="card-title" style="text-align: center;">RISK ASSESSMENT</div>
                            <div style="font-size: 24px; font-weight: 800; color: {brd_col}; margin: 20px 0;">{r_huge}</div>
                            <div style="color: #94a3b8; font-size: 12px; line-height: 1.5;">{r_desc}</div>
                        </div>
                    </div>
                </div>

                <div style="padding: 0 12px;">
                    <div class="dash-alert">
                        {banner}
                    </div>
                </div>

                <div style="padding: 0 12px; margin-top: 40px;">
                    <div style="font-size: 16px; font-weight: 700; color: #94a3b8; letter-spacing: 1px; margin-bottom: 15px; text-transform: uppercase;">Extracted Search Intent</div>
                    <div style="font-size: 13px; font-weight: 700; color: #ef4444; margin-bottom: 15px; letter-spacing: 0.5px;">GENERIC KEYWORDS</div>
                    {keywords_html}

                    <div style="font-size: 16px; font-weight: 700; color: #94a3b8; letter-spacing: 1px; margin-bottom: 20px; margin-top: 50px; text-transform: uppercase;">Recommended Category Pages</div>
                    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse: collapse; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; font-size: 13px; width: 100%;">
                        <thead>
                            <tr style="text-align: left; color: #64748b; border-bottom: 1px solid rgba(255,255,255,0.08);">
                                <th style="padding: 15px 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">
                                    <table width="100%" cellpadding="0" cellspacing="0" style="border: none;">
                                        <tr>
                                            <td style="font-weight: 600; padding: 0;">Keyword</td>
                                            <td style="text-align: right; font-weight: 600; padding: 0;">Rank</td>
                                        </tr>
                                    </table>
                                </th>
                                <th style="padding: 15px 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">Recommended Page</th>
                                <th style="padding: 15px 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">Walmart Brand Page</th>
                                <th style="padding: 15px 10px; font-weight: 600; text-align: center; text-transform: uppercase; letter-spacing: 0.5px;">Item Found</th>
                            </tr>
                        </thead>
                        <tbody>
                            {table_rows}
                        </tbody>
                    </table>
                </div>

            </div>
        </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_username
    msg["To"] = ", ".join(emails)

    to_addrs = list(emails)
    if settings.smtp_cc_email:
        msg["Cc"] = settings.smtp_cc_email
        to_addrs.append(settings.smtp_cc_email)

    msg.attach(MIMEText(html_content, "html"))

    try:
        if settings.smtp_port == 465:
            server = smtplib.SMTP_SSL(settings.smtp_server, settings.smtp_port)
        else:
            server = smtplib.SMTP(settings.smtp_server, settings.smtp_port)
            server.starttls()
            
        server.login(settings.smtp_username, settings.smtp_password)
        server.sendmail(settings.smtp_username, to_addrs, msg.as_string())
        server.quit()
        logger.info(f"Successfully sent analysis email to {len(emails)} recipients.")
        return True
    except Exception as e:
        logger.error(f"Failed to send email: {str(e)}")
        raise e
