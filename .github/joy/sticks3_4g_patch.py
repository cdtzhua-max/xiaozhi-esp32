"""
StickS3 + 4G (ML307 Cat-1, e.g. M5Stack Unit Cat1) dual-network build patch.
Run in the firmware repo root at build time. Env:
  GROVE_TX  ESP32 TX pin -> module RX   (default 10, Grove white wire)
  GROVE_RX  ESP32 RX pin <- module TX   (default 9, Grove yellow wire)
  NET_DEFAULT  0 = start on Wi-Fi, 1 = start on 4G  (default 0)
Controls on the device:
  side button long press (~2 s)  -> switch Wi-Fi <-> 4G (saved, reboots)
"""
import os, re

tx = int(os.environ.get("GROVE_TX", "10"))
rx = int(os.environ.get("GROVE_RX", "9"))
net_default = int(os.environ.get("NET_DEFAULT", "0"))

p = "main/boards/m5stack/stick-s3/m5stack_stick_s3.cc"
s = open(p, encoding="utf-8").read()

def rep(old, new, count=1):
    global s
    assert s.count(old) == count, f"stick-s3 board changed upstream, cannot find: {old[:60]!r}"
    s = s.replace(old, new)

rep('#include "wifi_board.h"', '#include "dual_network_board.h"\n#include "wifi_board.h"')
rep("class M5StackStickS3Board : public WifiBoard {", "class M5StackStickS3Board : public DualNetworkBoard {")
# Grove 5V needs the PM1 boost when running on battery
rep("pmic_->setBoostEnable(false);", "pmic_->setBoostEnable(true);  // patched: power Grove 5V for the 4G unit")
# Front button: only enter Wi-Fi setup when we're on Wi-Fi
rep("""            if (app.GetDeviceState() == kDeviceStateStarting && !WifiManager::GetInstance().IsConnected()) {
                EnterWifiConfigMode();
            }""", """            if (GetNetworkType() == NetworkType::WIFI &&
                app.GetDeviceState() == kDeviceStateStarting && !WifiManager::GetInstance().IsConnected()) {
                static_cast<WifiBoard&>(GetCurrentBoard()).EnterWifiConfigMode();
                return;
            }""")
# Side button long press: switch network
rep("""        // User button on GPIO12: single click sets stored volume to 70%""",
    """        // patched: side button long press switches Wi-Fi <-> 4G (saved, then reboots)
        user_button_.OnLongPress([this]() {
            ESP_LOGI(TAG, "Switching network type");
            SwitchNetworkType();
        });

        // User button on GPIO12: single click sets stored volume to 70%""")
rep("""    M5StackStickS3Board() :
        boot_button_(BOOT_BUTTON_GPIO),""",
    f"""    M5StackStickS3Board() :
        DualNetworkBoard(GPIO_NUM_{tx}, GPIO_NUM_{rx}, GPIO_NUM_NC, {net_default}),
        boot_button_(BOOT_BUTTON_GPIO),""")
open(p, "w", encoding="utf-8").write(s)

# Unit Cat1 is only reliable up to 460800 baud
p2 = "main/boards/common/ml307_board.cc"
s2 = open(p2, encoding="utf-8").read()
n = s2.count("921600")
assert n >= 1, "ml307_board.cc baud rate not found"
s2 = s2.replace("921600", "460800")
open(p2, "w", encoding="utf-8").write(s2)
print(f"StickS3 4G patch applied: TX=G{tx} RX=G{rx}, default network={'4G' if net_default else 'Wi-Fi'}, baud 460800")
