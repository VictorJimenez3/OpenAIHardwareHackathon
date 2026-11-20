# CoreS3 patient display

`BedsidePatient.ino` is the patient-side touch display for the bedside check-in MVP. It uses M5Unified, draws a high-contrast 320x240 landscape screen, and communicates with the Mac over newline-delimited JSON on USB serial.

The Mac should wait for this event before sending the first screen:

```json
{"type":"hello","role":"patient"}
```

Send a screen with two optional actions. Empty labels hide a button.

```json
{"type":"screen","prompt_id":"pain-1","title":"Are you experiencing pain?","text":"Choose one answer.","left_label":"Yes","left_value":"yes","right_label":"No","right_value":"no","status":"Touch an answer","receipt":""}
```

The optional `retry:true` (or `reset_input:true`) flag explicitly re-arms a
same-prompt screen. A prompt accepts one response. If a finger is already on
the glass when a new prompt arrives, lift it before tapping again.

The display shows **Yes recorded** or **No recorded** with the accepted input
method. The host can send immediate feedback for the current prompt:

```json
{"type":"answer_feedback","prompt_id":"pain-1","answer":"Yes","method":"Nod accepted"}
```

The host keeps the answered question on screen for 2.5 seconds with its
`receipt`, for example `"receipt":"Nod accepted: Yes"`. The next prompt has a
new ID and an empty receipt. Review waits for the patient to tap Send.

Touch produces an event tied to the currently displayed prompt.

```json
{"type":"input","prompt_id":"pain-1","value":"yes","source":"touch"}
```

The device responds to `{"type":"ping"}` with another hello event. It also re-announces itself every five seconds so a Mac process can reconnect without a board reset.

Camera mode is optional. After the Mac enables it, the bundled
`PatientCamera.h` uses the CoreS3 GC0308 camera and converts 320×240 RGB565
frames to JPEG at quality 40. It samples no more often than every 80 ms and
sends frames on the same USB link when there is room for a complete line:

```json
{"type":"camera","enabled":true,"prompt_id":"pain-1"}
{"type":"frame","prompt_id":"pain-1","seq":0,"jpeg":"/9j/..."}
```

The camera registers on the CoreS3 internal I²C bus without taking ownership,
so touch remains available while the camera streams. The stream continues
across prompts until the host disables it. Frames that started before the
current prompt are discarded. The firmware reserves USB transmit space for
input and control events and skips frames during backpressure. If the camera
cannot start or capture frames, the host receives `camera_error` and touch
remains available.

Select the M5Stack CoreS3 board profile with **USB CDC On Boot** enabled and
**Hardware CDC and JTAG** selected. Install M5Unified through the Library
Manager and upload the sketch over a data-capable USB-C cable. The sketch
does not wait for the serial monitor during boot.

Install M5Unit-GESTURE and its dependencies to build the U127 driver. Connect the
unit to Port A on the CoreS3 body. The host opts in with
`{"type":"gesture","enabled":true}` and receives `gesture_ready` or `gesture_error`.
Swipe inputs carry the active `prompt_id` and `source:"gesture"`. The firmware
accepts a swipe only when its action is visible on the current screen.
