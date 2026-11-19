# Bedside Check-In

A hardware hackathon prototype for a simulated ICU patient who can answer yes or no but has difficulty speaking or typing. The patient answers two spoken questions on the CoreS3, reviews the exact sentence, and explicitly sends it to the StickS3. The caregiver presses a physical button to acknowledge receipt.

## Run the demo

Connect the flashed CoreS3 and StickS3 to the Mac with USB data cables. Run from this folder:

```sh
uv run --python 3.12 --extra live python -m bedside
```

The host finds the boards by their reported roles. If needed, set `--patient-port` and `--caregiver-port` explicitly. Leave the Mac speaker on. `OPENAI_API_KEY` can be provided through the environment or the gitignored `.env` file. The `live` extra installs the GPT-Live client, which uses `ffplay` for playback. Use `--voice local` to select macOS speech. Review uses macOS speech so the spoken text stays identical to the message.

1. On the CoreS3, touch **Start → Yes → Yes**.
2. Review “I'm in pain and would like help changing my position.” Touch **Send**.
3. Check that the StickS3 displays that sentence. Press its blue front button next to the screen.
4. Look for **Caregiver acknowledged** on the CoreS3. Use **New check-in** to reset.

For the no-response demo, open the first question and wait. The CoreS3 shows **No answer yet** and the StickS3 stays idle. **Cancel** at review ends the session before delivery. A delivery failure shows **Retry** and preserves the same approved message and request ID.

`--camera` enables experimental nod and shake input while a yes/no question is open. Keep the CoreS3 level and face its camera. Hold still until the movement cue appears, then make one deliberate motion and return to center. Start and Send are touch controls. If the camera window ends without an answer, use the touchscreen when **No answer yet** appears. Use the [demo trial criteria](doc/PROJECT_SETUP.md#demo-decisions) before presenting camera input to an audience.

## Firmware

Build the two sketches in `firmware/` with the M5Stack Arduino ESP32 board package. Use the `M5CoreS3` and `M5StickS3` board profiles with **USB CDC On Boot** enabled and **Hardware CDC and JTAG**. Both use M5Unified. Upload each sketch to its corresponding board before starting the Mac host.

## Run without boards

```sh
uv run --python 3.12 python -m bedside --simulate both --voice local
```

Enter `start`, `yes`, `yes`, `send`, and `ack` on separate lines. `--simulate patient` or `--simulate caregiver` replaces only one board. Use `quit` to stop a simulated run or Ctrl+C to stop the physical demo.

The [project setup](doc/PROJECT_SETUP.md) and [showcase script](doc/USER_STORY_AND_DEMO.md) contain the original build plan and pitch.
