# Automating JUNG HOME button presses in Home Assistant

JUNG HOME rocker switches can be used as **triggers** for Home Assistant
automations — press a button to toggle a light, run a scene, etc. This guide
shows how, from the simplest case (do something on a press) up to detecting
**single / double / hold** gestures on a single button.

> **TL;DR — is there a "best" way?**
> If you just want *something to happen when a button is pressed*, it's one tiny
> automation with no helpers — see [Recipe 1](#recipe-1--do-something-on-a-press).
> The only time you need the complicated stuff is when you want to pack **several
> gestures onto one button** (single vs. double vs. hold). For that, prefer the
> **single self-contained automation** in
> [Recipe 3](#recipe-3--single--double--hold-on-one-button) over the older
> "many helpers + scripts" pattern — it does the same thing with no `counter`,
> `timer`, or `input_boolean` helpers to maintain.

## What the integration gives you

For every rocker the integration creates one or more **event entities**:

| Entity (example) | Fires when… |
|------------------|-------------|
| `event.living_room_r1_b_up`   | the *up* side is pressed/released |
| `event.living_room_r1_b_down` | the *down* side is pressed/released |
| `event.<button>_press`        | a single-button device is pressed/released |

> Installs created before the entity-naming rework may still have the older
> `event.<label>_<label>_up_request_event`-style IDs — existing entity IDs are
> sticky across upgrades. The recipes work the same either way; just use the
> IDs you find below.

Each entity reports exactly two **event types**:

- **`pressed`** — the moment the button goes down.
- **`depressed`** — the moment it is released.

That's all the hardware reports. Everything else (single, double, hold) is
derived from the timing between these two edges.

### A firmware quirk: one channel can echo the other

On some gateway firmware versions, a single physical press can emit
`pressed`/`depressed` edges on **both** `up_request` and `down_request` — not
just the one you'd expect — while a genuine *second* click, or a repeated
hold, often only echoes on one of them. If you treat every `pressed` edge on
either channel as a fresh click (as Recipe 3 and the blueprint below do), a
single press or a hold can get reported as a double-click, because the echo
looks exactly like "a second press."

Before wiring Recipe 3 or the blueprint, use the
[debug logger](#debug-logger--capture-the-raw-event-stream) below to do a
single press, a double press and a hold-and-release, a few times each, and
check whether one channel (e.g. `down_request`) fires a complete
`pressed`→`depressed` pair for *every* gesture while the other is
inconsistent. If so, list **only that one channel** — not both. This
sidesteps the echo entirely, at the cost of no longer covering JUNG's older
alternating-channel behavior (see the troubleshooting section below if a
press ever goes silent on your chosen channel).

### Find your exact entity IDs

Entity names are derived from the device **label**, so they depend on what you
named the switch in the JUNG HOME app. To find the real IDs:

1. Go to **Developer Tools → States**.
2. Filter for `event.` and look for your switch's label.
3. Note the entity ID and watch its `event_type` attribute while you press the
   button — it flips between `pressed` and `depressed`.

Use those IDs in place of the `event.living_room_r1_b_...` placeholders below.

---

## Recipe 1 — Do something on a press

The common case. No helpers, no scripts — one automation. It triggers whenever
the button's `event_type` becomes `pressed`:

```yaml
alias: R1 B - press toggles lamp
mode: single
triggers:
  - trigger: state
    entity_id: event.living_room_r1_b_up
conditions:
  - condition: template
    value_template: "{{ trigger is defined and trigger.to_state.attributes.event_type == 'pressed' }}"
actions:
  - action: light.toggle
    target:
      entity_id: light.living_room_lamp
```

Why trigger on *any* state change and filter with a condition, instead of
`attribute: event_type` / `to: pressed`? An event entity's *state* is just a
timestamp that changes on **both** press and release, and a `to:`-style trigger
only fires when the attribute *changes value* — so it silently misses a repeated
`pressed` (which happens when JUNG reports the same button twice, or alternates
between `up_request` and `down_request`). Triggering on the state change and
checking `event_type == 'pressed'` in a condition fires reliably **once per
press** and never on release.

The `trigger is defined` guard keeps Home Assistant from logging a
*"'trigger' is undefined"* warning when it renders the condition outside a
trigger context (e.g. when you save the automation or run it manually).

> Want it to react to **either** side of the rocker? List both entities under
> `entity_id:`.

---

## Recipe 2 — Toggle a blind/cover with one button

A practical variant — press once to open the cover if it's closed, otherwise
close it:

```yaml
alias: R1 B - press toggles blind
mode: single
triggers:
  - trigger: state
    entity_id: event.living_room_r1_b_up
conditions:
  - condition: template
    value_template: "{{ trigger is defined and trigger.to_state.attributes.event_type == 'pressed' }}"
actions:
  - if:
      - condition: state
        entity_id: cover.living_room_blind
        state: closed
    then:
      - action: cover.open_cover
        target:
          entity_id: cover.living_room_blind
    else:
      - action: cover.close_cover
        target:
          entity_id: cover.living_room_blind
```

(`cover.toggle` works too, if your cover supports it.)

---

## Recipe 3 — Single / double / hold on one button

If you want **one physical button to do three different things** depending on how
it's pressed, you have to measure timing yourself. The whole thing fits in **one
automation, with no helper entities**, using `wait_for_trigger`:

- **Hold** — pressed and *nothing else happens* within 2 s (still held).
- **Double** — a second press arrives within ~0.4 s (before *or* after the first
  release).
- **Single** — pressed and released, with no second press.

List the button's event entity under `entity_id:`. If the debug logger (above)
shows one channel reporting every gesture completely, list **only that one** —
don't add its sibling, since on some firmware an echoed `pressed` edge on the
sibling channel gets misread as a second click. Only list both if your debug
log shows no echoes and presses genuinely alternate channels the older way.

We trigger on *any* state change and filter with a condition rather than
`attribute: event_type` / `to: pressed`, because the `to:` form silently misses
repeated `pressed` events (e.g. from alternating channels on older firmware).

The key detail (confirmed from real device logs): on a double-click JUNG can
report the **second press before the first release**, e.g.
`DOWN pressed → UP pressed → DOWN depressed → UP depressed`. So instead of
assuming "press, then release, then maybe a second press", we just wait for the
*next event of any kind* and branch on it.

```yaml
alias: R1 B - single / double / hold
mode: single  # important: ignore re-triggers while we're measuring a gesture
triggers:
  - trigger: state
    entity_id:
      - event.living_room_r1_b_up
      - event.living_room_r1_b_down
conditions:
  # Only start on a press edge; ignore release ('depressed') state changes.
  - condition: template
    value_template: "{{ trigger is defined and trigger.to_state.attributes.event_type == 'pressed' }}"
actions:
  # Wait up to 2 s for the NEXT event of any kind:
  #   nothing      → still held        → HOLD
  #   another press → second click      → DOUBLE
  #   a release     → single/slow double → checked below
  - wait_for_trigger:
      - trigger: state
        entity_id:
          - event.living_room_r1_b_up
          - event.living_room_r1_b_down
    timeout: "00:00:02"
    continue_on_timeout: true
  - variables:
      evt: >-
        {{ wait.trigger.to_state.attributes.event_type
           if wait.trigger is not none else none }}

  - choose:
      # ---- HOLD: nothing arrived in time → still held ----
      - conditions:
          - "{{ wait.trigger is none }}"
        sequence:
          - action: notify.pushover
            data:
              message: R1 B held (2s)

      # ---- DOUBLE: a second press came before the first release ----
      - conditions:
          - "{{ evt == 'pressed' }}"
        sequence:
          - action: notify.pushover
            data:
              message: R1 B double click

    # ---- First press released: SINGLE, or a slower double ----
    default:
      # Wait once more for a second press within the double-click window.
      - wait_for_trigger:
          - trigger: state
            entity_id:
              - event.living_room_r1_b_up
              - event.living_room_r1_b_down
        timeout: "00:00:00.4"
        continue_on_timeout: true
      - choose:
          # A second press arrived → DOUBLE
          - conditions:
              - >
                {{ wait.trigger is not none
                   and wait.trigger.to_state.attributes.event_type == 'pressed' }}
            sequence:
              - action: notify.pushover
                data:
                  message: R1 B double click
        # No second press in the window → SINGLE
        default:
          - action: notify.pushover
            data:
              message: R1 B single click
```

### Tuning

- **2 s hold threshold** → change the first `timeout`.
- **Double-click window (0.4 s)** → change the second `timeout`. Too short and
  fast double-presses register as two singles; too long and every single click
  feels laggy because the automation waits before acting.
- Replace the `notify.pushover` actions with whatever you want — `light.toggle`,
  `scene.turn_on`, `script.turn_on`, etc.

### Why not the "helpers + scripts" approach?

An earlier version of this guide built the same behaviour out of a `counter`, two
`input_boolean`s, two `timer`s, three `script`s and three automations per button.
It works, but it's a lot of moving parts to copy and keep in sync for every
button — and the helper states can drift if Home Assistant restarts mid-press.
The single-automation version above is equivalent, self-contained, and easier to
duplicate. And if you have **many** buttons, you don't need to copy YAML at all —
use the bundled blueprint below.

---

## Recipe 4 — The blueprint (recommended for more than one button)

This repository ships a blueprint that wraps Recipe 3, so you configure each
button by **filling in a form** instead of editing YAML:
[`blueprints/automation/junghome/button_gestures.yaml`](../blueprints/automation/junghome/button_gestures.yaml).

It exposes:

- **Button (event entities)** — the `event.*` entity for one physical button.
  JUNG sometimes splits a button into separate `up_request` and `down_request`
  events. Check with the debug logger below first: if one channel reports every
  gesture completely, select **only that one** — selecting both can make an
  echoed edge on the sibling channel misread a single press or hold as a
  double-click on some firmware. Only select both if your debug log shows no
  echoes and presses genuinely alternate channels.
- **Hold time** and **Double-click window** — the two timing thresholds.
- **Single / Double / Hold action** — what to run for each gesture; leave any of
  them empty to ignore that gesture.

### Install it

Either:

- **Import from URL** — Home Assistant → *Settings → Automations & scenes →
  Blueprints → Import blueprint*, and paste the raw file URL:
  `https://github.com/ernetas/junghome/blob/main/blueprints/automation/junghome/button_gestures.yaml`

- **Or copy the file** into your config at
  `config/blueprints/automation/junghome/button_gestures.yaml` and reload
  blueprints (or restart Home Assistant).

### Use it

1. *Settings → Automations & scenes → Create automation → Use blueprint →
   **JUNG HOME button — single / double / hold***.
2. Pick the button's event entity, set the timings, and fill in the actions you
   want for single / double / hold.
3. Save. Repeat for each button side (create one automation per blueprint use).

---

## Troubleshooting

- **Automation never fires.** Confirm the entity ID in *Developer Tools →
  States*, and watch its `event_type` attribute flip while you press the button.
  If nothing changes, the rocker may not be exposed as an `event` entity (only
  `RockerSwitch` devices are) — check the device page.
- **Single click feels slow.** That delay is the double-click window in Recipe 3.
  If you don't need double-click, use Recipe 1, which acts instantly.
- **Hold fires on every long-ish press.** Lower/raise the 2 s threshold, or make
  sure your device actually sends a separate `depressed` (release) event — hold
  detection depends on press and release being reported separately, which JUNG
  rockers do.
- **Single presses or holds register as double-clicks.** This is the firmware
  echo described [above](#a-firmware-quirk-one-channel-can-echo-the-other) — one
  channel emits a spurious `pressed` edge that gets read as a second click. Use
  the debug logger below to find the one channel that reports every gesture
  completely, and select **only that one** channel in the automation/blueprint.
- **Double-clicks register as single (or vice-versa), and your debug log shows
  no echoes.** Make sure you selected **all** of the button's events (both
  `up_request` and `down_request`) — on that firmware JUNG alternates between
  them, so with only one selected, every other click is invisible. If genuine
  double-clicks are still missed, widen the **double-click window**; if singles
  are seen as doubles, shorten it.

### Debug logger — capture the raw event stream

To see exactly what your button emits (and the timing between events), add this
**automation** — not a Developer Tools → Template snippet; `trigger` only exists
inside an automation. Replace the entity IDs with your own:

```yaml
alias: JUNG button debug logger
mode: queued
max: 100
triggers:
  - trigger: state
    entity_id:
      - event.living_room_r1_b_up
      - event.living_room_r1_b_down
actions:
  - action: system_log.write
    data:
      level: warning
      logger: jung_button_debug
      message: >-
        {{ trigger.entity_id }} = {{ trigger.to_state.attributes.event_type }}
        @ {{ trigger.to_state.last_changed.timestamp() | round(3) }}
```

Do one single-click, one double-click and one hold, then read the lines from the
Home Assistant log (`grep jung_button_debug` in `home-assistant.log`). The
`@ <epoch>` timestamps let you measure the gaps and tune the hold time and
double-click window.

