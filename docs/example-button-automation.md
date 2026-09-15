# Automating JUNG HOME button presses in Home Assistant

JUNG HOME rocker switches can be used as **triggers** for Home Assistant
automations — press a button to toggle a light, hold it to dim, run a scene,
and so on. This guide shows how, from the simplest case up to click-and-hold
on one button.

> **TL;DR** — the integration classifies every press for you. Each button side
> is an `event` entity that fires **`click`** when a press is released within
> a second and **`hold_start`** / **`hold_end`** when it is held longer, and
> the same events are offered as **device triggers** ("Up button clicked").
> You do not need to measure timing, add cooldown helpers or use a blueprint
> for the common cases — [Recipe 1](#recipe-1--do-something-on-a-click) is one
> tiny automation. **Double-click is not available** on current JUNG device
> firmware; see [why](#the-firmware-quirk-and-what-the-integration-does-about-it).

## What the integration gives you

For every rocker the integration creates one or more **event entities**:

| Entity (example) | Fires for… |
|------------------|-------------|
| `event.living_room_r1_b_up`   | the *up* side |
| `event.living_room_r1_b_down` | the *down* side |
| `event.<button>_press`        | a single-button device |

> Installs created before the entity-naming rework may still have the older
> `event.<label>_<label>_up_request_event`-style IDs — existing entity IDs are
> sticky across upgrades. The recipes work the same either way; just use the
> IDs you find below.

Each entity reports these **event types** (the `event_type` attribute):

| `event_type`  | When |
|---------------|------|
| `click`       | the button was released within **1 second** of being pressed |
| `hold_start`  | the button has been held for **1 second** and is still down |
| `hold_end`    | a held button was released (always paired with a `hold_start`) |
| `pressed`     | raw edge: the moment the button went down |
| `depressed`   | raw edge: the moment it was released |

`click` and the hold events are what you normally automate on. The raw edges
are still there for automations written against earlier versions; on a tap
you get `pressed`, `depressed`, `click` in that order, and on a hold
`pressed`, `hold_start`, …, `depressed`, `hold_end`.

The same five events are available as **device triggers**: open the button's
device page, add an automation, and pick e.g. *"Up button clicked"* or *"Up
button hold started"*. That is the quickest route for a simple automation.

### Find your exact entity IDs

Entity names are derived from the device **label**, so they depend on what you
named the switch in the JUNG HOME app. To find the real IDs:

1. Go to **Developer Tools → States**.
2. Filter for `event.` and look for your switch's label.
3. Note the entity ID and watch its `event_type` attribute while you press the
   button — it shows `click`, `hold_start`, and so on.

Use those IDs in place of the `event.living_room_r1_b_...` placeholders below.

---

## Recipe 1 — Do something on a click

The common case. No helpers, no scripts — one automation:

```yaml
alias: R1 B - click toggles lamp
mode: single
triggers:
  - trigger: state
    entity_id: event.living_room_r1_b_up
conditions:
  - condition: template
    value_template: >
      {{ trigger is defined and trigger.to_state is not none
         and trigger.from_state is not none
         and trigger.from_state.state not in ('unavailable', 'unknown')
         and trigger.to_state.attributes.get('event_type') == 'click' }}
actions:
  - action: light.toggle
    target:
      entity_id: light.living_room_lamp
```

Why trigger on *any* state change and filter with a condition, instead of
`attribute: event_type` / `to: click`? An event entity's *state* is just a
timestamp, and a `to:`-style trigger only fires when the attribute *changes
value* — so it silently misses the second of two clicks in a row (the
attribute stays at `click`). Triggering on the state change and checking
`event_type == 'click'` in a condition fires reliably **once per click**.

The `trigger is defined` guard keeps Home Assistant from logging a
*"'trigger' is undefined"* warning when it renders the condition outside a
trigger context (e.g. when you save the automation or run it manually).

The `from_state` guard matters just as much: an event entity **restores its
last state** across a Home Assistant restart, an integration reload and a
connection loss, so the `unavailable → <restored timestamp>` transition
re-presents the stored `event_type` — and a button whose last event was
`click` would toggle your lamp **on its own on every recovery**. Requiring a
real previous state costs nothing: the first genuine click after a recovery
still transitions timestamp → timestamp and fires normally. (The bundled
blueprint applies the same guard.)

> Want it to react to **either** side of the rocker? List both entities under
> `entity_id:`. Prefer the UI? The device trigger *"Up button clicked"* is the
> same thing without the YAML.

---

## Recipe 2 — Hold to dim

`hold_start` fires while the button is still down, and the entity's
`event_type` leaves `hold_start` the moment it is released — so a `repeat …
until` loop that steps the brightness every 300 ms runs exactly as long as
the finger stays on the button:

```yaml
alias: R1 B - hold dims up
mode: single
triggers:
  - trigger: state
    entity_id: event.living_room_r1_b_up
conditions:
  # Same guards as Recipe 1, on the hold gesture.
  - condition: template
    value_template: >
      {{ trigger is defined and trigger.to_state is not none
         and trigger.from_state is not none
         and trigger.from_state.state not in ('unavailable', 'unknown')
         and trigger.to_state.attributes.get('event_type') == 'hold_start' }}
actions:
  - repeat:
      until:
        - condition: template
          value_template: >
            {{ state_attr('event.living_room_r1_b_up', 'event_type') != 'hold_start' }}
      sequence:
        - action: light.turn_on
          target:
            entity_id: light.living_room_lamp
          data:
            brightness_step_pct: 5
        - delay:
            milliseconds: 300
```

Pair it with Recipe 1 on the same side (click toggles, hold dims) — the two
never collide, because a press is either a click *or* a hold, never both.

---

## Recipe 3 — The blueprint (a form instead of YAML)

This repository ships a blueprint that maps the gestures to actions, so you
configure each button by **filling in a form**:
[`blueprints/automation/junghome/button_gestures.yaml`](../blueprints/automation/junghome/button_gestures.yaml).

It exposes:

- **Button (event entities)** — the `event.*` entity for one physical button
  side (a rocker exposes `..._up` and `..._down`, one per side).
- **Click action** and **Hold action** — what to run for each gesture; leave
  either empty to ignore it. Nothing waits: the click action runs the moment
  the button is released, the hold action after one second of holding.
- **Detect double-clicks (old firmware only)**, **Double-click window** and
  **Double-click action** — a legacy path, off by default. See
  [below](#double-click-on-old-firmware-only) before turning it on.

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
   **JUNG HOME button — click / hold***.
2. Pick the button's event entity and fill in the click and/or hold action.
3. Save. Repeat for each button side (one automation per blueprint use).

Automations created from the previous revision of the blueprint (which had a
"Hold time" and a double-click window) keep working after re-importing it;
their hold now uses the integration's fixed 1-second threshold.

---

## The firmware quirk, and what the integration does about it

Current JUNG device firmware (2.2.0.x, installed by the JUNG HOME app from
2.2.0 on) reports **every tap twice**: one physical click reaches Home
Assistant as *two* complete press/release pairs — the second 0.1–1 s after the
first, on the same side for a rocker half, and on the *other* side for a
single-key button. A hold is reported once. Older device firmware reports each
tap once. The gateway itself does not filter this; full evidence, including a
labelled measurement of every gesture type, is in the rocker section of
[gateway-websocket.md](gateway-websocket.md).

**The integration drops the copy for you.** After a click, the next press on
the same button (either side) within **1.2 seconds** is ignored, together with
its release — no second `click`, no second `pressed`/`depressed`, no second
device trigger. This is on by default and is what makes Recipe 1 toggle the
lamp once, not twice. Two things follow:

- **Two deliberate presses on one rocker within 1.2 s count as one.** Pressing
  *up* and then *down* half a second later loses the *down*. This is the
  price of dropping the copy on single-key buttons (whose copy lands on the
  other side); it is rarely noticeable in practice.
- A press that is still down after a second is never dropped — it becomes a
  hold — so "click, then immediately hold" works.

**Double-click is not available** on this firmware, and cannot be: a single
click and a double click both arrive as two identical pairs, with overlapping
gaps. No setting recovers the difference. Use click + hold instead.

### Turning the suppression off

*Settings → Devices & Services → Jung Home → Configure → Ignore duplicate
button presses.* Turn it off only if **both** apply:

- your buttons report each tap **once** (older device firmware — measure
  below), and
- you need presses closer together than 1.2 s, i.e. double-clicks.

With it off, every press/release pair is its own `click`.

### Double-click (on old firmware only)

With the suppression off and buttons that report once per tap, the blueprint's
**Detect double-clicks** input waits for a second `click` within the
**Double-click window** (default 1 s) before running the click action, and
runs the double-click action instead if one arrives. The window is measured
from click to click, and a click is reported at the gateway's synthesised
release, about 0.4-0.5 s after the press — so the second click of a
double-click lands 0.5-1 s after the first, and a window much under a second
misses it. This adds that window as latency to every single click, which is
why it is a separate opt-in.

### Measure your own buttons

The repo's capture tool timestamps every frame and prints per-gesture timings:

```sh
python tools/ws-capture/capture_ws.py capture --host <gateway> --script rocker
python tools/ws-capture/capture_ws.py analyze disk_dump/ws-capture-<stamp>/frames.jsonl
```

**Two** pairs per single click means your firmware doubles taps: leave the
suppression on. **One** pair means you may turn it off if you want
double-clicks.

---

## Troubleshooting

- **Automation never fires.** Confirm the entity ID in *Developer Tools →
  States*, and watch its `event_type` attribute while you press the button.
  If nothing changes, the rocker may not be exposed as an `event` entity (only
  `RockerSwitch` devices are) — check the device page. If the entity reads
  `unavailable`, the gateway's WebSocket is down: presses only arrive over it.
- **A click fires twice, or two different buttons on one rocker both fire.**
  Check that *Ignore duplicate button presses* is on in the integration's
  options (it is by default).
- **A quick second press on the same rocker is lost.** That is the 1.2 s
  window above. It only matters for presses less than 1.2 s apart.
- **Hold fires when I meant to click.** The threshold is 1 second: a press
  released later than that is a hold. There is no setting for it — measured
  clicks release within ~0.5 s regardless of how long the finger stays,
  because the gateway synthesises the release itself.
- **A `hold_start` without a `hold_end`.** The release was lost (WebSocket
  drop) or never reported by the gateway (single-key buttons, by the gateway
  code, can leave one side down after a hold). The integration fires the
  owed `hold_end` on the next event it sees on that side, so a `hold_end`
  always arrives — Recipe 2's loop ends then.
- **One side of the rocker never fires.** Each side is its own event entity —
  make sure the automation lists the side you are pressing. (A single-action
  button still exposes both `up` and `down` entities with only one of them
  live; the dead one never firing is a gateway limitation, not a fault.)

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

Do one click and one hold, then read the lines from the Home Assistant log
(`grep jung_button_debug` in `home-assistant.log`). You should see
`pressed`, `depressed`, `click` for the tap and `pressed`, `hold_start`,
`depressed`, `hold_end` for the hold; the `@ <epoch>` timestamps show the
gaps. For the integration's own view — including which presses it dropped as
duplicates — enable debug logging for `custom_components.junghome.event`.
