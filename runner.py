import os
import re
import json
import tempfile
from odoo_core import (
    STOP_EVENT,
    odoo_jsonrpc,
    execute_with_uid,
    get_auth_opener,
    sync_ui_to_json,
    fetch_single_wizard_fast,
)
from backend import (
    extract_grir_transport_fast,
    extract_sale_register_fast,
    extract_purchase_register_fast,
)

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
