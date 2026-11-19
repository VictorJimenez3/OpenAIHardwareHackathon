# CoreS3 patient display

`BedsidePatient.ino` is the patient-side touch display for the bedside check-in MVP. It uses M5Unified, draws a high-contrast 320x240 landscape screen, and communicates with the Mac over newline-delimited JSON on USB serial.

The Mac should wait for this event before sending the first screen:

```json
{"type":"hello","role":"patient"}
```

Send a screen with two optional actions. Empty labels hide a button.

```json
{"type":"screen","prompt_id":"pain-1","title":"Are you experiencing pain?","text":"Choose one answer.","left_label":"Yes","left_value":"yes","right_label":"No","right_value":"no","status":"Touch an answer"}
```

The optional `retry:true` (or `reset_input:true`) flag explicitly re-arms a
same-prompt screen. Otherwise each prompt accepts one touch only, and a new
prompt requires the finger to release before it can be selected.

Touch produces an event tied to the currently displayed prompt.

```json
{"type":"input","prompt_id":"pain-1","value":"yes","source":"touch"}
```

The device responds to `{"type":"ping"}` with another hello event. It also re-announces itself every five seconds so a Mac process can reconnect without a board reset.

Camera mode is optional and starts only when the Mac sends a command for the
active question. The bundled `PatientCamera.h` uses the CoreS3 GC0308 camera,
converts RGB565 frames to 320×240 JPEGs at quality 40, and emits events at up to five frames per second
on the same USB link:

```json
{"type":"camera","enabled":true,"prompt_id":"pain-1"}
{"type":"frame","prompt_id":"pain-1","seq":0,"jpeg":"/9j/..."}
```

The camera registers on the CoreS3 internal I²C bus without taking ownership,
so touch remains available during the capture window. When the Mac disables the
camera or the twelve-second window expires, the firmware removes only the
camera device. The firmware reports initialization and capture failures as
`camera_error`.

Select the M5Stack CoreS3 board profile in Arduino IDE, install M5Unified through the Library Manager, and upload this sketch over a data-capable USB-C cable. The sketch does not wait for the serial monitor during boot.

Install M5Unit-GESTURE and its dependencies to build the U127 driver. Connect the
unit to Port A on the CoreS3 body. The host opts in with
`{"type":"gesture","enabled":true}` and receives `gesture_ready` or `gesture_error`.
Swipe inputs carry the active `prompt_id` and `source:"gesture"`. The firmware
accepts a swipe only when its action is visible on the current screen.
