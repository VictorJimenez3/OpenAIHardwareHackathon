#include <Arduino.h>
#include <M5Unified.h>
#include "PatientCamera.h"
#include "PatientGesture.h"

// CoreS3 patient-side display. The Mac owns the check-in state machine and
// sends one newline-delimited JSON "screen" event at a time.
//
// Device -> Mac:
//   {"type":"hello","role":"patient"}
//   {"type":"input","prompt_id":"pain-1","value":"yes","source":"touch"}
// Mac -> device:
//   {"type":"ping"}
//   {"type":"screen","prompt_id":"pain-1","title":"...","text":"...",
//    "left_label":"Yes","left_value":"yes","right_label":"No","right_value":"no",
//    "status":"Touch an answer"}

namespace {

// M5GFX's default color type is RGB565. Keeping the source colors in RGB888
// and converting once prevents accidental truncation of six-digit colors.
constexpr uint16_t rgb565(uint8_t red, uint8_t green, uint8_t blue) {
  return static_cast<uint16_t>(((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3));
}

constexpr uint16_t kBackground = rgb565(11, 18, 32);
constexpr uint16_t kPanel = rgb565(22, 36, 58);
constexpr uint16_t kPanelAlt = rgb565(30, 51, 80);
constexpr uint16_t kText = rgb565(243, 247, 255);
constexpr uint16_t kMuted = rgb565(156, 175, 200);
constexpr uint16_t kAccent = rgb565(53, 208, 186);
constexpr uint16_t kReceiptBackground = rgb565(19, 79, 80);
constexpr uint16_t kWarning = rgb565(246, 196, 83);

int screenWidth = 320;
int screenHeight = 240;

String promptId = "start-1";
String titleText = "Start a check-in";
String bodyText = "Use the large buttons to answer. You can review the exact message before it is shared.";
String leftLabel = "Start";
String leftValue = "start";
String rightLabel;
String rightValue;
String statusText = "Waiting for your choice";
String hostStatusText;
String receiptText;
String pendingInput;
String serialLine;
unsigned long lastHelloAt = 0;
bool connected = false;
bool contentDirty = true;
bool connectionDirty = false;
bool receiptDirty = false;
bool statusDirty = false;
M5Canvas canvas(&M5.Display);
bool hasCanvas = false;
bool inputLatched = false;
bool touchArmed = true;
PatientTransport transport;
PatientCamera patientCamera;
PatientGesture patientGesture;

struct Button {
  int x;
  int y;
  int w;
  int h;
  String label;
  String value;
  bool visible;
};

String jsonValue(const String &json, const char *key) {
  String needle = String("\"") + key + "\"";
  int keyAt = json.indexOf(needle);
  if (keyAt < 0) return String();
  int colon = json.indexOf(':', keyAt + needle.length());
  if (colon < 0) return String();
  int quote = colon + 1;
  while (quote < json.length() && (json[quote] == ' ' || json[quote] == '\t')) ++quote;
  if (quote >= json.length() || json[quote] != '"') return String();

  String out;
  bool escaped = false;
  for (int i = quote + 1; i < json.length(); ++i) {
    char c = json[i];
    if (escaped) {
      if (c == 'n') out += '\n';
      else if (c == 'r') out += '\r';
      else if (c == 't') out += '\t';
      else out += c;
      escaped = false;
    } else if (c == '\\') {
      escaped = true;
    } else if (c == '"') {
      return out;
    } else {
      out += c;
    }
  }
  return String();
}

bool jsonTrue(const String &json, const char *key) {
  String needle = String("\"") + key + "\"";
  int keyAt = json.indexOf(needle);
  if (keyAt < 0) return false;
  int colon = json.indexOf(':', keyAt + needle.length());
  if (colon < 0) return false;
  String value = json.substring(colon + 1);
  value.trim();
  return value.startsWith("true");
}

bool setText(String& current, const String& next) {
  if (current == next) return false;
  current = next;
  return true;
}

void setStatus(const String& next) {
  statusDirty |= setText(statusText, next);
}

void setReceipt(const String& next) {
  receiptDirty |= setText(receiptText, next);
}

void markConnected() {
  if (connected) return;
  connected = true;
  connectionDirty = true;
}

void sendHello() {
  transport.tryControl(F("{\"type\":\"hello\",\"role\":\"patient\"}\n"));
  lastHelloAt = millis();
}

void sendPendingInput() {
  if (!pendingInput.isEmpty() && transport.tryControl(pendingInput)) {
    pendingInput = "";
    setStatus("Response sent");
  }
}

void sendInput(const String& value, const char* source) {
  if (value.isEmpty() || inputLatched) return;
  pendingInput = String(F("{\"type\":\"input\",\"prompt_id\":\"")) +
                 jsonEscape(promptId) + F("\",\"value\":\"") + jsonEscape(value) +
                 F("\",\"source\":\"") + source + F("\"}\n");
  inputLatched = true;
  sendPendingInput();
}

void drawWrapped(m5gfx::LovyanGFX& target, const String &text, int x, int y, int maxWidth, int lineHeight, int maxLines) {
  String line;
  int lineNo = 0;
  int start = 0;
  while (start <= text.length() && lineNo < maxLines) {
    int end = text.indexOf(' ', start);
    if (end < 0) end = text.length();
    String word = text.substring(start, end);
    String candidate = line.isEmpty() ? word : line + " " + word;
    if (!line.isEmpty() && target.textWidth(candidate) > maxWidth) {
      target.drawString(line, x, y + lineNo * lineHeight);
      line = word;
      ++lineNo;
    } else {
      line = candidate;
    }
    start = end + 1;
    if (start > text.length()) break;
  }
  if (lineNo < maxLines && !line.isEmpty()) {
    target.drawString(line, x, y + lineNo * lineHeight);
  }
}

Button buttonForSide(bool left) {
  const int padding = max(14, screenWidth / 18);
  const int gap = 10;
  const int height = min(64, max(56, screenHeight / 4));
  const bool hasBoth = !leftLabel.isEmpty() && !rightLabel.isEmpty();
  const int width = hasBoth ? (screenWidth - 2 * padding - gap) / 2 : screenWidth - 2 * padding;
  const int x = !left && hasBoth ? padding + width + gap : padding;
  const String& label = left ? leftLabel : rightLabel;
  return Button{x, screenHeight - height - 20, width, height, label,
                left ? leftValue : rightValue, !label.isEmpty()};
}

Button buttonForTouch(int16_t x, int16_t y) {
  for (bool left : {true, false}) {
    Button button = buttonForSide(left);
    if (button.visible && x >= button.x && x < button.x + button.w &&
        y >= button.y && y < button.y + button.h) return button;
  }
  return Button{0, 0, 0, 0, String(), String(), false};
}

void drawButton(m5gfx::LovyanGFX& target, const Button& button) {
  if (!button.visible) return;
  target.fillRoundRect(button.x, button.y, button.w, button.h, 12, kPanelAlt);
  target.drawRoundRect(button.x, button.y, button.w, button.h, 12, kAccent);
  target.setTextColor(kText);
  target.setTextSize(2);
  const int textX = button.x + (button.w - target.textWidth(button.label)) / 2;
  target.drawString(button.label, textX, button.y + (button.h - 16) / 2);
}

void drawConnection(m5gfx::LovyanGFX& target) {
  target.fillRect(screenWidth - 86, 0, 86, 36, kPanel);
  const String label = connected ? F("CONNECTED") : F("CONNECTING");
  const uint16_t color = connected ? kAccent : kWarning;
  target.setTextSize(1);
  target.setTextColor(color);
  const int dotX = screenWidth - 10;
  target.drawString(label, dotX - 8 - target.textWidth(label), 12);
  target.fillCircle(dotX, 16, 5, color);
}

constexpr int kReceiptY = 128;
constexpr int kReceiptHeight = 31;

void drawReceipt(m5gfx::LovyanGFX& target) {
  const int padding = max(14, screenWidth / 18);
  const int width = screenWidth - 2 * padding;
  target.fillRect(padding, kReceiptY, width, kReceiptHeight, kBackground);
  if (receiptText.isEmpty()) return;
  target.fillRoundRect(padding, kReceiptY, width, kReceiptHeight, 4, kReceiptBackground);
  const int separator = receiptText.indexOf(": ");
  const String answer = separator < 0 ? String() : receiptText.substring(separator + 2);
  target.setTextColor(kText);
  if (answer == "Yes" || answer == "No") {
    target.setTextSize(3);
    target.drawString(answer + " recorded", padding + 8, kReceiptY + 4);

    String method = receiptText.substring(0, separator);
    if (method.endsWith(" accepted")) method.remove(method.length() - 9);
    target.setTextColor(kAccent);
    target.setTextSize(1);
    const int methodX = screenWidth - padding - 8 - target.textWidth(method);
    target.drawString(method, methodX, kReceiptY + 12);
    return;
  }

  target.setTextSize(2);
  drawWrapped(target, receiptText, padding + 8, kReceiptY + 7, width - 16, 16, 1);
}

void drawStatus(m5gfx::LovyanGFX& target) {
  const int padding = max(14, screenWidth / 18);
  target.fillRect(0, screenHeight - 20, screenWidth, 20, kBackground);
  target.setTextColor(kMuted);
  target.setTextSize(1);
  drawWrapped(target, statusText, padding, screenHeight - 18, screenWidth - 2 * padding, 8, 2);
}

void pushRegion(int x, int y, int width, int height) {
  if (!hasCanvas) return;
  M5.Display.setClipRect(x, y, width, height);
  canvas.pushSprite(0, 0);
  M5.Display.clearClipRect();
}

void paint() {
  if (!contentDirty && !connectionDirty && !receiptDirty && !statusDirty) return;
  m5gfx::LovyanGFX& target = hasCanvas ? static_cast<m5gfx::LovyanGFX&>(canvas) : M5.Display;
  M5.Display.startWrite();
  if (contentDirty) {
    const int padding = max(14, screenWidth / 18);
    target.fillScreen(kBackground);
    target.fillRect(0, 0, screenWidth, 36, kPanel);
    target.setTextSize(1);
    target.setTextColor(kMuted);
    target.drawString(F("BEDSIDE CHECK-IN"), padding, 12);
    target.setTextColor(kText);
    target.setTextSize(2);
    drawWrapped(target, titleText, padding, 42, screenWidth - 2 * padding, 20, 2);
    target.setTextColor(kMuted);
    drawWrapped(target, bodyText, padding, 78, screenWidth - 2 * padding, 17, 3);
    drawButton(target, buttonForSide(true));
    drawButton(target, buttonForSide(false));
    drawConnection(target);
    drawReceipt(target);
    drawStatus(target);
    if (hasCanvas) canvas.pushSprite(0, 0);
  } else {
    if (connectionDirty) {
      drawConnection(target);
      pushRegion(screenWidth - 86, 0, 86, 36);
    }
    if (receiptDirty) {
      drawReceipt(target);
      pushRegion(0, kReceiptY, screenWidth, kReceiptHeight);
    }
    if (statusDirty) {
      drawStatus(target);
      pushRegion(0, screenHeight - 20, screenWidth, 20);
    }
  }
  M5.Display.endWrite();
  contentDirty = connectionDirty = receiptDirty = statusDirty = false;
}

void handleScreen(const String& line) {
  const String nextPrompt = jsonValue(line, "prompt_id");
  if (nextPrompt.isEmpty()) return;
  const bool newPrompt = setText(promptId, nextPrompt);
  const bool deliberateRetry = jsonTrue(line, "retry") || jsonTrue(line, "reset_input");
  if (newPrompt || deliberateRetry) {
    inputLatched = false;
    pendingInput = "";
    // Keep a finger already on the glass from answering the next question.
    touchArmed = !M5.Touch.getDetail().isPressed();
  }
  if (newPrompt) patientCamera.setPrompt(promptId);

  contentDirty |= setText(titleText, jsonValue(line, "title"));
  contentDirty |= setText(bodyText, jsonValue(line, "text"));
  contentDirty |= setText(leftLabel, jsonValue(line, "left_label"));
  contentDirty |= setText(rightLabel, jsonValue(line, "right_label"));
  leftValue = jsonValue(line, "left_value");
  rightValue = jsonValue(line, "right_value");
  const bool statusChanged = setText(hostStatusText, jsonValue(line, "status"));
  if (newPrompt || deliberateRetry || statusChanged) setStatus(hostStatusText);
  if (line.indexOf("\"receipt\"") >= 0) setReceipt(jsonValue(line, "receipt"));
  markConnected();
}

void handleLine(const String& line) {
  const String type = jsonValue(line, "type");
  if (type == "screen") {
    handleScreen(line);
  } else if (type == "answer_feedback") {
    if (jsonValue(line, "prompt_id") != promptId) return;
    const String answer = jsonValue(line, "answer");
    const String method = jsonValue(line, "method");
    if (answer.isEmpty() || method.isEmpty()) return;
    setReceipt((method == "Touch accepted" ? String("Touch") : method) + ": " + answer);
    inputLatched = true;
    pendingInput = "";
  } else if (type == "camera") {
    String cameraPrompt = jsonValue(line, "prompt_id");
    if (cameraPrompt.isEmpty()) cameraPrompt = promptId;
    patientCamera.setEnabled(jsonTrue(line, "enabled"), cameraPrompt, transport);
  } else if (type == "gesture") {
    String gesturePrompt = jsonValue(line, "prompt_id");
    if (gesturePrompt.isEmpty()) gesturePrompt = promptId;
    const bool enabled = jsonTrue(line, "enabled");
    const bool gestureReady = patientGesture.setEnabled(enabled, gesturePrompt, transport);
    setStatus(!enabled ? "Gesture input off" :
                        (gestureReady ? "Gesture input active" : "Gesture unavailable"));
  } else if (type == "ping") {
    markConnected();
    sendHello();
  }
}

void readSerial() {
  static bool discardLine = false;
  while (Serial.available() > 0) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\n') {
      if (!discardLine) {
        serialLine.trim();
        if (serialLine.startsWith("{") && serialLine.endsWith("}")) handleLine(serialLine);
      }
      serialLine = "";
      discardLine = false;
    } else if (c != '\r' && !discardLine) {
      if (c == '\x18' || serialLine.length() >= 2048 || !serialLine.concat(c)) discardLine = true;
    }
  }
}

}  // namespace

void setup() {
  transport.begin();
  auto config = M5.config();
  M5.begin(config);
  // CoreS3 rotation 0 is the physical landscape orientation used by the
  // camera and the demo stand. Keep a defensive landscape fallback for a
  // board profile that reports portrait dimensions.
  M5.Display.setRotation(0);
  screenWidth = M5.Display.width();
  screenHeight = M5.Display.height();
  if (screenWidth < screenHeight) {
    M5.Display.setRotation(3);
    screenWidth = M5.Display.width();
    screenHeight = M5.Display.height();
  }
  M5.Display.setTextDatum(top_left);
  M5.Display.setTextFont(1);
  M5.Display.setTextWrap(false);
  // Keep the backing buffer out of camera DMA memory. If PSRAM is
  // unavailable, draw directly, retaining status/receipt-only updates.
  if (psramFound()) {
    canvas.setPsram(true);
    canvas.setColorDepth(16);
    hasCanvas = canvas.createSprite(screenWidth, screenHeight) != nullptr;
    if (hasCanvas) {
      canvas.setTextDatum(top_left);
      canvas.setTextFont(1);
      canvas.setTextWrap(false);
    }
  }
  sendHello();
  paint();
}

void loop() {
  M5.update();
  readSerial();
  transport.update();
  sendPendingInput();

  String gestureValue;
  const bool allowGestureYes = leftValue == "yes" || rightValue == "yes";
  const bool allowGestureNo = leftValue == "no" || rightValue == "no";
  const bool allowGestureStart = leftValue == "start" || rightValue == "start";
  if (patientGesture.poll(promptId, allowGestureYes, allowGestureNo, allowGestureStart,
                          inputLatched, gestureValue)) {
    sendInput(gestureValue, "gesture");
  }

  // Re-announce after reconnects and make the serial link self-discovering.
  if (millis() - lastHelloAt > 5000) sendHello();
  auto touch = M5.Touch.getDetail();
  // Arm from the actual current state, not only the one-frame release event.
  // A screen update can arrive after the release frame and before the next
  // tap, which would otherwise discard the patient's first answer.
  if (!touch.isPressed()) touchArmed = true;
  if (touch.wasPressed() && touchArmed && !inputLatched) {
    Button button = buttonForTouch(touch.x, touch.y);
    if (button.visible && !button.value.isEmpty()) {
      touchArmed = false;
      sendInput(button.value, "touch");
    }
  }
  paint();
  patientCamera.update(transport, promptId);
  delay(8);
}
