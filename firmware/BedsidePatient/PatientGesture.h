#pragma once

// Optional, opt-in gesture input for the M5Stack Unit Gesture (U127).
//
// The unit is the PAJ7620U2 at I2C address 0x73.  On CoreS3, Port.A is
// SDA=2/SCL=1.  M5UnitUnified's board-aware wiring helper selects those pins
// without opening the CoreS3 internal I2C bus used by the camera/touch stack.
// The sensor is never probed during boot: the Mac must send
// {"type":"gesture","enabled":true} first.

#include <Arduino.h>
#include <Wire.h>
#include <M5UnitUnified.h>
#include <M5UnitUnifiedGESTURE.h>
#include <wiring/m5_unit_unified_wiring.hpp>

class PatientGesture {
 public:
  PatientGesture() = default;

  static constexpr uint8_t kGestureAddress = 0x73;
  static constexpr int kPortASda = 2;
  static constexpr int kPortAScl = 1;
  static constexpr int kPortBSda = 9;
  static constexpr int kPortBScl = 8;
  static constexpr uint32_t kGestureClock = 100000;

  // Enable or disable one gesture session.  A ready/error event is emitted at
  // most once for each enable cycle.  A missing sensor is a reported feature
  // error, not a reason to stop the touch UI or the serial loop.
  bool setEnabled(bool enabled, const String& promptId, Stream& output) {
    if (!enabled) {
      enabled_ = false;
      readySent_ = false;
      responseLatched_ = false;
      gestureArmed_ = false;
      promptId_ = promptId;
      if (attached_) sensor_.stopPeriodicMeasurement();
      return true;
    }

    enabled_ = true;
    if (!promptId.isEmpty() && promptId != promptId_) {
      promptId_ = promptId;
      responseLatched_ = false;
    }
    if (readySent_) return ready_;

    // M5.begin() normally enables the CoreS3 Grove 5 V rail. Keep the call
    // opt-in and conditional so an otherwise touch-only boot is unchanged.
    if (!M5.Power.getExtOutput()) M5.Power.setExtOutput(true);

    // Probe the documented red Port.A explicitly at 100 kHz. If the unit was
    // accidentally put on the DinBase black Port.B, try that exposed GPIO
    // pair as a bounded recovery; both paths use the same four-wire 5 V/GND
    // Grove supply and the ESP32 GPIO matrix supports I2C on either pair.
    int probeA = -1;
    int probeB = -1;
    if (!attached_) {
      probeA = beginAndProbeBus(kPortASda, kPortAScl);
      if (probeA == 0) {
        selectedSda_ = kPortASda;
        selectedScl_ = kPortAScl;
      } else {
        probeB = beginAndProbeBus(kPortBSda, kPortBScl);
        if (probeB == 0) {
          selectedSda_ = kPortBSda;
          selectedScl_ = kPortBScl;
        }
      }
      if (selectedSda_ < 0 || !units_.add(sensor_, Wire)) {
        sendProbeError(output, probeA, probeB);
        enabled_ = false;
        readySent_ = true;
        ready_ = false;
        return false;
      }
      attached_ = true;
    } else {
      // On a re-enable, the existing UnitUnified adapter points at Wire; keep
      // the selected bus and only perform a short presence probe.
      const int probe = beginAndProbeBus(selectedSda_, selectedScl_);
      if (probe != 0) {
        sendProbeError(output, probe, -1);
        enabled_ = false;
        readySent_ = true;
        ready_ = false;
        return false;
      }
    }

    auto cfg = sensor_.config();
    cfg.start_periodic = true;
    cfg.mode = m5::unit::paj7620u2::Mode::Gesture;
    cfg.frequency = m5::unit::paj7620u2::Frequency::Normal;
    cfg.hflip = false;
    cfg.vflip = true;
    cfg.rotation = 0;
    cfg.store_on_change = true;
    sensor_.config(cfg);

    if (!units_.begin()) {
      sendError(output, "gesture_init");
      enabled_ = false;
      readySent_ = true;
      ready_ = false;
      return false;
    }

    ready_ = true;
    readySent_ = true;
    responseLatched_ = false;
    gestureArmed_ = false;
    output.println(F("{\"type\":\"gesture_ready\"}"));
    output.flush();
    return true;
  }

  bool enabled() const { return enabled_; }
  bool ready() const { return ready_; }

  // Poll once without blocking.  Returns a semantic response only for a
  // fresh horizontal swipe.  Right means yes (or start on the start prompt),
  // Left means no.  All other PAJ7620U2 gestures are intentionally ignored:
  // there is no gesture for Send, Retry, or any other irreversible action.
  // `externalInputLatched` is the touch-side latch; sensor readings are still
  // consumed when touch won the race so they cannot leak into the next prompt.
  bool poll(const String& promptId, bool allowYes, bool allowNo, bool allowStart,
            bool externalInputLatched, String& value) {
    value = String();
    if (!enabled_ || !ready_) return false;

    if (promptId != promptId_) {
      promptId_ = promptId;
      responseLatched_ = false;
    }

    units_.update();
    if (!sensor_.updated()) return false;

    const auto gesture = sensor_.gesture();
    sensor_.discard();
    // Require a neutral reading between horizontal swipes.  This prevents a
    // hand held in place, or a stale reading that arrived while touch won the
    // race, from becoming an answer for the next prompt.
    if (gesture == m5::unit::paj7620u2::Gesture::None) {
      gestureArmed_ = true;
      return false;
    }
    if (gesture == m5::unit::paj7620u2::Gesture::Right ||
        gesture == m5::unit::paj7620u2::Gesture::Left) {
      const bool freshSwipe = gestureArmed_;
      gestureArmed_ = false;
      if (!freshSwipe) return false;
    }
    if (gesture == m5::unit::paj7620u2::Gesture::Right) {
      if (allowStart) {
        value = F("start");
      } else if (allowYes) {
        value = F("yes");
      } else {
        return false;
      }
    } else if (gesture == m5::unit::paj7620u2::Gesture::Left) {
      if (!allowNo) return false;
      value = F("no");
    } else {
      return false;
    }

    if (responseLatched_ || externalInputLatched) return false;

    responseLatched_ = true;
    return true;
  }

 private:
  int beginAndProbeBus(const int sda, const int scl) {
    Wire.end();
    if (!Wire.begin(sda, scl, kGestureClock)) return -1;
    Wire.setTimeOut(20);
    delay(25);
    Wire.beginTransmission(kGestureAddress);
    return static_cast<int>(Wire.endTransmission(true));
  }

  void sendError(Stream& output, const char* error) {
    output.print(F("{\"type\":\"gesture_error\",\"error\":\""));
    output.print(error);
    output.println(F("\"}"));
    output.flush();
  }

  void sendProbeError(Stream& output, const int probeA, const int probeB) {
    output.print(F("{\"type\":\"gesture_error\",\"error\":\"gesture_not_found\",\"port_a_sda\":2,\"port_a_scl\":1,\"port_a_probe\":"));
    output.print(probeA);
    output.print(F(",\"port_b_sda\":9,\"port_b_scl\":8,\"port_b_probe\":"));
    output.print(probeB);
    output.println(F("}"));
    output.flush();
  }

  m5::unit::UnitUnified units_;
  m5::unit::UnitGesture sensor_;
  String promptId_;
  bool enabled_ = false;
  bool attached_ = false;
  bool ready_ = false;
  bool readySent_ = false;
  bool responseLatched_ = false;
  bool gestureArmed_ = true;
  int selectedSda_ = -1;
  int selectedScl_ = -1;
};
