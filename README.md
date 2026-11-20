# Bedside Check-In

A hardware hackathon prototype for a simulated ICU patient who can answer yes or no but has difficulty speaking or typing. The patient can send a plain pain alert from the CoreS3 before answering optional questions about its location. The caregiver sees the message on the StickS3 and presses a physical button to acknowledge receipt.

## Run the demo

Connect the flashed CoreS3 and StickS3 to the Mac with USB data cables. Run from this folder:

```sh
uv run --python 3.12 --extra live python -m bedside
```

The host finds the boards by their reported roles. If needed, set `--patient-port` and `--caregiver-port` explicitly. Leave the Mac speaker on. `OPENAI_API_KEY` can be provided through the environment or the gitignored `.env` file. The `live` extra installs the GPT-Live client, which uses `ffplay` for playback. Use `--voice local` to select macOS speech. Review uses macOS speech so the spoken text stays identical to the message.

1. On the CoreS3, touch **Start → Yes**.
2. Review “I'm in pain.” Touch **Send**. Check that the StickS3 displays the alert before the CoreS3 asks about location.
3. Answer the optional questions to narrow the pain from upper or lower body to a region. To try the lower-leg path, answer **No → No → Yes**. Review and send the location update.
4. Press the blue button on the StickS3. Look for **Caregiver acknowledged** on the CoreS3 before starting a new check-in.

Answering **No** to pain opens one question about other help. **Yes** offers a generic help request for review. **No** ends the check-in. Silence on the first question leaves it open. Optional location questions time out after 25 seconds, leaving the first pain alert on the StickS3. A delivery failure shows **Retry** and preserves the same approved message and request ID.

## Try a different care context

```sh
uv run --python 3.12 --extra live python -m bedside \
  --handoff 'The patient gestured toward their right shoulder earlier when uncomfortable.'
```

Use a short fictional caregiver note. The first question asks whether the person is experiencing pain. After a **Yes**, the model can ask one specific yes/no follow-up about the location or a requested form of help. If the patient declines it, or the note has no useful clue, the person can review the plain pain report. Every message requires a separate **Send**.

`--adaptive --scenario comfort`, `--adaptive --scenario mouth`, and `--adaptive --scenario pain` run repeatable scenarios. The planner uses the scenario and the patient’s confirmed pain answer to pick a follow-up or move to review. The local scenario question tree handles planner timeouts. Use `--scripted` for the original pain and position demo.

`--camera` opens a live CoreS3 camera preview on the Mac and enables experimental gesture answers. The Mac uses Apple Vision and requires the Swift compiler. Face the level camera or hold one hand still in view. For **Yes**, nod or move that hand up and down. For **No**, shake your head or move that hand side to side. Return to rest. The CoreS3 shows **Yes recorded** or **No recorded** for 2.5 seconds and advances automatically. Each new question starts with a clear screen and calibrates again. With voice enabled, the Mac reads back the accepted answer. Start and Send use touch. See the [demo trial criteria](doc/PROJECT_SETUP.md#demo-decisions).

`--gesture` enables the optional U127 hand-swipe input. Connect the unit to the red Port A on the CoreS3 body. When the host reports **Gesture input ready**, swipe right for **Yes** and left for **No**, about 5–15 cm above the sensor. Use the touchscreen for **Send**.

## Firmware

Build the two sketches in `firmware/` with the M5Stack Arduino ESP32 board package. Use the `M5CoreS3` and `M5StickS3` board profiles with **USB CDC On Boot** enabled and **Hardware CDC and JTAG**. Both use M5Unified. The CoreS3 sketch also uses M5Unit-GESTURE and its library dependencies. Upload each sketch to its corresponding board before starting the Mac host.

## Run without boards

```sh
uv run --python 3.12 python -m bedside --simulate both --voice local
```

To test the lower-leg path, enter `start`, `yes`, `send`, `no`, `no`, `yes`, `send`, and `ack` on separate lines. Wait for the next prompt after each Yes or No. `--simulate patient` or `--simulate caregiver` replaces only one board. Use `quit` to stop a simulated run or Ctrl+C to stop the physical demo.

The [showcase runbook](doc/SHOWCASE.md) has the walk-up demo. The [project setup](doc/PROJECT_SETUP.md) and [user story](doc/USER_STORY_AND_DEMO.md) contain the original build plan.
