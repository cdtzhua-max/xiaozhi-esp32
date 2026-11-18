"""
Joy phone keyboard for M5StickS3 (build-time patch).
The device can act as a Bluetooth LE keyboard ("Joy Keyboard") for the user's iPhone, so the server can
open iPhone Shortcuts / apps by typing (Cmd+Space, type a name, Return). Only advertises when asked, and
disconnects after each task so the iPhone's on-screen keyboard comes back.

MCP tools:
  self.phone.connect(timeout_s)  start advertising / reconnect to the bonded iPhone; waits until ready to type
  self.phone.disconnect          disconnect and stop advertising
  self.phone.status              {"ready":..,"connected":..,"bonded":..}
  self.phone.key(keys)           press a key or combo: "cmd+space", "enter", "tab", "esc", "backspace", "up" ...
  self.phone.type(text, layout)  type text; layout "gb" (default, British keyboard) or "us"
"""
p = "main/boards/m5stack/stick-s3/m5stack_stick_s3.cc"
s = open(p, encoding="utf-8").read()
if "joy_kbd" in s:
    print("BLE keyboard patch already applied"); raise SystemExit


def rep(old, new):
    global s
    assert s.count(old) == 1, f"stick-s3 board changed upstream, cannot find: {old[:60]!r}"
    s = s.replace(old, new)


INCLUDES = '''#include "M5PM1.h"
#include "mcp_server.h"
#include <nimble/nimble_port.h>
#include <nimble/nimble_port_freertos.h>
#include <host/ble_hs.h>
#include <host/util/util.h>
#include <services/gap/ble_svc_gap.h>
#include <services/gatt/ble_svc_gatt.h>
#include <freertos/event_groups.h>
#include <atomic>
#include <cctype>
#include <cstring>
#include <string>
extern "C" void ble_store_config_init(void);'''

NS = r'''
// ===== Joy phone keyboard (BLE HID, patched) =====
namespace joy_kbd {
static const char* kTag = "JoyKbd";
static const char* kName = "Joy Keyboard";

static const uint8_t kReportMap[] = {
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x01,
    0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x08, 0x81, 0x01,
    0x95, 0x05, 0x75, 0x01, 0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x91, 0x02,
    0x95, 0x01, 0x75, 0x03, 0x91, 0x01,
    0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x25, 0x65, 0x05, 0x07, 0x19, 0x00, 0x29, 0x65, 0x81, 0x00,
    0xC0};

enum : uintptr_t { A_REPORT_MAP = 1, A_HID_INFO, A_CTRL_POINT, A_PROTO, A_IN_REPORT, A_OUT_REPORT,
                   A_IN_REF, A_OUT_REF, A_PNP, A_MANUF, A_BATT };

static ble_uuid16_t U(uint16_t v) { ble_uuid16_t u; u.u.type = BLE_UUID_TYPE_16; u.value = v; return u; }
static ble_uuid16_t u_hid = U(0x1812), u_map = U(0x2A4B), u_info = U(0x2A4A), u_ctrl = U(0x2A4C),
    u_proto = U(0x2A4E), u_report = U(0x2A4D), u_ref = U(0x2908), u_dis = U(0x180A), u_pnp = U(0x2A50),
    u_manuf = U(0x2A29), u_bas = U(0x180F), u_batt = U(0x2A19);

static uint16_t h_in_report = 0, h_batt = 0;
static uint8_t g_proto = 1;
static uint8_t g_own_addr_type = 0;
static std::atomic<uint16_t> g_conn{BLE_HS_CONN_HANDLE_NONE};
static std::atomic<bool> g_encrypted{false}, g_subscribed{false}, g_want_adv{false}, g_synced{false}, g_started{false};

static int Access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt* ctxt, void* arg) {
    uintptr_t what = (uintptr_t)arg;
    auto put = [&](const void* d, size_t n) { return os_mbuf_append(ctxt->om, d, n) == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES; };
    switch (what) {
        case A_REPORT_MAP: return put(kReportMap, sizeof(kReportMap));
        case A_HID_INFO: { static const uint8_t v[] = {0x11, 0x01, 0x00, 0x02}; return put(v, sizeof(v)); }
        case A_CTRL_POINT: return 0;
        case A_PROTO:
            if (ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
                if (OS_MBUF_PKTLEN(ctxt->om) >= 1) os_mbuf_copydata(ctxt->om, 0, 1, &g_proto);
                return 0;
            }
            return put(&g_proto, 1);
        case A_IN_REPORT: { static const uint8_t z[8] = {0}; return put(z, sizeof(z)); }
        case A_OUT_REPORT:
            if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) { static const uint8_t z = 0; return put(&z, 1); }
            return 0;  // LED state from the phone: ignored
        case A_IN_REF: { static const uint8_t v[] = {0x01, 0x01}; return put(v, sizeof(v)); }
        case A_OUT_REF: { static const uint8_t v[] = {0x01, 0x02}; return put(v, sizeof(v)); }
        case A_PNP: { static const uint8_t v[] = {0x02, 0x3A, 0x30, 0x01, 0x40, 0x00, 0x01}; return put(v, sizeof(v)); }
        case A_MANUF: { static const char v[] = "Hey Joy Home"; return put(v, sizeof(v) - 1); }
        case A_BATT: { static const uint8_t v = 100; return put(&v, 1); }
    }
    return BLE_ATT_ERR_UNLIKELY;
}

static ble_gatt_dsc_def in_dsc[2], out_dsc[2];
static ble_gatt_chr_def hid_chr[7], dis_chr[3], bas_chr[2];
static ble_gatt_svc_def svcs[4];

static void BuildTable() {
    memset(in_dsc, 0, sizeof(in_dsc)); memset(out_dsc, 0, sizeof(out_dsc));
    memset(hid_chr, 0, sizeof(hid_chr)); memset(dis_chr, 0, sizeof(dis_chr));
    memset(bas_chr, 0, sizeof(bas_chr)); memset(svcs, 0, sizeof(svcs));
    in_dsc[0].uuid = &u_ref.u; in_dsc[0].att_flags = BLE_ATT_F_READ | BLE_ATT_F_READ_ENC; in_dsc[0].access_cb = Access; in_dsc[0].arg = (void*)A_IN_REF;
    out_dsc[0].uuid = &u_ref.u; out_dsc[0].att_flags = BLE_ATT_F_READ | BLE_ATT_F_READ_ENC; out_dsc[0].access_cb = Access; out_dsc[0].arg = (void*)A_OUT_REF;
    int i = 0;
    hid_chr[i].uuid = &u_map.u; hid_chr[i].access_cb = Access; hid_chr[i].arg = (void*)A_REPORT_MAP;
    hid_chr[i++].flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_READ_ENC;
    hid_chr[i].uuid = &u_info.u; hid_chr[i].access_cb = Access; hid_chr[i].arg = (void*)A_HID_INFO;
    hid_chr[i++].flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_READ_ENC;
    hid_chr[i].uuid = &u_ctrl.u; hid_chr[i].access_cb = Access; hid_chr[i].arg = (void*)A_CTRL_POINT;
    hid_chr[i++].flags = BLE_GATT_CHR_F_WRITE_NO_RSP | BLE_GATT_CHR_F_WRITE_ENC;
    hid_chr[i].uuid = &u_proto.u; hid_chr[i].access_cb = Access; hid_chr[i].arg = (void*)A_PROTO;
    hid_chr[i++].flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_WRITE_NO_RSP | BLE_GATT_CHR_F_READ_ENC | BLE_GATT_CHR_F_WRITE_ENC;
    hid_chr[i].uuid = &u_report.u; hid_chr[i].access_cb = Access; hid_chr[i].arg = (void*)A_IN_REPORT;
    hid_chr[i].flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY | BLE_GATT_CHR_F_READ_ENC;
    hid_chr[i].descriptors = in_dsc; hid_chr[i++].val_handle = &h_in_report;
    hid_chr[i].uuid = &u_report.u; hid_chr[i].access_cb = Access; hid_chr[i].arg = (void*)A_OUT_REPORT;
    hid_chr[i].flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_NO_RSP | BLE_GATT_CHR_F_READ_ENC | BLE_GATT_CHR_F_WRITE_ENC;
    hid_chr[i++].descriptors = out_dsc;
    dis_chr[0].uuid = &u_pnp.u; dis_chr[0].access_cb = Access; dis_chr[0].arg = (void*)A_PNP; dis_chr[0].flags = BLE_GATT_CHR_F_READ;
    dis_chr[1].uuid = &u_manuf.u; dis_chr[1].access_cb = Access; dis_chr[1].arg = (void*)A_MANUF; dis_chr[1].flags = BLE_GATT_CHR_F_READ;
    bas_chr[0].uuid = &u_batt.u; bas_chr[0].access_cb = Access; bas_chr[0].arg = (void*)A_BATT;
    bas_chr[0].flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY; bas_chr[0].val_handle = &h_batt;
    svcs[0].type = BLE_GATT_SVC_TYPE_PRIMARY; svcs[0].uuid = &u_dis.u; svcs[0].characteristics = dis_chr;
    svcs[1].type = BLE_GATT_SVC_TYPE_PRIMARY; svcs[1].uuid = &u_bas.u; svcs[1].characteristics = bas_chr;
    svcs[2].type = BLE_GATT_SVC_TYPE_PRIMARY; svcs[2].uuid = &u_hid.u; svcs[2].characteristics = hid_chr;
}

static void Advertise();

static int GapEvent(struct ble_gap_event* ev, void*) {
    switch (ev->type) {
        case BLE_GAP_EVENT_CONNECT:
            if (ev->connect.status == 0) {
                g_conn = ev->connect.conn_handle;
                ESP_LOGI(kTag, "connected");
                ble_gap_security_initiate(ev->connect.conn_handle);
            } else if (g_want_adv) {
                Advertise();
            }
            break;
        case BLE_GAP_EVENT_DISCONNECT:
            ESP_LOGI(kTag, "disconnected (reason %d)", ev->disconnect.reason);
            g_conn = BLE_HS_CONN_HANDLE_NONE; g_encrypted = false; g_subscribed = false;
            if (g_want_adv) Advertise();
            break;
        case BLE_GAP_EVENT_ENC_CHANGE:
            g_encrypted = (ev->enc_change.status == 0);
            ESP_LOGI(kTag, "encryption %s", g_encrypted ? "on" : "failed");
            break;
        case BLE_GAP_EVENT_SUBSCRIBE:
            if (ev->subscribe.attr_handle == h_in_report) g_subscribed = ev->subscribe.cur_notify;
            break;
        case BLE_GAP_EVENT_ADV_COMPLETE:
            if (g_want_adv && g_conn == BLE_HS_CONN_HANDLE_NONE) Advertise();
            break;
        case BLE_GAP_EVENT_REPEAT_PAIRING: {
            struct ble_gap_conn_desc d;
            if (ble_gap_conn_find(ev->repeat_pairing.conn_handle, &d) == 0) ble_store_util_delete_peer(&d.peer_id_addr);
            return BLE_GAP_REPEAT_PAIRING_RETRY;
        }
        default: break;
    }
    return 0;
}

static void Advertise() {
    if (!g_synced || g_conn != BLE_HS_CONN_HANDLE_NONE || ble_gap_adv_active()) return;
    struct ble_hs_adv_fields f;
    memset(&f, 0, sizeof(f));
    f.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    f.appearance = 0x03C1; f.appearance_is_present = 1;
    f.uuids16 = &u_hid; f.num_uuids16 = 1; f.uuids16_is_complete = 1;
    f.name = (uint8_t*)kName; f.name_len = strlen(kName); f.name_is_complete = 1;
    int rc = ble_gap_adv_set_fields(&f);
    if (rc != 0) { ESP_LOGW(kTag, "adv fields rc=%d", rc); return; }
    struct ble_gap_adv_params ap;
    memset(&ap, 0, sizeof(ap));
    ap.conn_mode = BLE_GAP_CONN_MODE_UND; ap.disc_mode = BLE_GAP_DISC_MODE_GEN;
    rc = ble_gap_adv_start(g_own_addr_type, nullptr, BLE_HS_FOREVER, &ap, GapEvent, nullptr);
    if (rc != 0) ESP_LOGW(kTag, "adv start rc=%d", rc);
}

static void OnSync() {
    ble_hs_util_ensure_addr(0);
    ble_hs_id_infer_auto(0, &g_own_addr_type);
    g_synced = true;
    if (g_want_adv) Advertise();
}
static void OnReset(int reason) { g_synced = false; ESP_LOGW(kTag, "host reset %d", reason); }
static void HostTask(void*) { nimble_port_run(); nimble_port_freertos_deinit(); }

static bool Start() {
    if (g_started) return true;
    if (nimble_port_init() != ESP_OK) { ESP_LOGE(kTag, "nimble init failed"); return false; }
    ble_hs_cfg.sync_cb = OnSync;
    ble_hs_cfg.reset_cb = OnReset;
    ble_hs_cfg.store_status_cb = ble_store_util_status_rr;
    ble_hs_cfg.sm_io_cap = BLE_SM_IO_CAP_NO_IO;
    ble_hs_cfg.sm_bonding = 1;
    ble_hs_cfg.sm_mitm = 0;
    ble_hs_cfg.sm_sc = 1;
    ble_hs_cfg.sm_our_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;
    ble_hs_cfg.sm_their_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;
    ble_svc_gap_init();
    ble_svc_gatt_init();
    BuildTable();
    if (ble_gatts_count_cfg(svcs) != 0 || ble_gatts_add_svcs(svcs) != 0) { ESP_LOGE(kTag, "gatt setup failed"); return false; }
    ble_svc_gap_device_name_set(kName);
    ble_svc_gap_device_appearance_set(0x03C1);
    ble_store_config_init();
    nimble_port_freertos_init(HostTask);
    g_started = true;
    return true;
}

static bool Ready() { return g_conn != BLE_HS_CONN_HANDLE_NONE && g_encrypted && g_subscribed; }

static bool Notify(const uint8_t* r) {
    if (!Ready()) return false;
    struct os_mbuf* om = ble_hs_mbuf_from_flat(r, 8);
    if (!om) return false;
    return ble_gatts_notify_custom(g_conn, h_in_report, om) == 0;
}

static bool Press(uint8_t mod, uint8_t key) {
    uint8_t r[8] = {mod, 0, key, 0, 0, 0, 0, 0};
    if (!Notify(r)) return false;
    vTaskDelay(pdMS_TO_TICKS(18));
    uint8_t up[8] = {0};
    bool ok = Notify(up);
    vTaskDelay(pdMS_TO_TICKS(18));
    return ok;
}

// ASCII -> (modifier, usage). gb = British layout (iPhone default in the UK), us = US layout.
static bool MapChar(char c, bool gb, uint8_t& mod, uint8_t& key) {
    const uint8_t SH = 0x02;
    mod = 0;
    if (c >= 'a' && c <= 'z') { key = 0x04 + (c - 'a'); return true; }
    if (c >= 'A' && c <= 'Z') { mod = SH; key = 0x04 + (c - 'A'); return true; }
    if (c >= '1' && c <= '9') { key = 0x1E + (c - '1'); return true; }
    switch (c) {
        case '0': key = 0x27; return true;
        case ' ': key = 0x2C; return true;
        case '\n': key = 0x28; return true;
        case '\t': key = 0x2B; return true;
        case '-': key = 0x2D; return true;
        case '_': mod = SH; key = 0x2D; return true;
        case '=': key = 0x2E; return true;
        case '+': mod = SH; key = 0x2E; return true;
        case '[': key = 0x2F; return true;
        case '{': mod = SH; key = 0x2F; return true;
        case ']': key = 0x30; return true;
        case '}': mod = SH; key = 0x30; return true;
        case ';': key = 0x33; return true;
        case ':': mod = SH; key = 0x33; return true;
        case '\'': key = 0x34; return true;
        case ',': key = 0x36; return true;
        case '<': mod = SH; key = 0x36; return true;
        case '.': key = 0x37; return true;
        case '>': mod = SH; key = 0x37; return true;
        case '/': key = 0x38; return true;
        case '?': mod = SH; key = 0x38; return true;
        case '`': key = 0x35; return true;
        case '!': mod = SH; key = 0x1E; return true;
        case '$': mod = SH; key = 0x21; return true;
        case '%': mod = SH; key = 0x22; return true;
        case '^': mod = SH; key = 0x23; return true;
        case '&': mod = SH; key = 0x24; return true;
        case '*': mod = SH; key = 0x25; return true;
        case '(': mod = SH; key = 0x26; return true;
        case ')': mod = SH; key = 0x27; return true;
    }
    if (gb) {
        switch (c) {
            case '"': mod = SH; key = 0x1F; return true;
            case '@': mod = SH; key = 0x34; return true;
            case '#': key = 0x32; return true;
            case '~': mod = SH; key = 0x32; return true;
            case '\\': key = 0x64; return true;
            case '|': mod = SH; key = 0x64; return true;
        }
    } else {
        switch (c) {
            case '"': mod = SH; key = 0x34; return true;
            case '@': mod = SH; key = 0x1F; return true;
            case '#': mod = SH; key = 0x20; return true;
            case '~': mod = SH; key = 0x35; return true;
            case '\\': key = 0x31; return true;
            case '|': mod = SH; key = 0x31; return true;
        }
    }
    return false;
}

static std::string Lower(std::string x) { for (auto& ch : x) ch = (char)tolower((unsigned char)ch); return x; }

// "cmd+space", "enter", "shift+tab" ...
static bool Combo(const std::string& spec) {
    uint8_t mod = 0, key = 0;
    std::string rest = Lower(spec);
    size_t pos = 0;
    while (true) {
        size_t plus = rest.find('+', pos);
        std::string part = rest.substr(pos, plus == std::string::npos ? std::string::npos : plus - pos);
        if (part == "cmd" || part == "command" || part == "gui" || part == "win") mod |= 0x08;
        else if (part == "ctrl" || part == "control") mod |= 0x01;
        else if (part == "shift") mod |= 0x02;
        else if (part == "alt" || part == "option" || part == "opt") mod |= 0x04;
        else if (part == "enter" || part == "return") key = 0x28;
        else if (part == "esc" || part == "escape") key = 0x29;
        else if (part == "backspace" || part == "delete") key = 0x2A;
        else if (part == "tab") key = 0x2B;
        else if (part == "space") key = 0x2C;
        else if (part == "right") key = 0x4F;
        else if (part == "left") key = 0x50;
        else if (part == "down") key = 0x51;
        else if (part == "up") key = 0x52;
        else if (part.size() == 1) { uint8_t m2; if (!MapChar(part[0], true, m2, key)) return false; mod |= m2; }
        else return false;
        if (plus == std::string::npos) break;
        pos = plus + 1;
    }
    if (key == 0) return false;
    return Press(mod, key);
}

static std::string Status() {
    std::string j = "{\"ready\":";
    j += Ready() ? "true" : "false";
    j += ",\"connected\":"; j += (g_conn != BLE_HS_CONN_HANDLE_NONE) ? "true" : "false";
    j += ",\"advertising\":"; j += (g_started && ble_gap_adv_active()) ? "true" : "false";
    j += "}";
    return j;
}

static void RegisterTools() {
    auto& mcp = McpServer::GetInstance();
    mcp.AddTool("self.phone.connect",
        "Make the device available as the user's Bluetooth keyboard ('Joy Keyboard') and wait until the paired iPhone is connected and ready "
        "(or, for first-time pairing, until they tap 'Joy Keyboard' in iPhone Settings > Bluetooth). Returns status.",
        PropertyList({Property("timeout_s", kPropertyTypeInteger, 15, 3, 90)}),
        [](const PropertyList& props) -> ToolResult {
            if (!Start()) return std::string("{\"ready\":false,\"error\":\"bluetooth start failed\"}");
            g_want_adv = true;
            Advertise();
            int ms = props["timeout_s"].value<int>() * 1000;
            for (int t = 0; t < ms && !Ready(); t += 100) vTaskDelay(pdMS_TO_TICKS(100));
            return Status();
        });
    mcp.AddTool("self.phone.disconnect", "Disconnect the phone keyboard and stop advertising, so the iPhone's on-screen keyboard comes back.",
        PropertyList(),
        [](const PropertyList&) -> ToolResult {
            g_want_adv = false;
            if (g_started) {
                if (ble_gap_adv_active()) ble_gap_adv_stop();
                uint16_t c = g_conn;
                if (c != BLE_HS_CONN_HANDLE_NONE) ble_gap_terminate(c, BLE_ERR_REM_USER_CONN_TERM);
            }
            return Status();
        });
    mcp.AddTool("self.phone.status", "Phone keyboard status.", PropertyList(),
        [](const PropertyList&) -> ToolResult { return Status(); });
    mcp.AddTool("self.phone.key",
        "Press one key or key combination on the connected iPhone, e.g. cmd+space (Spotlight), enter, esc, tab, backspace, up, down.",
        PropertyList({Property("keys", kPropertyTypeString)}),
        [](const PropertyList& props) -> ToolResult {
            if (!Ready()) return std::string("{\"ok\":false,\"error\":\"not connected\"}");
            bool ok = Combo(props["keys"].value<std::string>());
            return std::string(ok ? "{\"ok\":true}" : "{\"ok\":false,\"error\":\"bad keys\"}");
        });
    mcp.AddTool("self.phone.type",
        "Type text on the connected iPhone. layout: gb (British keyboard, default) or us.",
        PropertyList({Property("text", kPropertyTypeString), Property("layout", kPropertyTypeString, std::string("gb"))}),
        [](const PropertyList& props) -> ToolResult {
            if (!Ready()) return std::string("{\"ok\":false,\"error\":\"not connected\"}");
            bool gb = Lower(props["layout"].value<std::string>()) != "us";
            std::string text = props["text"].value<std::string>();
            if (text.size() > 400) text.resize(400);
            int typed = 0, skipped = 0;
            for (size_t k = 0; k < text.size(); k++) {
                unsigned char c = text[k];
                // UTF-8 curly quotes / dashes -> ASCII
                if (c == 0xE2 && k + 2 < text.size()) {
                    unsigned char b2 = text[k + 1], b3 = text[k + 2];
                    k += 2;
                    char a = 0;
                    if (b2 == 0x80 && (b3 == 0x98 || b3 == 0x99)) a = '\'';
                    else if (b2 == 0x80 && (b3 == 0x9C || b3 == 0x9D)) a = '"';
                    else if (b2 == 0x80 && (b3 == 0x93 || b3 == 0x94)) a = '-';
                    if (!a) { skipped++; continue; }
                    c = a;
                } else if (c >= 0x80) { skipped++; continue; }
                uint8_t mod, key;
                if (!MapChar((char)c, gb, mod, key)) { skipped++; continue; }
                if (!Press(mod, key)) return std::string("{\"ok\":false,\"error\":\"disconnected while typing\"}");
                typed++;
            }
            return std::string("{\"ok\":true,\"typed\":") + std::to_string(typed) + ",\"skipped\":" + std::to_string(skipped) + "}";
        });
    ESP_LOGI(kTag, "phone keyboard tools registered");
}
}  // namespace joy_kbd

// reduce the output volume to 60%'''

rep('#include "M5PM1.h"', INCLUDES)
rep("\n// reduce the output volume to 60%", NS)
rep("        InitializeButtons();\n", "        InitializeButtons();\n        joy_kbd::RegisterTools();  // patched: Joy phone keyboard\n")
open(p, "w", encoding="utf-8").write(s)

c = "main/CMakeLists.txt"
cs = open(c, encoding="utf-8").read()
old = "        m5stack__m5pm1\n"
assert cs.count(old) == 1, "CMakeLists stick-s3 block changed upstream"
cs = cs.replace(old, "        m5stack__m5pm1\n        bt\n")
open(c, "w", encoding="utf-8").write(cs)

import json
j = "main/boards/m5stack/stick-s3/config.json"
cfg = json.load(open(j))
cfg["builds"][0]["sdkconfig_append"] += [
    "CONFIG_BT_ENABLED=y",
    "CONFIG_BT_NIMBLE_ENABLED=y",
    "CONFIG_BT_NIMBLE_ROLE_CENTRAL=n",
    "CONFIG_BT_NIMBLE_ROLE_OBSERVER=n",
    "CONFIG_BT_NIMBLE_MAX_CONNECTIONS=1",
    "CONFIG_BT_NIMBLE_MEM_ALLOC_MODE_EXTERNAL=y",
    "CONFIG_BT_NIMBLE_NVS_PERSIST=y",
    "CONFIG_BT_NIMBLE_SECURITY_ENABLE=y",
    "CONFIG_BT_NIMBLE_SM_SC=y",
]
json.dump(cfg, open(j, "w"), indent=4)
print("Joy phone keyboard (BLE HID) patch applied")
