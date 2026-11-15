import os

import gradio as gr
import urllib.request, urllib.error, http.cookiejar, json, base64, tempfile, os
import xml.etree.ElementTree as ET
from datetime import date
from urllib.parse import urlparse

def odoo_jsonrpc(url, service, method, args, timeout=300):
    clean_url = url.strip().rstrip("/")
    target_url = f"{clean_url}/jsonrpc"
    payload = {"jsonrpc": "2.0", "method": "call", "params": {"service": service, "method": method, "args": args}, "id": 1}
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    for _ in range(5):
        req = urllib.request.Request(target_url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
                if "error" in result:
                    err_msg = result["error"].get("data", {}).get("message") or result["error"].get("message")
                    raise Exception(f"Odoo Error: {err_msg}")
                return result.get("result")
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                new_url = e.headers.get("Location")
                target_url = f"{urlparse(target_url).scheme}://{urlparse(target_url).netloc}{new_url}" if new_url.startswith("/") else new_url
            else:
                raise Exception(f"HTTP {e.code}: {e.reason}")
    raise Exception("Too many redirects.")

def authenticate_and_execute(url, db, username, password, model, method, args, kwargs=None):
    uid = odoo_jsonrpc(url, "common", "authenticate", [db, username, password, {}], timeout=30)
    if not uid:
        raise Exception("Authentication failed.")
    call_args = [db, uid, password, model, method, args]
    if kwargs is not None:
        call_args.append(kwargs)
    return odoo_jsonrpc(url, "object", "execute_kw", call_args, timeout=300)

def get_auth_opener(url, db, username, password):
    clean_url = url.strip().rstrip("/")
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", "Mozilla/5.0")]
    auth_payload = json.dumps({"jsonrpc": "2.0", "params": {"db": db, "login": username, "password": password}}).encode("utf-8")
    auth_req = urllib.request.Request(f"{clean_url}/web/session/authenticate", data=auth_payload, headers={"Content-Type": "application/json"}, method="POST")
    with opener.open(auth_req, timeout=30) as resp:
        auth_res = json.loads(resp.read().decode("utf-8"))
        if "error" in auth_res or not auth_res.get("result", {}).get("uid"):
            raise Exception("Web session authentication failed.")
    return opener, clean_url

def download_odoo_report_web(url, db, username, password, report_name, record_id, report_type="qweb-pdf"):
    opener, clean_url = get_auth_opener(url, db, username, password)
    route_prefix = "xlsx" if "xlsx" in report_type else "pdf"
    ids_str = ",".join(str(x) for x in record_id) if isinstance(record_id, list) else str(record_id)
    report_url = f"{clean_url}/report/{route_prefix}/{report_name}/{ids_str}"
    with opener.open(report_url, timeout=300) as report_resp:
        return report_resp.read()

print("✅ Part 1 loaded!")

from datetime import datetime

def to_odoo_date(date_str):
    s = str(date_str or "").strip()
    if not s:
        return ""
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s

def build_wizard_profile(url, db, uid, pw, w, cur_from="", cur_to=""):
    """Inspects only the VISIBLE form fields on Odoo's wizard popup, plus defaults and buttons."""
    model = w["res_model"]
    f_info = execute_with_uid(url, db, uid, pw, model, "fields_get", [], {"attributes": ["string", "type", "relation", "selection", "required", "domain"]}, timeout=60)
    ignore = {"id", "create_uid", "create_date", "write_uid", "write_date", "__last_update", "display_name"}

    v_raw = w.get("view_id")
    v_id = next(iter(v_raw), False) if isinstance(v_raw, list) else False
    arch = ""
    try:
        arch = execute_with_uid(url, db, uid, pw, model, "get_views", [], {"views": [[v_id, "form"]]}, timeout=60).get("views", {}).get("form", {}).get("arch", "")
    except Exception:
        try:
            arch = execute_with_uid(url, db, uid, pw, model, "fields_view_get", [], {"view_id": v_id, "view_type": "form"}, timeout=60).get("arch", "")
        except Exception:
            pass

    btns, visible_names = [], []
    if arch:
        root = ET.fromstring(arch)
        btns = [b.attrib["name"] for b in root.iter("button") if b.attrib.get("name") and b.attrib.get("invisible") != "1"]
        for fld in root.iter("field"):
            fn = fld.attrib.get("name")
            if fn and fn in f_info and fn not in ignore and fld.attrib.get("invisible") != "1":
                if fn not in visible_names:
                    visible_names.append(fn)

    # Fallback to all non-binary fields only if no form XML was returned
    valid_fields = visible_names if visible_names else [k for k in f_info if k not in ignore and not k.startswith("message_") and f_info[k].get("type") != "binary"]

    try:
        default_vals = execute_with_uid(url, db, uid, pw, model, "default_get", [valid_fields], timeout=60) or {}
    except Exception:
        default_vals = {}

    field_summary, start_fields, end_fields, acc_map = [], [], [], {}
    for fname in valid_fields:
        meta = f_info[fname]
        ftype, label = meta.get("type"), meta.get("string") or fname
        req = bool(meta.get("required"))
        req_tag = " [REQUIRED]" if req else ""
        field_summary.append(f"- `{fname}` ({ftype}{req_tag}): {label}")

        if ftype in ("date", "datetime"):
            low_all = f"{fname.lower()} {label.lower()}"
            is_start = any(x in low_all for x in ("from", "start", "begin"))
            (start_fields if is_start else end_fields).append((fname, ftype))
            raw_d = to_odoo_date(cur_from) if is_start else to_odoo_date(cur_to)
            if raw_d:
                default_vals[fname] = f"{raw_d} 00:00:00" if ftype == "datetime" else raw_d
        elif ftype == "selection" and meta.get("selection") and fname not in default_vals:
            valid = [item for item in meta["selection"] if isinstance(item, (list, tuple)) and len(item) >= 2]
            if valid:
                default_vals[fname] = valid
        elif ftype == "many2one" and meta.get("relation") and req and not default_vals.get(fname):
            try:
                recs = execute_with_uid(url, db, uid, pw, meta["relation"], "search_read", [[]], {"fields": ["id"], "limit": 1}, timeout=30)
                if recs:
                    default_vals[fname] = recs["id"]
            except Exception:
                pass
        elif ftype in ("many2one", "many2many") and meta.get("relation") == "account.account":
            try:
                dom = [["name", "ilike", "GRIR"]] if "grir" in model.lower() or "grir" in w["name"].lower() else []
                accs = execute_with_uid(url, db, uid, pw, "account.account", "search_read", [dom], {"fields": ["id", "display_name"], "limit": 200}, timeout=30)
                if not accs:
                    accs = execute_with_uid(url, db, uid, pw, "account.account", "search_read", [[]], {"fields": ["id", "display_name"], "limit": 200}, timeout=30)
                acc_map = {f"{a['display_name']} (ID:{a['id']})": a["id"] for a in accs}
            except Exception:
                pass

    return {
        "valid_fields": valid_fields,
        "start_fields": start_fields,
        "end_fields": end_fields,
        "defaults": default_vals,
        "buttons": btns,
        "summary": field_summary,
        "acc_map": acc_map,
    }

def connect_and_discover(url, db, user, pw, progress=gr.Progress()):
    try:
        progress(0.05, desc="Authenticating & discovering wizards...")
        uid = odoo_jsonrpc(url, "common", "authenticate", [db, user, pw, {}], timeout=60)
        if not uid:
            raise Exception("Authentication failed.")
        wizards = execute_with_uid(url, db, uid, pw, "ir.actions.act_window", "search_read", [[["target", "=", "new"], ["res_model", "!=", False]]], {"fields": ["id", "name", "res_model", "view_id"], "limit": 500}, timeout=120)
        keywords = ["report", "ledger", "register", "grir", "aged", "tax", "statement", "summary", "breakup", "mis", "provision", "reconcile", "efd", "tansad", "kardex", "payroll", "employee", "stock"]
        filtered = [w for w in wizards if any(k in w["name"].lower() for k in keywords)]
        final_list = filtered if filtered else wizards

        wiz_map = {}
        total = len(final_list)
        for idx, w in enumerate(final_list):
            if idx % 5 == 0:
                progress(0.1 + 0.85 * (idx / max(total, 1)), desc=f"Testing wizard fields ({idx}/{total}): {w['name']}...")
            label = f"{w['name']} (Action ID: {w['id']} | {w['res_model']})"
            try:
                w["profile"] = build_wizard_profile(url, db, uid, pw, w)
            except Exception as prof_err:
                w["profile"] = {"error": str(prof_err), "valid_fields": [], "start_fields": [], "end_fields": [], "defaults": {}, "buttons": [], "summary": [], "acc_map": {}}
            wiz_map[label] = w

        choices = sorted(list(wiz_map.keys()))
        first_choice = next(iter(choices), None)
        progress(1.0, desc="All wizards tested and mapped!")
        return f"✅ Connected & tested **{len(choices)} wizards**! Every wizard's exact fields & defaults are now cached.", gr.update(choices=choices, value=first_choice), wiz_map
    except Exception as e:
        return f"❌ Error: {str(e)}", gr.update(choices=[]), {}

def inspect_wizard(url, db, user, pw, selected_label, wiz_map, cur_from, cur_to):
    if not selected_label or selected_label not in wiz_map:
        return "⚠️ Please select a wizard first.", "{}", "", False, gr.update(choices=[], value=[]), {}
    w = wiz_map[selected_label]
    prof = w.get("profile")
    if not prof or prof.get("error"):
        try:
            uid = odoo_jsonrpc(url, "common", "authenticate", [db, user, pw, {}], timeout=60)
            prof = build_wizard_profile(url, db, uid, pw, w, cur_from, cur_to)
            w["profile"] = prof
        except Exception as e:
            return f"❌ Error: {str(e)}", "{}", "", False, gr.update(choices=[], value=[]), {}

    vals = dict(prof.get("defaults", {}))
    clean_from, clean_to = to_odoo_date(cur_from), to_odoo_date(cur_to)
    if clean_from:
        for fname, ftype in prof.get("start_fields", []):
            vals[fname] = f"{clean_from} 00:00:00" if ftype == "datetime" else clean_from
    if clean_to:
        for fname, ftype in prof.get("end_fields", []):
            vals[fname] = f"{clean_to} 23:59:59" if ftype == "datetime" else clean_to

    btns = prof.get("buttons", [])
    first_btn = next(iter(btns), "action_print_report")
    acc_map = prof.get("acc_map", {})
    info_md = (
        f"### Wizard: **{w['name']}** (`{w['res_model']}`)\n"
        f"**Detected Buttons:** `{', '.join(btns) if btns else 'None (auto-trying standard methods)'}`\n\n"
        f"**Exact Model Fields:**\n" + "\n".join(prof.get("summary", []))
    )
    return info_md, json.dumps(vals, indent=2), first_btn, bool(vals.get("is_as_on", False)), gr.update(choices=sorted(list(acc_map.keys())), value=[]), acc_map

def sync_ui_to_json(params_json, d_from, d_to, is_as_on, selected_accs, acc_map, selected_label=None, wiz_map=None):
    try:
        data = json.loads(params_json) if params_json.strip() else {}
    except Exception:
        data = {}

    prof = (wiz_map or {}).get(selected_label, {}).get("profile", {})
    valid_set = set(prof.get("valid_fields", []))

    # Strip any invalid fields that do not exist on this wizard model
    if valid_set:
        data = {k: v for k, v in data.items() if k in valid_set}

    clean_from, clean_to = to_odoo_date(d_from), to_odoo_date(d_to)
    if clean_from:
        for fname, ftype in prof.get("start_fields", []):
            data[fname] = f"{clean_from} 00:00:00" if ftype == "datetime" else clean_from
    if clean_to:
        for fname, ftype in prof.get("end_fields", []):
            data[fname] = f"{clean_to} 23:59:59" if ftype == "datetime" else clean_to

    if "is_as_on" in data or "is_as_on" in valid_set:
        data["is_as_on"] = bool(is_as_on)
    if selected_accs and acc_map and (not valid_set or "account_ids" in valid_set):
        ids = [acc_map[a] for a in selected_accs if a in acc_map]
        data["account_ids"] = [[6, 0, ids]]
    return json.dumps(data, indent=2)

print("✅ Cell 2A loaded!")


import pandas as pd
import io, time, gc, xlsxwriter
from datetime import datetime, timedelta

def execute_with_uid(url, db, uid, pw, model, method, args, kwargs=None, timeout=900):
    call_args = [db, uid, pw, model, method, args]
    if kwargs is not None:
        call_args.append(kwargs)
    return odoo_jsonrpc(url, "object", "execute_kw", call_args, timeout=timeout)

def fetch_single_wizard_fast(url, db, uid, pw, opener, clean_base, w, wiz_vals, btn_method, user_ctx, timeout=900):
    # Strictly filter wiz_vals to fields that exist on this exact Odoo model
    f_info = execute_with_uid(url, db, uid, pw, w["res_model"], "fields_get", [], {"attributes": ["type"]}, timeout=timeout)
    safe_vals = {k: v for k, v in wiz_vals.items() if k in f_info}

    wiz_id = execute_with_uid(url, db, uid, pw, w["res_model"], "create", [safe_vals], {"context": user_ctx}, timeout=timeout)

    prof_btns = w.get("profile", {}).get("buttons", [])
    candidates = list(dict.fromkeys(
        ([btn_method.strip()] if btn_method and btn_method.strip() else [])
        + prof_btns
        + ["action_print_report", "print_report", "generate_report", "export_xls", "action_print_xlsx", "check_report"]
    ))

    res_action, used_m = None, None
    last_err = None
    for m in candidates:
        try:
            res_action = execute_with_uid(url, db, uid, pw, w["res_model"], m, [[wiz_id]], {"context": user_ctx}, timeout=timeout)
            used_m = m
            break
        except Exception as e:
            err_s = str(e)
            if "cannot marshal None" in err_s:
                used_m = m
                break
            if "has no attribute" in err_s or "does not exist" in err_s:
                last_err = e
                continue
            raise

    if used_m is None and last_err:
        raise last_err

    file_bytes, fname_out = None, f"{w['name'].replace(' ', '_')}.xlsx"
    if isinstance(res_action, dict) and res_action.get("report_name"):
        rep_type = res_action.get("report_type", "qweb-pdf")
        doc_ids = res_action.get("context", {}).get("active_ids") or [wiz_id]
        route_prefix = "xlsx" if "xlsx" in rep_type else ("html" if "html" in rep_type else "pdf")
        ids_str = ",".join(str(x) for x in doc_ids) if isinstance(doc_ids, list) else str(doc_ids)
        report_url = f"{clean_base}/report/{route_prefix}/{res_action['report_name']}/{ids_str}"
        with opener.open(report_url, timeout=timeout) as report_resp:
            file_bytes = report_resp.read()
        ext = "xlsx" if "xlsx" in rep_type else ("html" if "html" in rep_type else "pdf")
        fname_out = f"{w['name'].replace(' ', '_')}.{ext}"
    elif isinstance(res_action, dict) and res_action.get("url"):
        dl_url = res_action["url"]
        full_url = dl_url if dl_url.startswith("http") else f"{clean_base}{dl_url}"
        with opener.open(full_url, timeout=timeout) as report_resp:
            file_bytes = report_resp.read()
        url_tail = dl_url.split("/")[-1]
        fname_out = next(iter(url_tail.split("?")), fname_out) or fname_out
    else:
        bin_fields = [k for k, v in f_info.items() if v.get("type") == "binary"]
        char_fields = [k for k, v in f_info.items() if v.get("type") == "char"]
        recs = execute_with_uid(url, db, uid, pw, w["res_model"], "read", [[wiz_id]], {"fields": bin_fields + char_fields, "context": user_ctx}, timeout=timeout)
        rec = next(iter(recs), None) if isinstance(recs, list) else (recs if isinstance(recs, dict) else None)
        if isinstance(rec, dict):
            fname_out = next((rec[cf] for cf in char_fields if rec.get(cf) and "." in str(rec[cf])), fname_out)
            for bf in bin_fields:
                if rec.get(bf):
                    file_bytes = base64.b64decode(rec[bf])
                    break
        if not file_bytes:
            atts = execute_with_uid(url, db, uid, pw, "ir.attachment", "search_read", [[["res_model", "=", w["res_model"]], ["res_id", "=", wiz_id]]], {"fields": ["name", "datas"], "limit": 1, "context": user_ctx}, timeout=timeout)
            att = next(iter(atts), None) if isinstance(atts, list) else (atts if isinstance(atts, dict) else None)
            if isinstance(att, dict) and att.get("datas"):
                file_bytes = base64.b64decode(att["datas"])
                fname_out = att["name"]

    return file_bytes, fname_out, used_m

print("✅ Cell 3 loaded!")

import threading, re
from datetime import datetime
import json, tempfile, os, base64
import xlsxwriter
import gradio as gr

STOP_EVENT = threading.Event()

def stop_generation():
    STOP_EVENT.set()
    return "🛑 Stop signal sent!"

def rpc_fetch_all(url, db, uid, pw, model, domain, fields, batch_size=2000, progress=None, label="Fetching", p_start=0.1, p_end=0.5):
    total = execute_with_uid(url, db, uid, pw, model, "search_count", [domain], timeout=120)
    if not total:
        return list()
    out = list()
    offset = 0
    while offset < total:
        if STOP_EVENT.is_set():
            break
        if progress:
            frac = p_start + (p_end - p_start) * (offset / max(total, 1))
            progress(frac, desc=f"{label}: {offset:,} / {total:,} records...")
        chunk = execute_with_uid(url, db, uid, pw, model, "search_read", [domain], {"fields": fields, "offset": offset, "limit": batch_size, "order": "id asc"}, timeout=180)
        if not chunk:
            break
        out.extend(chunk)
        offset += len(chunk)
    return out

def extract_grir_transport_fast(url, db, uid, pw, acc_ids, d_from, d_to, out_xlsx_path, progress):
    d1_str = to_odoo_date(d_from)
    d2_str = to_odoo_date(d_to)
    dom = [["parent_state", "=", "posted"], ["date", ">=", d1_str], ["date", "<=", d2_str]]
    if acc_ids:
        dom.append(["account_id", "in", acc_ids])
    else:
        dom.extend([["account_id.name", "ilike", "GRIR"], ["account_id.name", "ilike", "TRANSPORT"]])

    progress(0.08, desc="Fetching posted GRIR Transport ledger items...")
    aml_fields = ["id", "date", "move_id", "move_name", "ref", "name", "po_ref", "partner_id", "account_id", "debit", "credit", "balance"]
    aml_rows = rpc_fetch_all(url, db, uid, pw, "account.move.line", dom, aml_fields, 2500, progress, "GRIR Ledger Lines", 0.10, 0.45)

    if not aml_rows:
        return 0, 0.0, 0.0, 0.0

    lc_by_grn = {}
    all_lc_rows = list()
    bill_move_ids = set()
    bill_lines = list()

    for r in aml_rows:
        ref_txt = str(r.get("ref") or "")
        name_txt = str(r.get("name") or "")
        combo = f"{ref_txt} | {name_txt}"
        m_lc = re.search(r"(LC/\\d{4}/\\d+)", combo)
        m_grn = re.search(r"(CM/GRN/\\d{4}/\\d+)", combo)
        m_po = re.search(r"([A-Z]+ORD/\\d{4}/\\d+)", combo)
        lc_no = m_lc.group(1) if m_lc else ""
        grn_no = m_grn.group(1) if m_grn else ""
        po_no = m_po.group(1) if m_po else str(r.get("po_ref") or "")
        p_name = r["partner_id"][1] if isinstance(r.get("partner_id"), list) else ""
        p_clean = re.sub(r"^\\[.*?\\]\\s*", "", p_name)

        entry = {
            "id": r["id"],
            "date": str(r.get("date") or ""),
            "move_id": r["move_id"][0] if isinstance(r.get("move_id"), list) else False,
            "move_name": str(r.get("move_name") or ""),
            "ref": ref_txt,
            "name": name_txt,
            "lc": lc_no,
            "grn": grn_no,
            "po": po_no,
            "partner": p_clean,
            "debit": float(r.get("debit") or 0.0),
            "credit": float(r.get("credit") or 0.0),
            "balance": float(r.get("balance") or 0.0),
        }
        if entry["debit"] > 0 and entry["move_id"]:
            bill_move_ids.add(entry["move_id"])
            bill_lines.append(entry)
        else:
            all_lc_rows.append(entry)
            if grn_no:
                lc_by_grn.setdefault(grn_no, list()).append(entry)

    progress(0.50, desc=f"Linking {len(bill_move_ids):,} Vendor Bills to TBS records...")
    move_map = {}
    tbs_ids = set()
    m_id_list = list(bill_move_ids)
    for i in range(0, len(m_id_list), 2000):
        batch_ids = m_id_list[i:i+2000]
        m_recs = execute_with_uid(url, db, uid, pw, "account.move", "read", [batch_ids], {"fields": ["id", "name", "ref", "invoice_date", "invoice_origin", "tbill_purchase_id", "partner_id"]}, timeout=180)
        for m in m_recs:
            move_map[m["id"]] = m
            if isinstance(m.get("tbill_purchase_id"), list):
                tbs_ids.add(m["tbill_purchase_id"][0])

    progress(0.65, desc=f"Fetching GRN links for {len(tbs_ids):,} TBS settlements...")
    tbs_lines_by_settle = {}
    t_id_list = list(tbs_ids)
    for i in range(0, len(t_id_list), 500):
        batch_tids = t_id_list[i:i+500]
        t_lines = execute_with_uid(
            url, db, uid, pw, "transporter.bill.purchase.line", "search_read",
            [[["settlement_id", "in", batch_tids]]],
            {"fields": ["settlement_id", "grn_nos", "grn_date", "vendor", "partner_id", "purchase_orders", "product", "qty_ton", "freight_amount", "vendor_bill_no", "vendor_bill_date"], "limit": 50000},
            timeout=180
        )
        for tl in t_lines:
            sid = tl["settlement_id"][0] if isinstance(tl.get("settlement_id"), list) else False
            if sid:
                tbs_lines_by_settle.setdefault(sid, list()).append(tl)

    progress(0.82, desc="Building Trial-Balance-exact GRIR Transport workbook...")
    workbook = xlsxwriter.Workbook(out_xlsx_path, {"constant_memory": True})
    ws_main = workbook.add_worksheet("GRIR_Transport_Breakup")
    ws_tb = workbook.add_worksheet("Trial_Balance_Check")
    ws_raw = workbook.add_worksheet("Raw_GRIR_Ledger_Lines")

    headers_main = [
        "TBS Bill No", "TBS Number", "TBS Vendor", "TBS Bill Date", "Posting Date",
        "LC", "LC Date", "Year (LC Date)", "Month (LC Date)",
        "Partner Name (GRN Vendor)", "Transport Name", "GRN", "PO Number", "Product",
        "Sum of TBS Amount", "Sum of LC Amount", "Sum of Bill Amount", "Net Amount", "Status"
    ]
    ws_main.write_row(0, 0, headers_main)

    curr_row = 1
    matched_lc_ids = set()

    for bl in bill_lines:
        m_info = move_map.get(bl["move_id"], {})
        bill_no = m_info.get("name") or bl["move_name"]
        tbs_pair = m_info.get("tbill_purchase_id")
        tbs_id = tbs_pair[0] if isinstance(tbs_pair, list) else False
        tbs_num = tbs_pair[1] if isinstance(tbs_pair, list) else str(m_info.get("invoice_origin") or "")
        tbs_vnd_inv = str(m_info.get("ref") or bl["ref"] or "")
        tbs_bill_dt = str(m_info.get("invoice_date") or bl["date" ] or "")
        trans_name = bl["partner"]

        linked_grn_lines = tbs_lines_by_settle.get(tbs_id, list()) if tbs_id else list()
        if linked_grn_lines:
            for idx_g, gl in enumerate(linked_grn_lines):
                grn_code = str(gl.get("grn_nos") or "").strip()
                grn_vendor = str(gl.get("vendor") or "").strip()
                po_code = str(gl.get("purchase_orders") or bl["po"] or "").strip()
                prod_name = str(gl.get("product") or "").strip()
                t_name = re.sub(r"^\\[.*?\\]\\s*", "", gl["partner_id"][1]) if isinstance(gl.get("partner_id"), list) else trans_name

                lc_matches = lc_by_grn.get(grn_code, list())
                lc_code = ", ".join(dict.fromkeys(x["lc"] for x in lc_matches if x["lc"]))
                lc_dt = next((x["date" ] for x in lc_matches if x["date"]), "")
                lc_yr, lc_mo = "", ""
                if len(lc_dt) >= 7:
                    try:
                        dt_obj = datetime.strptime(lc_dt[:10], "%Y-%m-%d")
                        lc_yr, lc_mo = dt_obj.year, dt_obj.strftime("%b")
                    except Exception:
                        lc_yr = lc_dt[:4]

                lc_amt = sum(x["balance"] for x in lc_matches if x["id"] not in matched_lc_ids)
                for x in lc_matches:
                    matched_lc_ids.add(x["id"])

                tbs_amt = bl["debit"] if idx_g == 0 else 0.0
                net_amt = tbs_amt + lc_amt
                status = "Matched (Bill + LC)" if lc_matches else "Bill Posted (No LC in Range)"

                ws_main.write_row(curr_row, 0, [
                    bill_no, tbs_num, tbs_vnd_inv, tbs_bill_dt, bl["date"],
                    lc_code, lc_dt, lc_yr, lc_mo,
                    grn_vendor, t_name, grn_code, po_code, prod_name,
                    tbs_amt, lc_amt, 0.0, net_amt, status
                ])
                curr_row += 1
        else:
            ws_main.write_row(curr_row, 0, [
                bill_no, tbs_num, tbs_vnd_inv, tbs_bill_dt, bl["date"],
                bl["lc"], "", "", "",
                "", trans_name, bl["grn"], bl["po"], bl["name"],
                bl["debit"], 0.0, 0.0, bl["debit"], "Bill Posted (Direct)"
            ])
            curr_row += 1

    for lc_r in all_lc_rows:
        if lc_r["id"] in matched_lc_ids:
            continue
        lc_dt = lc_r["date"]
        lc_yr, lc_mo = "", ""
        if len(lc_dt) >= 7:
            try:
                dt_obj = datetime.strptime(lc_dt[:10], "%Y-%m-%d")
                lc_yr, lc_mo = dt_obj.year, dt_obj.strftime("%b")
            except Exception:
                lc_yr = lc_dt[:4]
        ws_main.write_row(curr_row, 0, [
            "", "", "", "", lc_dt,
            lc_r["lc"], lc_dt, lc_yr, lc_mo,
            lc_r["partner"], "", lc_r["grn"], lc_r["po"], lc_r["name"],
            0.0, lc_r["balance"], 0.0, lc_r["balance"], "Pending GRN / Unbilled LC"
        ])
        curr_row += 1

    tot_debit = sum(x["debit"] for x in aml_rows)
    tot_credit = sum(x["credit" ] for x in aml_rows)
    net_gl = sum(x["balance"] for x in aml_rows)

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
        m_g = re.search(r"(CM/GRN/\\d{4}/\\d+)", f"{r.get('ref') or ''} | {r.get('name') or ''}")
        g_code = m_g.group(1) if m_g else ""
        tl = grn_meta.get(g_code, {})
        t_raw = tl.get("partner_id")
        t_name = re.sub(r"^\\[.*?\\]\\s*", "", str(t_raw[-1])) if isinstance(t_raw, list) and t_raw else (re.sub(r"^\\[.*?\\]\\s*", "", prt_lbl) if r.get("debit", 0) > 0 else "")
        ws_raw.write_row(idx_r, 0, [r["id"], r["date"], r["move_name"], acc_lbl, prt_lbl, r["ref"], r["name"], g_code, str(tl.get("grn_date") or ""), str(tl.get("vendor") or ""), t_name, str(tl.get("reference_no") or ""), str(tl.get("token_date") or ""), r["debit"], r["credit"], r["balance"]])

    workbook.close()
    progress(1.0, desc=f"Done! {curr_row - 1:,} rows exported.")
    return curr_row - 1, tot_debit, tot_credit, net_gl

def run_wizard_and_download(url, db, user, pw, selected_label, wiz_map, params_json, btn_method, d_from, d_to, is_as_on, selected_accs, acc_map, progress=gr.Progress()):
    try:
        if not selected_label or selected_label not in wiz_map:
            raise Exception("Please select a valid wizard report first.")

        progress(0.05, desc="Starting execution sequence...")
        w = wiz_map[selected_label]
        try:
            wiz_vals = json.loads(params_json) if params_json.strip() else {}
        except Exception:
            raise Exception("Invalid JSON payload format in the parameter field.")

        uid = odoo_jsonrpc(url, "common", "authenticate", [db, user, pw, {}], timeout=60)
        if not uid:
            raise Exception("Authentication failed.")

        opener, clean_base = get_auth_opener(url, db, user, pw)
        user_ctx = execute_with_uid(url, db, uid, pw, "res.users", "context_get", [], timeout=45) or {}

        file_bytes, fname_out, used_method = fetch_single_wizard_fast(
            url, db, uid, pw, opener, clean_base, w, wiz_vals, btn_method, user_ctx, timeout=900
        )

        if not file_bytes:
            raise Exception("Odoo finished execution but did not return any report document or file bytes.")

        out_dir = tempfile.gettempdir()
        out_path = os.path.join(out_dir, fname_out)
        with open(out_path, "wb") as f:
            f.write(file_bytes)

        return f"✅ Successfully generated report using method `{used_method}`! Ready for download.", out_path
    except Exception as e:
        return f"❌ Generation failed: {str(e)}", None

def disconnect_session():
    return "", "ℹ Session cleared successfully.", gr.update(choices=[], value=None), {}, "No report selected yet.", "{}", gr.update(choices=[], value=[]), {}, "Ready for instruction", None

# --- Styled Dynamic Theme block with flashing/glow animated logo & modern banner ---
custom_css = """
html, body, .gradio-container {
    background-color: #0b111e !important;
    background-image:
        radial-gradient(at 0% 0%, rgba(45, 212, 191, 0.1) 0, transparent 40%),
        radial-gradient(at 100% 100%, rgba(99, 102, 241, 0.1) 0, transparent 40%),
        linear-gradient(to right, rgba(255, 255, 255, 0.03) 1px, transparent 1px),
        linear-gradient(to bottom, rgba(255, 255, 255, 0.03) 1px, transparent 1px) !important;
    background-size: 100% 100%, 100% 100%, 35px 35px, 35px 35px !important;
    color: #f1f5f9 !important;
}

@keyframes flashingLogo {
    0%, 100% { filter: drop-shadow(0 0 10px #2dd4bf) drop-shadow(0 0 25px rgba(45, 212, 191, 0.6)); opacity: 0.95; }
    50% { filter: drop-shadow(0 0 25px #6366f1) drop-shadow(0 0 45px rgba(99, 102, 241, 0.8)); opacity: 1; }
}
@keyframes neonBorderPulse {
    0%, 100% { border-color: rgba(45, 212, 191, 0.6); box-shadow: 0 0 20px rgba(45, 212, 191, 0.25); }
    50% { border-color: rgba(99, 102, 241, 0.9); box-shadow: 0 0 35px rgba(99, 102, 241, 0.5); }
}
.glow-banner {
    background: linear-gradient(135deg, #090f1c 0%, #111a2e 50%, #050a14 100%);
    border-radius: 18px;
    border: 2px solid rgba(45, 212, 191, 0.6);
    padding: 2.5rem;
    text-align: center;
    animation: neonBorderPulse 5s infinite ease-in-out;
    margin-bottom: 2rem;
}
.logo-wrapper {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 15px;
    margin-bottom: 0.6rem;
}
.glow-logo {
    font-family: 'Courier New', Courier, monospace;
    font-weight: 900 !important;
    font-size: 3.8rem !important;
    background: linear-gradient(90deg, #2dd4bf, #38bdf8, #6366f1, #2dd4bf);
    background-size: 200% auto;
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    animation: flashingLogo 3s infinite ease-in-out;
    text-transform: uppercase;
    letter-spacing: 3px;
}
.glow-banner p {
    font-size: 1.25rem;
    color: #cbd5e1;
    margin: 0;
    font-weight: 600;
    letter-spacing: 1px;
}
"""

with gr.Blocks(title="MGR DOGRA - ENTERPRISE COCKPIT") as demo:
    # Dynamic Flashing Animated Logo Banner
    gr.HTML(
        """
        <div class='glow-banner'>
            <div class='logo-wrapper'>
                <span class='glow-logo'>✨ MGR DOGRA ✨</span>
            </div>
            <p>📋 ENTERPRISE COCKPIT • Odoo Unified Report & Ledger Center</p>
        </div>
        """
    )

    wiz_state, acc_state = gr.State({}), gr.State({})

    with gr.Accordion("🔑 Odoo Connection Setup", open=True) as conn_section:
        with gr.Row():
            in_url = gr.Textbox(label="Odoo ERP URL", value="https://erp.lakecement.co.tz/", placeholder="https://...")
            in_db = gr.Textbox(label="Database Name", value="parallel")
            in_user = gr.Textbox(label="Username / Email", placeholder="name@company.com")
            in_pass = gr.Textbox(label="Password", type="password")
        with gr.Row():
            btn_connect = gr.Button("🔓 Connect & Discover", variant="primary")
            btn_disconnect = gr.Button("🔒 Disconnect Session", variant="secondary")

    with gr.Group():
        status_box = gr.Markdown("ℹ *System offline. Fill in your credentials and click Connect.*")

    # Split layout: Left menu control panel / Right interactive console
    with gr.Row():
        with gr.Column(scale=5, variant="panel"):
            gr.Markdown("### 🛠️ Sidebar controls & Configuration")
            wiz_dropdown = gr.Dropdown(label="Select Wizard Report", choices=list())
            btn_load = gr.Button("⚙️ Fetch Wizard Profiles", variant="secondary")

            with gr.Row():
                in_from_date = gr.Textbox(label="From Date", placeholder="DD-MM-YYYY (e.g. 01-04-2024)")
                in_to_date = gr.Textbox(label="To Date", placeholder="DD-MM-YYYY (e.g. 31-10-2026)")

            in_as_on = gr.Checkbox(label="Print 'As On' Date (Historical)", value=False)
            in_accounts = gr.Dropdown(label="Filter Accounts (Optional)", choices=list(), multiselect=True)

        with gr.Column(scale=6, variant="panel"):
            gr.Markdown("### 🖥️ Active execution console")
            wiz_details = gr.Markdown("*Choose a report from the sidebar menu to populate details here.*")
            params_editor = gr.Code(label="Auto-Generated Wizard Payload (JSON)", language="json", value="{}", interactive=True)
            btn_method_in = gr.Textbox(label="Odoo Execution Method", value="action_print_report")

            gr.Markdown("### 🖨️ Action controllers")
            with gr.Row():
                btn_generate = gr.Button("🚀 Execute & Extract Ledger", variant="primary")
                btn_stop = gr.Button("⏹️ Cancel Request", variant="stop")

            gen_status = gr.Markdown("**Ready for instructions...**")
            download_file = gr.File(label="📥 Download Output Ledger File Here", interactive=False)

    btn_connect.click(connect_and_discover, [in_url, in_db, in_user, in_pass], [status_box, wiz_dropdown, wiz_state])
    btn_disconnect.click(disconnect_session, list(), [in_pass, status_box, wiz_dropdown, wiz_state, wiz_details, params_editor, in_accounts, acc_state, gen_status, download_file])

    wiz_dropdown.change(inspect_wizard, [in_url, in_db, in_user, in_pass, wiz_dropdown, wiz_state, in_from_date, in_to_date], [wiz_details, params_editor, btn_method_in, in_as_on, in_accounts, acc_state])
    btn_load.click(inspect_wizard, [in_url, in_db, in_user, in_pass, wiz_dropdown, wiz_state, in_from_date, in_to_date], [wiz_details, params_editor, btn_method_in, in_as_on, in_accounts, acc_state])

    for ctrl in [in_from_date, in_to_date, in_as_on, in_accounts]:
         ctrl.change(sync_ui_to_json, [params_editor, in_from_date, in_to_date, in_as_on, in_accounts, acc_state, wiz_dropdown, wiz_state], [params_editor])

    gen_event = btn_generate.click(run_wizard_and_download, [in_url, in_db, in_user, in_pass, wiz_dropdown, wiz_state, params_editor, btn_method_in, in_from_date, in_to_date, in_as_on, in_accounts, acc_state], [gen_status, download_file])
    btn_stop.click(stop_generation, list(), [gen_status], cancels=[gen_event])

gr.close_all()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    demo.launch(server_name="0.0.0.0", server_port=port, share=False)
