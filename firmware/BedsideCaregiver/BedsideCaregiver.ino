#include <M5Unified.h>

// Caregiver-side display for the bedside communication prototype.
//
// The Mac owns the conversation. This board only renders the exact approved
// sentence and turns one fresh press of BtnA into an acknowledgment event.
// Messages are newline-delimited JSON over the board's USB serial port.

namespace {

constexpr uint32_t kBaud = 115200;
constexpr uint32_t kHelloPeriodMs = 2000;
constexpr int kMaxLines = 8;

String currentRequestId;
String currentText;
bool acknowledged = false;
bool hasRequest = false;
bool acknowledgeArmed = false;
uint32_t lastHelloMs = 0;

int displayTextSize() {
  // StickS3 is 240x135 in landscape. Keep the same readable layout if a
  // different rotation or a smaller compatible panel is selected.
  return M5.Display.width() >= 200 ? 2 : 1;
}

int maxCharsPerLine() {
  const int glyphWidth = 6 * displayTextSize();
  const long chars = (M5.Display.width() - 4) / glyphWidth;
  return chars < 1 ? 1 : static_cast<int>(chars);
}

String jsonStringField(const String &line, const char *key) {
  String marker = String("\"") + key + "\"";
  int keyStart = line.indexOf(marker);
  if (keyStart < 0) return String();

  int colon = line.indexOf(':', keyStart + marker.length());
  if (colon < 0) return String();
  int quote = colon + 1;
  while (quote < line.length() && (line[quote] == ' ' || line[quote] == '\t')) quote++;
  if (quote >= line.length() || line[quote] != '"') return String();

  String value;
  bool escaped = false;
  for (int i = quote + 1; i < line.length(); ++i) {
    char c = line[i];
    if (escaped) {
      if (c == 'n') value += '\n';
      else if (c == 'r') value += '\r';
      else if (c == 't') value += '\t';
      else value += c;
      escaped = false;
    } else if (c == '\\') {
      escaped = true;
    } else if (c == '"') {
      return value;
    } else {
      value += c;
    }
  }
  return String();
}

String jsonQuote(const String &value) {
  String escaped = "\"";
  for (size_t i = 0; i < value.length(); ++i) {
    char c = value[i];
    if (c == '\\') escaped += "\\\\";
    else if (c == '"') escaped += "\\\"";
    else if (c == '\n') escaped += "\\n";
    else if (c == '\r') escaped += "\\r";
    else escaped += c;
  }
  escaped += '"';
  return escaped;
}

void sendHello() {
  Serial.println(F("{\"type\":\"hello\",\"role\":\"caregiver\"}"));
  Serial.flush();
}

void sendDisplayed() {
  Serial.print(F("{\"type\":\"displayed\",\"request_id\":"));
  Serial.print(jsonQuote(currentRequestId));
  Serial.println(F("}"));
  Serial.flush();
}

void sendAcknowledged() {
  Serial.print(F("{\"type\":\"acknowledged\",\"request_id\":"));
  Serial.print(jsonQuote(currentRequestId));
  Serial.println(F("}"));
  Serial.flush();
}

// Split a sentence into display-width-safe lines. StickS3's small screen is
// easiest to read with the built-in 6-pixel font at text size one.
int wrapText(const String &text, String lines[], int maxLines) {
  int count = 0;
  String line;
  int start = 0;

  while (start < text.length() && count < maxLines) {
    while (start < text.length() && text[start] == ' ') start++;
    if (start >= text.length()) break;
    int space = text.indexOf(' ', start);
    if (space < 0) space = text.length();
    String word = text.substring(start, space);
    start = space + 1;

    while (word.length() > 0 && count < maxLines) {
      // Break unusually long words so no text runs off the physical display.
      if (word.length() > maxCharsPerLine()) {
        if (line.length() > 0) {
          lines[count++] = line;
          line = String();
          if (count >= maxLines) break;
        }
        lines[count++] = word.substring(0, maxCharsPerLine());
        word = word.substring(maxCharsPerLine());
        continue;
      }

      if (line.length() == 0) {
        line = word;
      } else if (line.length() + 1 + word.length() <= maxCharsPerLine()) {
        line += ' ';
        line += word;
      } else {
        lines[count++] = line;
        line = word;
      }
      word = String();
    }
  }

  if (line.length() > 0 && count < maxLines) lines[count++] = line;
  return count;
}

void drawCentered(const String &text, int y) {
  int width = M5.Display.textWidth(text);
  int x = (M5.Display.width() - width) / 2;
  if (x < 0) x = 0;
  M5.Display.drawString(text, x, y);
}

void drawWaiting() {
  M5.Display.fillScreen(TFT_BLACK);
  M5.Display.setTextSize(displayTextSize());
  M5.Display.setTextColor(TFT_CYAN, TFT_BLACK);
  drawCentered("CAREGIVER DISPLAY", 1);
  M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
  drawCentered("Waiting for patient", 36);
  drawCentered("message...", 56);
}

void drawRequest() {
  M5.Display.fillScreen(TFT_BLACK);
  const int textSize = displayTextSize();
  const int lineHeight = 8 * textSize;
  M5.Display.setTextSize(textSize);
  M5.Display.setTextColor(acknowledged ? TFT_GREEN : TFT_CYAN, TFT_BLACK);
  drawCentered(acknowledged ? "ACKNOWLEDGED" : "NEW PATIENT MESSAGE", 1);

  String lines[kMaxLines];
  int lineCount = wrapText(currentText, lines, 5);
  M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
  const int firstY = 13;
  for (int i = 0; i < lineCount; ++i) {
    M5.Display.drawString(lines[i], 2, (firstY * textSize) + (i * lineHeight));
  }

  M5.Display.setTextColor(acknowledged ? TFT_GREEN : TFT_YELLOW, TFT_BLACK);
  if (!acknowledged) drawCentered("Press blue button", M5.Display.height() - (10 * textSize));
}

void handleMessage(const String &line) {
  String type = jsonStringField(line, "type");
  if (type == "ping") {
    sendHello();
    return;
  }

  if (type == "clear_request") {
    String requestId = jsonStringField(line, "request_id");
    // A stale clear from a previous session must never erase a newer request.
    if (hasRequest && requestId == currentRequestId) {
      hasRequest = false;
      acknowledged = false;
      currentRequestId = String();
      currentText = String();
      drawWaiting();
    }
    return;
  }

  if (type != "show_request") return;
  String requestId = jsonStringField(line, "request_id");
  String text = jsonStringField(line, "text");
  if (requestId.length() == 0 || text.length() == 0) return;

  // Re-delivery of the same request is safe. Keep its acknowledgment bit so
  // reconnect/retry cannot generate a second acknowledgment for one message.
  bool sameRequest = hasRequest && requestId == currentRequestId;
  if (!sameRequest) {
    acknowledged = false;
    acknowledgeArmed = false;
  }
  currentRequestId = requestId;
  currentText = text;
  hasRequest = true;
  drawRequest();
  sendDisplayed();
  // A host reconnect can resend show_request after the caregiver already
  // pressed A. Replay the acknowledgment so the host can recover its state,
  // while keeping the button locked for this request.
  if (sameRequest && acknowledged) sendAcknowledged();
}

void readSerial() {
  static String line;
  static bool discardLine = false;
  while (Serial.available() > 0) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\n') {
      if (!discardLine) {
        line.trim();
        if (line.startsWith("{") && line.endsWith("}")) handleMessage(line);
      }
      line = "";
      discardLine = false;
    } else if (c != '\r' && !discardLine) {
      if (c == '\x18' || line.length() >= 768 || !line.concat(c)) discardLine = true;
    }
  }
}

}  // namespace

void setup() {
  auto config = M5.config();
  M5.begin(config);
  M5.Display.setRotation(1);
  M5.Display.setTextWrap(false);
#if ARDUINO_USB_CDC_ON_BOOT && ARDUINO_USB_MODE
  Serial.setRxBufferSize(2048);
#endif
  Serial.begin(kBaud);
  delay(100);
  drawWaiting();
  sendHello();
  lastHelloMs = millis();
}

void loop() {
  M5.update();
  readSerial();

  if (hasRequest && !M5.BtnA.isPressed()) acknowledgeArmed = true;
  if (hasRequest && !acknowledged && acknowledgeArmed && M5.BtnA.wasPressed()) {
    acknowledged = true;
    drawRequest();
    sendAcknowledged();
  }

  uint32_t now = millis();
  if (now - lastHelloMs >= kHelloPeriodMs) {
    sendHello();
    lastHelloMs = now;
  }
  delay(5);
}
