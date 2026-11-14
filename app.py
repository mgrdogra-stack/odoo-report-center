import os
import gradio as gr
from backend import (
    connect_and_discover,
    inspect_wizard,
    sync_ui_to_json,
    run_wizard_and_download,
    stop_generation,
)

def disconnect_session():
    stop_generation()
    return ("", "🔒 Disconnected. Session cleared.", gr.update(choices=list(), value=None), {}, "", "{}", gr.update(choices=list(), value=list()), {}, "", None)

def run_with_progress(url, db, user, pw, selected_label, wiz_map, params_json, btn_method, d_from, d_to, is_as_on, selected_accs, acc_map, progress=gr.Progress()):
    return run_wizard_and_download(url, db, user, pw, selected_label, wiz_map, params_json, btn_method, d_from, d_to, is_as_on, selected_accs, acc_map, progress=progress)
def connect_ui(url, db, user, pw):
    status, choices, wiz_map = connect_and_discover(url, db, user, pw)
    return status, gr.update(choices=choices, value=next(iter(choices), None)), wiz_map

def inspect_ui(url, db, user, pw, selected_label, wiz_map, d_from, d_to):
    details, p_json, btn_m, as_on, acc_choices, acc_map = inspect_wizard(url, db, user, pw, selected_label, wiz_map, d_from, d_to)
    return details, p_json, btn_m, as_on, gr.update(choices=acc_choices, value=list()), acc_map
with gr.Blocks(title="Odoo Report & Ledger Center - MGR Dogra") as demo:
    gr.Markdown("# 📋 Odoo Native Report & Ledger Center\n#### *App by Muneesh Kumar Dogra (MGR Dogra)*")
    wiz_state, acc_state = gr.State({}), gr.State({})

    with gr.Row():
        in_url = gr.Textbox(label="Odoo URL", value="https://erp.lakecement.co.tz/")
        in_db = gr.Textbox(label="Database", value="parallel")
        in_user = gr.Textbox(label="Odoo Email / Username", placeholder="Enter your Odoo login")
        in_pass = gr.Textbox(label="Odoo Password", type="password", placeholder="Enter your Odoo password")

    with gr.Row():
        btn_connect = gr.Button("🔓 Connect & Discover Wizards", variant="primary")
        btn_disconnect = gr.Button("🔒 Disconnect & Clear Session", variant="secondary")
    status_box = gr.Markdown()

    wiz_dropdown = gr.Dropdown(label="Select Custom Report Wizard", choices=list())
    btn_load = gr.Button("⚙️ Load Report Parameters")
    wiz_details = gr.Markdown()

    with gr.Row():
        in_from_date = gr.Textbox(label="From Date (DD-MM-YYYY)", value="", placeholder="e.g. 01-10-2026")
        in_to_date = gr.Textbox(label="To Date (DD-MM-YYYY)", value="", placeholder="e.g. 31-10-2026")
        in_as_on = gr.Checkbox(label="Print As On", value=False)

    in_accounts = gr.Dropdown(label="Select Accounts (optional filter — leave blank for full TB group)", choices=list(), multiselect=True)

    with gr.Row():
        params_editor = gr.Code(label="Wizard Parameters (JSON)", language="json", value="{}")
        btn_method_in = gr.Textbox(label="Button Method Name", value="action_print_report")

    with gr.Row():
        btn_generate = gr.Button("🖨️ Generate & Download Report", variant="primary")
        btn_stop = gr.Button("⏹️ Stop / Cancel Generation", variant="stop")
    gen_status = gr.Markdown()
    download_file = gr.File(label="📥 Download Generated Report")

    btn_connect.click(connect_ui, [in_url, in_db, in_user, in_pass], [status_box, wiz_dropdown, wiz_state])
    btn_disconnect.click(disconnect_session, list(), [in_pass, status_box, wiz_dropdown, wiz_state, wiz_details, params_editor, in_accounts, acc_state, gen_status, download_file])
    btn_load.click(inspect_ui, [in_url, in_db, in_user, in_pass, wiz_dropdown, wiz_state, in_from_date, in_to_date], [wiz_details, params_editor, btn_method_in, in_as_on, in_accounts, acc_state])
    for ctrl in [in_from_date, in_to_date, in_as_on, in_accounts]:
        ctrl.change(sync_ui_to_json, [params_editor, in_from_date, in_to_date, in_as_on, in_accounts, acc_state], [params_editor])
    gen_event = btn_generate.click(run_with_progress, [in_url, in_db, in_user, in_pass, wiz_dropdown, wiz_state, params_editor, btn_method_in, in_from_date, in_to_date, in_as_on, in_accounts, acc_state], [gen_status, download_file])
    btn_stop.click(stop_generation, list(), [gen_status], cancels=[gen_event])

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    demo.launch(server_name="0.0.0.0", server_port=port)
