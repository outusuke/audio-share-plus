package io.github.mkckr0.audio_share_app.service

enum class LatencyProfile(
    val prebufferMs: Int,
    val minQueueMs: Int,
    val maxQueueMs: Int,
    val holdWifiLock: Boolean,
    val deepBuffer: Boolean,
) {
    LOW(20, 20, 100, true, false),
    BALANCED(60, 40, 200, true, false),
    // the deep queue absorbs Wi-Fi power save bursts
    // deep buffer output wakes the audio DSP far less often than the fast mixer
    STABLE(150, 100, 400, false, true);

    companion object {
        fun fromIndex(index: Int): LatencyProfile = values().getOrElse(index) { BALANCED }
    }
}
