#pragma once

// Optional CoreS3 camera transport.  The patient UI remains usable when the
// camera library is not installed or the sensor cannot be initialized.
// Frames are deliberately sent as one newline-delimited JSON event so the Mac
// can keep the same serial connection for screen events and camera samples.

#include <Arduino.h>
#include "PatientTransport.h"

#if __has_include(<esp_camera.h>) && __has_include(<M5Unified.h>)
#define BEDSIDE_PATIENT_CAMERA_AVAILABLE 1
#include <esp_camera.h>
#include <M5Unified.h>
#include <esp_timer.h>
#include <mbedtls/base64.h>
#else
#define BEDSIDE_PATIENT_CAMERA_AVAILABLE 0
#endif

class PatientCamera {
 public:
  PatientCamera() = default;

  // Camera startup is lazy and runs once for a continuous host stream.
  bool setEnabled(bool enabled, const String& promptId, PatientTransport& output) {
    setPrompt(promptId);
    if (!enabled) {
      enabled_ = false;
      shutdown();
      return true;
    }
    if (enabled_) return started_;

    enabled_ = true;
    lastCaptureAt_ = millis();
    if (!output.supportsFrames()) return fail(output, "camera_tx_buffer_unavailable");
    return ensureStarted(output);
  }

  void setPrompt(const String& promptId) {
    if (promptId == promptId_) return;
    promptId_ = promptId;
    sequence_ = 0;
#if BEDSIDE_PATIENT_CAMERA_AVAILABLE
    promptStartedAt_ = esp_timer_get_time();
#endif
  }

  void update(PatientTransport& output, const String& displayedPrompt) {
#if BEDSIDE_PATIENT_CAMERA_AVAILABLE
    if (!enabled_ || !started_ || promptId_ != displayedPrompt) return;

    // fb_get can wait for seconds on a dead or stalled sensor. This sketch
    // has a single camera consumer, so only take a buffer already queued.
    if (!esp_camera_available_frames()) {
      if (millis() - lastCaptureAt_ >= kCaptureTimeoutMs) fail(output, "capture_failed");
      return;
    }
    if (millis() - lastFrameAt_ < kFrameIntervalMs) return;
    camera_fb_t* frame = esp_camera_fb_get();
    if (!frame) {
      if (millis() - lastCaptureAt_ >= kCaptureTimeoutMs) fail(output, "capture_failed");
      return;
    }

    lastCaptureAt_ = millis();
    const int64_t capturedAt = int64_t(frame->timestamp.tv_sec) * 1000000 + frame->timestamp.tv_usec;
    // A buffered frame may have started before the latest prompt was shown.
    // Release it without spending the new prompt's frame interval.
    if (capturedAt >= promptStartedAt_) {
      lastFrameAt_ = lastCaptureAt_;
      sendFrame(output, frame);
    }
    esp_camera_fb_return(frame);
    if (conversionFailures_ >= 5) fail(output, "capture_failed");
#else
    (void)output;
    (void)displayedPrompt;
#endif
  }

 private:
  static constexpr uint32_t kFrameIntervalMs = 80;
  static constexpr uint32_t kCaptureTimeoutMs = 1000;
  static constexpr size_t kMaxJpegBytes = 40 * 1024;
  static constexpr size_t kMaxFrameBytes = 4 * ((kMaxJpegBytes + 2) / 3) + 512;
  bool enabled_ = false;
  bool started_ = false;
  uint32_t lastFrameAt_ = 0;
  uint32_t lastCaptureAt_ = 0;
  uint32_t sequence_ = 0;
  uint8_t conversionFailures_ = 0;
  String promptId_;
#if BEDSIDE_PATIENT_CAMERA_AVAILABLE
  int64_t promptStartedAt_ = 0;
  char* frameLine_ = nullptr;
#endif

  bool fail(PatientTransport& output, const char* error) {
    output.sendControl(String(F("{\"type\":\"camera_error\",\"prompt_id\":\"")) +
                       jsonEscape(promptId_) + F("\",\"error\":\"") + error + F("\"}\n"));
    enabled_ = false;
    shutdown();
    return false;
  }

  bool ensureStarted(PatientTransport& output) {
#if BEDSIDE_PATIENT_CAMERA_AVAILABLE
    frameLine_ = static_cast<char*>(ps_malloc(kMaxFrameBytes));
    if (!frameLine_) return fail(output, "camera_frame_buffer_unavailable");
    conversionFailures_ = 0;
    // The M5Stack CoreS3 board profile initializes the internal bus before
    // setup() returns. The ESP camera SCCB-ng driver can register the GC0308
    // as another device on that active bus. This keeps touch available while
    // the camera is streaming and avoids taking ownership of the bus.
    camera_config_t config = {
        .pin_pwdn = -1,
        .pin_reset = -1,
        .pin_xclk = -1,
        .pin_sccb_sda = -1,
        .pin_sccb_scl = -1,
        .pin_d7 = 47,
        .pin_d6 = 48,
        .pin_d5 = 16,
        .pin_d4 = 15,
        .pin_d3 = 42,
        .pin_d2 = 41,
        .pin_d1 = 40,
        .pin_d0 = 39,
        .pin_vsync = 46,
        .pin_href = 38,
        .pin_pclk = 45,
        .xclk_freq_hz = 20000000,
        .ledc_timer = LEDC_TIMER_0,
        .ledc_channel = LEDC_CHANNEL_0,
        .pixel_format = PIXFORMAT_RGB565,
        // Allocate the capture buffers at the native size used for the
        // head-motion samples. Do not resize the sensor after init because the
        // driver has already allocated fixed-size frame buffers.
        .frame_size = FRAMESIZE_QVGA,
        .jpeg_quality = 0,
        .fb_count = 2,
        .fb_location = CAMERA_FB_IN_PSRAM,
        .grab_mode = CAMERA_GRAB_WHEN_EMPTY,
        .sccb_i2c_port = M5.In_I2C.getPort(),
    };
    if (config.sccb_i2c_port < 0 || esp_camera_init(&config) != ESP_OK) {
      return fail(output, "camera_init_failed");
    }
    lastCaptureAt_ = millis();
    started_ = true;
    return true;
#else
    return fail(output, "camera_library_unavailable");
#endif
  }

  void shutdown() {
#if BEDSIDE_PATIENT_CAMERA_AVAILABLE
    if (started_) {
      // The SCCB-ng driver removes only the borrowed camera device.
      esp_camera_deinit();
      started_ = false;
    }
    free(frameLine_);
    frameLine_ = nullptr;
#endif
  }

#if BEDSIDE_PATIENT_CAMERA_AVAILABLE
  void sendFrame(PatientTransport& output, camera_fb_t* frame) {
    const String header = String(F("{\"type\":\"frame\",\"prompt_id\":\"")) +
                          jsonEscape(promptId_) + F("\",\"seq\":") + sequence_ + F(",\"jpeg\":\"");
    constexpr size_t kTrailerSize = 3;  // closing quote, brace, newline
    if (!output.canSendFrame(header.length() + kTrailerSize)) return;

    const uint8_t* jpeg = frame->buf;
    size_t jpegLength = frame->len;
    uint8_t* converted = nullptr;
    if (frame->format != PIXFORMAT_JPEG) {
      if (!frame2jpg(frame, 40, &converted, &jpegLength)) {
        // Return the camera buffer before deinitializing on repeated failure.
        free(converted);
        ++conversionFailures_;
        return;
      }
      jpeg = converted;
    }
    conversionFailures_ = 0;
    if (jpegLength <= kMaxJpegBytes) {
      const size_t headerSize = header.length();
      const size_t encodedSize = 4 * ((jpegLength + 2) / 3);
      const size_t lineSize = headerSize + encodedSize + kTrailerSize;
      if (lineSize <= kMaxFrameBytes && output.canSendFrame(lineSize)) {
        memcpy(frameLine_, header.c_str(), headerSize);
        size_t encoded = 0;
        if (mbedtls_base64_encode(reinterpret_cast<unsigned char*>(frameLine_ + headerSize),
                                  kMaxFrameBytes - headerSize, &encoded, jpeg, jpegLength) == 0 &&
            encoded == encodedSize) {
          memcpy(frameLine_ + headerSize + encoded, "\"}\n", kTrailerSize);
          if (output.sendFrame(frameLine_, lineSize)) ++sequence_;
        }
      }
    }
    free(converted);
  }
#endif
};
