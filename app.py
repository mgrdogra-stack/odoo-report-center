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
        m_lc = re.search(r"(LC/\d{4}/\d+)", combo)
        m_grn = re.search(r"(CM/GRN/\d{4}/\d+)", combo)
        m_po = re.search(r"([A-Z]+ORD/\d{4}/\d+)", combo)
        lc_no = m_lc.group(1) if m_lc else ""
        grn_no = m_grn.group(1) if m_grn else ""
        po_no = m_po.group(1) if m_po else str(r.get("po_ref") or "")
        p_name = r["partner_id"][1] if isinstance(r.get("partner_id"), list) else ""
        p_clean = re.sub(r"^\[.*?\].*", "", p_name)

        entry = {
            "id": r["id"],
            "date": str(r.get("date") or ""),
            "move_id": r["move_id"],
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
            bill_move_ids.add(entry["move_id"].get(0) if isinstance(entry["move_id"], list) else entry["move_id"])
            bill_lines.append(entry)
        else:
            all_lc_rows.append(entry)
            if grn_no:
                lc_by_grn.setdefault(grn_no, list()).append(entry)

    progress(0.50, desc="Linking bills...")
    move_map = {}
    tbs_ids = set()
    m_id_list = list(bill_move_ids)
    for i in range(0, len(m_id_list), 2000):
        batch_ids = m_id_list[i:i+2000]
        m_recs = execute_with_uid(url, db, uid, pw, "account.move", "read", [batch_ids], {"fields": ["id", "name", "ref", "invoice_date", "invoice_origin", "tbill_purchase_id", "partner_id"]}, timeout=180)
        for m in m_recs:
            move_map[m["id"]] = m

    workbook = xlsxwriter.Workbook(out_xlsx_path, {"constant_memory": True})
    ws_main = workbook.add_worksheet("GRIR_Transport_Breakup")
    workbook.close()
    return len(aml_rows), 0.0, 0.0, 0.0

def run_wizard_and_download(url, db, user, pw, selected_label, wiz_map, params_json, btn_method, d_from, d_to, is_as_on, selected_accs, acc_map, progress=gr.Progress()):
    try:
        if not selected_label or selected_label not in wiz_map:
            raise Exception("Please select a valid wizard report first.")

        progress(0.10, desc="Authenticating with Odoo...")
        uid = odoo_jsonrpc(url, "common", "authenticate", [db, user, pw, {}], timeout=60)
        opener, clean_base = get_auth_opener(url, db, user, pw)

        progress(0.30, desc="Loading wizard context & parameters...")
        w = wiz_map[selected_label]
        wiz_vals = json.loads(params_json) if params_json.strip() else {}
        user_ctx = execute_with_uid(url, db, uid, pw, "res.users", "context_get", [], timeout=45) or {}

        progress(0.55, desc=f"Executing '{w['name']}' on Odoo server (please wait)...")
        file_bytes, fname_out, used_method = fetch_single_wizard_fast(url, db, uid, pw, opener, clean_base, w, wiz_vals, btn_method, user_ctx, timeout=900)

        progress(0.90, desc="Downloading & saving report file...")
        out_path = os.path.join(tempfile.gettempdir(), fname_out)
        with open(out_path, "wb") as f:
            f.write(file_bytes)

        progress(1.0, desc="Done!")
        return f"✅ Successfully generated report ({fname_out})!", out_path
    except Exception as e:
        return f"❌ Generation failed: {str(e)}", None

def disconnect_session():
    return (
        "",
        "ℹ Session cleared successfully.",
        gr.update(choices=[], value=None),
        {},
        "*Choose a report from the sidebar menu to populate details here.*",
        "{}",
        gr.update(choices=[], value=[]),
        {},
        "**Ready for instructions...**",
        None,
    )

# Dynamic image loading & Base64 background mapping
image_b64 = ""
for candidate_path in [
    "accounting-vs-finance-differences-ohsc.webp",
    "/content/accounting-vs-finance-differences-ohsc.webp",
]:
    if os.path.exists(candidate_path):
        with open(candidate_path, "rb") as img_f:
            image_b64 = base64.b64encode(img_f.read()).decode("utf-8")
        break

custom_css = f"""
html, body, .gradio-container, .gradio-container-3-50, #component-0 {{
    background-color: #0b0f19 !important;
    background-image:
        linear-gradient(180deg, rgba(11, 15, 25, 0.25) 0%, rgba(11, 15, 25, 0.65) 100%),
        url('data:image/webp;base64,{image_b64}') !important;
    background-size: cover !important;
    background-position: center !important;
    background-attachment: fixed !important;
    color: #f8fafc !important;
}}

.gradio-container * {{
    font-family: 'Segoe UI', -apple-system, sans-serif !important;
}}

/* Make Gradio's Progress Bar & Loading Overlay clearly visible with Amber fill */
.gradio-container .wrap.default,
.gradio-container .progress-bar-wrap,
.gradio-container .toast-wrap {{
    background: rgba(15, 18, 28, 0.92) !important;
    backdrop-filter: blur(10px) !important;
    border: 1px solid rgba(251, 191, 36, 0.5) !important;
    border-radius: 8px !important;
    color: #ffffff !important;
    z-index: 9999 !important;
}}

/* Dark background track behind the moving bar */
.gradio-container .progress-level {{
    background: rgba(30, 41, 59, 0.85) !important;
    border-radius: 6px !important;
    overflow: hidden !important;
    height: 10px !important;
    border: 1px solid rgba(251, 191, 36, 0.3) !important;
}}

/* The actual moving progress bar fill in glowing Amber/Gold */
.gradio-container .progress-level-inner,
.gradio-container .progress-bar,
.gradio-container progress::-webkit-progress-value {{
    background: linear-gradient(90deg, #d97706 0%, #f59e0b 50%, #fde047 100%) !important;
    background-color: #f59e0b !important;
    box-shadow: 0 0 10px rgba(245, 158, 11, 0.75) !important;
    height: 100% !important;
    transition: width 0.3s ease !important;
}}

/* Percentage & status text inside the progress overlay */
.gradio-container .progress-text,
.gradio-container .meta-text {{
    color: #fcd34d !important;
    font-weight: 700 !important;
}}

.gradio-container .block,
.gradio-container .wrap,
.gradio-container .form,
.gradio-container .panel,
.gradio-container div[class*='svelte-'] {{
    background: transparent !important;
    background-color: transparent !important;
    border-color: rgba(255, 255, 255, 0.12) !important;
}}

/* Apply dark frosted glass ONLY to main cards, accordions, and panels */
.gradio-container .accordion,
.gradio-container div[class*='variant-panel'],
.gradio-container .gr-group {{
    background: rgba(11, 17, 32, 0.58) !important;
    backdrop-filter: blur(14px) !important;
    -webkit-backdrop-filter: blur(14px) !important;
    border: 1px solid rgba(56, 189, 248, 0.25) !important;
    border-radius: 12px !important;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.45) !important;
    padding: 12px !important;
}}

/* Fix Accordion Header ("Odoo Connection Setup") visibility */
.gradio-container .accordion > button,
.gradio-container .label-wrap {{
    background: rgba(15, 23, 42, 0.65) !important;
    color: #38bdf8 !important;
    font-weight: 700 !important;
    border-radius: 8px !important;
    padding: 8px 12px !important;
}}

/* Crisp, readable labels replacing awkward bright green */
.gradio-container label span, .gradio-container .form label {{
    color: #7dd3fc !important;
    font-weight: 700 !important;
    text-transform: uppercase !important;
    font-size: 0.8rem !important;
    letter-spacing: 0.6px !important;
}}

/* Clean translucent input fields */
.gradio-container input,
.gradio-container select,
.gradio-container textarea,
.gradio-container .secondary-wrap {{
    background: rgba(7, 11, 22, 0.72) !important;
    color: #ffffff !important;
    border: 1px solid rgba(125, 211, 252, 0.3) !important;
    border-radius: 6px !important;
}}

/* Refined Primary & Secondary Buttons */
.gradio-container button.primary {{
    background: linear-gradient(135deg, #0284c7 0%, #2563eb 100%) !important;
    color: #ffffff !important;
    font-weight: 700 !important;
    border: 1px solid rgba(125, 211, 252, 0.4) !important;
    box-shadow: 0 4px 15px rgba(2, 132, 199, 0.4) !important;
}}

.gradio-container button.secondary {{
    background: rgba(15, 23, 42, 0.75) !important;
    color: #e2e8f0 !important;
    font-weight: 600 !important;
    border: 1px solid rgba(148, 163, 184, 0.35) !important;
}}

.gradio-container button.secondary:hover {{
    background: rgba(30, 41, 59, 0.9) !important;
    border-color: #38bdf8 !important;
}}

@keyframes gearSpin {{
    0% {{ transform: rotate(0deg); }}
    100% {{ transform: rotate(360deg); }}
}}
@keyframes beltRoll {{
    0% {{ background-position-x: 0px; }}
    100% {{ background-position-x: 40px; }}
}}

.mechanical-roller {{
    height: 3px;
    width: 100%;
    background: repeating-linear-gradient(90deg, #38bdf8 0px, #38bdf8 10px, transparent 10px, transparent 20px);
    background-size: 40px 100%;
    animation: beltRoll 1.5s linear infinite;
    margin: 12px 0 0 0;
    border-radius: 2px;
    opacity: 0.8;
}}

.gear-icon {{
    display: inline-block;
    font-size: 2.2rem;
    color: #38bdf8;
    animation: gearSpin 6s linear infinite;
    line-height: 1;
}}

.glow-banner {{
    background: rgba(11, 17, 32, 0.55) !important;
    backdrop-filter: blur(12px) !important;
    border-radius: 12px !important;
    border: 1px solid rgba(56, 189, 248, 0.3) !important;
    padding: 1.5rem !important;
    text-align: center !important;
    margin-bottom: 1.2rem !important;
}}

.glow-logo {{
    font-weight: 800 !important;
    font-size: 2.6rem !important;
    color: #ffffff !important;
    letter-spacing: 3px !important;
}}

.glow-banner p {{
    font-size: 1rem !important;
    color: #cbd5e1 !important;
    margin: 0.4rem 0 0 0 !important;
    font-weight: 600 !important;
    letter-spacing: 1.2px !important;
}}
"""

with gr.Blocks(title="MGR DOGRA - ENTERPRISE COCKPIT") as demo:
    gr.HTML(
        """
        <div class='glow-banner'>
            <div style='display: flex; justify-content: center; align-items: center; gap: 15px;'>
                <span class='gear-icon'>⚙ </span>
                <span class='glow-logo'>MGR DOGRA</span>
                <span class='gear-icon' style='animation-direction: reverse;'>⚙ </span>
            </div>
            <p>ᄒ ENTERPRISE COCKPIT • ACCOUNTING LEDGER HUBS</p>
            <div class='mechanical-roller'></div>
        </div>
        """
    )

    wiz_state, acc_state = gr.State({}), gr.State({})

    with gr.Accordion("ᄑ Odoo Connection Setup", open=True) as conn_section:
        with gr.Row():
            in_url = gr.Textbox(label="Odoo ERP URL", value="https://erp.lakecement.co.tz/", placeholder="https://...")
            in_db = gr.Textbox(label="Database Name", value="parallel")
            in_user = gr.Textbox(label="Username / Email", placeholder="name@company.com")
            in_pass = gr.Textbox(label="Password", type="password")
        with gr.Row():
            btn_connect = gr.Button("ᄑ Connect & Discover", variant="primary")
            btn_disconnect = gr.Button("ᄑ Disconnect Session", variant="secondary")

    with gr.Group():
        status_box = gr.Markdown("ℹ *System offline. Fill in your credentials and click Connect.*")

    with gr.Row():
        with gr.Column(scale=5, variant="panel"):
            gr.Markdown("### ᄒ Sidebar controls & Configuration")
            wiz_dropdown = gr.Dropdown(label="Select Wizard Report", choices=list())
            btn_load = gr.Button("⚙  Fetch Wizard Profiles", variant="secondary")

            with gr.Row():
                in_from_date = gr.Textbox(label="From Date", placeholder="DD-MM-YYYY (e.g. 01-04-2024)")
                in_to_date = gr.Textbox(label="To Date", placeholder="DD-MM-YYYY (e.g. 31-10-2026)")

            in_as_on = gr.Checkbox(label="Print 'As On' Date (Historical)", value=False)
            in_accounts = gr.Dropdown(label="Filter Accounts (Optional)", choices=list(), multiselect=True)

        with gr.Column(scale=6, variant="panel"):
            gr.Markdown("### ᄒ Active execution console")
            wiz_details = gr.Markdown("*Choose a report from the sidebar menu to populate details here.*")
            params_editor = gr.Code(label="Auto-Generated Wizard Payload (JSON)", language="json", value="{}", interactive=True)
            btn_method_in = gr.Textbox(label="Odoo Execution Method", value="action_print_report")

            gr.Markdown("### ᄒ Action controllers")
            with gr.Row():
                btn_generate = gr.Button("ᄑ Execute & Extract Ledger", variant="primary")
                btn_stop = gr.Button("⏹ Cancel Request", variant="stop")

            gen_status = gr.Markdown("**Ready for instructions...**")
            download_file = gr.File(label="⌇ Download Output Ledger File Here", interactive=False)

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
    demo.launch(
        server_name="0.0.0.0",
        server_port=port,
        share=False,
        theme=gr.themes.Soft(primary_hue="amber", secondary_hue="stone"),
        css=custom_css,
    )
