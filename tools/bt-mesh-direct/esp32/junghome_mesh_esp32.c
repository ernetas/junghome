/*
 * Reference prototype: control JUNG HOME devices over Bluetooth Mesh from an
 * ESP32 (ESP-IDF / ESP-BLE-MESH), gateway-free.
 *
 * This is the ESP32 counterpart to ../junghome_mesh.py. It shows the client-model
 * SEND path for the standard SIG models JUNG uses (see ../../docs/bt-mesh-direct.md):
 *   - Generic OnOff Client   -> switches / on-off lights / sockets
 *   - Light Lightness Client -> dimmers
 *   - Generic Level Client   -> blinds, and color temperature (on element+1)
 *   - Scene Client           -> scene recall
 *
 * It is a *reference* sketch (won't be built in this repo). It assumes the ESP32
 * has joined the JUNG mesh and has the AppKey bound to these client models. On
 * ESP-BLE-MESH the usual route is to run the node as a provisioner that imports
 * the NetKey/AppKey exported from the JUNG HOME app, or to configure them via the
 * Config Client using the device keys. Provisioning/key import is omitted here.
 *
 * Build inside an ESP-IDF project that enables: Bluetooth, BLE Mesh, and the
 * Generic/Lighting/Time-Scene client models in menuconfig. Drop this in main/.
 */

#include <string.h>
#include "esp_log.h"
#include "esp_ble_mesh_defs.h"
#include "esp_ble_mesh_common_api.h"
#include "esp_ble_mesh_generic_model_api.h"
#include "esp_ble_mesh_lighting_model_api.h"
#include "esp_ble_mesh_time_scene_model_api.h"

#define TAG "JUNG_MESH"

/* Send tuning, matching the gateway (config.json -> btmesh). */
#define APP_IDX        0      /* AppKey index */
#define NET_IDX        0      /* NetKey index */
#define SEND_TTL       7

/* Color temperature: JUNG maps Kelvin 2000..6000 onto int16 -32768..32767 and
 * drives Generic Level on the CTL-temperature element (target address + 1). */
static int16_t kelvin_to_level(int kelvin)
{
    if (kelvin < 2000) kelvin = 2000;
    if (kelvin > 6000) kelvin = 6000;
    long span = 0x7FFF - (-0x8000);
    return (int16_t)(-0x8000 + ((long)(kelvin - 2000) * span) / (6000 - 2000));
}

static uint8_t s_tid;

static esp_ble_mesh_client_common_param_t common_param(
        uint32_t opcode, esp_ble_mesh_model_t *model, uint16_t addr)
{
    esp_ble_mesh_client_common_param_t c = {0};
    c.opcode = opcode;
    c.model = model;
    c.ctx.net_idx = NET_IDX;
    c.ctx.app_idx = APP_IDX;
    c.ctx.addr = addr;            /* node unicast or group address */
    c.ctx.send_ttl = SEND_TTL;
    c.msg_timeout = 0;            /* use stack default */
    return c;
}

/* --- switches / on-off lights / sockets --- */
void jung_set_onoff(esp_ble_mesh_model_t *onoff_client, uint16_t addr, bool on)
{
    esp_ble_mesh_client_common_param_t c =
        common_param(ESP_BLE_MESH_MODEL_OP_GEN_ONOFF_SET_UNACK, onoff_client, addr);
    esp_ble_mesh_generic_client_set_state_t set = {0};
    set.onoff_set.op_en = false;
    set.onoff_set.onoff = on ? 1 : 0;
    set.onoff_set.tid = s_tid++;
    esp_ble_mesh_generic_client_set_state(&c, &set);
}

/* --- dimmers (Light Lightness Actual, 0..0xFFFF) --- */
void jung_set_brightness(esp_ble_mesh_model_t *lightness_client, uint16_t addr, int percent)
{
    if (percent < 0) percent = 0; if (percent > 100) percent = 100;
    uint16_t level = (uint16_t)((percent * 0xFFFF) / 100);
    esp_ble_mesh_client_common_param_t c =
        common_param(ESP_BLE_MESH_MODEL_OP_LIGHT_LIGHTNESS_SET_UNACK, lightness_client, addr);
    esp_ble_mesh_light_client_set_state_t set = {0};
    set.lightness_set.op_en = false;
    set.lightness_set.lightness = level;
    set.lightness_set.tid = s_tid++;
    esp_ble_mesh_light_client_set_state(&c, &set);
}

/* --- tunable white (Generic Level on element+1) --- */
void jung_set_color_temp(esp_ble_mesh_model_t *level_client, uint16_t addr, int kelvin)
{
    esp_ble_mesh_client_common_param_t c =
        common_param(ESP_BLE_MESH_MODEL_OP_GEN_LEVEL_SET_UNACK, level_client, addr + 1);
    esp_ble_mesh_generic_client_set_state_t set = {0};
    set.level_set.op_en = false;
    set.level_set.level = kelvin_to_level(kelvin);
    set.level_set.tid = s_tid++;
    esp_ble_mesh_generic_client_set_state(&c, &set);
}

/* --- blinds (Generic Level Move: down=0x7FFF, up=0x8000(-32768), stop=0) --- */
void jung_blinds_move(esp_ble_mesh_model_t *level_client, uint16_t addr, const char *dir)
{
    int16_t delta = 0;
    if (strcmp(dir, "down") == 0) delta = 0x7FFF;
    else if (strcmp(dir, "up") == 0) delta = (int16_t)0x8000;
    esp_ble_mesh_client_common_param_t c =
        common_param(ESP_BLE_MESH_MODEL_OP_GEN_MOVE_SET_UNACK, level_client, addr);
    esp_ble_mesh_generic_client_set_state_t set = {0};
    set.move_set.op_en = true;
    set.move_set.delta_level = delta;
    set.move_set.trans_time = 0xFE;   /* ~max; mirrors transition 0xFFFE intent */
    set.move_set.tid = s_tid++;
    esp_ble_mesh_generic_client_set_state(&c, &set);
}

/* --- scene recall (0xFFFF = broadcast, or a group address) --- */
void jung_recall_scene(esp_ble_mesh_model_t *scene_client, uint16_t scene_number, uint16_t addr)
{
    esp_ble_mesh_client_common_param_t c =
        common_param(ESP_BLE_MESH_MODEL_OP_SCENE_RECALL_UNACK, scene_client, addr);
    esp_ble_mesh_time_scene_client_set_state_t set = {0};
    set.scene_recall.op_en = false;
    set.scene_recall.scene_number = scene_number;
    set.scene_recall.tid = s_tid++;
    esp_ble_mesh_time_scene_client_set_state(&c, &set);
}

/*
 * Receiving state & button events: register a client callback with
 * esp_ble_mesh_register_generic_client_callback() /
 * _light_client_callback() / _sensor_client_callback() and read the *_STATUS
 * messages (addr -> function via your CDB mapping).
 *
 * Rocker BUTTONS, status LED and parameters use the JUNG vendor model
 * (company 0x0527) — register a vendor/custom model with
 * esp_ble_mesh_register_custom_model_callback() and parse the 0x0527 opcodes.
 * See ../../docs/bt-mesh-direct.md for the vendor framing.
 */
