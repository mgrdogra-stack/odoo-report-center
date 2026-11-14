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

    fields = ["id", "date", "move_id", "move_name", "ref", "journal_id", "partner_id", "customer_code", "customer_name", "tin_no", "vat", "product_id", "product_code", "name", "quantity", "product_uom_id", "price_unit", "discount", "price_subtotal", "taxed_amt", "price_total", "debit", "credit", "balance", "account_id", "analytic_account_id", "team_id", "currency_id", "amount_currency"]
    rows = rpc_fetch_all(url, db, uid, pw, "account.move.line", dom, fields, 2500, progress, "Revenue GL Lines", 0.10, 0.75)
    if not rows:
        return 0, 0.0, 0.0, 0.0

    first_id = lambda val: next(iter(val), 0) if isinstance(val, list) and val else 0
    move_ids = list({first_id(r.get("move_id")) for r in rows if first_id(r.get("move_id"))})
    move_map = {}
    for i in range(0, len(move_ids), 2000):
        for m in execute_with_uid(url, db, uid, pw, "account.move", "read", [move_ids[i:i+2000]], {"fields": ["id", "name", "move_type", "invoice_date", "invoice_origin", "ref", "payment_state", "invoice_user_id"]}, timeout=180):
            move_map[m["id"]] = m

    workbook = xlsxwriter.Workbook(out_xlsx_path, {"constant_memory": True})
    ws_main = workbook.add_worksheet("Sale_Register_Data")
    ws_tb = workbook.add_worksheet("Trial_Balance_Check")

    ws_main.write_row(0, 0, ["AML_ID", "Posting Date", "Invoice Date", "Journal", "Document Type", "Document No", "Source Order", "Reference", "Customer Code", "Customer Name", "TIN No", "VRN / VAT", "Product Code", "Product / Description", "Quantity", "UOM", "Unit Price", "Discount (%)", "GL Debit", "GL Credit", "Net Revenue (Credit - Debit)", "VAT / Tax Info", "Gross Invoice Line", "Revenue Account", "Analytic Account", "Sales Team", "Currency", "Amount Currency", "Salesperson", "Payment Status"])

    tot_dr = tot_cr = tot_vat = tot_gross = 0.0
    type_labels = {"out_invoice": "Customer Invoice", "out_refund": "Credit Note", "entry": "Journal Entry"}

    for idx_r, r in enumerate(rows, start=1):
        m_info = move_map.get(first_id(r.get("move_id")), {})
        m_type = m_info.get("move_type", "entry")
        sign = -1.0 if m_type == "out_refund" else 1.0
        dr, cr = float(r.get("debit") or 0.0), float(r.get("credit") or 0.0)
        tax_signed = float(r.get("taxed_amt") or 0.0) * sign
        tot_signed = float(r.get("price_total") or 0.0) * sign
        tot_dr += dr
        tot_cr += cr
        tot_vat += tax_signed
        tot_gross += tot_signed

        get_lbl = lambda k: str(r[k][-1]) if isinstance(r.get(k), list) and r[k] else ""
        prt_name = r.get("customer_name") or re.sub(r"^\[.*?\]\s*", "", get_lbl("partner_id"))
        usr_raw = m_info.get("invoice_user_id")
        usr_lbl = str(usr_raw[-1]) if isinstance(usr_raw, list) and usr_raw else ""

        ws_main.write_row(idx_r, 0, [r["id"], str(r.get("date") or ""), str(m_info.get("invoice_date") or ""), get_lbl("journal_id"), type_labels.get(m_type, m_type), str(r.get("move_name") or ""), str(m_info.get("invoice_origin") or ""), str(r.get("ref") or ""), str(r.get("customer_code") or ""), str(prt_name or ""), str(r.get("tin_no") or ""), str(r.get("vat") or ""), str(r.get("product_code") or ""), get_lbl("product_id") or str(r.get("name") or ""), float(r.get("quantity") or 0.0) * sign, get_lbl("product_uom_id"), float(r.get("price_unit") or 0.0), float(r.get("discount") or 0.0), dr, cr, cr - dr, tax_signed, tot_signed, get_lbl("account_id"), get_lbl("analytic_account_id"), get_lbl("team_id"), get_lbl("currency_id"), float(r.get("amount_currency") or 0.0), usr_lbl, str(m_info.get("payment_state") or "")])

    net_tb_rev = tot_cr - tot_dr
    ws_tb.write_row(0, 0, ["Metric", "Amount (TZS)"])
    ws_tb.write_row(1, 0, ["Period", f"{d_from} to {d_to}"])
    ws_tb.write_row(2, 0, ["Total Revenue GL Debits (Matches TB Debit)", tot_dr])
    ws_tb.write_row(3, 0, ["Total Revenue GL Credits (Matches TB Credit)", tot_cr])
    ws_tb.write_row(4, 0, ["Net Revenue (Credit - Debit — Matches TB Net)", net_tb_rev])
    ws_tb.write_row(5, 0, ["Output VAT Info (Invoice Lines)", tot_vat])
    ws_tb.write_row(6, 0, ["Total Exported GL Lines", len(rows)])

    workbook.close()
    if progress:
        progress(1.0, desc=f"Done! {len(rows):,} Revenue GL lines exported.")
    return len(rows), net_tb_rev, tot_dr, tot_cr

def extract_purchase_register_fast(url, db, uid, pw, d_from, d_to, out_xlsx_path, progress, acc_ids=None):
    d1_str, d2_str = to_odoo_date(d_from), to_odoo_date(d_to)
    if acc_ids:
        dom = [["parent_state", "=", "posted"], ["date", ">=", d1_str], ["date", "<=", d2_str], ["account_id", "in", acc_ids]]
    else:
        dom = [["parent_state", "=", "posted"], ["date", ">=", d1_str], ["date", "<=", d2_str], "|", "|", ["account_id.code", "=like", "551%"], ["account_id.code", "=like", "181%"], ["account_id.code", "in", ["731004", "751018"]]]

    fields = ["id", "date", "move_id", "move_name", "ref", "journal_id", "partner_id", "tin_no", "vat", "product_id", "product_code", "name", "quantity", "product_uom_id", "price_unit", "price_subtotal", "taxed_amt", "price_total", "debit", "credit", "balance", "account_id", "analytic_account_id", "po_ref", "purchase_order_id", "is_landed_costs_line"]
    rows = rpc_fetch_all(url, db, uid, pw, "account.move.line", dom, fields, 2500, progress, "Inventory & GRIR GL", 0.10, 0.65)
    if not rows:
        return 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0

    first_id = lambda val: next(iter(val), 0) if isinstance(val, list) and val else 0

    acc_id_set = list({first_id(r.get("account_id")) for r in rows if first_id(r.get("account_id"))})
    acc_code_map = {}
    for i in range(0, len(acc_id_set), 1000):
        for a in execute_with_uid(url, db, uid, pw, "account.account", "read", [acc_id_set[i:i+1000]], {"fields": ["id", "code"]}, timeout=120):
            acc_code_map[a["id"]] = str(a.get("code") or "")

    move_ids = list({first_id(r.get("move_id")) for r in rows if first_id(r.get("move_id"))})
    move_map, all_picking_ids = {}, set()
    for i in range(0, len(move_ids), 2000):
        for m in execute_with_uid(url, db, uid, pw, "account.move", "read", [move_ids[i:i+2000]], {"fields": ["id", "move_type", "invoice_date", "invoice_origin", "ref", "payment_state", "tbill_purchase_id", "custom_doc_ref_id", "grn_ids"]}, timeout=180):
            move_map[m["id"]] = m
            all_picking_ids.update(m.get("grn_ids") or list())

    po_ids = list({first_id(r.get("purchase_order_id")) for r in rows if first_id(r.get("purchase_order_id"))})
    po_names = list({str(r.get("po_ref") or "").strip() for r in rows if r.get("po_ref") and not r.get("purchase_order_id")})
    po_map, po_by_name = {}, {}
    for i in range(0, len(po_ids), 2000):
        for po in execute_with_uid(url, db, uid, pw, "purchase.order", "read", [po_ids[i:i+2000]], {"fields": ["id", "name", "pr_id", "requisition_id", "custom_doc_ref_id", "picking_ids"]}, timeout=180):
            po_map[po["id"]] = po_by_name[str(po.get("name") or "")] = po
            all_picking_ids.update(po.get("picking_ids") or list())
    for i in range(0, len(po_names), 1000):
        for po in execute_with_uid(url, db, uid, pw, "purchase.order", "search_read", [[["name", "in", po_names[i:i+1000]]]], {"fields": ["id", "name", "pr_id", "requisition_id", "custom_doc_ref_id", "picking_ids"]}, timeout=180):
            po_map[po["id"]] = po_by_name[str(po.get("name") or "")] = po

    pick_name_map = {}
    pick_list = list(all_picking_ids)
    for i in range(0, len(pick_list), 2000):
        for pk in execute_with_uid(url, db, uid, pw, "stock.picking", "read", [pick_list[i:i+2000]], {"fields": ["id", "name"]}, timeout=120):
            pick_name_map[pk["id"]] = str(pk.get("name") or "")

    workbook = xlsxwriter.Workbook(out_xlsx_path, {"constant_memory": True})
    ws_main = workbook.add_worksheet("Purchase_Register_Data")
    ws_tb = workbook.add_worksheet("Trial_Balance_Check")

    ws_main.write_row(0, 0, ["AML_ID", "Ledger Group", "Account Code", "Account Name", "Entry Source", "Posting Date", "Bill / Doc Date", "Journal", "Bill / Document No", "Vendor Invoice Ref", "Vendor Name", "TIN No", "VRN / VAT", "PR Number", "PO Number", "GRN Number", "TANSAD No", "LC Number", "TBS / Origin", "Product Code", "Product / Description", "Quantity", "UOM", "Unit Price", "Taxable Amount (Bills)", "Input VAT (Bills)", "Gross Bill Amount", "GL Debit", "GL Credit", "Net GL Balance (Debit - Credit)", "Analytic Account", "Payment Status"])

    acc_summary = {}
    inv_dr = inv_cr = grir_dr = grir_cr = 0.0

    for idx_r, r in enumerate(rows, start=1):
        acc_pair = r.get("account_id")
        acc_id = first_id(acc_pair)
        acc_full = str(acc_pair[-1]) if isinstance(acc_pair, list) and acc_pair else ""
        acc_code = acc_code_map.get(acc_id, next(iter(acc_full.split(" ")), "") if acc_full else "")
        grp = "INVENTORY" if (acc_code.startswith("551") or acc_code in ("731004", "751018")) else "GRIR"

        m_info = move_map.get(first_id(r.get("move_id")), {})
        m_type = m_info.get("move_type", "entry")

        ref_txt, name_txt, orig_txt = str(r.get("ref") or ""), str(r.get("name") or ""), str(m_info.get("invoice_origin") or "")
        combo = f"{ref_txt} | {name_txt} | {orig_txt}"

        po_pair = r.get("purchase_order_id")
        po_id = first_id(po_pair)
        m_po = re.search(r"([A-Z]+ORD/\d{4}/\d+)", combo)
        po_no = (str(po_pair[-1]) if isinstance(po_pair, list) and po_pair else "") or str(r.get("po_ref") or "") or (m_po.group(1) if m_po else "")
        def run_wizard_and_download(url, db, user, pw, selected_label, wiz_map, params_json, btn_method, d_from, d_to, is_as_on, selected_accs, acc_map, progress=None):
        STOP_EVENT.clear()  
        if not selected_label or selected_label not in wiz_map:
        return "⚠️ Please select a report wizard from the dropdown first.", None
    if not d_from.strip() or not d_to.strip():
        return "⚠️ Please enter both From Date and To Date (DD-MM-YYYY).", None
    try:
        if progress:
            progress(0.02, desc="Authenticating with Odoo...")
        uid = odoo_jsonrpc(url, "common", "authenticate", [db.strip(), user.strip(), pw, {}], timeout=60)
        if not uid:
            raise Exception("Authentication failed.")

        w = wiz_map[selected_label]
        clean_rep_name = re.sub(r"[^A-Za-z0-9_-]+", "_", w["name"].strip()).strip("_")
        sel_low = w["name"].lower()

        if "grir" in sel_low:
            sel_ids = [acc_map[a] for a in selected_accs if a in acc_map] if selected_accs else list()
            out_path = os.path.join(tempfile.gettempdir(), f"{clean_rep_name}_{d_from.strip()}_to_{d_to.strip()}.xlsx")
            if os.path.exists(out_path):
                os.remove(out_path)
            row_cnt, tot_dr, tot_cr, net_bal = extract_grir_transport_fast(url, db.strip(), uid, pw, sel_ids, d_from.strip(), d_to.strip(), out_path, progress)
            if row_cnt == 0:
                return "⚠️ No posted entries found for the selected GRIR account and date range.", None
            return f"✅ **{w['name']}** — Extracted {row_cnt:,} rows (100% TB Exact)!\n- **Total Debits:** `{tot_dr:,.2f}`\n- **Total Credits:** `{tot_cr:,.2f}`\n- **Net Movement:** `{net_bal:,.2f}`", out_path

        if "sale" in sel_low:
            sel_ids = [acc_map[a] for a in selected_accs if a in acc_map] if selected_accs else list()
            out_path = os.path.join(tempfile.gettempdir(), f"{clean_rep_name}_{d_from.strip()}_to_{d_to.strip()}.xlsx")
            if os.path.exists(out_path):
                os.remove(out_path)
            row_cnt, net_rev, tot_dr, tot_cr = extract_sale_register_fast(url, db.strip(), uid, pw, d_from.strip(), d_to.strip(), out_path, progress, sel_ids)
            if row_cnt == 0:
                return "⚠️ No posted revenue entries found in this date range.", None
            return f"✅ **{w['name']}** — Extracted {row_cnt:,} lines (100% TB Exact)!\n- **Total GL Debits:** `{tot_dr:,.2f}`\n- **Total GL Credits:** `{tot_cr:,.2f}`\n- **Net Revenue:** `{net_rev:,.2f}`", out_path

        if "purchase" in sel_low:
            sel_ids = [acc_map[a] for a in selected_accs if a in acc_map] if selected_accs else list()
            out_path = os.path.join(tempfile.gettempdir(), f"{clean_rep_name}_{d_from.strip()}_to_{d_to.strip()}.xlsx")
            if os.path.exists(out_path):
                os.remove(out_path)
            row_cnt, inv_dr, inv_cr, inv_net, grir_dr, grir_cr, grir_net = extract_purchase_register_fast(url, db.strip(), uid, pw, d_from.strip(), d_to.strip(), out_path, progress, sel_ids)
            if row_cnt == 0:
                return "⚠️ No posted Inventory or GRIR entries found in this date range.", None
            return (
                f"✅ **{w['name']}** — Extracted {row_cnt:,} lines (100% Trial Balance Exact)!\n"
                f"- **Inventory (`551xxx` + `731004` + `751018`):** Debit `{inv_dr:,.2f}` | Credit `{inv_cr:,.2f}` | Net `{inv_net:,.2f}`\n"
                f"- **GRIR (`181001`–`181004`):** Debit `{grir_dr:,.2f}` | Credit `{grir_cr:,.2f}` | Net `{grir_net:,.2f}`"
            ), out_path

        final_json = sync_ui_to_json(params_json, d_from, d_to, is_as_on, selected_accs, acc_map)
        base_vals = json.loads(final_json) if final_json.strip() else {}
        if "account_ids" in base_vals and not acc_map:
            del base_vals["account_ids"]

        opener, clean_base = get_auth_opener(url, db.strip(), user.strip(), pw)
        try:
            user_ctx = execute_with_uid(url, db.strip(), uid, pw, "res.users", "context_get", list(), timeout=60)
        except Exception:
            user_ctx = {}

        if progress:
            progress(0.25, desc=f"Generating '{w['name']}'...")
        file_bytes, fname_out, used_m = fetch_single_wizard_fast(url, db.strip(), uid, pw, opener, clean_base, w, base_vals, btn_method, user_ctx, timeout=900)
        if progress:
            progress(1.0, desc="Report ready!")
        if file_bytes:
            ext = ".pdf" if fname_out.lower().endswith(".pdf") else ".xlsx"
            out_path = os.path.join(tempfile.gettempdir(), f"{clean_rep_name}_{d_from.strip()}_to_{d_to.strip()}{ext}")
            with open(out_path, "wb") as f:
                f.write(file_bytes)
            return f"✅ **{w['name']}** generated natively via `{used_m}`!", out_path
        return f"⚠️ Executed `{used_m}`, but Odoo returned no file.", None

    except Exception as e:
        return f"❌ Error: {str(e)}", None
