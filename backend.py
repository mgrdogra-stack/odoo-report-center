import os
import re
import json
import tempfile
from datetime import datetime
import xlsxwriter
from odoo_core import (
    STOP_EVENT,
    stop_generation,
    to_odoo_date,
    odoo_jsonrpc,
    execute_with_uid,
    get_auth_opener,
    connect_and_discover,
    inspect_wizard,
    sync_ui_to_json,
    rpc_fetch_all,
    fetch_single_wizard_fast,
)

def extract_grir_transport_fast(url, db, uid, pw, acc_ids, d_from, d_to, out_xlsx_path, progress):
    d1_str, d2_str = to_odoo_date(d_from), to_odoo_date(d_to)
    dom = [["parent_state", "=", "posted"], ["date", ">=", d1_str], ["date", "<=", d2_str]]
    if acc_ids:
        dom.append(["account_id", "in", acc_ids])
    else:
        dom.extend([["account_id.name", "ilike", "GRIR"], ["account_id.name", "ilike", "TRANSPORT"]])

    if progress:
        progress(0.08, desc="Fetching posted GRIR Transport ledger items...")
    aml_fields = ["id", "date", "move_id", "move_name", "ref", "name", "po_ref", "partner_id", "account_id", "debit", "credit", "balance"]
    aml_rows = rpc_fetch_all(url, db, uid, pw, "account.move.line", dom, aml_fields, 2500, progress, "GRIR Ledger Lines", 0.10, 0.45)
    if not aml_rows:
        return 0, 0.0, 0.0, 0.0

    first_id = lambda val: next(iter(val), 0) if isinstance(val, list) and val else 0
    lc_by_grn, all_lc_rows, bill_move_ids, bill_lines = {}, list(), set(), list()
    for r in aml_rows:
        ref_txt, name_txt = str(r.get("ref") or ""), str(r.get("name") or "")
        combo = f"{ref_txt} | {name_txt}"
        m_lc = re.search(r"(LC/\d{4}/\d+)", combo)
        m_grn = re.search(r"(CM/GRN/\d{4}/\d+)", combo)
        m_po = re.search(r"([A-Z]+ORD/\d{4}/\d+)", combo)
        p_name = r["partner_id"][-1] if isinstance(r.get("partner_id"), list) and r["partner_id"] else ""
        m_id = first_id(r.get("move_id"))

        entry = {
            "id": r["id"], "date": str(r.get("date") or ""), "move_id": m_id,
            "move_name": str(r.get("move_name") or ""), "ref": ref_txt, "name": name_txt,
            "lc": m_lc.group(1) if m_lc else "", "grn": m_grn.group(1) if m_grn else "",
            "po": m_po.group(1) if m_po else str(r.get("po_ref") or ""),
            "partner": re.sub(r"^\[.*?\]\s*", "", str(p_name)),
            "debit": float(r.get("debit") or 0.0), "credit": float(r.get("credit") or 0.0), "balance": float(r.get("balance") or 0.0),
        }
        if entry["debit"] > 0 and entry["move_id"]:
            bill_move_ids.add(entry["move_id"])
            bill_lines.append(entry)
        else:
            all_lc_rows.append(entry)
            if entry["grn"]:
                lc_by_grn.setdefault(entry["grn"], list()).append(entry)

    move_map, tbs_ids = {}, set()
    m_id_list = list(bill_move_ids)
    for i in range(0, len(m_id_list), 2000):
        for m in execute_with_uid(url, db, uid, pw, "account.move", "read", [m_id_list[i:i+2000]], {"fields": ["id", "name", "ref", "invoice_date", "invoice_origin", "tbill_purchase_id", "partner_id"]}, timeout=180):
            move_map[m["id"]] = m
            tid = first_id(m.get("tbill_purchase_id"))
            if tid:
                tbs_ids.add(tid)

    tbs_lines_by_settle = {}
    t_id_list = list(tbs_ids)
    for i in range(0, len(t_id_list), 500):
        for tl in execute_with_uid(url, db, uid, pw, "transporter.bill.purchase.line", "search_read", [[["settlement_id", "in", t_id_list[i:i+500]]]], {"fields": ["settlement_id", "grn_nos", "grn_date", "vendor", "partner_id", "purchase_orders", "product", "reference_no", "token_date"], "limit": 50000}, timeout=180):
            sid = first_id(tl.get("settlement_id"))
            if sid:
                tbs_lines_by_settle.setdefault(sid, list()).append(tl)

    workbook = xlsxwriter.Workbook(out_xlsx_path, {"constant_memory": True})
    ws_main = workbook.add_worksheet("GRIR_Transport_Breakup")
    ws_tb = workbook.add_worksheet("Trial_Balance_Check")
    ws_raw = workbook.add_worksheet("Raw_GRIR_Ledger_Lines")

    ws_main.write_row(0, 0, ["TBS Bill No", "TBS Number", "TBS Vendor", "TBS Bill Date", "Posting Date", "LC", "LC Date", "Year (LC Date)", "Month (LC Date)", "Partner Name (GRN Vendor)", "Transport Name", "GRN", "PO Number", "Product", "Sum of TBS Amount", "Sum of LC Amount", "Sum of Bill Amount", "Net Amount", "Status"])

    curr_row, matched_lc_ids = 1, set()
    for bl in bill_lines:
        m_info = move_map.get(bl["move_id"], {})
        bill_no = m_info.get("name") or bl["move_name"]
        tbs_pair = m_info.get("tbill_purchase_id")
        tbs_id = first_id(tbs_pair)
        tbs_num = tbs_pair[-1] if isinstance(tbs_pair, list) and tbs_pair else str(m_info.get("invoice_origin") or "")
        tbs_vnd_inv = str(m_info.get("ref") or bl["ref"] or "")
        tbs_bill_dt = str(m_info.get("invoice_date") or bl["date"] or "")
        trans_name = bl["partner"]

        linked_grn_lines = tbs_lines_by_settle.get(tbs_id, list()) if tbs_id else list()
        if linked_grn_lines:
            for idx_g, gl in enumerate(linked_grn_lines):
                grn_code = str(gl.get("grn_nos") or "").strip()
                grn_vendor = str(gl.get("vendor") or "").strip()
                po_code = str(gl.get("purchase_orders") or bl["po"] or "").strip()
                prod_name = str(gl.get("product") or "").strip()
                t_raw = gl.get("partner_id")
                t_name = re.sub(r"^\[.*?\]\s*", "", str(t_raw[-1])) if isinstance(t_raw, list) and t_raw else trans_name

                lc_matches = lc_by_grn.get(grn_code, list())
                lc_code = ", ".join(dict.fromkeys(x["lc"] for x in lc_matches if x["lc"]))
                lc_dt = next((x["date"] for x in lc_matches if x["date"]), "")
                lc_yr = lc_dt[:4] if len(lc_dt) >= 4 else ""
                lc_mo = datetime.strptime(lc_dt[:10], "%Y-%m-%d").strftime("%b") if len(lc_dt) >= 10 else ""

                lc_amt = sum(x["balance"] for x in lc_matches if x["id"] not in matched_lc_ids)
                for x in lc_matches:
                    matched_lc_ids.add(x["id"])

                tbs_amt = bl["debit"] if idx_g == 0 else 0.0
                ws_main.write_row(curr_row, 0, [bill_no, tbs_num, tbs_vnd_inv, tbs_bill_dt, bl["date"], lc_code, lc_dt, lc_yr, lc_mo, grn_vendor, t_name, grn_code, po_code, prod_name, tbs_amt, lc_amt, 0.0, tbs_amt + lc_amt, "Matched (Bill + LC)" if lc_matches else "Bill Posted (No LC in Range)"])
                curr_row += 1
        else:
            ws_main.write_row(curr_row, 0, [bill_no, tbs_num, tbs_vnd_inv, tbs_bill_dt, bl["date"], bl["lc"], "", "", "", "", trans_name, bl["grn"], bl["po"], bl["name"], bl["debit"], 0.0, 0.0, bl["debit"], "Bill Posted (Direct)"])
            curr_row += 1

    for lc_r in all_lc_rows:
        if lc_r["id"] in matched_lc_ids:
            continue
        lc_dt = lc_r["date"]
        lc_yr = lc_dt[:4] if len(lc_dt) >= 4 else ""
        lc_mo = datetime.strptime(lc_dt[:10], "%Y-%m-%d").strftime("%b") if len(lc_dt) >= 10 else ""
        ws_main.write_row(curr_row, 0, ["", "", "", "", lc_dt, lc_r["lc"], lc_dt, lc_yr, lc_mo, lc_r["partner"], "", lc_r["grn"], lc_r["po"], lc_r["name"], 0.0, lc_r["balance"], 0.0, lc_r["balance"], "Pending GRN / Unbilled LC"])
        curr_row += 1

    tot_debit, tot_credit, net_gl = sum(x["debit"] for x in aml_rows), sum(x["credit"] for x in aml_rows), sum(x["balance"] for x in aml_rows)
    ws_tb.write_row(0, 0, ["Metric", "Amount (TZS)"])
    ws_tb.write_row(1, 0, ["Period", f"{d_from} to {d_to}"])
    ws_tb.write_row(2, 0, ["Total GL Debits (TBS Bills)", tot_debit])
    ws_tb.write_row(3, 0, ["Total GL Credits (LC Accruals)", tot_credit])
    ws_tb.write_row(4, 0, ["Net GL Movement (Debit - Credit)", net_gl])
    ws_tb.write_row(5, 0, ["Total Exported Rows", curr_row - 1])

    grn_meta = {str(tl.get("grn_nos") or "").strip(): tl for s in tbs_lines_by_settle.values() for tl in s if tl.get("grn_nos")}
    ws_raw.write_row(0, 0, ["AML_ID", "Date", "Journal_Entry", "Account", "Partner", "Reference", "Label", "GRN", "GRN Date", "Partner Name", "Transporter Name", "Vendor DC No", "DC Date", "Debit", "Credit", "Balance"])
    for idx_r, r in enumerate(aml_rows, start=1):
        acc_lbl = r["account_id"][-1] if isinstance(r.get("account_id"), list) and r["account_id"] else ""
        prt_lbl = r["partner_id"][-1] if isinstance(r.get("partner_id"), list) and r["partner_id"] else ""
        m_g = re.search(r"(CM/GRN/\d{4}/\d+)", f"{r.get('ref') or ''} | {r.get('name') or ''}")
        g_code = m_g.group(1) if m_g else ""
        tl = grn_meta.get(g_code, {})
        t_raw = tl.get("partner_id")
        t_name = re.sub(r"^\[.*?\]\s*", "", str(t_raw[-1])) if isinstance(t_raw, list) and t_raw else (re.sub(r"^\[.*?\]\s*", "", prt_lbl) if r.get("debit", 0) > 0 else "")
        ws_raw.write_row(idx_r, 0, [r["id"], r["date"], r["move_name"], acc_lbl, prt_lbl, r["ref"], r["name"], g_code, str(tl.get("grn_date") or ""), str(tl.get("vendor") or ""), t_name, str(tl.get("reference_no") or ""), str(tl.get("token_date") or ""), r["debit"], r["credit"], r["balance"]])

    workbook.close()
    if progress:
        progress(1.0, desc=f"Done! {curr_row - 1:,} rows exported.")
    return curr_row - 1, tot_debit, tot_credit, net_gl

def extract_sale_register_fast(url, db, uid, pw, d_from, d_to, out_xlsx_path, progress, acc_ids=None):
    d1_str, d2_str = to_odoo_date(d_from), to_odoo_date(d_to)
    dom = [["parent_state", "=", "posted"], ["date", ">=", d1_str], ["date", "<=", d2_str]]
    dom.append(["account_id", "in", acc_ids] if acc_ids else ["account_id.code", "=like", "701%"])

